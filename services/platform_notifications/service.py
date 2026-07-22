"""Notification service using transactionally claimed outbox delivery."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from typing import Callable, Mapping

from packages.contracts import (
    DeadLetterRecord, DeliveryAttempt, DeliveryOutcome, Notification, NotificationChannel,
    NotificationPreference, NotificationStatus, NotificationTemplate, Permission, Product,
    TemplateVersion, utc_now,
)
from services.data_service_common import (
    AuditSink, Conflict, DataServiceAuthorizer, InfrastructureUnavailable, InvalidRequest, OutboxRecord, OutboxStatus,
    PolicyViolation, ResourceNotFound, ServiceAuditEvent, deterministic_id, platform_event,
)

from .repositories import NotificationProvider, ProviderAcceptance, UnitOfWorkFactory, WebhookDestinationAllowlist

TERMINAL = frozenset({NotificationStatus.DELIVERED, NotificationStatus.CANCELLED, NotificationStatus.SUPPRESSED, NotificationStatus.DEAD_LETTERED})


class PlatformNotificationService:
    def __init__(
        self,
        uow: UnitOfWorkFactory,
        authorizer: DataServiceAuthorizer,
        audit: AuditSink,
        providers: Mapping[NotificationChannel, NotificationProvider],
        webhooks: WebhookDestinationAllowlist,
        *,
        max_attempts: int = 3,
        retry_base_seconds: int = 30,
        lease_seconds: int = 30,
        worker_id: str = "notification-dispatcher",
        clock: Callable[[], str] = utc_now,
    ) -> None:
        self._uow = uow
        self._auth = authorizer
        self._audit = audit
        self._providers = dict(providers)
        self._webhooks = webhooks
        self._max = max_attempts
        self._base = retry_base_seconds
        self._lease_seconds = lease_seconds
        self._worker_id = worker_id
        self._clock = clock

    def register_template(self, identity, tenant_id, template_id, product, region, channel, content_reference, variable_keys, request_id):
        self._auth.require(identity, tenant_id, Permission.NOTIFICATIONS_MANAGE)
        now = self._clock()
        template = NotificationTemplate(template_id, tenant_id, product, region, None, now, 1)
        version = TemplateVersion(deterministic_id("tv", template_id, 1), template_id, tenant_id, 1, channel, content_reference, tuple(sorted(set(variable_keys))), now)
        with self._uow() as tx:
            tx.templates.create(template)
            tx.templates.append_version(version)
            tx.commit()
        return template, version

    def add_template_version(self, identity, tenant_id, template_id, channel, content_reference, variable_keys, expected_version, request_id):
        self._auth.require(identity, tenant_id, Permission.NOTIFICATIONS_MANAGE)
        with self._uow() as tx:
            template = tx.templates.get(template_id)
            if template is None or template.tenant_id != tenant_id:
                raise ResourceNotFound("unknown_template", "template does not exist")
            number = tx.templates.max_version(template_id) + 1
            value = TemplateVersion(deterministic_id("tv", template_id, number), template_id, tenant_id, number, channel, content_reference, tuple(sorted(set(variable_keys))), self._clock())
            tx.templates.append_version(value)
            tx.templates.update(replace(template, version=template.version + 1), expected_version)
            tx.commit()
            return value

    def activate_template(self, identity, tenant_id, template_id, version_number, expected_version, request_id):
        self._auth.require(identity, tenant_id, Permission.NOTIFICATIONS_MANAGE)
        with self._uow() as tx:
            template = tx.templates.get(template_id)
            version = tx.templates.get_version(template_id, version_number)
            if template is None or version is None or template.tenant_id != tenant_id:
                raise ResourceNotFound("unknown_template_version", "template version does not exist")
            result = tx.templates.update(replace(template, active_version=version_number, version=template.version + 1), expected_version)
            tx.commit()
            return result

    def create_notification(self, identity, tenant_id, product, region, template_id, channel, recipient_reference, deduplication_key, request_id):
        self._auth.require(identity, tenant_id, Permission.NOTIFICATIONS_SEND)
        now = self._clock()
        with self._uow() as tx:
            old = tx.notifications.get_by_dedup(tenant_id, deduplication_key)
            if old is not None:
                requested = (product, region, template_id, channel, recipient_reference)
                existing = (old.product, old.region, old.template_id, old.channel, old.recipient_reference)
                if existing != requested:
                    raise Conflict("deduplication_conflict", "deduplication key was reused with different notification input")
                tx.commit()
                return old
            template = tx.templates.get(template_id)
            if template is None or template.tenant_id != tenant_id or template.active_version is None:
                raise ResourceNotFound("inactive_template", "active template version does not exist")
            if template.product is not product or template.region != region:
                raise PolicyViolation("template_context_mismatch", "template product and region must match the notification")
            template_version = tx.templates.get_version(template_id, template.active_version)
            if template_version is None or template_version.channel is not channel:
                raise PolicyViolation("channel_mismatch", "template is not active for the requested channel")
            if channel is NotificationChannel.WEBHOOK and not self._webhooks.permits(tenant_id, recipient_reference):
                raise PolicyViolation("webhook_not_allowlisted", "webhook destination is not allowlisted")
            preference = tx.preferences.get(tenant_id, recipient_reference, channel.value)
            status = NotificationStatus.SUPPRESSED if preference is not None and not preference.enabled else NotificationStatus.ENQUEUED
            notification_id = deterministic_id("notification", tenant_id, deduplication_key)
            value = Notification(notification_id, tenant_id, product, region, template_id, template.active_version, channel, recipient_reference, deduplication_key, status, now, now, 1)
            tx.notifications.create(value)
            if status is NotificationStatus.ENQUEUED:
                event = platform_event("notification.delivery.requested", tenant_id, request_id, {"resource_id": notification_id, "region": region, "product": product.value}, now)
                tx.outbox.enqueue(OutboxRecord(deterministic_id("outbox", notification_id), event, OutboxStatus.PENDING, 0, None, 1, max_attempts=self._max))
            tx.commit()
        self._audit.emit(ServiceAuditEvent("notification.created", request_id, "succeeded", tenant_id, identity.subject, resource_id=notification_id))
        return value

    def cancel(self, identity, tenant_id, notification_id, expected_version, request_id):
        self._auth.require(identity, tenant_id, Permission.NOTIFICATIONS_SEND)
        with self._uow() as tx:
            value = tx.notifications.get(notification_id)
            if value is None or value.tenant_id != tenant_id:
                raise ResourceNotFound("unknown_notification", "notification does not exist")
            if value.status in TERMINAL:
                raise Conflict("invalid_notification_transition", "terminal notification cannot be cancelled")
            result = tx.notifications.update(replace(value, status=NotificationStatus.CANCELLED, updated_at=self._clock(), version=value.version + 1), expected_version)
            outbox = tx.outbox.get(deterministic_id("outbox", notification_id))
            if outbox is not None:
                tx.outbox.cancel(outbox.record_id)
            tx.commit()
            return result

    def set_preference(self, identity, tenant_id, subject_reference, channel, enabled, expected_version, request_id):
        self._auth.require(identity, tenant_id, Permission.NOTIFICATIONS_MANAGE)
        now = self._clock()
        with self._uow() as tx:
            old = tx.preferences.get(tenant_id, subject_reference, channel.value)
            value = NotificationPreference(deterministic_id("pref", tenant_id, subject_reference, channel.value), tenant_id, subject_reference, channel, enabled, now, old.version + 1 if old else 1)
            result = tx.preferences.put(value, expected_version)
            tx.commit()
            return result

    def get(self, identity, tenant_id, notification_id):
        self._auth.require(identity, tenant_id, Permission.NOTIFICATIONS_READ, allow_suspended=True)
        with self._uow() as tx:
            value = tx.notifications.get(notification_id)
            tx.commit()
        if value is None or value.tenant_id != tenant_id:
            raise ResourceNotFound("unknown_notification", "notification does not exist")
        return value

    def list(self, identity, tenant_id, limit=50, cursor=None):
        self._auth.require(identity, tenant_id, Permission.NOTIFICATIONS_READ, allow_suspended=True)
        with self._uow() as tx:
            page = tx.notifications.list(tenant_id, limit, cursor)
            tx.commit()
            return page

    def get_preference(self, identity, tenant_id, subject_reference, channel):
        self._auth.require(identity, tenant_id, Permission.NOTIFICATIONS_READ, allow_suspended=True)
        with self._uow() as tx:
            value = tx.preferences.get(tenant_id, subject_reference, channel.value)
            tx.commit()
        if value is None:
            raise ResourceNotFound("unknown_notification_preference", "notification preference does not exist")
        return value

    def get_dead_letter(self, identity, tenant_id, dead_letter_id):
        self._auth.require(identity, tenant_id, Permission.NOTIFICATIONS_READ, allow_suspended=True)
        with self._uow() as tx:
            value = tx.dead_letters.get(dead_letter_id)
            tx.commit()
        if value is None or value.tenant_id != tenant_id:
            raise ResourceNotFound("unknown_dead_letter", "dead-letter record does not exist")
        return value

    def retry_delay(self, attempt_number):
        return min(self._base * (2 ** max(0, attempt_number - 1)), 3600)

    def _claim(self, tenant_id: str, notification_id: str):
        now = self._clock()
        with self._uow() as tx:
            notification = tx.notifications.get(notification_id)
            if notification is None or notification.tenant_id != tenant_id:
                raise ResourceNotFound("unknown_notification", "notification does not exist")
            outbox_id = deterministic_id("outbox", notification_id)
            record = tx.outbox.get(outbox_id)
            if notification.status in TERMINAL:
                if record is not None:
                    tx.outbox.cancel(outbox_id)
                tx.commit()
                return notification, None, None
            if record is None:
                raise Conflict("notification_outbox_missing", "notification delivery record does not exist")
            claimed = tx.outbox.claim(outbox_id, self._worker_id, now, self._lease_seconds)
            template = tx.templates.get_version(notification.template_id, notification.template_version)
            tx.commit()
            return notification, template, claimed

    def _finalize(self, tenant_id: str, notification_id: str, claimed, acceptance: ProviderAcceptance):
        with self._uow() as tx:
            notification = tx.notifications.get(notification_id)
            if notification is None or notification.tenant_id != tenant_id:
                raise ResourceNotFound("unknown_notification", "notification does not exist")
            current_record = tx.outbox.get(claimed.record_id)
            if current_record is None:
                raise Conflict("notification_outbox_missing", "notification delivery record does not exist")
            if notification.status in TERMINAL:
                tx.outbox.cancel(claimed.record_id)
                tx.commit()
                return notification
            number = claimed.attempts
            now = self._clock()
            if acceptance.accepted:
                attempt = DeliveryAttempt(deterministic_id("attempt", notification_id, number), notification_id, tenant_id, number, DeliveryOutcome.ACCEPTED, acceptance.provider_reference, None, now, None)
                status = NotificationStatus.DELIVERED
                tx.attempts.append(attempt)
                result = tx.notifications.update(replace(notification, status=status, updated_at=now, version=notification.version + 1), notification.version)
                tx.outbox.acknowledge(claimed.record_id, self._worker_id, claimed.version, now)
            elif acceptance.retryable and number < self._max:
                next_at = (datetime.fromisoformat(now.replace("Z", "+00:00")) + timedelta(seconds=self.retry_delay(number))).isoformat()
                attempt = DeliveryAttempt(deterministic_id("attempt", notification_id, number), notification_id, tenant_id, number, DeliveryOutcome.RETRYABLE_FAILURE, None, acceptance.reason_code or "provider_failure", now, next_at)
                tx.attempts.append(attempt)
                result = tx.notifications.update(replace(notification, status=NotificationStatus.ENQUEUED, updated_at=now, version=notification.version + 1), notification.version)
                tx.outbox.retry(claimed.record_id, self._worker_id, claimed.version, next_at, now)
            else:
                reason = acceptance.reason_code or "provider_failure"
                attempt = DeliveryAttempt(deterministic_id("attempt", notification_id, number), notification_id, tenant_id, number, DeliveryOutcome.PERMANENT_FAILURE, None, reason, now, None)
                tx.attempts.append(attempt)
                result = tx.notifications.update(replace(notification, status=NotificationStatus.DEAD_LETTERED, updated_at=now, version=notification.version + 1), notification.version)
                tx.dead_letters.create(DeadLetterRecord(deterministic_id("dead", notification_id), notification_id, tenant_id, reason, number, now))
                tx.outbox.transition(claimed.record_id, OutboxStatus.DEAD_LETTERED, claimed.version, lease_owner=self._worker_id, now=now)
            tx.commit()
            return result

    def dispatch(self, identity, tenant_id, notification_id, request_id):
        self._auth.require_executor(identity, tenant_id, allow_suspended=True)
        notification, template, claimed = self._claim(tenant_id, notification_id)
        if claimed is None:
            return notification
        provider = self._providers.get(notification.channel)
        if template is None:
            return self._finalize(tenant_id, notification_id, claimed, ProviderAcceptance(False, False, None, "template_missing"))
        if provider is None:
            self._finalize(tenant_id, notification_id, claimed, ProviderAcceptance(False, False, None, "provider_not_configured"))
            raise InfrastructureUnavailable("provider_not_configured", "notification provider is not configured")
        try:
            acceptance = provider.deliver(notification, template, claimed.event.event_id)
        except Exception:
            acceptance = ProviderAcceptance(False, True, None, "provider_unavailable")
        return self._finalize(tenant_id, notification_id, claimed, acceptance)

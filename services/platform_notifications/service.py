"""Notification application service with transactional outbox delivery."""
from __future__ import annotations
from dataclasses import replace
from datetime import datetime,timedelta
from typing import Callable,Mapping
from packages.contracts import *
from services.data_service_common import *
from .repositories import NotificationProvider,UnitOfWorkFactory,WebhookDestinationAllowlist

TERMINAL=frozenset({NotificationStatus.DELIVERED,NotificationStatus.CANCELLED,NotificationStatus.SUPPRESSED,NotificationStatus.DEAD_LETTERED})
class PlatformNotificationService:
    def __init__(self,uow:UnitOfWorkFactory,authorizer:DataServiceAuthorizer,audit:AuditSink,providers:Mapping[NotificationChannel,NotificationProvider],webhooks:WebhookDestinationAllowlist,*,max_attempts:int=3,retry_base_seconds:int=30,clock:Callable[[],str]=utc_now)->None:
        self._uow=uow; self._auth=authorizer; self._audit=audit; self._providers=dict(providers); self._webhooks=webhooks; self._max=max_attempts; self._base=retry_base_seconds; self._clock=clock
    def register_template(self,identity,tenant_id,template_id,product,region,channel,content_reference,variable_keys,request_id):
        self._auth.require(identity,tenant_id,Permission.NOTIFICATIONS_MANAGE); now=self._clock(); template=NotificationTemplate(template_id,tenant_id,product,region,None,now,1); version=TemplateVersion(deterministic_id("tv",template_id,1),template_id,tenant_id,1,channel,content_reference,tuple(sorted(set(variable_keys))),now)
        with self._uow() as tx: tx.templates.create(template); tx.templates.append_version(version); tx.commit()
        return template,version
    def add_template_version(self,identity,tenant_id,template_id,channel,content_reference,variable_keys,expected_version,request_id):
        self._auth.require(identity,tenant_id,Permission.NOTIFICATIONS_MANAGE)
        with self._uow() as tx:
            template=tx.templates.get(template_id)
            if template is None or template.tenant_id!=tenant_id: raise ResourceNotFound("unknown_template","template does not exist")
            number=(template.active_version or 1)+1; value=TemplateVersion(deterministic_id("tv",template_id,number),template_id,tenant_id,number,channel,content_reference,tuple(sorted(set(variable_keys))),self._clock()); tx.templates.append_version(value); tx.templates.update(replace(template,version=template.version+1),expected_version); tx.commit(); return value
    def activate_template(self,identity,tenant_id,template_id,version_number,expected_version,request_id):
        self._auth.require(identity,tenant_id,Permission.NOTIFICATIONS_MANAGE)
        with self._uow() as tx:
            t=tx.templates.get(template_id); v=tx.templates.get_version(template_id,version_number)
            if t is None or v is None or t.tenant_id!=tenant_id: raise ResourceNotFound("unknown_template_version","template version does not exist")
            result=tx.templates.update(replace(t,active_version=version_number,version=t.version+1),expected_version); tx.commit(); return result
    def create_notification(self,identity,tenant_id,product,region,template_id,channel,recipient_reference,deduplication_key,request_id):
        self._auth.require(identity,tenant_id,Permission.NOTIFICATIONS_SEND); now=self._clock()
        with self._uow() as tx:
            old=tx.notifications.get_by_dedup(tenant_id,deduplication_key)
            if old is not None: tx.commit(); return old
            template=tx.templates.get(template_id)
            if template is None or template.tenant_id!=tenant_id or template.active_version is None: raise ResourceNotFound("inactive_template","active template version does not exist")
            tv=tx.templates.get_version(template_id,template.active_version)
            if tv is None or tv.channel is not channel: raise PolicyViolation("channel_mismatch","template is not active for the requested channel")
            if channel is NotificationChannel.WEBHOOK and not self._webhooks.permits(tenant_id,recipient_reference): raise PolicyViolation("webhook_not_allowlisted","webhook destination is not allowlisted")
            pref=tx.preferences.get(tenant_id,recipient_reference,channel.value); status=NotificationStatus.SUPPRESSED if pref is not None and not pref.enabled else NotificationStatus.ENQUEUED
            nid=deterministic_id("notification",tenant_id,deduplication_key); value=Notification(nid,tenant_id,product,region,template_id,template.active_version,channel,recipient_reference,deduplication_key,status,now,now,1); tx.notifications.create(value)
            if status is NotificationStatus.ENQUEUED:
                evt=platform_event("notification.delivery.requested",tenant_id,request_id,{"resource_id":nid,"region":region,"product":product.value},now); tx.outbox.enqueue(OutboxRecord(deterministic_id("outbox",nid),evt,OutboxStatus.PENDING,0,None,1))
            tx.commit()
        self._audit.emit(ServiceAuditEvent("notification.created",request_id,"succeeded",tenant_id,identity.subject,resource_id=nid)); return value
    def cancel(self,identity,tenant_id,notification_id,expected_version,request_id):
        self._auth.require(identity,tenant_id,Permission.NOTIFICATIONS_SEND)
        with self._uow() as tx:
            value=tx.notifications.get(notification_id)
            if value is None or value.tenant_id!=tenant_id: raise ResourceNotFound("unknown_notification","notification does not exist")
            if value.status in TERMINAL: raise Conflict("invalid_notification_transition","terminal notification cannot be cancelled")
            result=tx.notifications.update(replace(value,status=NotificationStatus.CANCELLED,updated_at=self._clock(),version=value.version+1),expected_version); tx.commit(); return result
    def set_preference(self,identity,tenant_id,subject_reference,channel,enabled,expected_version,request_id):
        self._auth.require(identity,tenant_id,Permission.NOTIFICATIONS_MANAGE); now=self._clock()
        with self._uow() as tx:
            old=tx.preferences.get(tenant_id,subject_reference,channel.value); value=NotificationPreference(deterministic_id("pref",tenant_id,subject_reference,channel.value),tenant_id,subject_reference,channel,enabled,now,old.version+1 if old else 1); result=tx.preferences.put(value,expected_version); tx.commit(); return result
    def get(self,identity,tenant_id,notification_id):
        self._auth.require(identity,tenant_id,Permission.NOTIFICATIONS_READ,allow_suspended=True)
        with self._uow() as tx: v=tx.notifications.get(notification_id); tx.commit()
        if v is None or v.tenant_id!=tenant_id: raise ResourceNotFound("unknown_notification","notification does not exist")
        return v
    def list(self,identity,tenant_id,limit=50,cursor=None):
        self._auth.require(identity,tenant_id,Permission.NOTIFICATIONS_READ,allow_suspended=True)
        with self._uow() as tx: page=tx.notifications.list(tenant_id,limit,cursor); tx.commit(); return page
    def get_preference(self,identity,tenant_id,subject_reference,channel):
        self._auth.require(identity,tenant_id,Permission.NOTIFICATIONS_READ,allow_suspended=True)
        with self._uow() as tx:value=tx.preferences.get(tenant_id,subject_reference,channel.value);tx.commit()
        if value is None:raise ResourceNotFound("unknown_notification_preference","notification preference does not exist")
        return value
    def get_dead_letter(self,identity,tenant_id,dead_letter_id):
        self._auth.require(identity,tenant_id,Permission.NOTIFICATIONS_READ,allow_suspended=True)
        with self._uow() as tx:value=tx.dead_letters.get(dead_letter_id);tx.commit()
        if value is None or value.tenant_id!=tenant_id:raise ResourceNotFound("unknown_dead_letter","dead-letter record does not exist")
        return value
    def retry_delay(self,attempt_number): return min(self._base*(2**max(0,attempt_number-1)),3600)
    def dispatch(self,identity,tenant_id,notification_id,request_id):
        self._auth.require_executor(identity,tenant_id,allow_suspended=True)
        with self._uow() as tx:
            n=tx.notifications.get(notification_id)
            if n is None or n.tenant_id!=tenant_id: raise ResourceNotFound("unknown_notification","notification does not exist")
            if n.status in TERMINAL: tx.commit(); return n
            template=tx.templates.get_version(n.template_id,n.template_version); attempts=tx.attempts.list(notification_id); number=len(attempts)+1
            if template is None: acceptance=None
            else: acceptance=self._providers[n.channel].deliver(n,template)
            now=self._clock()
            if acceptance is not None and acceptance.accepted:
                attempt=DeliveryAttempt(deterministic_id("attempt",notification_id,number),notification_id,tenant_id,number,DeliveryOutcome.ACCEPTED,acceptance.provider_reference,None,now,None); status=NotificationStatus.DELIVERED
            elif acceptance is not None and acceptance.retryable and number<self._max:
                nxt=(datetime.fromisoformat(now)+timedelta(seconds=self.retry_delay(number))).isoformat(); attempt=DeliveryAttempt(deterministic_id("attempt",notification_id,number),notification_id,tenant_id,number,DeliveryOutcome.RETRYABLE_FAILURE,None,acceptance.reason_code or "provider_failure",now,nxt); status=NotificationStatus.ENQUEUED
            else:
                reason="template_missing" if acceptance is None else acceptance.reason_code or "provider_failure"; attempt=DeliveryAttempt(deterministic_id("attempt",notification_id,number),notification_id,tenant_id,number,DeliveryOutcome.PERMANENT_FAILURE,None,reason,now,None); status=NotificationStatus.DEAD_LETTERED; tx.dead_letters.create(DeadLetterRecord(deterministic_id("dead",notification_id),notification_id,tenant_id,reason,number,now)); tx.outbox.enqueue(OutboxRecord(deterministic_id("outbox","dead",notification_id),platform_event("notification.dead-lettered",tenant_id,request_id,{"resource_id":notification_id,"reason_code":reason},now),OutboxStatus.PENDING,0,None,1))
            tx.attempts.append(attempt); result=tx.notifications.update(replace(n,status=status,updated_at=now,version=n.version+1),n.version)
            outbox_id=deterministic_id("outbox",notification_id); record=tx.outbox.get(outbox_id)
            if record is not None:
                if status is NotificationStatus.DELIVERED: tx.outbox.transition(outbox_id,OutboxStatus.ACCEPTED,record.version)
                elif status is NotificationStatus.DEAD_LETTERED: tx.outbox.transition(outbox_id,OutboxStatus.DEAD_LETTERED,record.version)
                else: tx.outbox.transition(outbox_id,OutboxStatus.PENDING,record.version,next_attempt_at=attempt.next_attempt_at)
            tx.commit(); return result

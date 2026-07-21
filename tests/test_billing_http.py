from __future__ import annotations

import json
from http.server import ThreadingHTTPServer
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from packages.contracts import VerifiedSubjectIdentity
from services.platform_billing.app import BillingDependencies, create_server
from services.platform_control_plane.auth import AuthenticationError

from billing_support import EXECUTOR, FUTURE, fund, make_billing_fixture, provision
from control_plane_support import ADMIN_A, PLATFORM, bootstrap_tenant_admin


class Authenticator:
    identities = {"Bearer platform": PLATFORM, "Bearer executor": EXECUTOR, "Bearer admin-a": ADMIN_A}
    def authenticate(self, authorization: str) -> VerifiedSubjectIdentity:
        identity = self.identities.get(authorization)
        if identity is None: raise AuthenticationError("invalid_token", "trusted bearer assertion is required")
        return identity


class PlatformBillingHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = make_billing_fixture(); provision(cls.fixture, "tenant-http")
        cls.server: ThreadingHTTPServer = create_server(BillingDependencies(cls.fixture.service, Authenticator(), cls.fixture.audit), port=0)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True); cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join()

    def request(self, method, path, body=None, token="platform", key=None, request_id="req-http-billing"):
        data = json.dumps(body).encode() if isinstance(body, dict) else body
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json", "X-Request-ID": request_id}
        if key is not None: headers["Idempotency-Key"] = key
        return urlopen(Request(f"{self.base}{path}", data=data, headers=headers, method=method))

    def test_health_authentication_and_request_ids(self):
        with urlopen(f"{self.base}/healthz") as response:
            payload = json.load(response)
        self.assertEqual(payload["service"], "platform-billing")
        self.assertEqual(response.headers["X-Request-ID"], payload["request_id"])
        with self.assertRaises(HTTPError) as caught:
            urlopen(Request(f"{self.base}/v1/billing/accounts/tenant-http"))
        self.assertEqual(caught.exception.code, 401)

    def test_create_read_balance_grant_and_pagination(self):
        tenant = "tenant-http-created"
        self.fixture.control.service.create_tenant(PLATFORM, tenant, "HTTP", "in", "create-http-created", "req-create")
        with self.request("POST", "/v1/billing/accounts", {"tenant_id": tenant, "expected_version": 0}, key="create-account-http") as response:
            self.assertEqual(response.status, 201)
        with self.request("POST", f"/v1/billing/accounts/{tenant}/credits/grants", {"amount_microunits": 1000, "expected_version": 1}, key="grant-http") as response:
            self.assertEqual(response.status, 201)
        with self.request("GET", f"/v1/billing/accounts/{tenant}/balance") as response:
            self.assertEqual(json.load(response)["data"]["available_microunits"], 1000)
        with self.request("GET", f"/v1/billing/accounts/{tenant}/ledger?limit=1") as response:
            page = json.load(response)["data"]
        self.assertEqual(len(page["items"]), 1)

    def test_reservation_capture_usage_and_release_endpoints(self):
        balance = fund(self.fixture, "tenant-http", 10_000).balance
        body = {"tenant_id": "tenant-http", "product": "aisa", "model": "uwo-general-v1", "request_id": "req-http-reserve", "estimated_microunits": 5000, "expires_at": FUTURE, "expected_balance_version": balance.version}
        with self.request("POST", "/v1/billing/reservations", body, token="executor", key="reserve-http") as response:
            reserved = json.load(response)["data"]
        capture = {"usage_event_id": "usage-http", "provider_id": "azure-openai-in", "provider_model_id": "deployment-a", "region": "in", "provider_request_id": "provider-http", "input_tokens": 1000, "output_tokens": 0, "total_tokens": 1000, "expected_reservation_version": 1, "expected_balance_version": reserved["balance"]["version"]}
        with self.request("POST", f"/v1/billing/reservations/{reserved['reservation']['reservation_id']}/capture", capture, token="executor", key="capture-http") as response:
            captured = json.load(response)["data"]
        release = {"expected_reservation_version": captured["reservation"]["version"], "expected_balance_version": captured["balance"]["version"]}
        with self.request("POST", f"/v1/billing/reservations/{reserved['reservation']['reservation_id']}/release", release, token="executor", key="release-http") as response:
            self.assertEqual(json.load(response)["data"]["reservation"]["status"], "released")
        with self.request("GET", "/v1/billing/accounts/tenant-http/usage/usage-http") as response:
            self.assertEqual(json.load(response)["data"]["provider_request_id"], "provider-http")

    def test_cross_tenant_denial_emits_exactly_one_redacted_event(self):
        tenant = "tenant-http-isolated"; provision(self.fixture, tenant)
        request_id = "req-http-cross-tenant"
        with self.assertRaises(HTTPError) as caught:
            self.request("GET", f"/v1/billing/accounts/{tenant}", token="admin-a", request_id=request_id)
        self.assertEqual(caught.exception.code, 403)
        events = [event for event in self.fixture.audit.events if event.request_id == request_id]
        self.assertEqual(len(events), 1)
        self.assertNotIn("Bearer admin-a", json.dumps([event.__dict__ for event in events]))

    def test_unknown_tenant_is_404_with_exactly_one_denial_event(self):
        request_id = "req-http-unknown-tenant"
        with self.assertRaises(HTTPError) as caught:
            self.request("GET", "/v1/billing/accounts/tenant-does-not-exist", request_id=request_id)
        self.assertEqual(caught.exception.code, 404)
        self.assertEqual(json.load(caught.exception)["error"]["code"], "unknown_tenant")
        self.assertEqual(len([event for event in self.fixture.audit.events if event.request_id == request_id]), 1)

    def test_authorization_repository_failures_are_redacted_500_not_denials(self):
        if self.fixture.control.tenants.get("tenant-a") is None:
            bootstrap_tenant_admin(self.fixture.control, "tenant-a", ADMIN_A)
            self.fixture.service.create_account(PLATFORM, "tenant-a", "account-tenant-a", "req-account-tenant-a")
        failures = (
            (self.fixture.control.tenants, "get", "req-http-tenant-repository-failure"),
            (self.fixture.control.memberships, "get", "req-http-membership-repository-failure"),
            (self.fixture.control.roles, "get", "req-http-role-repository-failure"),
        )
        detail = "secret authorization repository detail"
        for repository, method, request_id in failures:
            with self.subTest(request_id=request_id), patch.object(repository, method, side_effect=RuntimeError(detail)):
                with self.assertRaises(HTTPError) as caught:
                    self.request("GET", "/v1/billing/accounts/tenant-a", token="admin-a", request_id=request_id)
                self.assertEqual(caught.exception.code, 500)
                payload = json.load(caught.exception)
                self.assertEqual(payload["error"], {"code": "internal_error", "message": "an internal error occurred"})
                self.assertNotIn(detail, json.dumps(payload))
            events = [event for event in self.fixture.audit.events if event.request_id == request_id]
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].event_type, "billing.internal_error")

    def test_malformed_oversized_and_internal_errors_are_redacted(self):
        with self.assertRaises(HTTPError) as malformed:
            self.request("POST", "/v1/billing/accounts", b"{bad", key="bad")
        self.assertEqual(malformed.exception.code, 400)
        with self.assertRaises(HTTPError) as oversized:
            self.request("POST", "/v1/billing/accounts", b"x" * 65_537, key="large")
        self.assertEqual(oversized.exception.code, 413)
        detail = "secret repository implementation detail"
        with patch.object(self.fixture.accounts, "get_by_tenant", side_effect=RuntimeError(detail)):
            with self.assertRaises(HTTPError) as caught:
                self.request("GET", "/v1/billing/accounts/tenant-http", request_id="req-http-500")
        self.assertEqual(caught.exception.code, 500)
        payload = json.load(caught.exception)
        self.assertEqual(payload["error"]["code"], "internal_error")
        self.assertNotIn(detail, json.dumps(payload))


if __name__ == "__main__": unittest.main()

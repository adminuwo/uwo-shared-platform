from __future__ import annotations

import json
from http.server import ThreadingHTTPServer
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from services.platform_control_plane.app import ControlPlaneDependencies, create_server

from control_plane_support import ADMIN_A, HeaderAuthenticator, PLATFORM, bootstrap_tenant_admin, make_fixture


class PlatformControlPlaneHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture = make_fixture()
        bootstrap_tenant_admin(cls.fixture, "tenant-a", ADMIN_A)
        cls.server: ThreadingHTTPServer = create_server(ControlPlaneDependencies(cls.fixture.service, HeaderAuthenticator(), cls.fixture.audit), port=0)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def request(self, method: str, path: str, body: dict | bytes | None = None, token: str = "platform", idempotency_key: str | None = None):
        data = None
        if isinstance(body, dict):
            data = json.dumps(body).encode()
        elif isinstance(body, bytes):
            data = body
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json", "X-Request-ID": "req-http-test"}
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        return urlopen(Request(f"{self.base_url}{path}", data=data, headers=headers, method=method))

    def test_health_is_public_and_correlated(self) -> None:
        with urlopen(f"{self.base_url}/healthz") as response:
            payload = json.load(response)
        self.assertEqual(payload["service"], "platform-control-plane")
        self.assertEqual(response.headers["X-Request-ID"], payload["request_id"])

    def test_v1_requires_authentication_with_consistent_error(self) -> None:
        request = Request(f"{self.base_url}/v1/tenants/tenant-a", method="GET")
        with self.assertRaises(HTTPError) as caught:
            urlopen(request)
        self.assertEqual(caught.exception.code, 401)
        payload = json.load(caught.exception)
        self.assertEqual(payload["error"]["code"], "invalid_token")
        self.assertIn("request_id", payload)

    def test_create_and_read_tenant_boundaries(self) -> None:
        body = {"tenant_id": "tenant-http", "name": "HTTP Tenant", "region": "in"}
        with self.request("POST", "/v1/tenants", body, idempotency_key="http-create") as response:
            created = json.load(response)
        self.assertEqual(response.status, 201)
        self.assertEqual(created["data"]["tenant_id"], "tenant-http")
        with self.request("GET", "/v1/tenants/tenant-http") as response:
            loaded = json.load(response)
        self.assertEqual(loaded["data"]["version"], 1)

    def test_membership_role_and_permissions_boundaries(self) -> None:
        with self.request("PUT", "/v1/tenants/tenant-a/memberships/user-a", {"status": "active", "expected_version": 0}) as response:
            membership = json.load(response)["data"]
        with self.request("POST", "/v1/tenants/tenant-a/memberships/user-a/roles/tenant-reader", {"expected_version": membership["version"]}) as response:
            self.assertEqual(response.status, 201)
        with self.request("GET", "/v1/tenants/tenant-a/permissions/user-a") as response:
            permissions = json.load(response)["data"]["permissions"]
        self.assertEqual(permissions, ["entitlement.read", "policy.read", "tenant.read"])

    def test_entitlement_grant_read_and_revoke_boundaries(self) -> None:
        tenant = "tenant-entitlement-http"
        self.fixture.service.create_tenant(PLATFORM, tenant, "Entitlement Tenant", "in", "create-ent-http", "req-create")
        with self.request("POST", f"/v1/tenants/{tenant}/entitlements/products/aisa", {"expected_version": 1}, idempotency_key="grant-http-product") as response:
            self.assertEqual(response.status, 201)
        with self.request("POST", f"/v1/tenants/{tenant}/entitlements/models/uwo-general-v1", {"expected_version": 2}, idempotency_key="grant-http-model") as response:
            self.assertEqual(response.status, 201)
        with self.request("GET", f"/v1/tenants/{tenant}/entitlements") as response:
            snapshot = json.load(response)["data"]
        self.assertEqual(snapshot["version"], 3)
        with self.request("DELETE", f"/v1/tenants/{tenant}/entitlements/models/uwo-general-v1", {"expected_version": 3}) as response:
            self.assertEqual(json.load(response)["data"]["models"], [])

    def test_tenant_admin_cannot_cross_tenant_boundary(self) -> None:
        self.fixture.service.create_tenant(PLATFORM, "tenant-isolated-http", "Isolated", "in", "isolated-http", "req-isolated")
        with self.assertRaises(HTTPError) as caught:
            self.request("GET", "/v1/tenants/tenant-isolated-http", token="admin-a")
        self.assertEqual(caught.exception.code, 403)
        self.assertEqual(json.load(caught.exception)["error"]["code"], "tenant_isolation_violation")

    def test_malformed_and_oversized_requests_are_rejected(self) -> None:
        with self.assertRaises(HTTPError) as malformed:
            self.request("POST", "/v1/tenants", b"{not-json", idempotency_key="malformed")
        self.assertEqual(malformed.exception.code, 400)
        self.assertEqual(json.load(malformed.exception)["error"]["code"], "invalid_request")
        with self.assertRaises(HTTPError) as oversized:
            self.request("POST", "/v1/tenants", b"x" * 65_537, idempotency_key="oversized")
        self.assertEqual(oversized.exception.code, 413)

    def test_pagination_contract_is_stable(self) -> None:
        for suffix in ("page-a", "page-b", "page-c"):
            self.fixture.service.create_tenant(PLATFORM, suffix, suffix, "in", f"create-{suffix}", f"req-{suffix}")
        with self.request("GET", "/v1/tenants?limit=2") as response:
            first = json.load(response)["data"]
        self.assertEqual(len(first["items"]), 2)
        self.assertIsNotNone(first["page"]["next_cursor"])
        with self.request("GET", f"/v1/tenants?limit=2&cursor={first['page']['next_cursor']}") as response:
            second = json.load(response)["data"]
        self.assertTrue(second["items"])
        self.assertNotEqual(first["items"][0]["tenant_id"], second["items"][0]["tenant_id"])


if __name__ == "__main__":
    unittest.main()

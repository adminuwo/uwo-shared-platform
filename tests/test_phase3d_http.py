import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from services.data_service_common import MemoryAuditSink
from services.platform_governance.app import make_handler as governance_handler
from services.platform_operations.app import make_handler as operations_handler
from services.platform_tenant_admin.app import make_handler as tenant_admin_handler
from control_plane_support import HeaderAuthenticator


class ExplodingOperations:
    def latest_service_health(self,*args):raise RuntimeError("repository secret detail")


class Phase3DHttpTests(unittest.TestCase):
    def test_new_services_have_public_health_and_authenticated_v1_boundaries(self):
        for name,factory in (("tenant-admin",tenant_admin_handler),("governance",governance_handler),("operations",operations_handler)):
            with self.subTest(service=name):
                audit=MemoryAuditSink();server=ThreadingHTTPServer(("127.0.0.1",0),factory(object(),HeaderAuthenticator(),audit));thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
                try:
                    with urlopen(f"http://127.0.0.1:{server.server_port}/healthz",timeout=2) as response:
                        payload=json.loads(response.read());self.assertEqual(payload["status"],"ok");self.assertTrue(response.headers["X-Request-ID"])
                    with self.assertRaises(HTTPError) as denied:urlopen(f"http://127.0.0.1:{server.server_port}/v1/unknown",timeout=2)
                    self.assertEqual(denied.exception.code,401)
                finally:server.shutdown();server.server_close();thread.join()
    def test_internal_errors_are_redacted(self):
        audit=MemoryAuditSink();server=ThreadingHTTPServer(("127.0.0.1",0),operations_handler(ExplodingOperations(),HeaderAuthenticator(),audit));thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            request=Request(f"http://127.0.0.1:{server.server_port}/v1/operations/health/services/service-a?tenant_id=tenant-a",headers={"Authorization":"Bearer admin-a"})
            with self.assertRaises(HTTPError) as failed:urlopen(request,timeout=2)
            body=json.loads(failed.exception.read());self.assertEqual(failed.exception.code,500);self.assertEqual(body["error"]["code"],"internal_error");self.assertNotIn("secret",json.dumps(body))
        finally:server.shutdown();server.server_close();thread.join()

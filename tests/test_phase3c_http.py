import json,threading,unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request,urlopen
from services.data_service_common import MemoryAuditSink
from services.platform_storage.app import make_handler as storage_handler
from services.platform_notifications.app import make_handler as notification_handler
from services.platform_analytics.app import make_handler as analytics_handler
from services.platform_audit.app import make_handler as audit_handler
from control_plane_support import HeaderAuthenticator

class Phase3CHttpSmokeTests(unittest.TestCase):
    def test_each_service_health_and_v1_authentication_boundary(self):
        for name,factory in (("storage",storage_handler),("notifications",notification_handler),("analytics",analytics_handler),("audit",audit_handler)):
            with self.subTest(service=name):
                audit=MemoryAuditSink();server=ThreadingHTTPServer(("127.0.0.1",0),factory(object(),HeaderAuthenticator(),audit));thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
                try:
                    with urlopen(f"http://127.0.0.1:{server.server_port}/v1/health",timeout=2) as response:self.assertEqual(json.loads(response.read())["service"],f"platform-{name}")
                    with self.assertRaises(HTTPError) as caught:urlopen(Request(f"http://127.0.0.1:{server.server_port}/v1/unknown"),timeout=2)
                    self.assertEqual(caught.exception.code,401);self.assertEqual(len([e for e in audit.events if e.outcome=="denied"]),1)
                finally:server.shutdown();server.server_close();thread.join()

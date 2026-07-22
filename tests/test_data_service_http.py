import json,threading,unittest
from http import HTTPStatus
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request,urlopen
from services.data_service_common import AuthorizationDenied,InfrastructureUnavailable,MemoryAuditSink
from services.data_service_http import handler
from control_plane_support import HeaderAuthenticator

class DataServiceHttpTests(unittest.TestCase):
    def setUp(self):
        self.audit=MemoryAuditSink()
        def router(method,parts,query,body,identity,request_id,key):
            if parts==["v1","explode"]:raise RuntimeError("secret repository detail")
            if parts==["v1","deny"]:raise AuthorizationDenied("tenant_isolation_violation","access denied")
            if parts==["v1","provider"]:raise InfrastructureUnavailable("provider_not_configured","sensitive provider detail")
            return {"ok":True},HTTPStatus.OK
        h=handler("test-data",HeaderAuthenticator(),self.audit,router);self.server=ThreadingHTTPServer(("127.0.0.1",0),h);self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start();self.base=f"http://127.0.0.1:{self.server.server_port}"
    def tearDown(self):self.server.shutdown();self.server.server_close();self.thread.join()
    def request(self,path,token=None):
        headers={"Authorization":token} if token else {}
        try:
            with urlopen(Request(self.base+path,headers=headers),timeout=2) as response:return response.status,json.loads(response.read())
        except HTTPError as exc:return exc.code,json.loads(exc.read())
    def test_health_auth_denial_and_redacted_internal_error(self):
        self.assertEqual(self.request("/healthz")[0],200);status,body=self.request("/v1/ok");self.assertEqual(status,401);self.assertEqual(len([e for e in self.audit.events if e.outcome=="denied"]),1)
        status,body=self.request("/v1/explode","Bearer platform");self.assertEqual(status,500);self.assertEqual(body["error"]["code"],"internal_error");self.assertNotIn("secret",json.dumps(body))
    def test_each_denied_request_has_exactly_one_redacted_audit_event(self):
        status,body=self.request("/v1/deny","Bearer platform");self.assertEqual(status,403);denied=[event for event in self.audit.events if event.request_id==body["request_id"] and event.outcome=="denied"];self.assertEqual(len(denied),1);self.assertEqual(denied[0].reason_code,"tenant_isolation_violation");self.assertNotIn("Authorization",json.dumps(body))
    def test_provider_infrastructure_failure_is_not_a_policy_denial(self):
        status,body=self.request("/v1/provider","Bearer platform");self.assertEqual(status,503);events=[event for event in self.audit.events if event.request_id==body["request_id"]];self.assertEqual(len(events),1);self.assertEqual((events[0].outcome,events[0].reason_code),("failed","provider_not_configured"));self.assertNotIn("sensitive",json.dumps(body))

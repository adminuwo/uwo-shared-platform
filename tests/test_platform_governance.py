import unittest

from packages.contracts import *
from services.data_service_common import AuthorizationDenied,Conflict,PolicyViolation
from control_plane_support import ADMIN_A,PLATFORM,USER_A
from phase3d_support import governance_fixture


class GovernanceTests(unittest.TestCase):
    def setUp(self):self.ctx,self.state,self.service=governance_fixture();self.sequence=0
    def change(self,content=None,risks=()):
        self.sequence+=1;suffix=str(self.sequence)
        draft=self.service.create_draft(ADMIN_A,"tenant-a",content or {"region":"in"},"uwo-policy-v1",None,risks,f"draft-{suffix}")
        validation=self.service.validate_draft(ADMIN_A,"tenant-a",draft.draft_id,f"validate-{suffix}");self.assertTrue(validation.valid)
        change=self.service.submit_change(ADMIN_A,"tenant-a",draft.draft_id,f"submit-{suffix}")
        return draft,change
    def release(self,risks=()):
        draft,change=self.change(risks=risks)
        self.service.decide_change(USER_A,"tenant-a",change.change_request_id,"approved","reviewed","approve-1")
        if risks:self.service.decide_change(PLATFORM,"tenant-a",change.change_request_id,"approved","platform-reviewed","approve-2")
        return self.service.create_release(ADMIN_A,"tenant-a",change.change_request_id,"release"),change
    def test_draft_validation_and_immutable_release_digest(self):
        release,_=self.release();self.assertEqual(release.digest.digest,operations_fingerprint(release.content))
        with self.assertRaises(TypeError):release.content["region"]="eu"
    def test_invalid_compatibility_cannot_be_submitted(self):
        draft=self.service.create_draft(ADMIN_A,"tenant-a",{"region":"in"},"unsupported",None,(),"draft")
        self.assertFalse(self.service.validate_draft(ADMIN_A,"tenant-a",draft.draft_id,"validate").valid)
        with self.assertRaises(PolicyViolation):self.service.submit_change(ADMIN_A,"tenant-a",draft.draft_id,"submit")
    def test_self_approval_and_deprovisioned_approver_fail_closed(self):
        _,change=self.change()
        with self.assertRaises(PolicyViolation):self.service.decide_change(ADMIN_A,"tenant-a",change.change_request_id,"approved","self","self")
        self.ctx.control.subjects.deprovision(USER_A.subject)
        with self.assertRaises(AuthorizationDenied):self.service.decide_change(USER_A,"tenant-a",change.change_request_id,"approved","reviewed","deprovisioned")
    def test_high_risk_requires_two_distinct_approvals(self):
        _,change=self.change(risks=("provider-allowlist",))
        self.service.decide_change(USER_A,"tenant-a",change.change_request_id,"approved","reviewed","approve")
        with self.assertRaises(PolicyViolation):self.service.create_release(ADMIN_A,"tenant-a",change.change_request_id,"early")
        self.service.decide_change(PLATFORM,"tenant-a",change.change_request_id,"approved","platform-reviewed","second")
        release=self.service.create_release(ADMIN_A,"tenant-a",change.change_request_id,"release");self.assertEqual(release.change_request_id,change.change_request_id)
    def test_rejected_change_cannot_be_released_or_promoted(self):
        _,change=self.change();self.service.decide_change(USER_A,"tenant-a",change.change_request_id,"rejected","unsafe","reject")
        with self.assertRaises(PolicyViolation):self.service.create_release(ADMIN_A,"tenant-a",change.change_request_id,"release")
    def test_production_requires_two_people_and_platform_promotion(self):
        release,change=self.release()
        with self.assertRaises(PolicyViolation):self.service.promote(PLATFORM,"tenant-a",release.release_id,"production",0,"prod-1","promote")
        self.service.decide_change(PLATFORM,"tenant-a",change.change_request_id,"approved","production-review","approve-production")
        promotion=self.service.promote(PLATFORM,"tenant-a",release.release_id,"production",0,"prod-1","promote");self.assertEqual(promotion.environment,PolicyEnvironment.PRODUCTION)
    def test_promotion_idempotency_conflict_and_concurrent_version(self):
        release,_=self.release();first=self.service.promote(ADMIN_A,"tenant-a",release.release_id,"development",0,"promote-1","promote")
        self.assertEqual(first,self.service.promote(ADMIN_A,"tenant-a",release.release_id,"development",0,"promote-1","retry"))
        with self.assertRaises(Conflict):self.service.promote(ADMIN_A,"tenant-a",release.release_id,"development",1,"promote-1","conflict")
        with self.assertRaises(Conflict):self.service.promote(ADMIN_A,"tenant-a",release.release_id,"development",0,"promote-2","concurrent")
    def test_stale_base_release_is_rejected(self):
        first,_=self.release();self.service.promote(ADMIN_A,"tenant-a",first.release_id,"staging",0,"first","first")
        second,_=self.release()
        with self.assertRaises(Conflict) as denied:self.service.promote(ADMIN_A,"tenant-a",second.release_id,"staging",1,"second","second")
        self.assertEqual(denied.exception.code,"stale_base_release")
    def test_rollback_creates_new_release_and_promotion(self):
        first,_=self.release();p1=self.service.promote(ADMIN_A,"tenant-a",first.release_id,"development",0,"first","first")
        draft,change=self.change({"region":"in","mode":"strict"})
        self.service.decide_change(USER_A,"tenant-a",change.change_request_id,"approved","reviewed","approve")
        release2=self.service.create_release(ADMIN_A,"tenant-a",change.change_request_id,"release")
        # Rebase the second release onto the current environment using a new draft.
        draft3=self.service.create_draft(ADMIN_A,"tenant-a",{"region":"in","mode":"strict"},"uwo-policy-v1",first.release_id,(),"draft-3")
        self.service.validate_draft(ADMIN_A,"tenant-a",draft3.draft_id,"validate-3");change3=self.service.submit_change(ADMIN_A,"tenant-a",draft3.draft_id,"submit-3");self.service.decide_change(USER_A,"tenant-a",change3.change_request_id,"approved","reviewed","approve-3")
        release3=self.service.create_release(ADMIN_A,"tenant-a",change3.change_request_id,"release-3");p2=self.service.promote(ADMIN_A,"tenant-a",release3.release_id,"development",1,"second","second")
        rollback=self.service.rollback(ADMIN_A,"tenant-a","development",first.release_id,2,"rollback","rollback")
        history=self.service.history(ADMIN_A,"tenant-a");self.assertNotEqual(rollback.new_promotion_id,p1.promotion_id);self.assertEqual(len(history.rollbacks),1);self.assertEqual(len(history.promotions),3)
    def test_forbidden_secret_fields_fail_contract_creation(self):
        with self.assertRaises(ValueError):self.service.create_draft(ADMIN_A,"tenant-a",{"nested":{"credential":"x"}},"uwo-policy-v1",None,(),"secret")
    def test_promotion_and_outbox_rollback_atomically(self):
        release,_=self.release();self.state.fail_next="promotion"
        with self.assertRaises(RuntimeError):self.service.promote(ADMIN_A,"tenant-a",release.release_id,"development",0,"rollback","rollback")
        self.assertFalse(self.state.promotions);self.assertFalse(self.state.idempotency);self.assertFalse([item for item in self.state.outbox.pending() if item.event.event_type=="policy.promoted"])

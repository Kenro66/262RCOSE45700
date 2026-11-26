"""V5.1 S3 acceptance regressions. Synthetic HTTP only; no model calls."""
import unittest
from unittest.mock import patch
from copy import deepcopy
import issue48_formal_pipeline as p
from test_issue48_formal_pipeline import Fake, case, text

class ErrorAfterAccept(Fake):
    def __init__(self, *, lost=False, persisted=True, unchanged=False, wrong_session=False):
        super().__init__([text('age')]); self.lost=lost; self.persisted=persisted; self.unchanged=unchanged; self.wrong_session=wrong_session
    def __call__(self,method,url,payload,timeout):
        if method=='POST' and len(self.posts)==1 and self.lost:
            self.posts.append(dict(payload)); raise RuntimeError('HTTP_TRANSPORT_ERROR: response lost')
        if method=='GET' and url.endswith('/execution') and len(self.posts)==2:
            return 200, {'session_id':self.posts[0]['session_id'],'question_id':'fixture-q','execution_status':'FAILED','response':None}
        if method=='GET' and not url.endswith('/execution') and self.posts:
            cond={'age':'25'} if self.unchanged or (len(self.posts)==2 and self.persisted) else {}
            return 200, {'session_id':('wrong' if self.wrong_session else self.posts[0]['session_id']),'questions':[{'question_id':'fixture-q','message':self.posts[0]['message'],'conditions':cond,'retrieval_count':1,'clarification_count':1}]}
        return super().__call__(method,url,payload,timeout)

class Tests(unittest.TestCase):
    def setUp(self):
        self.guard=patch.object(p.v4,'urlopen',side_effect=AssertionError('network forbidden'));self.guard.start();self.addCleanup(self.guard.stop)
    def run_case(self,f): return p.agent_case(case(),f,'sim')
    def test_accepted_failed_execution_keeps_pair(self):
        f=ErrorAfterAccept(); a=self.run_case(f)
        self.assertEqual(a['mapping']['condition_acceptance']['source'],'POST_ACK')
        self.assertIsNotNone(a['final_error']);self.assertEqual(a['mapping']['status'],'SECOND_TURN_ERROR')
        s=p.single_case(case(),'single_matched_conditions',Fake(mode='single'),'sim',a)
        self.assertEqual(s['provided_conditions'],{'age':'25'});self.assertIsNone(s['task_success'])
    def test_lost_post_persisted_keeps_pair(self):
        a=self.run_case(ErrorAfterAccept(lost=True));self.assertEqual(a['mapping']['condition_acceptance']['source'],'PERSISTED_CONDITIONS');self.assertEqual(a['mapping']['submitted_conditions'],{'age':'25'});self.assertIsNotNone(a['final_error'])
    def test_lost_post_no_state_skips(self):
        a=self.run_case(ErrorAfterAccept(lost=True,persisted=False));self.assertEqual(p.pair_skip_reason(a),'ACCEPTANCE_NOT_CONFIRMED')
    def test_unchanged_state_is_not_proof(self):
        a=self.run_case(ErrorAfterAccept(lost=True,unchanged=True));self.assertIsNone(a['mapping']['submitted_conditions'])
    def test_wrong_session_is_not_proof(self):
        a=self.run_case(ErrorAfterAccept(lost=True,wrong_session=True));self.assertIsNone(a['mapping']['submitted_conditions'])
    def test_explicit_rejection_skips(self):
        a=self.run_case(Fake([text('age')],fail_second=True));self.assertEqual(p.pair_skip_reason(a),'SECOND_POST_REJECTED')
    def test_tampered_evidence_rejected(self):
        a=self.run_case(ErrorAfterAccept());a['mapping']['condition_acceptance']['source']='invented'
        with self.assertRaises(ValueError):p.matched_message(case(),a,'sim')
    def test_protocol_mismatch_never_accepts(self):
        a=self.run_case(ErrorAfterAccept());first,second=deepcopy(a['turns']);second['error']='PROTOCOL_SESSION_ID_MISMATCH';self.assertFalse(p.condition_acceptance(first,second,{'age':'25'})['accepted'])
    def test_no_unverified_submission_reason(self):
        a=self.run_case(Fake());self.assertEqual(p.pair_skip_reason(a),'NO_SECOND_TURN_SUBMISSION')

if __name__=='__main__': unittest.main(verbosity=2)

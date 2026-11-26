"""Offline integration tests; no actual network or formal evaluation."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import issue48_formal_collect as c
import test_issue48_formal_http as th


def cases():
    return [dict(qid='F001', question='原始问题', lang='zh', category='普通问答', gold_answer='NEVER_SEND'),
            dict(qid='F023', question='过境中国', lang='zh', category='国家/方向区分', gold_answer='NEVER_SEND',
                 two_turn_gold={'turn1':{'gold_answer':'NEVER_SEND'}, 'turn2':{'user_reply':'已获准离开监管区。','fixed_conditions':{'country':'CN','travel_type':'TRANSIT','authorized_to_leave_customs_control_area':True},'gold_answer':'NEVER_SEND'}})]


class Tests(unittest.TestCase):
    def setUp(self):
        self.transports=[]
        guard=patch.object(c.p.v4,'urlopen',side_effect=AssertionError('Unexpected network'))
        guard.start();self.addCleanup(guard.stop)
    def factory(self, case, mode):
        t=th.Fake(mode, clarification=case['qid']=='F023', fail_second=True)
        self.transports.append(t);return t
    def run_test(self, out):
        return c.collect(cases(),out,transport_factory=self.factory,meta={'code_commit':'test'},kind=c.TEST_KIND,turn_fn=th.turn)
    def test_split_pair_and_failure(self):
        with tempfile.TemporaryDirectory() as d:
            out=Path(d)/'new';m=self.run_test(out)
            self.assertEqual(m['scenario_counts'],dict(zip(c.p.SCENARIOS,(2,2,1,1))))
            self.assertEqual(m['technical_error_counts']['agent_original'],1)
            self.assertEqual(len(m['skipped']),2)
            read=lambda s:[json.loads(x) for x in (out/(s+'.jsonl')).read_text(encoding='utf-8').splitlines()]
            agent=read('agent_original')[1];s3=read('single_matched_conditions')[0]
            self.assertEqual(agent['mapping']['submitted_conditions'],{'destination_country':'CN'})
            self.assertEqual(s3['paired_agent_record_id'],agent['record_id'])
            self.assertEqual(s3['provided_conditions'],{'destination_country':'CN'})
            self.assertIsNotNone(agent['final_error'])
            for s in c.p.SCENARIOS:
                for r in read(s):
                    self.assertFalse(r['is_evaluation_result']);self.assertEqual(r['execution_kind'],c.TEST_KIND)
                    self.assertTrue(all(r[k] is None for k in c.h.SCORE_KEYS))
                self.assertEqual(m['output_sha256'][s+'.jsonl'],c.h.digest(out/(s+'.jsonl')))
    def test_no_gold_in_requests(self):
        with tempfile.TemporaryDirectory() as d:
            self.run_test(Path(d)/'new')
        payloads=[p for t in self.transports for p in t.posts]
        self.assertNotIn('NEVER_SEND',json.dumps(payloads))
        self.assertTrue(all(set(p) in ({'session_id','message'},{'session_id','message','question_id','conditions'}) for p in payloads))
    def test_no_overwrite(self):
        with tempfile.TemporaryDirectory() as d:
            out=Path(d)/'new';self.run_test(out);n=len(self.transports)
            with self.assertRaises(FileExistsError):self.run_test(out)
            self.assertEqual(n,len(self.transports))
    def test_release_gate(self):
        with tempfile.TemporaryDirectory() as d:
            out=Path(d)/'new'
            with self.assertRaises(PermissionError):c.collect(cases(),out,transport_factory=self.factory,meta={},kind=c.LIVE_KIND)
            self.assertFalse(out.exists());self.assertFalse(self.transports)
        with self.assertRaises(SystemExit) as ex:c.main(['--run-formal'])
        self.assertNotEqual(ex.exception.code,0)
    def test_abort_keeps_partial(self):
        calls=0
        def broken(case,mode):
            nonlocal calls
            calls+=1
            if calls==2:raise RuntimeError('synthetic infrastructure failure')
            return self.factory(case,mode)
        with tempfile.TemporaryDirectory() as d:
            out=Path(d)/'new'
            with self.assertRaisesRegex(RuntimeError,'synthetic infrastructure'):
                c.collect(cases(),out,transport_factory=broken,meta={},kind=c.TEST_KIND,turn_fn=th.turn)
            self.assertTrue((out/'ABORTED.json').exists());self.assertFalse((out/'manifest.json').exists())
            self.assertEqual(len((out/'agent_original.jsonl').read_text(encoding='utf-8').splitlines()),1)
    def test_test_namespace_requires_injection(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaisesRegex(ValueError,'synthetic turn'):
                c.collect(cases(),Path(d)/'new',transport_factory=self.factory,meta={},kind=c.TEST_KIND)
    def test_pair_tamper(self):
        case=c.input_projection(cases()[1]);a=c.agent_record(case,self.factory(case,'agent'),'run',c.TEST_KIND,{},th.turn)
        a['mapping']['submitted_conditions']={'destination_country':'KR'}
        with self.assertRaises(ValueError):c.matched_input(case,a,'run',c.TEST_KIND)
    def test_bad_dataset_plan(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/c.p.pre.FORMAL_NAME;path.write_text('{}')
            with self.assertRaisesRegex(ValueError,'SHA256'):c.main(['--plan','--questions',str(path)])

if __name__=='__main__':unittest.main(verbosity=2)

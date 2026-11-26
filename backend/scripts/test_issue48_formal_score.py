"""Synthetic tests only. Never grade actual smoke or formal answers here."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
import issue48_formal_score as s
import issue48_formal_collect as c
import test_issue48_formal_http as th
import test_issue48_formal_collect as tc


def cases():
    rows=[]
    for i in range(1,31):
        q=f'F{i:03d}';case=dict(qid=q,question='synthetic '+q,lang='zh',category='synthetic',expected_status='ANSWERED' if i<25 else 'INSUFFICIENT_EVIDENCE',gold_answer='synthetic' if i<25 else None,gold_evidence=[],answerable=i<25)
        if q in c.p.pre.SOURCES:
            keys=sum(c.p.pre.SOURCES[q],());case.update(expected_status='NEEDS_CLARIFICATION',answerable=False,gold_answer=None,two_turn_gold={'turn1':{'expected_status':'NEEDS_CLARIFICATION'},'turn2':{'expected_status':'ANSWERED','gold_answer':'synthetic','gold_evidence':[],'user_reply':'synthetic reply','fixed_conditions':{k:'CN' if k=='country' else 'synthetic' for k in keys}}})
        if i>=25:case['refusal_subtype']='OUT_OF_KB_COUNTRY' if i<29 else 'IN_KB_TOPIC_MISSING_NUMERIC_EVIDENCE'
        rows.append(case)
    return rows


def row(case,scenario=s.S[0],*,error=False,blocked=False,answered=True):
    return {'run_id':'fixture','record_id':case['qid']+scenario,'qid':case['qid'],'comparison_case':scenario,'final_error':'timeout' if error else None,'final_status':'ANSWERED' if answered else 'INSUFFICIENT_EVIDENCE','turns':[{'status':'ANSWERED','citations':[]}],'mapping':{'status':'ADAPTER_UNMAPPED' if blocked else 'NOT_REQUIRED'}}


def review(r,case,**overrides):
    t=s.template(r,case);t['reviewer']='fixture reviewer';t['notes']='Synthetic test only.'
    for f in t['required_judgements']:t['judgements'][f]=True
    t['judgements'].update(overrides);return t


class Tests(unittest.TestCase):
    def test_gold_final_not_top_level(self):
        cs=cases();self.assertEqual(sum(s.gold_for(x)['final_expected']=='ANSWERED' for x in cs),24)
        self.assertEqual(sum('turn1' in s.gold_for(x) for x in cs),7)
        self.assertEqual(s.gold_for(cs[12])['turn2']['gold_answer'],'synthetic')
    def test_direct_task(self):
        case=cases()[0];r=row(case);v=s.evaluate(r,case,review(r,case));self.assertTrue(v['task_success']);self.assertIsNone(v['two_turn_flow_pass']);self.assertTrue(v['citation_missing'])
    def test_technical_and_block_not_excluded(self):
        case=cases()[12]
        for r in (row(case,error=True),row(case,blocked=True)):
            v=s.evaluate(r,case,review(r,case));self.assertFalse(v['task_success']);self.assertFalse(v['two_turn_flow_pass']);self.assertEqual(s.metric([v],'task_success')['denominator'],1)
    def test_task_vs_two_turn_flow(self):
        case=cases()[12];r=row(case);v=s.evaluate(r,case,review(r,case,clarification_correct=False,clarification_recovered=False));self.assertTrue(v['task_success']);self.assertFalse(v['two_turn_flow_pass'])
        r['turns']=[{'status':'NEEDS_CLARIFICATION'},{'status':'ANSWERED','citations':[]}];r['mapping']={'submitted_conditions':{'destination_country':'CN'},'condition_acceptance':{'accepted':True}}
        self.assertTrue(s.evaluate(r,case,review(r,case))['two_turn_flow_pass'])
    def test_behavior_separate(self):
        case=cases()[12];r=row(case,s.S[1],answered=False);v=s.evaluate(r,case,review(r,case,answer_correct=False,answer_complete=False));self.assertFalse(v['task_success']);self.assertTrue(v['behavior_pass']);self.assertIsNone(v['two_turn_flow_pass'])
    def test_refusal(self):
        case=cases()[24];r=row(case,answered=False);v=s.evaluate(r,case,review(r,case));self.assertTrue(v['task_success']);self.assertIsNone(v['answer_correct']);self.assertEqual(v['refusal_subtype'],'OUT_OF_KB_COUNTRY')
    def test_no_incomplete_or_tampered_review(self):
        case=cases()[0];r=row(case);t=review(r,case)
        for k,v in [('answer_correct',None),('answer_correct',1),('refusal_correct',False)]:
            x=copy.deepcopy(t);x['judgements'][k]=v
            with self.assertRaises(ValueError):s.evaluate(r,case,x)
        t['gold']['gold_answer']='tamper'
        with self.assertRaises(ValueError):s.evaluate(r,case,t)
    def test_independent_denominators(self):
        cs=cases();rs=[]
        for scenario,case in [(s.S[0],cs[0]),(s.S[0],cs[12]),(s.S[1],cs[12]),(s.S[2],cs[12])]:
            r=row(case,scenario);rs.append(s.evaluate(r,case,review(r,case)))
        z=s.summarize(rs);self.assertEqual(z[s.S[0]]['task_success']['denominator'],2);self.assertEqual(z[s.S[1]]['task_success']['denominator'],1);self.assertEqual(z[s.S[2]]['same_qid_comparison'][s.S[0]]['task_success']['denominator'],1);self.assertIsNone(z[s.S[3]]['task_success']['rate'])
    def test_reject_fixture_and_smoke(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);m={'status':'COLLECTION_COMPLETE','execution_kind':c.TEST_KIND,'is_evaluation_result':False,'dataset_sha256':c.p.pre.FORMAL_SHA256,'mapping_version':c.p.pre.MAPPING_VERSION}
            for kind in (c.TEST_KIND,'HTTP_SMOKE','OFFLINE_SIMULATION'):
                m['execution_kind']=kind;(p/'manifest.json').write_text(json.dumps(m))
                with self.assertRaisesRegex(ValueError,'Only complete formal'):s.load_raw(p,cases())
    def test_coverage_and_hash(self):
        # Build an entirely synthetic collection, relabel ONLY in temp test
        # storage to exercise future formal-reader invariants. Never deliver it.
        with tempfile.TemporaryDirectory() as d:
            out=Path(d)/'new';cs=cases()
            m=c.collect(cs,out,transport_factory=lambda case,mode:th.Fake(mode,clarification=False),meta={},kind=c.TEST_KIND,turn_fn=th.turn)
            m['execution_kind']=c.LIVE_KIND
            for name in m['output_sha256']:
                f=out/name;rs=s.read_jsonl(f)
                for r in rs:r['execution_kind']=c.LIVE_KIND
                f.write_text(''.join(json.dumps(r)+'\n' for r in rs),encoding='utf-8');m['output_sha256'][name]=c.h.digest(f)
            (out/'manifest.json').write_text(json.dumps(m));_,rs=s.load_raw(out,cs);self.assertEqual(len(rs),67)
            f=out/(s.S[0]+'.jsonl');f.write_text(f.read_text()+'\n')
            with self.assertRaisesRegex(ValueError,'hash mismatch'):s.load_raw(out,cs)

if __name__=='__main__':unittest.main(verbosity=2)

import json
import unittest
from unittest.mock import patch
import issue48_formal_pipeline as p


def case(qid='F015'):
    if qid=='F015':
        fixed={'country':'CN','direction':'INBOUND','age':25,'frequent_traveler':False,'crew':False,'goods':'ordinary_personal_goods','value_cny':2500,'purchased_abroad':True,'retained_in_cn':True,'personal_use':True,'split_transport':False,'resident_status':'RESIDENT_TRAVELER'}
        return {'qid':qid,'question':'原始问题','lang':'zh','category':'条件追问','conditions':'DO_NOT_SEND','gold_answer':'DO_NOT_SEND','gold_evidence':['DO_NOT_SEND'],'two_turn_gold':{'turn1':{'required_fields':['DO_NOT_SEND']},'turn2':{'user_reply':'我是居民旅客。','fixed_conditions':fixed,'gold_answer':'DO_NOT_SEND'}}}
    return {'qid':qid,'question':'普通问题','lang':'zh','gold_answer':'DO_NOT_SEND'}

def select(field,*options): return {'field':field,'type':'select','options':list(options)}
def text(field): return {'field':field,'type':'text','options':[]}

class Fake:
    def __init__(self,qs=None,*,mode='agent',second_status='ANSWERED',fail_second=False,detail_fail=False):
        self.qs=qs;self.mode=mode;self.second_status=second_status;self.fail_second=fail_second;self.detail_fail=detail_fail;self.posts=[]
    def __call__(self,method,url,payload,timeout):
        assert url.startswith(p.SIM_URL+'/')
        if method=='POST':
            self.posts.append(dict(payload))
            if self.mode=='single': return 200,{'status':'ANSWERED','answer':'SIMULATED','citations':[]}
            if len(self.posts)==2 and self.fail_second: return 422,{'error':'fixture error'}
            return 202,{'session_id':payload['session_id'],'question_id':'fixture-q','execution_status':'RUNNING'}
        if url.endswith('/execution'):
            status=('NEEDS_CLARIFICATION' if self.qs is not None else 'ANSWERED') if len(self.posts)==1 else self.second_status
            return 200,{'session_id':self.posts[0]['session_id'],'question_id':'fixture-q','execution_status':'SUCCEEDED','response':{'status':status,'answer':'SIMULATED' if status=='ANSWERED' else None,'citations':[],'clarification':{'questions':self.qs} if status=='NEEDS_CLARIFICATION' else None,'model_name':'fixture-model'}}
        if self.detail_fail: return 500,{}
        return 200,{'questions':[{'question_id':'fixture-q','retrieval_count':int(len(self.posts)>1),'clarification_count':int(self.qs is not None)}]}

class Tests(unittest.TestCase):
    def setUp(self):
        # If V4 ever bypasses the fake HTTP injection, fail before a network call.
        self.guard=patch.object(p.v4,'urlopen',side_effect=AssertionError('network forbidden'));self.guard.start();self.addCleanup(self.guard.stop)
    def successful(self):
        f=Fake([select('traveler_status','RESIDENT_TRAVELER','NON_RESIDENT_TRAVELER')]);r=p.agent_case(case(),f,'sim');return f,r
    def test_agent_two_turn(self):
        f,r=self.successful();a,b=f.posts
        self.assertEqual(set(a),{'session_id','message'});self.assertEqual(b,dict(a,question_id='fixture-q',conditions={'traveler_status':'RESIDENT_TRAVELER'}));self.assertEqual(r['mapping']['status'],'SECOND_TURN_RETURNED');self.assertEqual(r['mapping']['decisions'][0]['source'],'turn2.user_reply');self.assertNotIn('DO_NOT_SEND',json.dumps(f.posts));self.assertIsNone(r['task_success'])
    def test_agent_no_clarification(self):
        f=Fake();r=p.agent_case(case(),f,'sim');self.assertEqual(len(f.posts),1);self.assertEqual(r['mapping']['status'],'NOT_REQUIRED');self.assertIsNone(r['mapping']['submitted_conditions'])
    def test_unknown_blocks_all(self):
        f=Fake([text('age'),text('unknown')]);r=p.agent_case(case(),f,'sim');self.assertEqual(len(f.posts),1);self.assertEqual(r['mapping']['status'],'ADAPTER_UNMAPPED');self.assertIsNone(r['mapping']['submitted_conditions'])
    def test_non_scripted_blocks(self):
        f=Fake([text('age')]);r=p.agent_case(case('F029'),f,'sim');self.assertEqual(len(f.posts),1);self.assertEqual(r['mapping']['status'],'ADAPTER_UNMAPPED')
    def test_legacy_field_blocks(self):
        f=Fake([text('country')]);r=p.agent_case(case(),f,'sim');self.assertEqual(r['mapping']['status'],'ADAPTER_UNMAPPED');self.assertEqual(len(f.posts),1)
    def test_no_third_turn(self):
        f=Fake([text('age')],second_status='NEEDS_CLARIFICATION');r=p.agent_case(case(),f,'sim');self.assertEqual(len(f.posts),2);self.assertEqual(r['mapping']['status'],'CLARIFICATION_LIMIT_REACHED');self.assertEqual(r['mapping']['submitted_conditions'],{'age':'25'})
    def test_failed_second_not_pairable(self):
        f=Fake([text('age')],fail_second=True);r=p.agent_case(case(),f,'sim');self.assertEqual(r['mapping']['attempted_conditions'],{'age':'25'});self.assertIsNone(r['mapping']['submitted_conditions']);self.assertIsNone(p.single_case(case(),'single_matched_conditions',Fake(mode='single'),'sim',r))
    def test_session_detail_block(self):
        f=Fake([text('age')],detail_fail=True);r=p.agent_case(case(),f,'sim');self.assertEqual(len(f.posts),1);self.assertEqual(r['mapping']['status'],'ADAPTER_BLOCKED')
    def test_single_original(self):
        f=Fake(mode='single');r=p.single_case(case(),'single_original',f,'sim');self.assertEqual(f.posts[0]['message'],case()['question']);self.assertIsNone(r['clarification_count']);self.assertIsNone(r['retrieval_count']);self.assertIsNone(r['model_name'])
    def test_single_full(self):
        f=Fake(mode='single');r=p.single_case(case(),'single_full_conditions',f,'sim');self.assertEqual(f.posts[0]['message'],'原始问题\n\n补充信息：我是居民旅客。');self.assertEqual(set(f.posts[0]),{'session_id','message'});self.assertIsNone(p.single_case(case('F029'),'single_full_conditions',f,'sim'))
    def test_single_matched(self):
        f,a=self.successful();s=Fake(mode='single');r=p.single_case(case(),'single_matched_conditions',s,'sim',a);self.assertEqual(r['provided_conditions'],f.posts[1]['conditions']);self.assertEqual(r['paired_agent_record_id'],a['record_id']);self.assertEqual(s.posts[0]['message'].split('补充信息（字段及值）：')[1],json.dumps(f.posts[1]['conditions'],ensure_ascii=False,sort_keys=True));self.assertNotEqual(s.posts[0]['session_id'],f.posts[0]['session_id']);self.assertNotIn('DO_NOT_SEND',json.dumps(r))
    def test_mismatched_pair_rejected(self):
        _,a=self.successful()
        with self.assertRaises(ValueError):p.single_case(case(),'single_matched_conditions',Fake(mode='single'),'other',a)
        a['mapping']['submitted_conditions']={'age':'99'}
        with self.assertRaises(ValueError):p.single_case(case(),'single_matched_conditions',Fake(mode='single'),'sim',a)
    def test_fallback_observation(self):
        self.assertEqual(p.observe({'raw_response':{'response':{'model_name':'fixture','workflow_fallback_triggered':False,'model_fallback_triggered':True}}})['workflow_fallback_triggered'],False);self.assertIsNone(p.observe({'raw_response':{'model_name':'fixture'}})['model_fallback_triggered'])
    def test_jsonl_schema(self):
        _,a=self.successful();self.assertEqual(json.loads(json.dumps(a,ensure_ascii=False)),a);self.assertEqual(a['execution_kind'],'OFFLINE_SIMULATION');self.assertFalse(a['is_evaluation_result']);self.assertTrue(all(a[k] is None for k in ('task_success','clarification_correct','continuation_correct','clarification_recovered')))
    def test_no_live_cli(self):
        with patch('sys.argv',['script','--questions','x','--run-real']),self.assertRaises(SystemExit) as e:p.main()
        self.assertNotEqual(e.exception.code,0)

if __name__=='__main__': unittest.main(verbosity=2)

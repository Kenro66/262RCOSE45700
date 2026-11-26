"""V5.2 tests use synthetic HTTP responses; no actual backend/model calls."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import issue48_formal_http as h

class Fake:
    def __init__(self, mode, *, wrong_mode=False, wrong_docs=False, clarification=True, fail_second=False, unmap=False):
        self.mode=mode; self.wrong_mode=wrong_mode; self.wrong_docs=wrong_docs;self.clarification=clarification;self.fail_second=fail_second;self.unmap=unmap;self.posts=[];self.calls=[]
    def __call__(self,method,url,payload,timeout):
        self.calls.append((method,url,payload))
        assert url.startswith(h.BASES[self.mode]+'/')
        if url.endswith('/health'):return 200,{'status':'ok','database':{'vector_extension':True}}
        if url.endswith('/openapi.json'):
            agent=(self.mode=='agent') != self.wrong_mode
            return 200,{'paths':dict.fromkeys(h.CHAT_PATHS if agent else ['/api/chat'],{})}
        if url.endswith('/api/documents'):return 200,[{'document_id':'DOC002' if self.wrong_docs else 'DOC001','status':'READY','page_count':2,'filename':'政策.pdf'}]
        if method=='POST':
            self.posts.append(dict(payload))
            if self.mode=='single':return 200,{'status':'ANSWERED','answer':'fixture','citations':[],'model_name':'fixture'}
            return 202,{'session_id':payload['session_id'],'question_id':'q-smoke','execution_status':'RUNNING'}
        sid=self.posts[0]['session_id']; msg=self.posts[0]['message']; n=len(self.posts)
        if url.endswith('/execution'):
            if self.fail_second and n==2:return 200,{'session_id':sid,'question_id':'q-smoke','execution_status':'FAILED','response':None}
            clar=self.clarification and n==1
            return 200,{'session_id':sid,'question_id':'q-smoke','execution_status':'SUCCEEDED','response':{'status':'NEEDS_CLARIFICATION' if clar else 'ANSWERED','answer':None if clar else 'fixture','citations':[],'model_name':'fixture','clarification':{'questions':[{'field':'unreviewed' if self.unmap else 'destination_country','type':'select','options':['CN','KR']}]} if clar else None}}
        return 200,{'session_id':sid,'questions':[{'question_id':'q-smoke','message':msg,'conditions':self.posts[-1].get('conditions',{}),'retrieval_count':int(n==2),'clarification_count':int(self.clarification)}]}

def turn(t,mode,message,previous=None,conditions=None):
    return h.live_turn(t,mode,message,previous,conditions,request_timeout=1,poll_interval=.001,poll_timeout=1)

class Tests(unittest.TestCase):
    def setUp(self):
        for name in ('urlopen',):
            g=patch.object(h.p.v4,name,side_effect=AssertionError('Unexpected V4 network'));g.start();self.addCleanup(g.stop)
    def test_get_only_precheck(self):
        ts={m:Fake(m) for m in h.BASES};r=h.precheck(ts);self.assertEqual(r['agent']['ready_count'],1);self.assertTrue(all(c[0]=='GET' for t in ts.values() for c in t.calls));self.assertEqual(len(r['document_snapshot_sha256']),64)
    def test_wrong_mode(self):
        with self.assertRaisesRegex(ValueError,'mode mismatch'):h.precheck({'agent':Fake('agent',wrong_mode=True),'single':Fake('single')})
    def test_wrong_documents(self):
        with self.assertRaisesRegex(ValueError,'records differ'):h.precheck({'agent':Fake('agent'),'single':Fake('single',wrong_docs=True)})
    def test_transport_boundary(self):
        with self.assertRaises(ValueError):h.LocalHTTP('https://example.com')
        t=h.LocalHTTP(h.BASES['agent'])
        for method,url,payload in [('POST',h.BASES['single']+'/api/chat?wait=false',{}),('DELETE',h.BASES['agent']+'/api/documents',None),('POST',h.BASES['agent']+'/api/chat?wait=false',{'session_id':'s','message':'m','gold':'x'}),('POST',h.BASES['agent']+'/api/chat?wait=false',{'session_id':'s','message':'m','question_id':'q','conditions':{'age':25}})]:
            with self.assertRaises(ValueError):t(method,url,payload,1)
    def test_utf8_real_transport_codec(self):
        t=h.LocalHTTP(h.BASES['agent']);raw={'filename':'海关 한국.pdf'}
        with patch.object(t.opener,'open') as o:
            r=o.return_value.__enter__.return_value;r.status=200;r.read.return_value=json.dumps(raw,ensure_ascii=False).encode('utf-8');code,obj=t('GET',h.BASES['agent']+'/api/documents',None,1)
            self.assertEqual((code,obj),(200,raw))
    def test_accepted_second_failure_retains_s3(self):
        a=Fake('agent',fail_second=True);rec=h.agent_smoke(a,'run',turn);self.assertEqual(rec['mapping']['submitted_conditions'],{'destination_country':'CN'});self.assertIsNotNone(rec['final_error']);s=Fake('single');paired=h.single_smoke('single_matched_conditions',s,'run',rec,turn);self.assertEqual(paired['paired_agent_record_id'],rec['record_id']);self.assertEqual(paired['provided_conditions'],{'destination_country':'CN'});self.assertNotEqual(a.posts[0]['session_id'],s.posts[0]['session_id']);self.assertEqual(a.posts[0]['message'],a.posts[1]['message']);self.assertEqual(a.posts[1]['question_id'],'q-smoke')
    def test_unmapped_no_second(self):
        a=Fake('agent',unmap=True);rec=h.agent_smoke(a,'run',turn);self.assertEqual(len(a.posts),1);self.assertEqual(rec['mapping']['status'],'ADAPTER_UNMAPPED');self.assertIsNone(h.single_smoke('single_matched_conditions',Fake('single'),'run',rec,turn))
    def test_s2_and_record_boundary(self):
        s=Fake('single');rec=h.single_smoke('single_full_conditions',s,'run',None,turn);self.assertEqual(s.posts[0]['message'],h.SMOKE_QUESTION+'\n\n补充信息：'+h.SMOKE_REPLY);self.assertEqual(set(s.posts[0]),{'message','session_id'});self.assertEqual(rec['execution_kind'],'HTTP_SMOKE');self.assertFalse(rec['is_evaluation_result']);self.assertTrue(all(rec[k] is None for k in h.SCORE_KEYS));self.assertIsNone(rec['retrieval_count'])
    def test_pair_tampering(self):
        rec=h.agent_smoke(Fake('agent'),'run',turn);rec['mapping']['submitted_conditions']={'destination_country':'KR'}
        with self.assertRaisesRegex(ValueError,'evidence mismatch'):h.single_smoke('single_matched_conditions',Fake('single'),'run',rec,turn)
    def test_formal_cli_locked(self):
        with patch.object(h,'LocalHTTP',side_effect=AssertionError('Network forbidden')):
            for args in (['--run-formal'],['--questions','x'],['--smoke'],['--check-only','--output-dir','x']):
                with self.assertRaises(SystemExit) as e:h.main(args)
                self.assertNotEqual(e.exception.code,0)
    def test_output_separation_no_overwrite(self):
        ts={m:Fake(m) for m in h.BASES};env=h.precheck(ts)
        with tempfile.TemporaryDirectory() as td:
            out=Path(td)/'fresh';counts,skip=h.run_smoke(ts,out,env,Path(td),turn);self.assertEqual(len(counts),4);self.assertEqual(skip,[]);manifest=json.loads((out/'manifest.json').read_text());self.assertEqual(manifest['formal_execution'],'DISABLED');self.assertFalse(manifest['is_evaluation_result']);self.assertEqual(len(manifest['output_sha256']),4)
            before=sum(len(t.posts) for t in ts.values())
            with self.assertRaises(FileExistsError):h.run_smoke(ts,out,env,Path(td),turn)
            self.assertEqual(sum(len(t.posts) for t in ts.values()),before)

if __name__=='__main__':unittest.main(verbosity=2)

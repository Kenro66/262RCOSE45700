import unittest, argparse, json, tempfile
from pathlib import Path
from unittest.mock import patch
import eval_http_pipeline as e

def case(qid): return {'qid':qid,**e.R02_FACTS[qid]['source_input_text'],'gold_answer':'DO_NOT_SEND'}
def select(field,*options): return {'field':field,'type':'select','options':list(options)}
def text(field): return {'field':field,'type':'text','options':[]}
def map_(qid,*qs): return e.map_conditions(case(qid),{'questions':list(qs)})
class Tests(unittest.TestCase):
 def test_all_facts(self):
  self.assertEqual(len(e.R02_FACTS),40)
  for qid,v in e.R02_FACTS.items(): self.assertTrue(e.matches_input(case(qid),v))
 def test_scope(self): self.assertEqual(map_('P001',select('destination_country','CN','KR'),select('direction','INBOUND','OUTBOUND'))[0],{'destination_country':'KR','direction':'INBOUND'})
 def test_alias(self): self.assertEqual(map_('P019',select('resident_status','居民旅客','非居民旅客'))[0],{'resident_status':'居民旅客'})
 def test_boolean(self): self.assertEqual(map_('P027',select('adult','是','否'),select('is_frequent_border_crosser','true','false'))[0],{'adult':'是','is_frequent_border_crosser':'true'})
 def test_region(self): self.assertEqual(map_('P021',select('origin_region','香港或澳门','非港澳地区'))[0],{'origin_region':'香港或澳门'})
 def test_no_guesses(self):
  for f in ['age','quantity','unknown']:
   v,a=map_('P021',text(f));self.assertIsNone(v);self.assertEqual(a['status'],'ADAPTER_UNMAPPED')
 def test_ambiguous(self): self.assertIsNone(map_('P019',select('traveler_status','RESIDENT_TRAVELER','居民旅客'))[0])
 def test_domain(self):
  for qid in ['P037','P038','P039','P040']: self.assertIsNone(map_(qid,select('destination_country','CN','KR'))[0])
  self.assertEqual(map_('P037',select('country','日本','中国'))[0],{'country':'日本'})
 def test_holds(self):
  for qid,fields in e.REVIEW_HOLDS.items():
   for field in fields:
    v,a=map_(qid,text(field));self.assertIsNone(v);self.assertIn('PENDING_FACT_REVIEW',a['unmapped'][0]['reason'])
 def test_all_or_nothing(self):
  v,a=map_('P001',select('destination_country','CN','KR'),text('age'));self.assertIsNone(v);self.assertIsNone(a['submitted_conditions']);self.assertEqual(len(a['decisions']),1);self.assertEqual(len(a['unmapped']),1)
 def test_duplicate(self): self.assertIsNone(map_('P001',text('direction'),text('direction'))[0])
 def test_changed(self): self.assertIsNone(e.map_conditions(dict(case('P001'),conditions='changed'),{'questions':[text('direction')]})[0])
 def simulate(self,qs):
  posts=[]; args=argparse.Namespace(base_url='http://localhost',mode='agent',request_timeout=5,poll_interval=.001,poll_timeout=5)
  def http(method,url,payload,timeout):
   if method=='POST':
    posts.append(payload);return 202,{'session_id':payload['session_id'],'question_id':'q','execution_status':'RUNNING'}
   sid=posts[0]['session_id']
   if url.endswith('/execution'): return 200,{'session_id':sid,'question_id':'q','execution_status':'SUCCEEDED','response':{'status':'NEEDS_CLARIFICATION' if len(posts)==1 else 'ANSWERED','answer':None if len(posts)==1 else 'ok','citations':[],'clarification':{'questions':qs} if len(posts)==1 else None}}
   return 200,{'questions':[{'question_id':'q','retrieval_count':len(posts)-1,'clarification_count':1}]}
  with patch.object(e,'http_json',side_effect=http): t,a=e.run_case(case('P001'),args)
  return t,a,posts
 def test_two_turn(self):
  t,a,p=self.simulate([select('country','CN','KR'),select('direction','INBOUND','OUTBOUND')]);self.assertEqual(len(p),2);self.assertEqual(p[1],dict(p[0],question_id='q',conditions={'country':'KR','direction':'INBOUND'}));self.assertNotIn('DO_NOT_SEND',json.dumps(p));self.assertEqual(t[-1]['retrieval_count'],1);self.assertEqual(a['status'],'SECOND_TURN_RETURNED')
 def test_block_no_partial(self):
  t,a,p=self.simulate([text('direction'),text('age')]);self.assertEqual(len(p),1);self.assertEqual(a['status'],'ADAPTER_UNMAPPED')
 def test_single(self):
  with patch.object(e,'http_json',return_value=(200,{'status':'ANSWERED','answer':'ok','citations':[]})) as h:
   t=e.run_first_turn('http://localhost','single','q',5,.001,5);self.assertEqual(h.call_count,1);self.assertIsNone(t['retrieval_count']);self.assertEqual(t['status'],'ANSWERED')
if __name__=='__main__': unittest.main()

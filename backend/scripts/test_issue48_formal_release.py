"""Synthetic release-gate tests; no production HTTP, no formal questions."""
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import issue48_formal_release as r
import test_issue48_formal_http as th

class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.repo=self.root/'repo';self.repo.mkdir();(self.repo/'eval').mkdir()
        self.q=self.repo/'eval'/r.c.p.pre.FORMAL_NAME;self.q.write_text('fixture')
        self.out=self.root/'fresh';self.ev=self.root/'evidence.json';self.ap=self.root/'approval.json'
        self.now=datetime(2026,12,5,tzinfo=timezone.utc);self.commit='a'*40;self.hashes={'source':'b'*64}
        self.ts={m:th.Fake(m) for m in r.h.BASES};self.snap=r.h.precheck(self.ts)
        self.cases=[{'qid':'fixture'}]
        cfg=dict.fromkeys(r.ENV_FIELDS,'fixture')
        cfg.update(generation_endpoint='https://example.invalid/v1',embedding_endpoint='https://example.invalid/embed',max_tokens=2048,temperature=.6,retrieval_top_k=5,execution_timeout_seconds=120,generation_timeout_seconds=60,index_fingerprint='c'*64)
        self.e={'code_commit':self.commit,'endpoints':r.h.BASES,'document_snapshot_sha256':self.snap['document_snapshot_sha256'],'operator':'synthetic','common_config':cfg,'corpus':[{'document_id':'DOC001','filename':'政策.pdf','page_count':2,'pdf_sha256':'d'*64}],'evidence_notes':'synthetic only'}
        self.a={'status':'APPROVED_FORMAL_30','release_version':r.VERSION,'code_commit':self.commit,'source_sha256':self.hashes,'dataset_sha256':r.c.p.pre.FORMAL_SHA256,'mapping_version':r.c.p.pre.MAPPING_VERSION,'rubric_version':r.score.VERSION,'endpoints':r.h.BASES,'approval_references':['https://github.com/Kenro66/262RCOSE45700/issues/48#synthetic-test-only'],'approved_by':['synthetic-test-only'],'confirmations':dict.fromkeys(r.REQUIRED_CONFIRMATIONS,True),'valid_from':'2026-12-04T00:00:00+00:00','valid_until':'2026-12-06T00:00:00+00:00','document_snapshot_sha256':self.snap['document_snapshot_sha256'],'environment_evidence_sha256':None,'output_dir':str(self.out)}
        self.write()
    def write(self):
        self.ev.write_text(json.dumps(self.e),encoding='utf-8');self.a['environment_evidence_sha256']=r.h.digest(self.ev);self.ap.write_text(json.dumps(self.a),encoding='utf-8')
    def prepare(self):
        with patch.object(r,'git',return_value='tracked'),patch.object(r,'source_state',return_value=(self.commit,self.hashes)),patch.object(r.c.p.pre,'read_formal',return_value=self.cases),patch.object(r.h,'LocalHTTP',side_effect=lambda base:self.ts[next(m for m,b in r.h.BASES.items() if b==base)]):
            return r.prepare(self.repo,self.q,self.out,self.ap,self.ev,now=self.now)
    def test_valid_preflight_get_only(self):
        cases,out,meta,ts,permit=self.prepare();r.validate_permit(permit,cases,out,meta)
        self.assertFalse(out.exists());self.assertTrue(all(not t.posts for t in ts.values()));self.assertEqual(meta['code_commit'],self.commit)
    def test_draft_rejected_before_http(self):
        self.a['status']='DRAFT_NOT_AUTHORIZED';self.write()
        with self.assertRaises(PermissionError):self.prepare()
        self.assertEqual(sum(len(t.calls) for t in self.ts.values()),6)
    def test_missing_confirmation(self):
        self.a['confirmations']['F018_mapping_confirmed']=False;self.write()
        with self.assertRaisesRegex(ValueError,'confirmation'):self.prepare()
    def test_wrong_commit_and_hash(self):
        for field,val in [('code_commit','e'*40),('source_sha256',{}),('dataset_sha256','f'*64),('mapping_version','wrong'),('rubric_version','wrong')]:
            orig=copy.deepcopy(self.a);self.a[field]=val;self.write()
            with self.assertRaises(ValueError):self.prepare()
            self.a=orig
    def test_expired(self):
        self.a['valid_until']='2026-12-04T01:00:00+00:00';self.write()
        with self.assertRaisesRegex(ValueError,'window'):self.prepare()
    def test_tampered_evidence(self):
        self.ev.write_text('{}')
        with self.assertRaisesRegex(ValueError,'hash mismatch'):self.prepare()
    def test_unset_config(self):
        self.e['common_config']['temperature']='UNSET';self.write()
        with self.assertRaisesRegex(ValueError,'Unverified'):self.prepare()
    def test_snapshot_changed(self):
        self.ts={m:th.Fake(m,wrong_docs=True) for m in r.h.BASES}
        with self.assertRaisesRegex(ValueError,'snapshot differs'):self.prepare()
        self.assertTrue(all(not t.posts for t in self.ts.values()))
    def test_corpus_mismatch(self):
        self.e['corpus'][0]['page_count']=99;self.write()
        with self.assertRaisesRegex(ValueError,'inventory'):self.prepare()
    def test_output_exists(self):
        self.out.mkdir()
        with self.assertRaisesRegex(ValueError,'Output must be new'):self.prepare()
    def test_permit_cannot_rebind(self):
        cases,out,meta,ts,permit=self.prepare()
        for xs,o,m in [([],out,meta),(cases,self.root/'other',meta),(cases,out,{**meta,'code_commit':'x'})]:
            with self.assertRaises(PermissionError):r.validate_permit(permit,xs,o,m)
        with self.assertRaises(PermissionError):r.validate_permit(None,cases,out,meta)
    def test_unconfirmed_cli_no_prepare(self):
        with patch.object(r,'prepare',side_effect=AssertionError('Must not prepare')):
            with self.assertRaises(SystemExit):r.main(['--run-formal','--questions',str(self.q),'--approval',str(self.ap),'--environment-evidence',str(self.ev),'--output-dir',str(self.out)])

if __name__=='__main__':unittest.main(verbosity=2)

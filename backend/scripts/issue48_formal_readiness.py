#!/usr/bin/env python3
"""Read-only acceptance check for V5 offline rehearsal. Never calls HTTP."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import issue48_formal_batch as batch
import issue48_formal_pipeline as p

SCORES = ('task_success','answer_correct','answer_complete','citation_correct','clarification_correct','continuation_correct','clarification_recovered','refusal_correct')
EXPECTED = dict(zip(p.SCENARIOS,(30,30,7,6)))

def require(ok, message):
    if not ok:
        raise ValueError(message)

def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def verify(cases, root, expected_counts):
    root = Path(root)
    manifest = json.loads((root/'manifest.json').read_text(encoding='utf-8'))
    mark = json.loads((root/'OFFLINE_ONLY.json').read_text(encoding='utf-8'))
    run = manifest.get('run_id')
    require(isinstance(run,str) and run.startswith('offline-v5-'), 'Invalid run ID')
    for x in (manifest,mark):
        require(x.get('execution_kind')=='OFFLINE_SIMULATION' and x.get('is_evaluation_result') is False and x.get('run_id')==run, 'Invalid offline marker')
    require(manifest.get('status')=='COMPLETE' and manifest.get('mapping_version')==p.pre.MAPPING_VERSION and manifest.get('dataset_sha256')==p.pre.FORMAL_SHA256, 'Incomplete run or dataset mismatch')
    source = {Path(x).name:digest(Path(x)) for x in (batch.__file__,p.__file__,p.pre.__file__,p.v4.__file__)}
    require(manifest.get('source_sha256')==source, 'Source hash mismatch')
    require(manifest.get('scenario_counts')==expected_counts, 'Scenario count mismatch')
    require(set(manifest.get('output_sha256',{}))=={s+'.jsonl' for s in p.SCENARIOS}, 'Output set mismatch')
    byid={c['qid']:c for c in cases}
    agents, ids, sessions, seen, actual = {},set(),set(),set(),{}
    for s in p.SCENARIOS:
        f=root/(s+'.jsonl')
        require(digest(f)==manifest['output_sha256'][f.name], f'{s}: output hash mismatch')
        rows=[json.loads(l) for l in f.read_text(encoding='utf-8').splitlines() if l.strip()]
        require(len(rows)==expected_counts[s], f'{s}: row count mismatch')
        actual[s]=len(rows)
        for r in rows:
            qid=r['qid']; tag=(s,qid); rid=r['record_id']
            require(qid in byid and tag not in seen and isinstance(rid,str) and bool(rid) and rid not in ids, 'Duplicate/unknown case or record')
            seen.add(tag); ids.add(rid); c=byid[qid]
            require(r['comparison_case']==s and r['run_id']==run and r['question']==c['question'] and r['dataset_sha256']==p.pre.FORMAL_SHA256 and r['mapping_version']==p.pre.MAPPING_VERSION and r['execution_kind']=='OFFLINE_SIMULATION' and r['is_evaluation_result'] is False, f'{tag}: invalid identity')
            require(r['pipeline_sha256']==source[Path(p.__file__).name] and r['preflight_sha256']==source[Path(p.pre.__file__).name] and r['v4_pipeline_sha256']==source[Path(p.v4.__file__).name], f'{tag}: source hash mismatch')
            require(all(k in r and r[k] is None for k in SCORES), f'{tag}: score present or missing')
            turns=r['turns']; first=turns[0]['request_body']; sid=first['session_id']
            require(isinstance(sid,str) and bool(sid) and sid not in sessions, f'{tag}: reused first-round session')
            sessions.add(sid)
            require(set(first)=={'session_id','message'} and r['turn_count']==len(turns) and all(t['request_url'].startswith(p.SIM_URL+'/') for t in turns), f'{tag}: invalid turn request')
            if s=='agent_original':
                require(r['rag_mode']=='agent' and first['message']==c['question'] and len(turns) in (1,2), f'{tag}: invalid Agent input')
                require(r['provided_conditions'] is None and r['paired_agent_record_id'] is None, f'{tag}: invalid Agent comparison metadata')
                if len(turns)==2:
                    second=turns[1]['request_body']; audit=r['mapping']; expected,_=p.map_formal(c,turns[0]['clarification'])
                    require(turns[0]['status']=='NEEDS_CLARIFICATION' and set(second)=={'session_id','message','question_id','conditions'} and second['session_id']==sid and second['question_id']==turns[0]['question_id'] and second['message']==c['question'] and expected and second['conditions']==expected==audit['attempted_conditions'], f'{tag}: invalid continuation')
                    acceptance=p.condition_acceptance(turns[0],turns[1],expected)
                    require(audit['condition_acceptance']==acceptance and audit['submitted_conditions']==(expected if acceptance['accepted'] else None), f'{tag}: acceptance audit mismatch')
                else:
                    require(r['mapping']['submitted_conditions'] is None, f'{tag}: unexpected submitted facts')
                agents[qid]=r
            else:
                require(r['rag_mode']=='single' and len(turns)==1, f'{tag}: invalid Single mode')
                if s=='single_original':
                    msg=c['question']; supplied=None; pair=None
                elif s=='single_full_conditions':
                    msg=p.pre.full_conditions_message(c); supplied={'user_reply':c['two_turn_gold']['turn2']['user_reply']}; pair=None
                else:
                    agent=agents[qid]; msg,supplied=p.matched_message(c,agent,run); pair=agent['record_id']
                require(msg is not None and first['message']==msg and r['provided_conditions']==supplied and r['paired_agent_record_id']==pair, f'{tag}: invalid Single input/pair')
    expected_skips={}
    for qid,c in byid.items():
        for s in p.SCENARIOS:
            reason=None
            if s=='single_full_conditions' and p.pre.full_conditions_message(c) is None: reason='NO_SCRIPTED_REPLY'
            if s=='single_matched_conditions' and not agents[qid]['mapping']['submitted_conditions']: reason=p.pair_skip_reason(agents[qid])
            if reason: expected_skips[(s,qid)]=reason
            else: require((s,qid) in seen, f'{s}/{qid}: missing eligible case')
    skips=manifest['skipped']; observed={(x['comparison_case'],x['qid']):x['reason'] for x in skips}
    require(len(skips)==len(observed) and observed==expected_skips and not seen.intersection(observed), 'Skip audit mismatch')
    return actual,len(skips)

class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'run'; self.cases=batch.Tests().fixture_cases()
        self.manifest=batch.rehearsal(self.cases,self.root); self.expected=self.manifest['scenario_counts']
    def test_valid(self):
        self.assertEqual(verify(self.cases,self.root,self.expected),(self.expected,4))
    def test_changed_output(self):
        f=self.root/'agent_original.jsonl'; f.write_bytes(f.read_bytes()+b'\n')
        with self.assertRaisesRegex(ValueError,'output hash mismatch'): verify(self.cases,self.root,self.expected)
    def test_score_even_if_hash_updated(self):
        f=self.root/'agent_original.jsonl'; rows=[json.loads(x) for x in f.read_text(encoding='utf-8').splitlines()]; rows[0]['task_success']=True
        f.write_text(''.join(json.dumps(x,ensure_ascii=False)+'\n' for x in rows),encoding='utf-8')
        self.manifest['output_sha256'][f.name]=digest(f); (self.root/'manifest.json').write_text(json.dumps(self.manifest),encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'score present'): verify(self.cases,self.root,self.expected)
    def test_wrong_marker(self):
        f=self.root/'OFFLINE_ONLY.json'; mark=json.loads(f.read_text()); mark['is_evaluation_result']=True; f.write_text(json.dumps(mark))
        with self.assertRaisesRegex(ValueError,'offline marker'): verify(self.cases,self.root,self.expected)
    def test_wrong_skip(self):
        self.manifest['skipped'][0]['reason']='made up'; (self.root/'manifest.json').write_text(json.dumps(self.manifest))
        with self.assertRaisesRegex(ValueError,'Skip audit mismatch'): verify(self.cases,self.root,self.expected)

def main():
    ap=argparse.ArgumentParser(description=__doc__); ap.add_argument('--self-test',action='store_true'); ap.add_argument('--questions',type=Path); ap.add_argument('--rehearsal-dir',type=Path); args=ap.parse_args()
    if args.self_test:
        if args.questions or args.rehearsal_dir: ap.error('Use --self-test alone')
        return 0 if unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(Tests)).wasSuccessful() else 1
    if not args.questions or not args.rehearsal_dir: ap.error('Specify --questions and --rehearsal-dir')
    try:
        counts, skips=verify(p.pre.read_formal(args.questions),args.rehearsal_dir,EXPECTED)
    except (OSError,ValueError,KeyError,TypeError,IndexError) as e: ap.error(str(e))
    print('OFFLINE ARTIFACT AUDIT: PASS')
    for s,n in counts.items(): print(s+':',n)
    print('Records:',sum(counts.values()),'Skipped:',skips)
    print('Source/output hashes, request boundaries, session separation, pair links and null scores: PASS')
    print('FORMAL LIVE EXECUTION: NOT AUTHORIZED / NOT IMPLEMENTED BY THIS SCRIPT')
    return 0
if __name__=='__main__': raise SystemExit(main())

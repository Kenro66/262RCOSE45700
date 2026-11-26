#!/usr/bin/env python3
"""V5 batch/persistence rehearsal only. No live transport or grading entrypoint."""
from __future__ import annotations
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4
import issue48_formal_pipeline as p

VERSION = '5.1-formal-batch-offline-draft'
MARK = 'OFFLINE_SIMULATION'
# Artificial clarification requests, solely to exercise protocol branches.
# These are NOT predictions of what the actual Agent will request.
FIXTURES = {
    'F013': ('destination_country', 'select', ['CN','KR']),
    'F014': ('has_items_requiring_declaration', 'text', []),
    'F015': ('traveler_status', 'select', ['RESIDENT_TRAVELER','NON_RESIDENT_TRAVELER']),
    'F016': ('customs_verified_export', 'text', []),
    'F017': ('is_professional_crew', 'text', []),
    'F018': ('age', 'text', []),
    'F023': ('destination_country', 'select', ['CN','KR']),
    'F029': ('unreviewed_field', 'text', []),
}

class FixtureTransport:
    def __init__(self, qid, mode):
        self.mode, self.qid = mode, qid
        self.posts = []
        self.question_id = 'fixture-' + uuid4().hex
        self.rule = FIXTURES.get(qid) if mode == 'agent' else None
    def __call__(self, method, url, payload, timeout):
        if not url.startswith(p.SIM_URL + '/'):
            raise AssertionError('Only offline fixture URL is allowed')
        if method == 'POST':
            self.posts.append(dict(payload))
            if self.mode == 'single':
                return 200, {'status':'ANSWERED','answer':'OFFLINE_FIXTURE_ONLY','citations':[]}
            return 202, {'session_id':payload['session_id'], 'question_id':self.question_id,'execution_status':'RUNNING'}
        if method != 'GET' or not self.posts:
            raise AssertionError('Unexpected fixture call')
        if url.endswith('/execution'):
            clarifying = self.rule is not None and len(self.posts) == 1
            questions = []
            if clarifying:
                field,kind,options = self.rule
                questions = [{'field':field,'type':kind,'options':options}]
            return 200, {'session_id':self.posts[0]['session_id'],'question_id':self.question_id,
                         'execution_status':'SUCCEEDED','response':{
                             'status':'NEEDS_CLARIFICATION' if clarifying else 'ANSWERED',
                             'answer':None if clarifying else 'OFFLINE_FIXTURE_ONLY','citations':[],
                             'clarification':{'questions':questions} if clarifying else None}}
        return 200, {'questions':[{'question_id':self.question_id,
                                  'retrieval_count':int(not self.rule or len(self.posts)>1),
                                  'clarification_count':int(self.rule is not None)}]}


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def dump_json(path, value):
    with path.open('x', encoding='utf-8', newline='\n') as f:
        json.dump(value,f,ensure_ascii=False,indent=2,allow_nan=False)
        f.write('\n')


def write_row(f, row, scenario, run_id):
    if (row.get('execution_kind') != MARK or row.get('is_evaluation_result') is not False
            or row.get('comparison_case') != scenario or row.get('run_id') != run_id):
        raise ValueError('Refusing to persist non-fixture or mismatched row')
    if any(row.get(k) is not None for k in ('task_success','answer_correct','answer_complete','citation_correct','refusal_correct','clarification_correct','continuation_correct','clarification_recovered')):
        raise ValueError('Offline fixture must not carry scores')
    f.write(json.dumps(row,ensure_ascii=False,allow_nan=False)+'\n')
    f.flush()


def rehearsal(cases, output_dir):
    # New directory only: cannot replace historic results or reuse a run folder.
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=False, exist_ok=False)
    run_id = 'offline-v5-' + uuid4().hex
    counts, skips, agents = Counter(), [], {}
    dump_json(output_dir/'OFFLINE_ONLY.json', {'execution_kind':MARK,'is_evaluation_result':False,'run_id':run_id,
              'status':'STARTED','note':'Artificial fixture responses; never use for accuracy or latency metrics.'})
    # Also block accidental access if the V4 patch is ever bypassed.
    with patch.object(p.v4,'urlopen',side_effect=AssertionError('Network forbidden in rehearsal')):
        for scenario in p.SCENARIOS:
            with (output_dir/(scenario+'.jsonl')).open('x',encoding='utf-8',newline='\n') as f:
                for case in cases:
                    qid = case['qid']
                    if scenario == 'agent_original':
                        row = p.agent_case(case,FixtureTransport(qid,'agent'),run_id)
                        agents[qid] = row
                    else:
                        row = p.single_case(case,scenario,FixtureTransport(qid,'single'),run_id,agents.get(qid))
                    if row is None:
                        reason = ('NO_SCRIPTED_REPLY' if scenario=='single_full_conditions'
                                  else p.pair_skip_reason(agents[qid]))
                        skips.append({'qid':qid,'comparison_case':scenario,'reason':reason})
                        continue
                    write_row(f,row,scenario,run_id)
                    counts[scenario] += 1
    files = {s+'.jsonl':digest(output_dir/(s+'.jsonl')) for s in p.SCENARIOS}
    manifest = {'version':VERSION,'execution_kind':MARK,'is_evaluation_result':False,'run_id':run_id,
                'status':'COMPLETE','mapping_version':p.pre.MAPPING_VERSION,'dataset_sha256':p.pre.FORMAL_SHA256,'scenario_counts':{s:counts[s] for s in p.SCENARIOS},
                'skipped':skips,'source_sha256':{Path(x).name:digest(x) for x in (__file__,p.__file__,p.pre.__file__,p.v4.__file__)},
                'output_sha256':files,'note':'Counts describe fixture coverage only; no formal results or scoring.'}
    dump_json(output_dir/'manifest.json',manifest)
    return manifest


class Tests(unittest.TestCase):
    def fixture_cases(self):
        # Synthetic inputs for the persistence tests; real frozen file is checked by CLI.
        return [{'qid':'F001','question':'fixture','lang':'zh'},
                {'qid':'F015','question':'fixture','lang':'zh','two_turn_gold':{'turn2':{'user_reply':'我是居民旅客。','fixed_conditions':{
                    'country':'CN','direction':'INBOUND','age':25,'frequent_traveler':False,'crew':False,'goods':'ordinary_personal_goods','value_cny':2500,'purchased_abroad':True,'retained_in_cn':True,'personal_use':True,'split_transport':False,'resident_status':'RESIDENT_TRAVELER'}}}},
                {'qid':'F029','question':'fixture','lang':'zh'}]
    def test_split_and_pair(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)/'new'; m=rehearsal(self.fixture_cases(),root)
            self.assertEqual(m['scenario_counts'],dict(zip(p.SCENARIOS,(3,3,1,1))))
            rows={s:[json.loads(l) for l in (root/(s+'.jsonl')).read_text(encoding='utf-8').splitlines()] for s in p.SCENARIOS}
            self.assertTrue(all(x['execution_kind']==MARK and x['task_success'] is None for r in rows.values() for x in r))
            pair=rows['single_matched_conditions'][0]; a=rows['agent_original'][1]
            self.assertEqual(pair['paired_agent_record_id'],a['record_id']); self.assertEqual(pair['provided_conditions'],a['mapping']['submitted_conditions'])
            self.assertEqual(rows['agent_original'][2]['mapping']['status'],'ADAPTER_UNMAPPED')
            self.assertEqual(m['output_sha256']['agent_original.jsonl'],digest(root/'agent_original.jsonl'))
    def test_no_overwrite(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)/'new'; rehearsal(self.fixture_cases(),root); before=digest(root/'manifest.json')
            with self.assertRaises(FileExistsError): rehearsal(self.fixture_cases(),root)
            self.assertEqual(before,digest(root/'manifest.json'))
    def test_reject_scores_and_real(self):
        import io
        for x in ({'execution_kind':'LIVE'},{'task_success':True}):
            row={'execution_kind':MARK,'is_evaluation_result':False,'comparison_case':'agent_original','run_id':'r',**x}
            with self.assertRaises(ValueError): write_row(io.StringIO(),row,'agent_original','r')
    def test_no_live_cli(self):
        with self.assertRaises(SystemExit) as e: main(['--run-real'])
        self.assertNotEqual(e.exception.code,0)


def main(argv=None):
    ap=argparse.ArgumentParser(description='V5 OFFLINE fixture rehearsal; no real execution option')
    group=ap.add_mutually_exclusive_group(required=True)
    group.add_argument('--self-test',action='store_true')
    group.add_argument('--simulate',action='store_true')
    ap.add_argument('--questions',type=Path)
    ap.add_argument('--output-dir',type=Path)
    args=ap.parse_args(argv)
    if args.self_test:
        if args.questions or args.output_dir: ap.error('--self-test takes no dataset/output arguments')
        return 0 if unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(Tests)).wasSuccessful() else 1
    if args.questions is None or args.output_dir is None: ap.error('--simulate requires --questions and --output-dir')
    # Validate before creating any directory. Keep mock outputs outside repo.
    cases=p.pre.read_formal(args.questions)
    for c in cases: p.pre.candidate_facts(c)
    repo=Path(__file__).resolve().parents[2]
    if repo in (args.output_dir.resolve(), *args.output_dir.resolve().parents):
        ap.error('Use a new output directory outside the repository')
    m=rehearsal(cases,args.output_dir)
    print('V5 OFFLINE FIXTURE REHEARSAL / NO HTTP')
    for scenario,n in m['scenario_counts'].items(): print(f'{scenario}: {n}')
    print('Skipped scenario/case pairs:',len(m['skipped']))
    print('Output:',args.output_dir.resolve())
    print('Formal live execution: DISABLED. No evaluation scores generated.')
    return 0

if __name__=='__main__': raise SystemExit(main())

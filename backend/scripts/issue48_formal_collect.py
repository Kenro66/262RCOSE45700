#!/usr/bin/env python3
"""V5.5 review candidate: formal batch collector, live release gated.

Collection is separate from scoring. The legacy CLI only permits the frozen input
plan. The separately authorized release entry uses V5.2 live_turn; tests
inject synthetic transports. No formal authorization has been issued.
"""
from __future__ import annotations
import argparse
from collections import Counter
import json
import os
from pathlib import Path
from uuid import uuid4
from unittest.mock import patch
import issue48_formal_http as h
import issue48_formal_pipeline as p

VERSION = '5.5-collector-review-20261126'
TEST_KIND = 'OFFLINE_FORMAL_ENGINE_TEST'
LIVE_KIND = 'FORMAL_HTTP_RAW'


def input_projection(case):
    """Discard all grading keys before collection; retain only reviewed inputs."""
    c = {k: case[k] for k in ('qid', 'question', 'lang', 'category')}
    if c['qid'] in p.pre.SOURCES:
        p.pre.candidate_facts(case)
        t = case['two_turn_gold']['turn2']
        c['two_turn_gold'] = {'turn2': {k: t[k] for k in ('user_reply', 'fixed_conditions')}}
    return c


def as_record(row, kind, meta):
    # p.row is an offline draft builder. Re-label *before* persistence;
    # no production result is ever stored under the offline V5.1 namespace.
    row.update(schema_version=VERSION, execution_kind=kind,
               is_evaluation_result=False,
               code_commit=meta.get('code_commit'),
               document_snapshot_sha256=meta.get('document_snapshot_sha256'),
               collector_sha256=h.digest(__file__),
               http_pipeline_sha256=h.digest(h.__file__),
               configured_model=meta.get('configured_models', {}).get(row['rag_mode']),
               model_observations_by_turn=[p.observe(t) for t in row['turns']])
    return row


def agent_record(case, transport, run_id, kind, meta, turn_fn):
    # Reuse the reviewed two-turn algorithm without altering its source.
    # Scoped substitution is safe only in this sequential collector.
    with patch.object(p, 'turn', side_effect=turn_fn):
        return as_record(p.agent_case(case, transport, run_id), kind, meta)


def matched_input(case, agent, run_id, kind):
    if (agent.get('execution_kind') != kind or agent.get('run_id') != run_id
        or agent.get('qid') != case['qid'] or agent.get('question') != case['question']
        or agent.get('dataset_sha256') != p.pre.FORMAL_SHA256
        or agent.get('mapping_version') != p.pre.MAPPING_VERSION
        or agent.get('comparison_case') != 'agent_original'):
        raise ValueError('Agent pair identity mismatch')
    # Reuse the reviewed pure validation helper, adapting only its expected
    # namespace on a temporary copy. The original record remains unchanged.
    return p.matched_message(case, {**agent, 'execution_kind': 'OFFLINE_SIMULATION'}, run_id)


def single_record(case, scenario, transport, run_id, kind, meta, agent, turn_fn):
    provided = pair = None
    if scenario == 'single_original':
        message = case['question']
    elif scenario == 'single_full_conditions':
        message = p.pre.full_conditions_message(case)
        if message is None: return None
        provided = {'user_reply': case['two_turn_gold']['turn2']['user_reply']}
    elif scenario == 'single_matched_conditions':
        message, provided = matched_input(case, agent, run_id, kind)
        if message is None: return None
        pair = agent['record_id']
    else: raise ValueError('Unknown scenario')
    row = p.row(case, scenario, [turn_fn(transport, 'single', message)], p.new_audit(), run_id,
                pair=pair, provided=provided)
    return as_record(row, kind, meta)


def write_row(stream, row, scenario, run_id, kind):
    if (row['execution_kind'] != kind or row['run_id'] != run_id
        or row['comparison_case'] != scenario or row['is_evaluation_result'] is not False
        or any(row[k] is not None for k in h.SCORE_KEYS)):
        raise ValueError('Raw record boundary violation')
    stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False)+'\n')
    stream.flush()
    os.fsync(stream.fileno())


def collect(cases, output, *, transport_factory, meta, kind, turn_fn=h.live_turn, release=None, completion_check=None):
    """Sequential, append-only-in-new-dir batch. Release permit validation precedes collection I/O.

    `transport_factory(case, mode)` is injected for offline tests. Live release
    binds reviewed local endpoints only through a separate release entry.
    Unexpected infrastructure exceptions abort the run and preserve partial
    results; ordinary V4 HTTP failures are stored as rows, without retries.
    """
    if kind not in (TEST_KIND, LIVE_KIND): raise ValueError('Unknown execution kind')
    if kind == LIVE_KIND:
        # Only the separately validated release entry may issue a run permit.
        from issue48_formal_release import validate_permit
        validate_permit(release, cases, output, meta)
        if turn_fn is not h.live_turn or completion_check is None:
            raise PermissionError('Formal collection requires live transport and completion check')
    elif release is not None or completion_check is not None:
        raise ValueError('No release permit in test namespace')
    # No live transport in test namespace, including accidental factory use.
    if kind == TEST_KIND and turn_fn is h.live_turn:
        raise ValueError('Test namespace requires explicit synthetic turn function')
    cases = [input_projection(c) for c in cases]
    if len({c['qid'] for c in cases}) != len(cases): raise ValueError('Duplicate qid')
    if kind == LIVE_KIND and [c['qid'] for c in cases] != [f'F{i:03d}' for i in range(1,31)]:
        raise ValueError('Formal frozen case count/order mismatch')
    out = Path(output).resolve()
    repo = Path(__file__).resolve().parents[2]
    if kind == LIVE_KIND and repo in (out, *out.parents): raise ValueError('Output must be outside repo')
    out.mkdir(parents=False, exist_ok=False)
    run_id = ('formal-http-' if kind == LIVE_KIND else 'offline-collector-test-') + uuid4().hex
    start = {**meta, 'version': VERSION, 'execution_kind': kind, 'run_id': run_id,
             'is_evaluation_result': False, 'dataset_sha256': p.pre.FORMAL_SHA256,
             'mapping_version': p.pre.MAPPING_VERSION,
             'source_sha256': {Path(f).name: h.digest(f) for f in (__file__, h.__file__, p.__file__, p.pre.__file__, p.v4.__file__)},
             'status': 'STARTED'}
    h.write_json(out/'STARTED.json', start)
    counts, errors, skips, agents = Counter(), Counter(), [], {}
    seen_sessions = set()
    try:
        for scenario in p.SCENARIOS:
            with (out/(scenario+'.jsonl')).open('x', encoding='utf-8', newline='\n') as f:
                for case in cases:
                    qid = case['qid']
                    if scenario == 'agent_original':
                        row = agent_record(case, transport_factory(case, 'agent'), run_id, kind, meta, turn_fn)
                        agents[qid] = row
                    else:
                        if scenario == 'single_full_conditions' and qid not in p.pre.SOURCES:
                            row = None
                        elif scenario == 'single_matched_conditions' and not agents[qid]['mapping'].get('submitted_conditions'):
                            row = None
                        else:
                            row = single_record(case, scenario, transport_factory(case, 'single'), run_id, kind, meta, agents[qid], turn_fn)
                    if row is None:
                        skips.append({'qid': qid, 'comparison_case': scenario, 'reason': 'NO_SCRIPTED_REPLY' if scenario == 'single_full_conditions' else p.pair_skip_reason(agents[qid])})
                        continue
                    # Check the actual request IDs, even if a server response is wrong.
                    request_ids = {t['request_body']['session_id'] for t in row['turns']}
                    if len(request_ids) != 1 or seen_sessions.intersection(request_ids):
                        raise ValueError('Session separation violated')
                    seen_sessions.update(request_ids)
                    write_row(f, row, scenario, run_id, kind)
                    counts[scenario] += 1
                    if row['final_error'] or any(t.get('session_detail_error') for t in row['turns']): errors[scenario] += 1
        completion = completion_check() if completion_check is not None else None
        manifest = {**start, 'status': 'COLLECTION_COMPLETE',
                    'completion_check': completion,
                    'scenario_counts': {s: counts[s] for s in p.SCENARIOS},
                    'technical_error_counts': {s: errors[s] for s in p.SCENARIOS},
                    'skipped': skips, 'output_sha256': {s+'.jsonl': h.digest(out/(s+'.jsonl')) for s in p.SCENARIOS}}
        if sum(counts.values()) + len(skips) != 4*len(cases): raise ValueError('Coverage mismatch')
        h.write_json(out/'manifest.json', manifest)
        return manifest
    except BaseException as exc:
        h.write_json(out/'ABORTED.json', {'run_id': run_id, 'status': 'ABORTED', 'error_type': type(exc).__name__, 'error': str(exc), 'counts': dict(counts)})
        raise


def main(argv=None):
    ap = argparse.ArgumentParser(description='V5.3 review candidate; formal live release locked')
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument('--plan', action='store_true')
    group.add_argument('--run-formal', action='store_true')
    ap.add_argument('--questions', type=Path)
    args = ap.parse_args(argv)
    if args.run_formal: ap.error('Formal execution DISABLED: joint approval, release and environment binding required')
    if args.questions is None: ap.error('--plan requires --questions')
    cases = [input_projection(c) for c in p.pre.read_formal(args.questions)]
    print('V5.3 FORMAL COLLECTOR PLAN / NO HTTP')
    print('Dataset SHA256:', p.pre.FORMAL_SHA256)
    print('Mapping version:', p.pre.MAPPING_VERSION)
    print('agent_original:',len(cases))
    print('single_original:',len(cases))
    print('single_full_conditions:',sum(p.pre.full_conditions_message(c) is not None for c in cases))
    print('single_matched_conditions: deferred until accepted second-turn conditions')
    print('Formal execution: DISABLED. No output written.')
    return 0

if __name__ == '__main__': raise SystemExit(main())

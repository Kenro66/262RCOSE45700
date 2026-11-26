#!/usr/bin/env python3
"""V5 formal evaluation integration, OFFLINE REVIEW DRAFT ONLY.

Reuse V4 HTTP protocol handling with an in-process simulated transport.
No live CLI, no server URL option, no output-file writes. Never use simulated
answers as evaluation scores. The V4 frozen sources are not modified.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4
import eval_http_pipeline as v4
import issue48_formal_preflight as pre

VERSION = '5.1-formal-offline-draft'
SCENARIOS = ('agent_original','single_original','single_full_conditions','single_matched_conditions')
SIM_URL = 'http://offline.invalid'


def map_formal(case, clarification):
    facts = pre.candidate_facts(case)
    qs = clarification.get('questions') if isinstance(clarification,dict) else None
    mapped, blocked = pre.preview_mapping(case['qid'], facts, qs)
    decisions = []
    if mapped is not None:
        for field, value in mapped.items():
            key = next(k for k in (field,) + pre.ALIASES.get(field,()) if k in facts)
            decisions.append({'field':field,'fact_key':key,'value':value,'source':facts[key]['source']})
    return mapped, {'status':'MAPPED' if mapped is not None else 'ADAPTER_UNMAPPED',
                    'source':'formal_reviewed_input_candidates',
                    'requested_fields':[q.get('field') if isinstance(q,dict) else None for q in qs] if isinstance(qs,list) else [],
                    'mapped_conditions':mapped,'attempted_conditions':None,'submitted_conditions':None,'condition_acceptance':None,
                    'decisions':decisions,'unmapped':blocked,
                    'reason':None if mapped is not None else 'ADAPTER_UNMAPPED'}


def turn(transport, mode, message, previous=None, conditions=None):
    # Mock transport is mandatory. V4 http_json is replaced for every call,
    # including session-detail reads and polling. No live branch exists here.
    if not callable(transport):
        raise ValueError('Offline simulated transport required')
    with patch.object(v4,'http_json',side_effect=transport):
        return v4.run_turn(SIM_URL,mode,message,5,.001,5,previous=previous,conditions=conditions)


def new_audit():
    return {'status':'NOT_REQUIRED','source':None,'requested_fields':[],
            'mapped_conditions':None,'attempted_conditions':None,'submitted_conditions':None,'condition_acceptance':None,
            'decisions':[],'unmapped':[],'reason':None}


def observe(turn):
    raw=turn.get('raw_response')
    actual=v4.extract_answer(raw) or {}
    # Do not infer flags from names, errors or fallback-like text.
    return {k:actual.get(k) for k in ('model_name','fallback_triggered','fallback_model','workflow_fallback_triggered','model_fallback_triggered')}


def row(case, scenario, turns, audit, run_id, *, pair=None, provided=None):
    final=turns[-1]
    return {'schema_version':VERSION,'execution_kind':'OFFLINE_SIMULATION','is_evaluation_result':False,
            'run_id':run_id,'record_id':uuid4().hex,'recorded_at':v4.iso_now(),
            'dataset_file':pre.FORMAL_NAME,'dataset_sha256':pre.FORMAL_SHA256,'mapping_version':pre.MAPPING_VERSION,
            'pipeline_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'preflight_sha256':hashlib.sha256(Path(pre.__file__).read_bytes()).hexdigest(),
            'v4_pipeline_sha256':hashlib.sha256(Path(v4.__file__).read_bytes()).hexdigest(),
            'qid':case['qid'],'category':case.get('category'),'lang':case.get('lang'),
            'question':case['question'],'comparison_case':scenario,
            'rag_mode':'agent' if scenario=='agent_original' else 'single',
            'paired_agent_record_id':pair,'provided_conditions':provided,
            'turns':turns,'turn_count':len(turns),'mapping':audit,
            'total_system_seconds':round(sum(t['latency_seconds'] for t in turns),3),
            'final_status':final['status'],'final_execution_status':final['execution_status'],'final_error':final['error'],
            'retrieval_count':final['retrieval_count'],'clarification_count':final['clarification_count'],
            **observe(final), 'configured_model':None,
            'answer_correct':None,'answer_complete':None,'citation_correct':None,
            'clarification_correct':None,'continuation_correct':None,'clarification_recovered':None,
            'refusal_correct':None,'task_success':None,'notes':None}


def condition_acceptance(first, second, expected):
    """Prove server acceptance, independently of final generation success.

    At reviewed backend 56510d8, reserve validates and persists before HTTP
    202/200. Alternatively a newly persisted exact subset can prove acceptance
    after lost/failed HTTP response. Never infer acceptance from attempted POST.
    """
    def result(accepted, source, reason):
        return {'accepted':accepted,'source':source,'reason':reason}
    request=second.get('request_body') or {}
    sid,qid=first.get('session_id'),first.get('question_id')
    if (not sid or not qid or not expected or request.get('conditions')!=expected
        or request.get('message')!=(first.get('request_body') or {}).get('message')
        or request.get('session_id')!=sid or request.get('question_id')!=qid
        or second.get('session_id')!=sid or second.get('question_id')!=qid
        or str(second.get('error')).startswith('PROTOCOL_')):
        return result(False,None,'REQUEST_OR_PROTOCOL_MISMATCH')
    # Reject explicit client-error POST; do not mistake an existing question
    # for a newly accepted submission. Use only the actual second POST trace.
    post=next((x for x in second.get('http_trace',[]) if x.get('method')=='POST'),None)
    if post:
        code=post.get('status_code'); body=post.get('body')
        if type(code) is int and 400<=code<500:
            return result(False,None,'SECOND_POST_REJECTED')
        if isinstance(body,dict):
            parts=[body]+([body['response']] if isinstance(body.get('response'),dict) else [])
            if any(x.get('session_id') not in (None,sid) or x.get('question_id') not in (None,qid) for x in parts):
                return result(False,None,'POST_ID_MISMATCH')
            if any(x.get('session_id')==sid and x.get('question_id')==qid and
                   ((code==202 and x.get('execution_status') in ('RUNNING','CANCELLING')) or
                    (code==200 and x.get('execution_status')=='SUCCEEDED')) for x in parts):
                return result(True,'POST_ACK','MATCHED_IDS_AND_ACCEPTED_EXECUTION')
    # Fallback proof: a fresh state change in the matching persisted question.
    # If before-state lacks an explicit conditions map, do not assume it empty.
    def state(turn):
        raw=turn.get('session_detail_response')
        if turn.get('session_detail_error') or not isinstance(raw,dict) or raw.get('session_id')!=sid:
            return None
        qs=raw.get('questions')
        if not isinstance(qs,list): return None
        matches=[q for q in qs if isinstance(q,dict) and q.get('question_id')==qid and q.get('message')==request['message']]
        if len(matches)!=1 or not isinstance(matches[0].get('conditions'),dict): return None
        return matches[0]['conditions']
    before,after=state(first),state(second)
    if (before is not None and after is not None and
        all(after.get(k)==v for k,v in expected.items()) and
        any(before.get(k)!=v for k,v in expected.items())):
        return result(True,'PERSISTED_CONDITIONS','MATCHED_QUESTION_AND_NEW_EXACT_CONDITIONS')
    return result(False,None,'ACCEPTANCE_NOT_CONFIRMED')


def agent_case(case, transport, run_id):
    first=turn(transport,'agent',case['question']); turns=[first]; audit=new_audit()
    if first['status']=='NEEDS_CLARIFICATION':
        if first['error'] or first.get('session_detail_error') or first['clarification_count']!=1:
            audit.update(status='ADAPTER_BLOCKED',reason='FIRST_TURN_OR_SESSION_NOT_VERIFIED')
        else:
            mapped,audit=map_formal(case,first['clarification'])
            if mapped is not None:
                audit['attempted_conditions']=dict(mapped)
                second=turn(transport,'agent',case['question'],previous=first,conditions=mapped)
                turns.append(second)
                acceptance=condition_acceptance(first,second,mapped)
                audit['condition_acceptance']=acceptance
                if acceptance['accepted']:
                    audit['submitted_conditions']=dict(mapped)
                if second['error'] or second.get('session_detail_error'):
                    audit.update(status='SECOND_TURN_ERROR',reason=second['error'] or second['session_detail_error'])
                elif not acceptance['accepted']:
                    audit.update(status='SECOND_TURN_ACCEPTANCE_UNVERIFIED',reason=acceptance['reason'])
                elif second['status']=='NEEDS_CLARIFICATION':
                    audit.update(status='CLARIFICATION_LIMIT_REACHED',reason='NO_THIRD_TURN')
                else:
                    audit['status']='SECOND_TURN_RETURNED'
    return row(case,'agent_original',turns,audit,run_id)


def matched_message(case, agent, run_id):
    if (agent.get('execution_kind')!='OFFLINE_SIMULATION' or agent.get('run_id')!=run_id
        or agent.get('qid')!=case['qid'] or agent.get('question')!=case['question']
        or agent.get('dataset_sha256')!=pre.FORMAL_SHA256 or agent.get('mapping_version')!=pre.MAPPING_VERSION or agent.get('comparison_case')!='agent_original'):
        raise ValueError('Mismatched Agent pair')
    subset=agent['mapping'].get('submitted_conditions')
    if not subset:
        return None,None
    ts=agent['turns']
    if len(ts)!=2 or ts[0].get('status')!='NEEDS_CLARIFICATION':
        raise ValueError('Unverified actual Agent subset')
    # Revalidate against actual first-round fields and reviewed inputs.
    expected,_=map_formal(case,ts[0]['clarification'])
    acceptance=condition_acceptance(ts[0],ts[1],subset)
    if expected!=subset or not acceptance['accepted'] or acceptance!=agent['mapping'].get('condition_acceptance'):
        raise ValueError('Agent subset differs from reviewed mapping or server acceptance evidence')
    prefix='补充信息（字段及值）：' if case.get('lang')=='zh' else '추가 정보 (필드 및 값): '
    return case['question']+'\n\n'+prefix+json.dumps(subset,ensure_ascii=False,sort_keys=True),dict(subset)


def pair_skip_reason(agent):
    if agent['mapping'].get('submitted_conditions'): return None
    audit=agent['mapping']; acceptance=audit.get('condition_acceptance')
    return (acceptance['reason'] if acceptance else audit.get('reason') or 'NO_SECOND_TURN_SUBMISSION')


def single_case(case, scenario, transport, run_id, agent=None):
    provided=pair=None
    if scenario=='single_original':
        message=case['question']
    elif scenario=='single_full_conditions':
        message=pre.full_conditions_message(case)
        if message is None: return None
        provided={'user_reply':case['two_turn_gold']['turn2']['user_reply']}
    elif scenario=='single_matched_conditions':
        if agent is None: return None
        message,provided=matched_message(case,agent,run_id)
        if message is None: return None
        pair=agent['record_id']
    else: raise ValueError('Unknown Single scenario')
    return row(case,scenario,[turn(transport,'single',message)],new_audit(),run_id,pair=pair,provided=provided)


def main():
    parser=argparse.ArgumentParser(description='V5 formal offline input plan; real execution disabled')
    parser.add_argument('--questions',type=Path,required=True)
    parser.add_argument('--check-only',action='store_true',required=True)
    args=parser.parse_args()
    cases=pre.read_formal(args.questions)
    for c in cases: pre.candidate_facts(c)
    print('V5 FORMAL OFFLINE PLAN / NO HTTP')
    print('Dataset:',pre.FORMAL_SHA256)
    print('agent_original:',len(cases))
    print('single_original:',len(cases))
    print('single_full_conditions:',sum(pre.full_conditions_message(c) is not None for c in cases))
    print('single_matched_conditions: deferred until verified Agent second-turn submissions')
    print('Formal live execution: DISABLED. No output files created.')
    return 0

if __name__=='__main__': raise SystemExit(main())

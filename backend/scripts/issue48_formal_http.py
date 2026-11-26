#!/usr/bin/env python3
"""V5.2 HTTP transport and non-formal smoke only. Formal batch remains disabled.

Reuse reviewed V4 polling and V5.1 mapping/acceptance helpers without changing
those sources. All requests use an explicit loopback-only transport; no redirect,
no proxy, no retry of POST. Run sequentially (V4 dependency is scoped patched).
"""
from __future__ import annotations
import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler
from unittest.mock import patch
from uuid import uuid4
import issue48_formal_pipeline as p

VERSION = '5.2-http-smoke-review-20261126'
BASES = {'agent':'http://127.0.0.1:8000','single':'http://127.0.0.1:8001'}
SMOKE_QID = 'SMOKE_HTTP_01'
SMOKE_QUESTION = '联调测试：我乘国际航班回国，携带一瓶自己使用的香水，是否需要申报？'
SMOKE_REPLY = '我从韩国入境中国内地，25岁，是居民旅客，不是乘务员，不是经常出入境人员；香水一瓶30毫升、人民币300元，自用。'
SMOKE_FACTS = {'country':'CN','direction':'INBOUND','departure_region':'KR','age':'25','resident_status':'RESIDENT_TRAVELER','frequent_traveler':'false','crew':'false','personal_use':'true'}
SCORE_KEYS = ('answer_correct','answer_complete','citation_correct','clarification_correct','continuation_correct','clarification_recovered','refusal_correct','task_success','notes')
CHAT_PATHS = {'/api/chat','/api/chat/sessions','/api/chat/sessions/{session_id}', '/api/chat/sessions/{session_id}/questions/{question_id}/execution','/api/chat/sessions/{session_id}/questions/{question_id}/cancel'}

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

class LocalHTTP:
    def __init__(self, base):
        u=urlsplit(base)
        if u.scheme!='http' or u.hostname!='127.0.0.1' or not u.port or u.path or u.query or u.fragment or u.username or u.password:
            raise ValueError('Only explicit loopback HTTP address allowed')
        self.base=base
        self.opener=build_opener(ProxyHandler({}),NoRedirect())
    def __call__(self, method, url, payload, timeout):
        if not url.startswith(self.base+'/'):
            raise ValueError('Cross-service request blocked')
        suffix=url[len(self.base):]
        # Restrict methods and paths, no upload/delete/cancel.
        if method=='POST':
            if suffix!='/api/chat?wait=false' or not isinstance(payload,dict) or set(payload) not in ({'session_id','message'},{'session_id','message','question_id','conditions'}):
                raise ValueError('POST request boundary violation')
            if not isinstance(payload.get('session_id'),str) or not isinstance(payload.get('message'),str):
                raise ValueError('Invalid chat request')
            if 'conditions' in payload and (not isinstance(payload['conditions'],dict) or not payload['conditions'] or any(not isinstance(v,str) for v in payload['conditions'].values())):
                raise ValueError('Conditions must be nonempty string values')
        elif method=='GET':
            if payload is not None or not (suffix in ('/health','/openapi.json','/api/documents') or suffix.startswith('/api/chat/sessions/')):
                raise ValueError('GET request boundary violation')
        else: raise ValueError('Method blocked')
        body=json.dumps(payload,ensure_ascii=False).encode('utf-8') if payload is not None else None
        req=Request(url,data=body,method=method,headers={'Accept':'application/json','Content-Type':'application/json; charset=utf-8'})
        try:
            with self.opener.open(req,timeout=timeout) as r:
                code=r.status; raw=r.read()
        except HTTPError as e:
            code=e.code; raw=e.read()
        except (URLError,TimeoutError,OSError) as e:
            raise RuntimeError(f'HTTP_TRANSPORT_ERROR: {type(e).__name__}: {e}') from e
        try: data=json.loads(raw.decode('utf-8'))
        except (UnicodeError,ValueError): data={'non_json_body':raw.decode('utf-8',errors='replace')[:20000]}
        return code,data


def digest(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def write_json(path,value):
    with Path(path).open('x',encoding='utf-8',newline='\n') as f:
        json.dump(value,f,ensure_ascii=False,indent=2,allow_nan=False); f.write('\n')

def precheck(transports):
    result={}
    for mode,t in transports.items():
        info={}
        for path in ('/health','/openapi.json','/api/documents'):
            code,value=t('GET',BASES[mode]+path,None,10)
            if code!=200: raise ValueError(f'{mode} {path}: HTTP {code}')
            info[path]=value
        health=info['/health']; spec=info['/openapi.json']; docs=info['/api/documents']
        if health.get('status')!='ok' or health.get('database',{}).get('vector_extension') is not True:
            raise ValueError(f'{mode}: database not healthy')
        paths={k for k in spec['paths'] if k.startswith('/api/chat')}
        if paths!=(CHAT_PATHS if mode=='agent' else {'/api/chat'}): raise ValueError(f'{mode}: API mode mismatch: {sorted(paths)}')
        if not isinstance(docs,list) or not docs or any(d.get('status')!='READY' for d in docs):
            raise ValueError(f'{mode}: expected nonempty READY-only local corpus')
        result[mode]={'base_url':BASES[mode],'health':health,'chat_paths':sorted(paths),'documents':docs,
                      'ready_count':len(docs),'page_count':sum(d['page_count'] for d in docs)}
    canon=lambda a:sorted(a,key=lambda d:d['document_id'])
    if canon(result['agent']['documents'])!=canon(result['single']['documents']): raise ValueError('Document records differ')
    result['document_snapshot_sha256']=hashlib.sha256(json.dumps(canon(result['agent']['documents']),sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    return result


def live_turn(transport,mode,message,previous=None,conditions=None,*,request_timeout=150,poll_interval=0.5,poll_timeout=180):
    # The dependency substitution is a real, explicit HTTP transport, not a fixture.
    # V4 remains byte-for-byte unchanged. This runner is strictly sequential.
    with patch.object(p.v4,'http_json',side_effect=transport):
        return p.v4.run_turn(BASES[mode],mode,message,request_timeout,poll_interval,poll_timeout,previous=previous,conditions=conditions)


def smoke_mapping(clarification):
    # Only the fixed non-formal smoke user's facts; no formal case or gold data.
    facts={k:{'value':v,'source':'smoke_user_reply','source_text':SMOKE_REPLY} for k,v in SMOKE_FACTS.items()}
    qs=clarification.get('questions') if isinstance(clarification,dict) else None
    mapped,blocked=p.pre.preview_mapping(SMOKE_QID,facts,qs)
    return mapped,{'status':'MAPPED' if mapped is not None else 'ADAPTER_UNMAPPED','source':'smoke_user_reply','mapped_conditions':mapped,'attempted_conditions':None,'submitted_conditions':None,'condition_acceptance':None,'unmapped':blocked,'reason':None if mapped is not None else 'ADAPTER_UNMAPPED'}


def agent_smoke(transport,run_id,turn_fn=live_turn):
    first=turn_fn(transport,'agent',SMOKE_QUESTION); turns=[first]; audit=p.new_audit()
    if first['status']=='NEEDS_CLARIFICATION':
        if first['error'] or first.get('session_detail_error') or first['clarification_count']!=1:
            audit.update(status='ADAPTER_BLOCKED',reason='FIRST_TURN_OR_SESSION_NOT_VERIFIED')
        else:
            mapped,audit=smoke_mapping(first['clarification'])
            if mapped is not None:
                audit['attempted_conditions']=dict(mapped)
                second=turn_fn(transport,'agent',SMOKE_QUESTION,previous=first,conditions=mapped);turns.append(second)
                acceptance=p.condition_acceptance(first,second,mapped);audit['condition_acceptance']=acceptance
                if acceptance['accepted']:audit['submitted_conditions']=dict(mapped)
                if second['error'] or second.get('session_detail_error'):audit.update(status='SECOND_TURN_ERROR',reason=second['error'] or second['session_detail_error'])
                elif not acceptance['accepted']:audit.update(status='SECOND_TURN_ACCEPTANCE_UNVERIFIED',reason=acceptance['reason'])
                elif second['status']=='NEEDS_CLARIFICATION':audit.update(status='CLARIFICATION_LIMIT_REACHED',reason='NO_THIRD_TURN')
                else:audit['status']='SECOND_TURN_RETURNED'
    return record('agent_original',turns,audit,run_id)


def record(scenario,turns,audit,run_id,*,pair=None,provided=None):
    last=turns[-1]
    # Real smoke has its own namespace. It is never a formal result or a fixture.
    return {'schema_version':VERSION,'execution_kind':'HTTP_SMOKE','is_evaluation_result':False,'run_id':run_id,'record_id':uuid4().hex,'recorded_at':p.v4.iso_now(),'qid':SMOKE_QID,'question':SMOKE_QUESTION,'comparison_case':scenario,'rag_mode':'agent' if scenario=='agent_original' else 'single','mapping_version':p.pre.MAPPING_VERSION,'paired_agent_record_id':pair,'provided_conditions':provided,'turns':turns,'turn_count':len(turns),'mapping':audit,'total_system_seconds':round(sum(t['latency_seconds'] for t in turns),3),'final_status':last['status'],'final_execution_status':last['execution_status'],'final_error':last['error'],'retrieval_count':last['retrieval_count'],'clarification_count':last['clarification_count'],**p.observe(last),'model_observations_by_turn':[p.observe(t) for t in turns],**dict.fromkeys(SCORE_KEYS)}


def single_smoke(scenario,transport,run_id,agent,turn_fn=live_turn):
    pair=provided=None
    if scenario=='single_original':message=SMOKE_QUESTION
    elif scenario=='single_full_conditions':message=SMOKE_QUESTION+'\n\n补充信息：'+SMOKE_REPLY;provided={'user_reply':SMOKE_REPLY}
    elif scenario=='single_matched_conditions':
        if agent.get('execution_kind')!='HTTP_SMOKE' or agent.get('run_id')!=run_id or agent.get('qid')!=SMOKE_QID or agent.get('question')!=SMOKE_QUESTION or agent.get('comparison_case')!='agent_original':raise ValueError('Pair identity mismatch')
        provided=agent['mapping'].get('submitted_conditions')
        if not provided:return None
        ts=agent['turns']
        if len(ts)!=2:raise ValueError('Invalid two-turn pair')
        expected,_=smoke_mapping(ts[0]['clarification']);proof=p.condition_acceptance(ts[0],ts[1],provided)
        if expected!=provided or not proof['accepted'] or proof!=agent['mapping'].get('condition_acceptance'):raise ValueError('Pair evidence mismatch')
        message=SMOKE_QUESTION+'\n\n补充信息（字段及值）：'+json.dumps(provided,ensure_ascii=False,sort_keys=True);pair=agent['record_id']
    else:raise ValueError('Unknown smoke scenario')
    return record(scenario,[turn_fn(transport,'single',message)],p.new_audit(),run_id,pair=pair,provided=provided)


def run_smoke(transports,output,env,repo_root,turn_fn=live_turn):
    output=Path(output).resolve();output.mkdir(parents=False,exist_ok=False)
    run_id='http-smoke-'+uuid4().hex
    sha=p.v4.get_git_sha(repo_root)
    meta={'version':VERSION,'execution_kind':'HTTP_SMOKE','is_evaluation_result':False,'run_id':run_id,'code_commit':sha,'source_sha256':{Path(x).name:digest(x) for x in (__file__,p.__file__,p.pre.__file__,p.v4.__file__)},'environment':env,'formal_execution':'DISABLED','configured_model_observation':{'source':'user terminal report, not process attestation','single':'gpt-6-sol','agent':'gpt-6-sol','temperature':None,'max_tokens':None}}
    write_json(output/'STARTED.json',meta)
    counts={};skips=[];agents=None
    for scenario in p.SCENARIOS:
        if scenario=='agent_original':row=agents=agent_smoke(transports['agent'],run_id,turn_fn)
        else:row=single_smoke(scenario,transports['single'],run_id,agents,turn_fn)
        if row is None:
            skips.append({'comparison_case':scenario,'reason':p.pair_skip_reason(agents)});continue
        with (output/(scenario+'.jsonl')).open('x',encoding='utf-8',newline='\n') as f:
            f.write(json.dumps(row,ensure_ascii=False,allow_nan=False)+'\n'); f.flush()
        counts[scenario]=1
        print(f'{scenario}: status={row["final_status"]}, error={row["final_error"]}, mapping={row["mapping"]["status"]}')
    # COMPLETE describes collection only; a recorded technical error is not success.
    write_json(output/'manifest.json',{**meta,'status':'COLLECTION_COMPLETE','counts':counts,'skipped':skips,'output_sha256':{x.name:digest(x) for x in output.glob('*.jsonl')}})
    return counts,skips


def main(argv=None):
    ap=argparse.ArgumentParser(description='V5.2 local HTTP preflight / non-formal smoke; formal 30 disabled')
    modes=ap.add_mutually_exclusive_group(required=True);modes.add_argument('--check-only',action='store_true');modes.add_argument('--smoke',action='store_true')
    ap.add_argument('--output-dir',type=Path);ap.add_argument('--confirm-smoke',action='store_true')
    args=ap.parse_args(argv)
    if args.smoke and (not args.confirm_smoke or args.output_dir is None):ap.error('smoke needs --confirm-smoke and a new --output-dir')
    if args.smoke and args.output_dir.exists():ap.error('output directory already exists')
    if args.check_only and (args.output_dir is not None or args.confirm_smoke):ap.error('check-only has no write or POST options')
    transports={m:LocalHTTP(b) for m,b in BASES.items()};env=precheck(transports)
    print('V5.2 HTTP PREFLIGHT: PASS')
    for mode in BASES: print(f'{mode}: {BASES[mode]}, READY={env[mode]["ready_count"]}, pages={env[mode]["page_count"]}')
    print('Document snapshot SHA256:',env['document_snapshot_sha256'])
    print('Formal 30 execution: DISABLED')
    if args.check_only:print('GET only. No POST, no model invocation.');return 0
    run_smoke(transports,args.output_dir,env,Path(__file__).resolve().parents[2]);print('Smoke collection complete; not an evaluation result.');return 0

if __name__=='__main__':raise SystemExit(main())

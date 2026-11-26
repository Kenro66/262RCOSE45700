"""V5.5 controlled formal entry. No authorization is bundled.
Approval is a human-controlled record, not a cryptographic signature. Never
create an approval on behalf of a reviewer or use smoke configuration as formal.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit
import re
import subprocess
from dataclasses import dataclass
import issue48_formal_collect as c
import issue48_formal_http as h
import issue48_formal_score as score

VERSION = '5.5-release-review-20261126'
# Approval policy checks a tracked, clean source tree and the frozen dataset.
# Environment evidence must be supplied by the actual service operator, reviewed
# by the team; the public HTTP API does not expose effective process settings.
REQUIRED_CONFIRMATIONS = ('formal_corpus_and_index_verified','running_process_config_verified',
    'same_model_parameters_and_index','independent_sessions','F018_mapping_confirmed',
    'scoring_rubric_confirmed','fallback_observation_limit_confirmed')
ENV_FIELDS = ('generation_profile','generation_model','generation_endpoint','embedding_model',
              'embedding_endpoint','max_tokens','temperature','retrieval_top_k','reranker',
              'evidence_completion','agent_json_schema','fallback','execution_timeout_seconds',
              'generation_timeout_seconds','index_fingerprint','index_fingerprint_method')
PERMIT_SEAL = object()
REPO = Path(__file__).resolve().parents[2]


def git(repo, *args):
    return subprocess.check_output(['git','-C',str(repo),*args],text=True,encoding='utf-8').strip()


def source_state(repo):
    commit=git(repo,'rev-parse','HEAD')
    if git(repo,'status','--porcelain','--untracked-files=no'):
        raise ValueError('Tracked worktree is not clean')
    # Old untracked result files are harmless; untracked source/config files are not.
    untracked=git(repo,'ls-files','--others','--exclude-standard','--','backend','eval')
    if any(not x.startswith('eval/results/') for x in untracked.splitlines() if x):
        raise ValueError('Untracked backend/eval files outside historical results')
    files=sorted((repo/'backend/scripts').glob('*issue48*.py'))
    files += [repo/'backend/scripts'/n for n in ('eval_http_pipeline.py','condition_adapter.py','clarification-facts.json','manifest.json')]
    result={}
    for f in files:
        rel=f.relative_to(repo).as_posix()
        git(repo,'ls-files','--error-unmatch','--',rel)
        result[rel]=h.digest(f)
    return commit,result


def valid_sha(x,n=64): return isinstance(x,str) and re.fullmatch(r'[0-9a-f]{'+str(n)+r'}',x) is not None


def json_file(path): return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def validate_approval(a, *, commit, hashes, evidence_path, output, now):
    if not isinstance(a,dict): raise ValueError('Approval object required')
    if a.get('status')!='APPROVED_FORMAL_30' or a.get('release_version')!=VERSION:
        raise PermissionError('Explicit formal 30 approval missing')
    if not valid_sha(commit,40) or a.get('code_commit')!=commit or a.get('source_sha256')!=hashes:
        raise ValueError('Approved commit/source hashes mismatch')
    if (a.get('dataset_sha256')!=c.p.pre.FORMAL_SHA256 or a.get('mapping_version')!=c.p.pre.MAPPING_VERSION
        or a.get('rubric_version')!=score.VERSION or a.get('endpoints')!=h.BASES):
        raise ValueError('Approved dataset/mapping/rubric/endpoints mismatch')
    if set(a)!={'status','release_version','code_commit','source_sha256','dataset_sha256','mapping_version','rubric_version','endpoints','approval_references','approved_by','confirmations','valid_from','valid_until','document_snapshot_sha256','environment_evidence_sha256','output_dir'}: raise ValueError('Unexpected/missing approval fields')
    refs=a.get('approval_references')
    if not isinstance(refs,list) or not refs or any(not isinstance(x,str) or not re.fullmatch(r'https://github\.com/Kenro66/262RCOSE45700/(?:issues|pull)/\d+#[A-Za-z0-9_-]+',x) for x in refs):
        raise ValueError('Recorded project approval reference required')
    if not isinstance(a.get('approved_by'),list) or not a['approved_by'] or any(not isinstance(x,str) or not x.strip() for x in a['approved_by']):
        raise ValueError('Reviewer names required')
    for k in REQUIRED_CONFIRMATIONS:
        if a.get('confirmations',{}).get(k) is not True: raise ValueError('Missing confirmation: '+k)
    start=datetime.fromisoformat(a['valid_from']);end=datetime.fromisoformat(a['valid_until'])
    if start.tzinfo is None or end.tzinfo is None or not start<=now<=end: raise ValueError('Approval time window invalid/expired')
    if not valid_sha(a.get('document_snapshot_sha256')) or not valid_sha(a.get('environment_evidence_sha256')) or h.digest(evidence_path)!=a['environment_evidence_sha256']:
        raise ValueError('Approved environment evidence hash mismatch')
    if Path(a['output_dir']).resolve()!=Path(output).resolve(): raise ValueError('Approved output path mismatch')
    e=json_file(evidence_path)
    if e.get('code_commit')!=commit or e.get('endpoints')!=h.BASES or e.get('document_snapshot_sha256')!=a['document_snapshot_sha256']:
        raise ValueError('Environment evidence identity mismatch')
    if not isinstance(e.get('operator'),str) or not e['operator'].strip(): raise ValueError('Environment operator required')
    if set(e)!={'code_commit','endpoints','document_snapshot_sha256','operator','common_config','corpus','evidence_notes'}: raise ValueError('Unexpected/missing environment fields')
    cfg=e.get('common_config',{})
    if set(cfg)!=set(ENV_FIELDS): raise ValueError('Unexpected/missing common config fields')
    for k in ENV_FIELDS:
        v=cfg.get(k)
        if v is None or (isinstance(v,str) and (not v.strip() or v.upper() in ('UNSET','UNKNOWN','TBD'))): raise ValueError('Unverified environment field: '+k)
    for k in ('generation_endpoint','embedding_endpoint'):
        u=urlsplit(cfg[k])
        if u.scheme not in ('http','https') or not u.hostname or u.username or u.password or u.query or u.fragment: raise ValueError('Use credential-free endpoint URL: '+k)
    if type(cfg['max_tokens']) is not int or cfg['max_tokens']<=0 or type(cfg['temperature']) not in (int,float) or not 0<=cfg['temperature']<=2: raise ValueError('Explicit generation limits required')
    for k in ('retrieval_top_k','execution_timeout_seconds','generation_timeout_seconds'):
        if type(cfg[k]) not in (int,float) or cfg[k]<=0: raise ValueError('Explicit positive '+k+' required')
    if not valid_sha(cfg['index_fingerprint']): raise ValueError('Expected index SHA256')
    corpus=e.get('corpus')
    if not isinstance(corpus,list) or not corpus or any(not isinstance(d,dict) or set(d)!={'document_id','filename','pdf_sha256','page_count'} or not valid_sha(d['pdf_sha256']) or type(d['page_count']) is not int or d['page_count']<=0 for d in corpus): raise ValueError('Reviewed corpus PDF hashes and page counts required')
    if len({d['document_id'] for d in corpus})!=len(corpus): raise ValueError('Duplicate corpus document ID')
    if not isinstance(e.get('evidence_notes'),str) or not e['evidence_notes'].strip(): raise ValueError('Environment evidence notes required')
    return e


@dataclass(frozen=True)
class Permit:
    seal: object
    cases_sha: str
    output: Path
    meta_sha: str


def object_digest(obj):
    return hashlib.sha256(json.dumps(obj,ensure_ascii=False,sort_keys=True,allow_nan=False).encode('utf-8')).hexdigest()


def validate_permit(permit,cases,output,meta):
    if not isinstance(permit,Permit) or permit.seal is not PERMIT_SEAL:
        raise PermissionError('Formal execution not authorized: validated release required')
    if (permit.cases_sha!=object_digest(cases) or permit.output!=Path(output).resolve()
        or permit.meta_sha!=object_digest(meta)): raise PermissionError('Release binding mismatch')


def prepare(repo,questions,output,approval_path,evidence_path,*,now=None):
    # All checks before any POST or output creation. No network until local
    # approval, source, dataset and output binding are checked.
    repo=Path(repo).resolve();out=Path(output).resolve()
    if repo in (out,*out.parents) or out.exists() or not out.parent.is_dir(): raise ValueError('Output must be new, outside repo, with existing parent')
    questions=Path(questions).resolve()
    if questions!=repo/'eval'/c.p.pre.FORMAL_NAME: raise ValueError('Use tracked frozen dataset in repository')
    git(repo,'ls-files','--error-unmatch','--',questions.relative_to(repo).as_posix())
    cases=c.p.pre.read_formal(questions)
    commit,hashes=source_state(repo)
    approval_hash=h.digest(approval_path); evidence_hash=h.digest(evidence_path)
    a=json_file(approval_path)
    e=validate_approval(a,commit=commit,hashes=hashes,evidence_path=evidence_path,output=out,now=now or datetime.now(timezone.utc))
    transports={m:h.LocalHTTP(b) for m,b in h.BASES.items()}
    snap=h.precheck(transports)
    if snap['document_snapshot_sha256']!=a['document_snapshot_sha256']: raise ValueError('Live document snapshot differs from approved snapshot')
    key=lambda d: (d['document_id'],d['filename'],d['page_count'])
    if sorted(map(key,e['corpus']))!=sorted(map(key,snap['agent']['documents'])): raise ValueError('Live corpus differs from approved inventory')
    if h.digest(approval_path)!=approval_hash or h.digest(evidence_path)!=evidence_hash: raise ValueError('Approval/evidence changed during preflight')
    # Recheck code after preflight. No independent proof of live embedding bytes
    # or effective process config via these endpoints; operator evidence remains mandatory.
    if source_state(repo)!=(commit,hashes): raise ValueError('Source changed during preflight')
    meta={'release_version':VERSION,'code_commit':commit,'approved_source_sha256':hashes,
          'approval_sha256':approval_hash,'approval':a,'environment_evidence':e,
          'environment_evidence_sha256':evidence_hash,'document_snapshot_sha256':snap['document_snapshot_sha256'],
          'http_preflight':snap,'configured_models':dict.fromkeys(h.BASES,e['common_config']['generation_model']),
          'rubric_version':score.VERSION,'http_timeouts':{'request_seconds':150,'poll_interval_seconds':0.5,'poll_timeout_seconds':180},'max_chat_posts':81,'environment_verification_limit':'HTTP mode and document records rechecked; effective process config and vector content require operator evidence and human approval.'}
    return cases,out,meta,transports,Permit(PERMIT_SEAL,object_digest(cases),out,object_digest(meta))


def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__);g=ap.add_mutually_exclusive_group(required=True)
    g.add_argument('--check-release',action='store_true');g.add_argument('--run-formal',action='store_true')
    ap.add_argument('--questions',type=Path,required=True);ap.add_argument('--approval',type=Path,required=True)
    ap.add_argument('--environment-evidence',type=Path,required=True);ap.add_argument('--output-dir',type=Path,required=True)
    ap.add_argument('--confirm-formal',action='store_true')
    args=ap.parse_args(argv)
    if args.run_formal!=args.confirm_formal: ap.error('Run requires --confirm-formal; check must not confirm')
    cases,out,meta,ts,permit=prepare(REPO,args.questions,args.output_dir,args.approval,args.environment_evidence)
    print('V5.5 RELEASE CHECK: PASS (human approval record + local checks; no independent authentication)')
    if args.check_release:
        print('GET only; no POST, no output written.');return 0
    def completion_check():
        final=h.precheck(ts)
        if final['document_snapshot_sha256']!=meta['document_snapshot_sha256'] or source_state(REPO)!=(meta['code_commit'],meta['approved_source_sha256']):
            raise ValueError('Source or document snapshot changed during formal collection')
        return {'document_snapshot_sha256':final['document_snapshot_sha256'],'checked_at':datetime.now(timezone.utc).isoformat()}
    result=c.collect(cases,out,transport_factory=lambda case,mode:ts[mode],meta=meta,kind=c.LIVE_KIND,release=permit,completion_check=completion_check)
    print('FORMAL RAW COLLECTION COMPLETE; HUMAN SCORING REQUIRED:',result['scenario_counts'])
    return 0

if __name__=='__main__':
    # Import by canonical module name so Permit identity is shared with collector.
    from issue48_formal_release import main as entry
    raise SystemExit(entry())

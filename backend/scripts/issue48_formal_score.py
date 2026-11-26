"""V5.4 review-only human scoring, independent of HTTP collection.
No automatic semantic judgement; no smoke/fixture scoring via CLI.
"""
from __future__ import annotations
import argparse
import json
from collections import Counter
from pathlib import Path
import issue48_formal_collect as c

VERSION = '5.4-scoring-review-20261126'
FIELDS = ('answer_correct','answer_complete','citation_correct','clarification_correct',
          'continuation_correct','clarification_recovered','refusal_correct','behavior_reasonable')
S = c.p.SCENARIOS

def gold_for(case):
    if 'two_turn_gold' in case:
        g = case['two_turn_gold']
        if g['turn1']['expected_status'] != 'NEEDS_CLARIFICATION' or g['turn2']['expected_status'] != 'ANSWERED':
            raise ValueError('Unexpected two-turn gold')
        return {'turn1':g['turn1'], 'turn2':g['turn2'], 'final_expected':'ANSWERED'}
    if case['expected_status'] not in ('ANSWERED','INSUFFICIENT_EVIDENCE'):
        raise ValueError('Unexpected single-turn gold')
    return {'final_expected':case['expected_status'],'gold_answer':case['gold_answer'],
            'gold_evidence':case['gold_evidence'],'refusal_requirements':case.get('expected_refusal_requirements'),
            'refusal_subtype':case.get('refusal_subtype'),'partial_context_not_gold':case.get('partial_context_not_gold')}

def read_jsonl(path):
    return [json.loads(l) for l in Path(path).read_text(encoding='utf-8-sig').splitlines() if l.strip()]

def load_raw(folder, cases):
    folder = Path(folder)
    m = json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
    if (m.get('status') != 'COLLECTION_COMPLETE' or m.get('execution_kind') != c.LIVE_KIND
        or m.get('is_evaluation_result') is not False or m.get('dataset_sha256') != c.p.pre.FORMAL_SHA256
        or m.get('mapping_version') != c.p.pre.MAPPING_VERSION or (folder/'ABORTED.json').exists()):
        raise ValueError('Only complete formal raw collection may be scored')
    if len(cases)!=30 or len({x['qid'] for x in cases})!=30: raise ValueError('Expected 30 frozen questions')
    by = {x['qid']:x for x in cases}; rows=[]; agents={}; ids=set(); sessions=set(); coverage=set()
    if set(m['output_sha256']) != {s+'.jsonl' for s in S}: raise ValueError('Missing/extra output files')
    for s in S:
        f=folder/(s+'.jsonl')
        if c.h.digest(f)!=m['output_sha256'][f.name]: raise ValueError('Raw hash mismatch')
        part=read_jsonl(f)
        if len(part)!=m['scenario_counts'][s]: raise ValueError('Count mismatch')
        for r in part:
            q=r['qid']; key=(s,q)
            if (q not in by or key in coverage or r['record_id'] in ids or r['comparison_case']!=s
                or r['question']!=by[q]['question'] or r['run_id']!=m['run_id']
                or r['dataset_sha256']!=m['dataset_sha256'] or r['mapping_version']!=m['mapping_version']
                or r['execution_kind']!=c.LIVE_KIND or r['is_evaluation_result'] is not False
                or any(r[k] is not None for k in c.h.SCORE_KEYS)):
                raise ValueError('Raw identity or score boundary mismatch')
            turns=r['turns']; request_ids={t['request_body']['session_id'] for t in turns}
            if len(request_ids)!=1 or sessions.intersection(request_ids) or not 1<=len(turns)<=(2 if s==S[0] else 1):
                raise ValueError('Session/turn mismatch')
            msg=turns[0]['request_body']['message']; case=by[q]
            if s==S[0]: agents[q]=r; expected=case['question']
            elif s==S[1]: expected=case['question']
            elif s==S[2]: expected=c.p.pre.full_conditions_message(case)
            else:
                expected, facts=c.matched_input(case,agents[q],m['run_id'],c.LIVE_KIND)
                if r['paired_agent_record_id']!=agents[q]['record_id'] or facts!=r['provided_conditions']:
                    raise ValueError('Pair mismatch')
            if expected is None or msg!=expected: raise ValueError('Input mismatch')
            coverage.add(key); ids.add(r['record_id']); sessions.update(request_ids); rows.append(r)
    expected={(s,q) for s in S[:2] for q in by}|{(S[2],q) for q in c.p.pre.SOURCES}|{(S[3],q) for q,a in agents.items() if c.matched_input(by[q],a,m['run_id'],c.LIVE_KIND)[0] is not None}
    if coverage!=expected: raise ValueError('Missing required coverage')
    skips=[(x['comparison_case'],x['qid']) for x in m['skipped']]
    if len(skips)!=len(set(skips)) or set(skips)!={(s,q) for s in S for q in by}-coverage or any(not x.get('reason') for x in m['skipped']):
        raise ValueError('Skip coverage mismatch')
    return m,rows

def identity(row):
    return {k:row[k] for k in ('run_id','record_id','qid','comparison_case')}

def technical(row):
    return bool(row.get('final_error') or any(t.get('error') or t.get('session_detail_error') for t in row['turns']))

def blocked(row):
    return row.get('mapping',{}).get('status') in ('ADAPTER_UNMAPPED','ADAPTER_BLOCKED','SECOND_TURN_ACCEPTANCE_UNVERIFIED')

def required_fields(row, case):
    s=row['comparison_case']; multi='two_turn_gold' in case
    fields=[]
    if gold_for(case)['final_expected']=='ANSWERED': fields+=['answer_correct','answer_complete']
    else: fields+=['refusal_correct']
    if row['turns'][-1].get('citations'): fields+=['citation_correct']
    if s==S[0] and (multi or row['turns'][0].get('status')=='NEEDS_CLARIFICATION'):
        fields+=['clarification_correct']
    if s==S[0] and multi: fields+=['clarification_recovered']
    if s==S[0] and len(row['turns'])==2: fields+=['continuation_correct']
    if s in (S[1],S[3]) and multi: fields+=['behavior_reasonable']
    return fields

def template(row, case):
    return {**identity(row),'rubric_version':VERSION,'gold':gold_for(case),
            'required_judgements':required_fields(row,case), 'technical_failure':technical(row),
            'adapter_blocked':blocked(row),'judgements':dict.fromkeys(FIELDS), 'reviewer':None,'notes':None}

def evaluate(row, case, review):
    # Human scores only; strict booleans and nonempty evidence notes/reviewer.
    t=template(row,case)
    if any(review.get(k)!=t[k] for k in (*identity(row),'rubric_version','gold','required_judgements','technical_failure','adapter_blocked')):
        raise ValueError('Review identity/rubric mismatch')
    for k in ('reviewer','notes'):
        if not isinstance(review.get(k),str) or not review[k].strip(): raise ValueError('Reviewer and evidence notes required')
    j=review['judgements']
    if set(j)!=set(FIELDS) or any(type(j[k]) is not bool if k in t['required_judgements'] else j[k] is not None for k in FIELDS):
        raise ValueError('Required judgements must be bool; N/A must be null')
    failure=t['technical_failure'] or t['adapter_blocked']; expected=t['gold']['final_expected']
    final_ok=(row['final_status']==expected and (j['answer_correct'] if expected=='ANSWERED' else j['refusal_correct']) is True and not failure)
    flow=None
    if row['comparison_case']==S[0] and 'two_turn_gold' in case:
        flow=bool(final_ok and len(row['turns'])==2 and row['turns'][0]['status']=='NEEDS_CLARIFICATION'
                  and j['clarification_correct'] and j['continuation_correct'] and j['clarification_recovered']
                  and row['mapping'].get('submitted_conditions') and row['mapping'].get('condition_acceptance',{}).get('accepted'))
    return {**identity(row),'rubric_version':VERSION,'reviewer':review['reviewer'],'notes':review['notes'],
            **j,'task_success':final_ok,'two_turn_flow_pass':flow,
            'behavior_pass':None if j['behavior_reasonable'] is None else bool(j['behavior_reasonable'] and not failure),
            'technical_failure':t['technical_failure'],'adapter_blocked':t['adapter_blocked'],
            'final_expected':expected,'refusal_subtype':case.get('refusal_subtype'),
            'two_turn_case':'two_turn_gold' in case,
            'citation_missing':row['final_status']=='ANSWERED' and not row['turns'][-1].get('citations')}

def metric(rows,k):
    vals=[r[k] for r in rows if r[k] is not None]; n=len(vals); p=sum(v is True for v in vals)
    return {'pass':p,'fail':n-p,'denominator':n,'na':len(rows)-n,'rate':p/n if n else None}

def summarize(rows):
    def group(rs):
        return {'records':len(rs),'technical_failures':sum(r['technical_failure'] for r in rs),
                'adapter_blocked':sum(r['adapter_blocked'] for r in rs),'citation_missing':sum(r['citation_missing'] for r in rs),
                **{k:metric(rs,k) for k in ('task_success','two_turn_flow_pass','behavior_pass',*FIELDS)}}
    out={s:group([r for r in rows if r['comparison_case']==s]) for s in S}
    for s in S:
        rs=[r for r in rows if r['comparison_case']==s]
        out[s]['refusal_subtypes']={sub:metric([r for r in rs if r['refusal_subtype']==sub],'task_success') for sub in sorted({r['refusal_subtype'] for r in rs if r['refusal_subtype']})}
    for s in S[2:]:
        ids={r['qid'] for r in rows if r['comparison_case']==s}
        out[s]['same_qid_comparison']={mode:group([r for r in rows if r['comparison_case']==mode and r['qid'] in ids]) for mode in (S[0],S[1],s)}
    return out

def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__); g=ap.add_mutually_exclusive_group(required=True)
    g.add_argument('--plan',action='store_true');g.add_argument('--prepare',action='store_true');g.add_argument('--summarize',action='store_true')
    ap.add_argument('--questions',type=Path,required=True);ap.add_argument('--raw-dir',type=Path);ap.add_argument('--reviews',type=Path);ap.add_argument('--output',type=Path)
    a=ap.parse_args(argv);cases=c.p.pre.read_formal(a.questions);by={r['qid']:r for r in cases}
    if a.plan:
        if any((a.raw_dir,a.reviews,a.output)):ap.error('plan is read-only')
        print('V5.4 SCORING PLAN / NO HTTP / NO SCORES')
        print('Final gold:',dict(Counter(gold_for(x)['final_expected'] for x in cases)))
        print('Two-turn gold:',sum('two_turn_gold' in x for x in cases))
        print('Formal execution: DISABLED. Human semantic review required.');return 0
    if not a.raw_dir or not a.output:ap.error('raw-dir and output required')
    if a.raw_dir.resolve() in (a.output.resolve(),*a.output.resolve().parents):ap.error('Output must be outside raw directory')
    m,rows=load_raw(a.raw_dir,cases)
    if a.prepare:
        if a.reviews:ap.error('prepare does not accept reviews')
        with a.output.open('x',encoding='utf-8') as f:
            for r in rows:f.write(json.dumps(template(r,by[r['qid']]),ensure_ascii=False)+'\n')
    else:
        if not a.reviews:ap.error('reviews required')
        reviews=read_jsonl(a.reviews); indexed={r['record_id']:r for r in reviews}
        if len(indexed)!=len(reviews) or set(indexed)!={r['record_id'] for r in rows}:raise ValueError('Review coverage mismatch')
        scored=[evaluate(r,by[r['qid']],indexed[r['record_id']]) for r in rows]
        c.h.write_json(a.output,{'rubric_version':VERSION,'run_id':m['run_id'],'raw_manifest_sha256':c.h.digest(a.raw_dir/'manifest.json'),'reviews_sha256':c.h.digest(a.reviews),'status':'HUMAN_REVIEW_CANDIDATE','scored_records':scored,'scenario_metrics':summarize(scored),'skipped':m['skipped']})
    return 0
if __name__=='__main__':raise SystemExit(main())

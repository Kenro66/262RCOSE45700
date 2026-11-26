#!/usr/bin/env python3
"""Issue #48 V5 preparation: offline-only formal input audit (review draft).

No HTTP client, no result file writes, no gold-answer/evidence access.
The approved V4 runner is not imported or modified. Candidate rules here are
NOT approved runtime mappings; activate only after joint interface review.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import re
from pathlib import Path
import unittest

FORMAL_NAME = 'issue44_formal28_review.jsonl'
# Raw bytes as verified on the user's Windows checkout, not a Git object SHA.
FORMAL_SHA256 = '20e6a04ff8777ef8539068e935c54a1ef10881e23f38b9c7e76ba91861657712'
EXPECTED_CATEGORIES = {'普通问答': 12, '条件追问': 7, '国家/方向区分': 5, '证据不足拒答': 6}
# Explicit source review: which fixed-condition keys are expressed in the original
# question, and which in the scripted user reply. No field is sourced from the
# descriptive conditions string, answer key, evidence, or expected status.
SOURCES = {
    'F013': (('item','cigarette_sticks'), ('country','direction','departure_region','age','traveler_role','frequent_traveler','crew','personal_use')),
    'F014': ((), ('country','direction','channel_system','has_items_requiring_declaration','understands_channel_rules')),
    'F015': (('country','direction','age','frequent_traveler','crew','goods','value_cny','purchased_abroad','retained_in_cn','personal_use','split_transport'), ('resident_status',)),
    'F016': (('country','direction','item'), ('customs_verified_export','same_item_reimported_by_owner')),
    'F017': (('country','direction','frequent_business_trips'), ('profession','is_professional_crew')),
    'F018': (('country','direction'), ('departure_region','age','frequent_traveler','crew','item','personal_use','frequency_uses_today','transport_worker')),
    'F023': (('country','travel_type'), ('authorized_to_leave_customs_control_area',)),
}
# Candidate aliases, not a general inference mechanism. No cross-field inference
# (e.g. frequent_traveler -> traveler_status, age -> adult) or translation guessing.
ALIASES = {'destination_country': ('country',), 'traveler_status': ('resident_status','traveler_role'), 'item_name': ('item',)}
# Generic declaration field may have a wider meaning than written declaration.
HOLDS = {('F014','has_items_requiring_declaration'): 'Confirm written-declaration semantics with API owner.'}
# 2026-11-25: confirmed policy-country semantics, not travel destination.
MAPPING_VERSION = '5.1-review-20261125'
FIELD_HOLDS = {}
FORBIDDEN_FIELDS = {'country', 'jurisdiction', 'travel_direction'}
CORE_OPTIONS = {'destination_country': {'CN','KR'}, 'direction': {'INBOUND','OUTBOUND'}}
# Generic aliases for role/item are previewed only if a select option matches
# the reviewed code exactly. Free-text semantics require further review.
SELECT_ONLY_ALIASES = {'traveler_status', 'item_name'}
FIELD_PATTERN = re.compile(r'^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$')


def full_conditions_message(row):
    """Candidate S2 input: original question plus verbatim scripted reply.

    Never serializes fixed_conditions, gold answers, evidence or scoring metadata.
    Use the frozen read_formal() result as input. No HTTP execution here.
    """
    if row['qid'] not in SOURCES:
        return None
    candidate_facts(row)  # check reviewed structure before rendering
    reply = row['two_turn_gold']['turn2']['user_reply']
    return row['question'] + '\n\n' + ('补充信息：' if row.get('lang') == 'zh' else '추가 정보: ') + reply



def scalar_text(value):
    if type(value) is bool:
        return 'true' if value else 'false'
    if type(value) is int:
        return str(value)
    if isinstance(value, str) and value.strip():
        return value
    raise ValueError('Unsupported or empty candidate fact')


def candidate_facts(row):
    qid = row['qid']
    if qid not in SOURCES:
        return {}
    turn2 = row['two_turn_gold']['turn2']
    fixed = turn2['fixed_conditions']
    question, reply = row['question'], turn2['user_reply']
    if not isinstance(question, str) or not question.strip() or not isinstance(reply, str) or not reply.strip():
        raise ValueError(f'{qid}: missing source text')
    qkeys, rkeys = SOURCES[qid]
    if not isinstance(fixed, dict) or set(fixed) != set(qkeys + rkeys):
        raise ValueError(f'{qid}: fixed_conditions keys changed; manual review required')
    return {key: {'value': scalar_text(fixed[key]),
                  'source': 'question' if key in qkeys else 'turn2.user_reply',
                  'source_text': question if key in qkeys else reply}
            for key in qkeys + rkeys}


def preview_mapping(qid, facts, questions):
    """Pure offline exact-option mapping prototype; all-or-nothing; never sends.

    Only the actual requested field names would be used. Unapproved synonyms,
    ambiguous options, held semantics, or unsupported fields are blocked.
    """
    if not isinstance(questions, list) or not questions:
        return None, [{'reason': 'MISSING_CLARIFICATION_QUESTIONS','status':'ADAPTER_UNMAPPED'}]
    values, blocked, seen = {}, [], set()
    for q in questions:
        field = q.get('field') if isinstance(q, dict) else None
        kind = q.get('type') if isinstance(q, dict) else None
        options = q.get('options', []) if isinstance(q, dict) else None
        if (not isinstance(field, str) or FIELD_PATTERN.fullmatch(field) is None or field in FORBIDDEN_FIELDS or field in seen or kind not in ('select','text')
                or (kind == 'select' and (not isinstance(options, list) or not options or any(not isinstance(x,str) for x in options)))
                or (kind == 'text' and options not in (None, []))):
            blocked.append({'field':field,'reason':'INVALID_OR_DUPLICATE_FIELD','status':'ADAPTER_UNMAPPED'})
            continue
        seen.add(field)
        if (qid,field) in FIELD_HOLDS:
            blocked.append({'field':field,'reason':FIELD_HOLDS[qid,field],'status':'ADAPTER_UNMAPPED'})
            continue
        if kind == 'select' and field in CORE_OPTIONS and (len(options) != len(CORE_OPTIONS[field]) or set(options) != CORE_OPTIONS[field]):
            blocked.append({'field':field,'reason':'INVALID_CANONICAL_OPTIONS','status':'ADAPTER_UNMAPPED'})
            continue
        if field in SELECT_ONLY_ALIASES and kind != 'select':
            blocked.append({'field':field,'reason':'ALIAS_TEXT_SEMANTICS_UNCONFIRMED','status':'ADAPTER_UNMAPPED'})
            continue
        keys = [k for k in (field,)+ALIASES.get(field, ()) if k in facts]
        if len(keys) != 1:
            blocked.append({'field':field,'reason':'NO_UNIQUE_REVIEWED_FACT','status':'ADAPTER_UNMAPPED'})
            continue
        key = keys[0]
        if (qid,key) in HOLDS:
            blocked.append({'field':field,'reason':HOLDS[qid,key],'status':'ADAPTER_UNMAPPED'})
            continue
        value = facts[key]['value']
        if field in CORE_OPTIONS and value not in CORE_OPTIONS[field]:
            blocked.append({'field':field,'reason':'INVALID_CANONICAL_VALUE','status':'ADAPTER_UNMAPPED'})
            continue
        if kind == 'select' and options.count(value) != 1:
            blocked.append({'field':field,'reason':'NO_UNIQUE_EXACT_OPTION','status':'ADAPTER_UNMAPPED'})
            continue
        values[field] = value
    return (None if blocked else values), blocked


def read_formal(path):
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if path.name != FORMAL_NAME or digest != FORMAL_SHA256:
        raise ValueError('Formal filename/raw SHA256 mismatch; do not normalize or rewrite frozen file.')
    rows = [json.loads(line) for line in raw.decode('utf-8-sig').splitlines() if line.strip()]
    if [r.get('qid') for r in rows] != [f'F{i:03d}' for i in range(1,31)]:
        raise ValueError('Formal qid order/count mismatch')
    if Counter(r.get('category') for r in rows) != EXPECTED_CATEGORIES:
        raise ValueError('Formal category counts mismatch')
    if {r['qid'] for r in rows if isinstance(r.get('two_turn_gold'),dict)} != set(SOURCES):
        raise ValueError('Formal scripted-reply set mismatch')
    return rows


class OfflineTests(unittest.TestCase):
    def test_scalar(self):
        self.assertEqual([scalar_text(x) for x in (False,True,0,25,'KR')],['false','true','0','25','KR'])
        for x in (None,[],{},1.5,''):
            with self.assertRaises(ValueError): scalar_text(x)
    def test_candidate_provenance(self):
        row={'qid':'F023','question':'transit in China','two_turn_gold':{'turn2':{'user_reply':'yes, authorized','fixed_conditions':{'country':'CN','travel_type':'TRANSIT','authorized_to_leave_customs_control_area':True},'gold_answer':'NEVER_EXPORT'}}}
        facts=candidate_facts(row)
        self.assertEqual(facts['country']['source'],'question')
        self.assertEqual(facts['authorized_to_leave_customs_control_area']['source'],'turn2.user_reply')
        self.assertNotIn('NEVER_EXPORT',json.dumps(facts))
        row['two_turn_gold']['turn2']['fixed_conditions']['unreviewed']=True
        with self.assertRaises(ValueError): candidate_facts(row)
    def test_exact_and_alias(self):
        facts={'country':{'value':'CN'},'age':{'value':'25'}}
        self.assertEqual(preview_mapping('F013',facts,[{'field':'destination_country','type':'select','options':['CN','KR']},{'field':'age','type':'text'}]),({'destination_country':'CN','age':'25'},[]))
    def test_block_partial(self):
        result,blocked=preview_mapping('F013',{'country':{'value':'CN'}},[{'field':'destination_country','type':'select','options':['CN','KR']},{'field':'age','type':'text'}])
        self.assertIsNone(result); self.assertEqual(len(blocked),1)
    def test_no_guess_or_duplicate(self):
        for opts in (['韩国','中国'],['CN','CN'],['KR']):
            self.assertIsNone(preview_mapping('F013',{'country':{'value':'CN'}},[{'field':'destination_country','type':'select','options':opts}])[0])
        q={'field':'age','type':'text'}
        self.assertIsNone(preview_mapping('F013',{'age':{'value':'25'}},[q,q])[0])
    def test_semantic_hold(self):
        self.assertIsNone(preview_mapping('F014',{'has_items_requiring_declaration':{'value':'false'}},[{'field':'has_items_requiring_declaration','type':'text'}])[0])
    def test_no_facts_non_scripted(self):
        self.assertEqual(candidate_facts({'qid':'F029','conditions':'DO NOT SEND'}),{})
    def test_corrected_provenance(self):
        for qid, original_keys in [('F013', {'item','cigarette_sticks'}), ('F018', {'country','direction'})]:
            qkeys,rkeys=SOURCES[qid]
            self.assertEqual(set(qkeys), original_keys)
            row={'qid':qid,'question':'original','two_turn_gold':{'turn2':{'user_reply':'reply','fixed_conditions':{k:'test' for k in qkeys+rkeys}}}}
            facts=candidate_facts(row)
            self.assertEqual({k for k,v in facts.items() if v['source']=='question'},original_keys)
    def test_forbidden_fields(self):
        for field in ('country','jurisdiction','travel_direction','Bad-Field'):
            result, blocked=preview_mapping('F013',{field:{'value':'CN'}},[{'field':field,'type':'text'}])
            self.assertIsNone(result); self.assertEqual(blocked[0]['status'],'ADAPTER_UNMAPPED')
    def test_canonical_options(self):
        facts={'country':{'value':'CN'}}
        for opts in (['CN'],['CN','KR','JP'],['CN','KR','KR']):
            self.assertIsNone(preview_mapping('F013',facts,[{'field':'destination_country','type':'select','options':opts}])[0])
    def test_transit_policy_country(self):
        facts={'country':{'value':'CN'},'travel_type':{'value':'TRANSIT'}}
        self.assertEqual(preview_mapping('F023',facts,[{'field':'destination_country','type':'select','options':['CN','KR']}])[0],{'destination_country':'CN'})
        self.assertIsNone(preview_mapping('F023',facts,[{'field':'direction','type':'text'}])[0])
    def test_alias_requires_exact_select(self):
        facts={'resident_status':{'value':'RESIDENT_TRAVELER'}}
        self.assertIsNone(preview_mapping('F015',facts,[{'field':'traveler_status','type':'text'}])[0])
        self.assertEqual(preview_mapping('F015',facts,[{'field':'traveler_status','type':'select','options':['RESIDENT_TRAVELER','NON_RESIDENT_TRAVELER']}])[0],{'traveler_status':'RESIDENT_TRAVELER'})
    def test_full_input_no_gold(self):
        row={'qid':'F023','lang':'zh','question':'原始问题','conditions':'DO NOT SEND','gold_answer':'DO NOT SEND','two_turn_gold':{'turn1':{'required_fields':['DO NOT SEND']},'turn2':{'user_reply':'批准离开监管区','fixed_conditions':{'country':'CN','travel_type':'TRANSIT','authorized_to_leave_customs_control_area':True},'gold_answer':'DO NOT SEND'}}}
        self.assertEqual(full_conditions_message(row),'原始问题\n\n补充信息：批准离开监管区')
        self.assertIsNone(full_conditions_message({'qid':'F029'}))
    def test_ambiguous_role(self):
        facts={'resident_status':{'value':'RESIDENT_TRAVELER'},'traveler_role':{'value':'GENERAL_TRAVELER'}}
        self.assertIsNone(preview_mapping('F015',facts,[{'field':'traveler_status','type':'select','options':['RESIDENT_TRAVELER','GENERAL_TRAVELER']}])[0])


def main():
    p=argparse.ArgumentParser(description='V5 preparation ONLY: offline formal input audit; no HTTP.')
    g=p.add_mutually_exclusive_group(required=True)
    g.add_argument('--questions',type=Path)
    g.add_argument('--self-test',action='store_true')
    args=p.parse_args()
    if args.self_test:
        return 0 if unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(OfflineTests)).wasSuccessful() else 1
    try:
        rows=read_formal(args.questions)
        print('FORMAL PREFLIGHT v5.1: REVIEW DRAFT / NO HTTP')
        print('Formal SHA256:',FORMAL_SHA256)
        print('Questions:',len(rows))
        for row in rows:
            facts=candidate_facts(row)
            if facts:
                print(row['qid'],json.dumps({k:{'value':v['value'],'source':v['source'],'hold':HOLDS.get((row['qid'],k))} for k,v in facts.items()},ensure_ascii=False))
        candidates = [full_conditions_message(row) for row in rows]
        print('Scripted user replies:', sum(x is not None for x in candidates), '/ 7; other 23 cases: no additional facts proposed in this draft')
        print('Single S2 candidate inputs:',sum(x is not None for x in candidates))
        print('Semantic holds:',json.dumps({f'{q}:{k}':v for (q,k),v in {**HOLDS,**FIELD_HOLDS}.items()},ensure_ascii=False))
        print('Formal live execution: DISABLED. V4 unchanged. No output files created.')
    except (OSError,ValueError,KeyError,TypeError) as e:
        p.error(str(e))
    return 0

if __name__=='__main__':
    raise SystemExit(main())

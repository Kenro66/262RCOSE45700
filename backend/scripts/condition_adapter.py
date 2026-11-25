"""Exact r02 evaluation adapter extracted from frozen run.py; no Agent logic."""

ALIASES={'country':['destination_country'],'jurisdiction':['destination_country'],'traveler_role':['traveler_status'],'resident_status':['traveler_status'],'is_frequent_traveler':['frequent_traveler'],'is_frequent_border_crosser':['frequent_traveler'],'adult':['is_adult'],'origin_region':['departure_region'],'departure_country':['departure_region'],'item':['item_name']}

TRANSLATIONS={'CN':['中国','中国内地','중국'],'KR':['韩国','한국'],'JP':['日本','일본'],'TH':['泰国','태국'],'SG':['新加坡','싱가포르'],'VN':['越南','베트남'],'INBOUND':['入境','进境','입국'],'OUTBOUND':['出境','출국'],'GENERAL_TRAVELER':['一般旅客','普通旅客','일반 여행자'],'RESIDENT_TRAVELER':['居民旅客','거주자'],'NON_RESIDENT_TRAVELER':['非居民旅客','비거주자'],'FREQUENT_TRAVELER':['经常出入境人员'],'HK_OR_MO':['香港或澳门','港澳地区','香港、澳门'],'NON_HK_MO':['非港澳地区','其他地区'],'MO':['澳门'],'true':['是','有','是的','true','예','네'],'false':['否','没有','不是','false','아니요','아니오']}

def adapt(qs,facts,provenance):
 values={};decisions=[];unmapped=[]
 for q in qs:
  field=q['field'];keys=[field]+ALIASES.get(field,[]);key=next((k for k in keys if k in facts),None)
  if key is None:unmapped.append({'question':q,'reason':'No explicit input/conditions fact for this field; never infer exact age or policy answer.'});continue
  v=facts[key];candidates=[v]+TRANSLATIONS.get(v,[])
  if q.get('type')=='select':
   matched=[x for x in q.get('options',[]) if x in candidates]
   if len(matched)!=1:unmapped.append({'question':q,'fact_key':key,'fact_value':v,'reason':'No unique original option consistent with explicit fact; no forced CN/KR or arbitrary region choice.'});continue
   v=matched[0]
  values[field]=v;decisions.append({'field':field,'value':v,'source_fact':key,'source_text':provenance[key]})
 return values,decisions,unmapped

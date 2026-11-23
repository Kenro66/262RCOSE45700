"""Issue #48 comment 6966737991: new fields and read-only legacy access.

Only explicit country/border relations are read from questions. Persisted values
are never rewritten; ambiguous travel plans are left to the existing decision.
"""

import re
from typing import Any

from .state import ClarificationQuestion

CORE_FIELDS = ("destination_country", "direction", "item_name", "quantity", "age", "traveler_status")
NEW_FIELD_PATTERN = r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$"
# Reject these *new* synonyms. country/jurisdiction are ambiguous in old states.
CORE_ALIASES = {"travel_direction", "country", "jurisdiction"}
CORE_OPTIONS = {"destination_country": {"CN", "KR"}, "direction": {"INBOUND", "OUTBOUND"}}


class ConditionConflict(ValueError):
    def __init__(self, field: str):
        self.field = field
        super().__init__(f"Conflicting canonical and legacy condition: {field}")


def has_value(value: Any) -> bool:
    return bool(value.strip()) if isinstance(value, str) else value not in (None, [])


def _value(field: str, value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if field == "direction":
        return {"INBOUND": "INBOUND", "OUTBOUND": "OUTBOUND", "입국": "INBOUND", "출국": "OUTBOUND",
                "入境": "INBOUND", "出境": "OUTBOUND"}.get(text.upper(), value)
    if field == "destination_country":
        return {"CN": "CN", "KR": "KR", "中国": "CN", "韩国": "KR", "중국": "CN", "한국": "KR"}.get(text.upper(), value)
    return value


def condition_view(raw: dict[str, Any]) -> dict[str, Any]:
    """Read clear legacy direction semantics; never infer country from nationality."""
    view = {key: _value(key, value) for key, value in raw.items()}
    legacy = raw.get("travel_direction")
    if has_value(legacy):
        legacy = _value("direction", legacy)
        if legacy not in ("INBOUND", "OUTBOUND"):
            return view  # e.g. "return home" has no unambiguous policy direction.
        if has_value(view.get("direction")) and view["direction"] != legacy:
            raise ConditionConflict("direction")
        view["direction"] = legacy
    return view


def _direction_without_country(question: str, directions: set[str]) -> dict[str, str]:
    """Read an affirmative carrying action, not a route or a shop's name.

    Called only after the shared ambiguity guards and with no full scope pair.
    This deliberately returns no country, including when nationality is stated.
    """
    if len(directions) != 1 or not re.search(r"海关|通关|申报|免税|限额|限量|额度|规定|세관|통관|신고|면세|한도|규정", question):
        return {}
    if re.search(r"(?:去|到|往|向|赴).{0,12}(?:携带|带着)|(?:으로|로).{0,20}(?:휴대|가지고)", question):
        return {}  # A leg toward a destination does not identify the policy direction.
    targets = re.findall(r"([\u4e00-\u9fff]{2,8})(?:海关|政策)|([가-힣]{2,8})\s*(?:세관|정책)", question)
    if len({a or b for a, b in targets}) > 1:
        return {}
    for match in re.finditer(r"(?:携带|带着)([^，。！？；;\n]{1,24}?)(?:入境|出境)(?!免税店|商店)", question):
        if not re.search(r"去|到|往|向|赴|飞|转机|中转|离开|返回", match[1]):
            return {"direction": next(iter(directions))}
    if re.search(r"(?:入境|出境)(?:时|前|后|期间)?\s*(?:携带|带着)|"
                 r"(?:휴대(?:하여|하고|해서|한\s*채)|가지고)\s*(?:입국|출국)(?!장)", question):
        return {"direction": next(iter(directions))}
    return {}


def explicit_question_scope(question: str) -> dict[str, str]:
    """Conservative bilingual relations, not a country-keyword detector.

    No inference for negation, alternatives, quotes, or more than one scope.
    Unsupported grammar intentionally produces no facts rather than a guess.
    """
    if re.search(r"不是|并非|不会|没有|不确定|可能|也许|或者|还是|或|不要|请勿|禁止|不得|并未|无需|无须|不必|不.{0,12}[入出]境|"
                 r"(?:无需|无须|不必|禁止|不得|并未).{0,12}[入出]境|未[入出]境|"
                 r"(?:안|못)\s*(?:입국|출국)|(?:입국|출국)(?:은|는|을|를|이|하[지려]?)?\s*(?:안|못)\s*(?:하|해|했|할|합|되|돼)|"
                 r"않|아니|말고|미정|모르|아마|또는|혹은|없|금지|(?:입국|출국)하지\s*마|[\"'“”「」]", question):
        return {}
    if re.search(r"出境\s*(?:去|到|前往|赴|往)|출국.*(?:으로|로)\s*(?:가|이동|향)", question):
        return {}  # A departure leg toward another country is not its policy scope.
    if re.search(r"比较|相比|区别|分别|不同|비교|차이|"
                 r"(?:和|与|及)\s*[\u4e00-\u9fff]{2,8}(?:海关|政策|规定)|"
                 r"(?:와|과|및)\s*[가-힣]{2,8}\s*(?:세관|정책|통관)", question):
        # A shared explicit border-time policy can establish direction without
        # selecting either comparison country as the destination.
        directions = {"INBOUND" if v in ("入境", "입국") else "OUTBOUND"
                      for v in re.findall(r"入境|出境|입국|출국", question)}
        if len(directions) == 1 and re.search(
                r"(?:入境|出境)时[^。！？；\n]{0,20}(?:免税|通关|申报|限额|额度)|"
                r"(?:입국|출국)\s*시[^.!?\n]{0,20}(?:면세|통관|신고|한도)", question):
            return {"direction": next(iter(directions))}
        return {}  # Never collapse a comparison into one policy country.
    # A single-country pair must not hide another unqualified border direction.
    directions = set(re.findall(r"入境|出境|입국|출국", question))
    semantic_directions = {"INBOUND" if value in ("入境", "입국") else "OUTBOUND" for value in directions}
    if len(semantic_directions) > 1:
        return {}
    pairs = set()
    countries = {"中国": "CN", "韩国": "KR", "중국": "CN", "한국": "KR"}
    for match in re.finditer(r"(中国|韩国)\s*(入境|出境)", question):
        # “从韩国入境中国” identifies China; Korea is only the origin.
        if match[2] == "入境" and re.search(r"(?:从|由|来自|离开|自|经|经由|途经)\s*$", question[:match.start()]):
            continue
        if match[2] == "出境" and re.search(r"(?:到|去|前往|赴|往|向|至)\s*$", question[:match.start()]):
            continue
        pairs.add((countries[match[1]], "INBOUND" if match[2] == "入境" else "OUTBOUND"))
    for match in re.finditer(r"(入境|出境)\s*(中国|韩国)(?=$|[\s，。！？；：、,.!?;:]|时|的|前|后|应|要|能|可|需|有|一般|规定|政策|免税|通关)", question):
        pairs.add((countries[match[2]], "INBOUND" if match[1] == "入境" else "OUTBOUND"))
    # 에/으로/로 mark the arrival country; 에서 marks the departure country.
    for pattern, direction in ((r"(한국|중국)(?:에|으로|로)\s*입국", "INBOUND"),
                               (r"(한국|중국)에서\s*출국", "OUTBOUND"),
                               (r"(한국|중국)\s+입국", "INBOUND"),
                               (r"(한국|중국)\s+출국(?=\s*(?:규정|정책|통관|면세|시|할\s*때))", "OUTBOUND")):
        for match in re.finditer(pattern, question):
            if match.start() and re.match(r"[가-힣A-Za-z]", question[match.start() - 1]):
                continue
            pairs.add((countries[match[1]], direction))
    if len(pairs) > 1:
        return {}
    if not pairs:
        return _direction_without_country(question, semantic_directions)
    country, direction = pairs.pop()
    # An itinerary's explicit exit must not override a different policy target.
    targets = re.findall(r"(中国|韩国)\s*(?:海关|通关|政策|法规|规定|免税)|"
                         r"(한국|중국)(?:의)?\s*(?:세관|관세|통관|정책|규정|면세)", question)
    if any(countries[a or b] != country for a, b in targets):
        return {}
    return {"destination_country": country, "direction": direction}


def confirmed_conditions(question: str, raw: dict[str, Any]) -> dict[str, Any]:
    """Combine clear user facts in a view, without changing State/HTTP values."""
    view = condition_view(raw)
    for field, value in explicit_question_scope(question).items():
        if has_value(view.get(field)) and view[field] != value:
            raise ConditionConflict(field)
        view[field] = value
    return view


def outside_supported_policy(view: dict[str, Any]) -> bool:
    country = view.get("destination_country")
    return has_value(country) and country not in ("CN", "KR")


def comparison_policy_countries(question: str) -> tuple[str, ...]:
    """Recognize explicit bilateral policy comparison, never a mere journey."""
    if re.search(r"不比较|不要比较|不讨论|不是|并非|不涉及|仅问|只问|只咨询|비교하지|아닌|아니라", question):
        return ()
    if not re.search(r"比较|对比|相比|区别|差异|不同|不一样|是否相同|哪一套|비교|차이|다른|달라|같은|compare|difference", question, re.I):
        return ()
    policy = r"资料|政策|规定|法规|海关|通关|免税|자료|정책|규정|세관|통관|면세"
    if re.search(r"中韩\s*(?:" + policy + r")", question):
        return ("CN", "KR")
    qualifier = r"(?:的|의)?\s*(?:普通旅客|一般旅客|旅客|일반\s*여행자|여행자)?\s*"
    references = set(re.findall(r"(中国|韩国|중국|한국)" + qualifier + r"(?:" + policy + r")", question))
    pair = re.search(r"(中国|韩国|중국|한국)\s*(?:和|与|及|跟|、|以及|와|과|및)\s*"
                     r"(中国|韩国|중국|한국)" + qualifier + r"(?:" + policy + r")", question)
    if pair:
        references.update(pair.groups())
    countries = {"CN" if name in ("中国", "중국") else "KR" for name in references}
    return ("CN", "KR") if countries == {"CN", "KR"} else ()


def retrieval_policy_scope(question: str, raw: dict[str, Any]) -> dict[str, str] | None:
    """Optional narrowing only; never convert an ambiguous itinerary into scope."""
    if comparison_policy_countries(question):
        return None
    try:
        known = confirmed_conditions(question, raw)
    except ConditionConflict:
        return None  # The existing condition node owns the safe refusal.
    country = known.get("destination_country")
    if country not in ("CN", "KR"):
        return None
    if re.search(r"比较|相比|区别|差异|分别|两国|中韩|不同|或|还是|不确定|可能|并非|不是|不.{0,12}[入出]境|"
                 r"비교|차이|각각|양국|또는|혹은|아니|않|모르|(?:안|못)\s*(?:입국|출국)|"
                 r"(?:입국|출국)\s*(?:안|못)|compare|difference|both|[\"“”「」]", question, re.I):
        return None
    if re.search(r"(中国|韩国|중국|한국|\bCN\b|\bKR\b)\s*(?:资料|规定|政策|法规|海关|자료|규정|정책|세관)?\s*"
                 r"(?:和|与|及|、|跟|/|以及|와|과|및|and)\s*"
                 r"(?:中国|韩国|중국|한국|\bCN\b|\bKR\b)", question, re.I):
        return None
    directions = {"INBOUND" if v in ("入境", "입국") else "OUTBOUND"
                  for v in re.findall(r"入境|出境|입국|출국", question)}
    if len(directions) > 1:
        return None
    # Multiple country names alone may describe an origin/destination journey.
    # Only explicit policy/border relations can disqualify a single-country view.
    targets = re.finditer(r"(中国|韩国|日本|泰国|新加坡|越南)\s*(?:海关|政策|法规|通关|入境|出境)|"
                          r"(?:入境|出境)\s*(中国|韩国|日本|泰国|新加坡|越南)|"
                          r"(중국|한국|일본|태국|싱가포르|베트남)(?:의)?\s*(?:세관|정책|규정|통관)|"
                          r"(중국|한국|일본|태국|싱가포르|베트남)(?:에|으로|로)\s*입국", question)
    mapping = {"中国": "CN", "韩国": "KR", "중국": "CN", "한국": "KR"}
    for match in targets:
        if (match[1] and match[0].endswith("入境")
                and re.search(r"(?:从|由|来自|离开|自|经|经由|途经)\s*$", question[:match.start()])):
            continue  # Origin is not a second policy target.
        if mapping.get(next(value for value in match.groups() if value)) != country:
            return None
    scope = {"destination_country": country}
    if known.get("direction") in ("INBOUND", "OUTBOUND"):
        scope["direction"] = known["direction"]
    return scope


def validate_new_questions(questions: list[ClarificationQuestion]) -> None:
    """Only validate newly generated questions, not restored legacy clarification."""
    seen = set()
    for question in questions:
        field = question.field
        if re.fullmatch(NEW_FIELD_PATTERN, field) is None or field in CORE_ALIASES:
            raise ValueError("New clarification must use snake_case and canonical core fields")
        if field in seen:
            raise ValueError("Duplicate new clarification field")
        seen.add(field)
        if question.type == "text" and question.options:
            raise ValueError("New text clarification must use empty options")
        if question.type == "select" and field in CORE_OPTIONS:
            if len(question.options) != len(CORE_OPTIONS[field]) or set(question.options) != CORE_OPTIONS[field]:
                raise ValueError("New core clarification options must use canonical codes")

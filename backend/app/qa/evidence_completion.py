"""Conservative age-definition and enumerated-continuation completion."""

import logging
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from ..contracts import Chunk, SearchHit

logger = logging.getLogger(__name__)
ChunkLookup = Callable[[list[str]], Awaitable[Sequence[Chunk]]]
AGE = re.compile(r"만\s*(\d+)\s*세\s*(미만|이하|이상|초과)")
PRODUCT = re.compile(r"(?<![가-힣])(술|담배|향수)(?=[\s,ㆍ및은는을를에가와과]|$)")
# Unknown identity/scope qualifications are not interpreted as universal definitions.
QUALIFIED = re.compile(
    r"승무원|쿠리어|내국인|외국인|비거주자|거주자|우범여행자|단체여행자|"
    r"이\s*(?:편|장|절|관|조|항|호|목)(?:에서|에\s*한)|이\s*경우|한정|한하여|한해서|경우에만"
)
ARTICLE = re.compile(r"第[一二三四五六七八九十百零〇0-9]+条")
ENUMERATION = re.compile(r"^[ \t]*[（(]([一二三四五六七八九十0-9]+)[）)]", re.MULTILINE)
ENUM_INTRO = re.compile(r"(?:有下列[^：:。！？]{0,40}之一|如下|包括)[^：:。！？]{0,80}[：:]")
CONDITION = re.compile(r"(超过|超出|低于|少于|多于|不足|不满|未满)([\u3400-\u9fff]{3,12}?)(?=的|[，。？！、；：]|$)")
NEGATED = re.compile(r"(?:不|未|不得|没有|并未|并非|不是|无需|禁止)$")


@dataclass(frozen=True)
class SupplementalEvidence:
    chunk: Chunk
    related_chunk_id: str
    signals: tuple[str, ...]


def _units(text: str) -> list[str]:
    return [re.sub(r"\s+", " ", unit).strip() for unit in
            re.split(r"[①②③④⑤⑥⑦⑧⑨⑩]|^\s*\d+\.\s*", text, flags=re.MULTILINE)
            if unit.strip()]


def _note(unit: str, match: re.Match) -> str | None:
    adjacent = re.match(r"인\s*(?:사람|자)\s*\(", unit[match.end():])
    if adjacent is None:
        return None
    start = match.end() + adjacent.end() - 1
    depth = 0
    for index in range(start, len(unit)):
        depth += (unit[index] == "(") - (unit[index] == ")")
        if depth == 0:
            return unit[start + 1:index]
    return None


def _rules(hits: Sequence[SearchHit]):
    for hit in hits:
        for unit in _units(hit.text):
            # A comma-separated courier rule must not qualify the preceding person rule.
            for clause in re.split(r"[,;]", unit):
                if QUALIFIED.search(clause):
                    continue
                products = set(PRODUCT.findall(clause))
                for match in AGE.finditer(clause):
                    prefix = clause[:match.start()].strip()
                    unqualified = not prefix or re.fullmatch(r"제\d+항제\d+호에도\s*불구하고", prefix)
                    if (unqualified and products and re.match(r"인\s*(?:사람|자)가\s*반입하는", clause[match.end():])
                            and _note(clause, match) is None):
                        yield hit, match.groups(), products


def _age_candidates(initial: Sequence[SearchHit], chunks: Sequence[Chunk]) -> list[SupplementalEvidence]:
    by_id = {chunk.chunk_id: chunk for chunk in chunks}
    if len(by_id) != len(chunks):
        return []
    candidates: dict[str, SupplementalEvidence] = {}
    for hit, term, products in _rules(initial):
        source = by_id.get(hit.chunk_id)
        if (source is None or source.language != "ko" or source.document_id != hit.document_id or source.text != hit.text
                or source.page_number != hit.page_number or QUALIFIED.search(source.section_title or "")):
            continue
        for chunk in chunks:
            if (chunk.language != "ko" or chunk.document_id != hit.document_id or chunk.chunk_index >= source.chunk_index
                    or QUALIFIED.search(chunk.section_title or "")):
                continue
            for unit in _units(chunk.text):
                if QUALIFIED.search(unit):
                    continue
                for match in AGE.finditer(unit):
                    if match.groups() != term:
                        continue
                    prefix = re.split(r"[.。]", unit[:match.start()])[-1].strip()
                    if prefix not in ("", "다만,"):
                        continue
                    note = _note(unit, match)
                    if not note or "제외한다" not in note or "이하 같다" not in note:
                        continue
                    # Product compatibility must occur in the age-bearing provision,
                    # not in another item elsewhere in the same Chunk.
                    if not products.intersection(PRODUCT.findall(unit)):
                        continue
                    if not re.search(r"\)이\s*반입하는", unit[match.end():]):
                        continue
                    candidates[chunk.chunk_id] = SupplementalEvidence(chunk, hit.chunk_id, (
                        "same_age", "adjacent_exception", "forward_same", "preceding", "shared_product",
                        "unqualified_person_provision"))
    return list(candidates.values())


def _ordinal(value: str) -> int | None:
    if value.isascii() and value.isdigit():
        number = int(value)
        return number if 1 <= number <= 99 else None
    digits = {char: index for index, char in enumerate("一二三四五六七八九", 1)}
    if value in digits:
        return digits[value]
    if re.fullmatch(r"[一二三四五六七八九]?十[一二三四五六七八九]?", value):
        tens, ones = value.split("十")
        return digits.get(tens, 1) * 10 + digits.get(ones, 0)
    return None


def _items(text: str) -> list[tuple[int, str]]:
    matches = list(ENUMERATION.finditer(text))
    values = [_ordinal(match[1]) for match in matches]
    if not values or any(value is None for value in values):
        return []
    if values != list(range(values[0], values[0] + len(values))):
        return []
    return [(value, text[match.end():matches[index + 1].start() if index + 1 < len(matches) else len(text)])
            for index, (match, value) in enumerate(zip(matches, values))]


def _open_enumeration(hit: SearchHit) -> bool:
    items = _items(hit.text)
    return bool(ARTICLE.match(hit.text.strip()) and items and items[0][0] == 1
                and ENUM_INTRO.search(hit.text[:ENUMERATION.search(hit.text).start()])
                and hit.text.rstrip().endswith(("；", ";")))


def _conditions(question: str | None) -> list[tuple[str, set[str]]]:
    if not question:
        return []
    result = []
    for match in CONDITION.finditer(question):
        if NEGATED.search(question[:match.start()]):
            continue
        subject = match[2]
        result.append((match[1], {subject[index:index + 2] for index in range(len(subject) - 1)}))
    return result


def _condition_matches(items: list[tuple[int, str]], conditions: list[tuple[str, set[str]]]) -> bool:
    # Lexical condition matching only: an identical comparator and two subject
    # bigrams must occur together in one continuation item. No synonym inference.
    for _, body in items:
        body = re.sub(r"\s+", "", body)
        for operator, grams in conditions:
            for match in re.finditer(re.escape(operator), body):
                if NEGATED.search(body[:match.start()]):
                    continue
                tail = re.split(r"[，,；;。！？]", body[match.end():], maxsplit=1)[0]
                if sum(gram in tail for gram in grams) >= 2:
                    return True
    return False


def _positive_action_forms(action: str) -> tuple[str, ...]:
    """Elide only recognized adjuncts, retaining the complete lexical action.

    A recipient can be separated only when a known manner marks its end.
    Otherwise retain the whole predicate rather than guessing its last verb.
    """
    parts = re.fullmatch(
        r"(?P<recipient>向[\u3400-\u9fff]{1,12}?(?=书面|如实|依法|及时))?"
        r"(?P<manner>(?:(?:书面|如实|依法|及时))*)(?P<verb>[\u3400-\u9fff]{2,12})", action)
    if parts is None:
        return ()
    verb = parts["verb"]
    # These are polarity/modal operators at the parsed predicate boundary,
    # not a keyword search over the question. Unknown modal chains abstain.
    if re.match(r"不|未|无|免|禁止|可以|能够|应当|应该|必须|需要", verb):
        return ()
    recipient, manner = parts["recipient"] or "", parts["manner"]
    return tuple(dict.fromkeys((action, recipient + verb, manner + verb, verb)))


def _enumeration_overview(question: str | None, hit: SearchHit) -> bool:
    """Accept bounded positive list-question forms; abstain on other wording."""
    if not question:
        return False
    first = ENUMERATION.search(hit.text)
    if first is None:
        return False
    heading = re.sub(r"\s+", "", hit.text[:first.start()])
    heading = ARTICLE.sub("", heading, count=1)
    match = re.fullmatch(
        r"(?P<subject>[\u3400-\u9fff]{2,24})有下列(?:情形|情况|条件)之一的[，,]"
        r"(?:应当|应该|必须|需要|应|须)(?P<action>[\u3400-\u9fff]{2,24})[：:]", heading)
    if match is None:
        return False
    forms = _positive_action_forms(match["action"])
    if not forms:
        return False
    query = re.sub(r"\s+", "", question)
    subject = re.escape(match["subject"])
    action = "(?:" + "|".join(re.escape(form) for form in forms) + ")"
    duty = r"(?:应当|应该|必须|需要|应|须)"
    # Match the entire clause: no negated/qualified subject prefixes, arbitrary
    # modifiers between duty and action, or predicates from another clause.
    prefix = r"(?:请问|请说明|请列出|请介绍|请告诉我)?" + subject
    question_forms = (
        prefix + r"(?:在)?(?:哪些|什么|何种)(?:情况|情形|条件)(?:下)?" + duty + action,
        prefix + duty + action + r"的(?:全部|所有|完整)(?:的)?(?:情况|情形|条件)",
    )
    return any(re.fullmatch(pattern + r"[？?。]?", query) for pattern in question_forms)


def _continuation_candidates(initial: Sequence[SearchHit], chunks: Sequence[Chunk],
                             question: str | None) -> list[SupplementalEvidence]:
    conditions = _conditions(question)
    by_id = {chunk.chunk_id: chunk for chunk in chunks}
    candidates = []
    for hit in initial:
        overview = _enumeration_overview(question, hit)
        if not _open_enumeration(hit) or not (conditions or overview):
            continue
        source = by_id.get(hit.chunk_id)
        if (source is None or source.language != "zh" or source.document_id != hit.document_id
                or source.page_number != hit.page_number or source.text != hit.text
                or not source.section_title or not ARTICLE.match(source.section_title)
                or ARTICLE.match(source.text.strip())[0] != ARTICLE.match(source.section_title)[0]):
            continue
        source_items = _items(source.text)
        for chunk in chunks:
            if (chunk.language != "zh" or chunk.document_id != source.document_id
                    or chunk.section_title != source.section_title
                    or chunk.chunk_index != source.chunk_index + 1
                    or chunk.page_number != source.page_number + 1
                    or chunk.page_index != source.page_index + 1):
                continue
            items = _items(chunk.text)
            first = ENUMERATION.search(chunk.text)
            if (not items or first is None or chunk.text[:first.start()].strip()
                    or items[0][0] != source_items[-1][0] + 1
                    or re.search(r"^\s*第[一二三四五六七八九十百0-9]+条", chunk.text, re.MULTILINE)
                    or not (overview or _condition_matches(items, conditions))):
                continue
            candidates.append(SupplementalEvidence(chunk, hit.chunk_id, (
                "same_document_article", "consecutive_chunk", "next_physical_page",
                "open_enumeration", "consecutive_item_numbers",
                "enumeration_overview" if overview else "question_condition_overlap")))
    return candidates


def find_supplemental(initial: Sequence[SearchHit], chunks: Sequence[Chunk], *,
                      question: str | None = None) -> list[SupplementalEvidence]:
    """One unique source across both rules; uncertainty/duplicates keep Initial only."""
    if len({chunk.chunk_id for chunk in chunks}) != len(chunks):
        return []
    candidates = {item.chunk.chunk_id: item for item in (
        *_age_candidates(initial, chunks), *_continuation_candidates(initial, chunks, question))}
    existing = {hit.chunk_id for hit in initial}
    return list(candidates.values()) if len(candidates) == 1 and not existing.intersection(candidates) else []


async def complete_evidence(initial: Sequence[SearchHit], *, load_chunks_fn: ChunkLookup,
                            question: str | None = None) -> list[SupplementalEvidence]:
    """Read-only completion failures fall back; cancellation is never swallowed."""
    if not initial:
        return []
    documents = list(dict.fromkeys([
        *(hit.document_id for hit, _, _ in _rules(initial)),
        *(hit.document_id for hit in initial
          if _open_enumeration(hit) and (_conditions(question) or _enumeration_overview(question, hit))),
    ]))
    if not documents:
        return []
    try:
        chunks = await load_chunks_fn(documents)
        return find_supplemental(initial, chunks, question=question)
    except Exception:
        logger.warning("Evidence completion unavailable; retaining Initial retrieval", exc_info=True)
        return []

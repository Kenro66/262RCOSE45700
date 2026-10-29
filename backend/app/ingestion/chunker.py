"""Policy-structure-first splitting with an unchanged ParsedPage/C2 contract."""

from collections import Counter, defaultdict
from functools import lru_cache
import hashlib
import re

from ..adapters.config import settings
from ..contracts import Chunk, ParsedPage

MAX_TOKENS = 500
OVERLAP = 75
_HEADING = re.compile(r"(?m)^[ \t\u3000]*(第\s*[一二三四五六七八九十百千零〇两0-9]+\s*[编章节条]|제\s*\d+\s*(?:장|절|조)(?:의\s*\d+)?(?=[（( \t]|$))(?:[（(]([^\n）)]{1,60})[）)])?")
_CHINESE_REFERENCE = re.compile(
    r"[ \t\u3000]*(?:规定(?:的|中|[，,])|所(?:列|述|称|指|定|规定|确定)|的|中|"
    r"第\s*[一二三四五六七八九十百千零〇两0-9]+\s*[款项]|"
    r"(?:至|到|以及|或者|及|和|与|、)[ \t\u3000]*第)"
)
_PAGE_MARKER = re.compile(r"(?P<mark>[-—–－])\s*(?P<number>\d{1,4})\s*(?P=mark)")
_AGENCIES = {"海关总署规章", "海关总署发布"}
_ITEM = re.compile(r"(?m)^[ \t\u3000]*(?:[（(][一二三四五六七八九十百千0-9]+[）)]|[①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳]|\d{1,3}[.．、)](?!\d)|[가나다라마바사아자차카타파하][.．])")
_EXCEPTION = re.compile(r"\s*(?:但|除外|다만|제외한다|단[,， ])")
_BREAKS = [_ITEM, re.compile(r"\n[ \t]*\n"), re.compile(r"[。！？；;!?]|(?<!\d)\.(?=\s|$)"), re.compile(r"\n"), re.compile(r"[ \t]+")]
_TERMS = ("中华人民共和国", "海关总署", "人民币", "增值税", "消费税", "进出境")


class ChunkingError(ValueError):
    """Invalid page input or tokenizer failure; never a document-status decision."""


@lru_cache(maxsize=1)
def _load_tokenizer(name: str, cache_dir: str | None):
    from transformers import AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(name, cache_dir=cache_dir, use_fast=True, trust_remote_code=False)
    if not tokenizer.is_fast:
        raise ChunkingError("A fast BGE-M3 tokenizer with character offsets is required")
    return tokenizer


def _tokenizer():
    config = settings()
    return _load_tokenizer(config.get("EMBEDDING_MODEL", "BAAI/bge-m3"), config.get("EMBEDDING_CACHE_DIR", "").strip() or None)


def _encoded(tokenizer, text: str):
    return tokenizer(text, add_special_tokens=False, truncation=False, return_offsets_mapping=True,
                     return_attention_mask=False, return_token_type_ids=False, verbose=False)


def _count(tokenizer, text: str) -> int:
    return len(tokenizer.encode(text, add_special_tokens=False, truncation=False, verbose=False))


def _edge_indices(lines):
    nonempty = [i for i, line in enumerate(lines) if line.strip()]
    return set(nonempty[:4] + nonempty[-3:])


def _headings(text: str):
    # Wrapped references such as 第九条规定的... continue the enclosing article.
    return [match for match in _HEADING.finditer(text)
            if not (match.group(1).startswith("第")
                    and _CHINESE_REFERENCE.match(text, match.end(1)))]


def _prepare(pages: list[ParsedPage], audit: list[dict]) -> list[tuple[ParsedPage, str]]:
    repeated = defaultdict(Counter)
    for page in pages:
        lines = page.text.splitlines()
        candidates = {lines[i].strip() for i in _edge_indices(lines)}
        for line in candidates:
            repeated[page.document_id][line] += 1
    prepared = []
    title_seen = set()
    for page in pages:
        lines = page.text.splitlines()
        edges = _edge_indices(lines)
        nonempty = [i for i, line in enumerate(lines) if line.strip()]
        boundaries = {nonempty[0], nonempty[-1]} if nonempty else set()
        kept = []
        for i, line in enumerate(lines):
            stripped = line.strip()
            number = _PAGE_MARKER.fullmatch(stripped)
            korean_header = re.fullmatch(r"법제처\s+(\d+)\s+국가법령정보센터", stripped)
            agency = stripped in _AGENCIES
            title = (re.match(r"中华人民共和国|进境物品|여행자|관세법", stripped)
                     and re.search(r"办法|规定|公告|告示|고시|시행규칙", stripped)
                     and not _HEADING.match(stripped) and not re.search(r"[。；;：:]|\d", stripped)
                     and len(stripped) <= 100)
            repeated_edge = repeated[page.document_id][stripped] >= 2
            # A bare number may be a legal quantity even when it equals the page.
            # Require paired page-marker punctuation plus an actual boundary or known furniture.
            number_context = i in boundaries or any(
                0 <= j < len(lines) and lines[j].strip() in _AGENCIES
                and repeated[page.document_id][lines[j].strip()] >= 2
                for j in (i - 1, i + 1)
            )
            remove = i in edges and ((number and int(number["number"]) == page.page_number and number_context)
                                     or (korean_header and int(korean_header[1]) == page.page_number)
                                     or (agency and repeated_edge))
            title_key = (page.document_id, stripped)
            if i in edges and title and repeated_edge:
                remove = title_key in title_seen
                title_seen.add(title_key)
            if remove:
                audit.append({"kind": "header_footer_removed", "document_id": page.document_id,
                              "page_number": page.page_number, "line": stripped})
            else:
                kept.append(line)
        text = "\n".join(kept)
        # Join only known legal terms, not every CJK line or paragraph boundary.
        for term in _TERMS:
            flexible = re.compile(r"(?:[ \t]*\n[ \t]*)?".join(map(re.escape, term)))
            def join(match):
                original = match.group()
                if "\n" in original:
                    audit.append({"kind": "known_term_line_join", "document_id": page.document_id,
                                  "page_number": page.page_number, "before": original, "after": term})
                return term
            text = flexible.sub(join, text)
        prepared.append((page, text))
    return prepared


def _hard_end(tokenizer, text: str, start: int, stop: int) -> int:
    data = _encoded(tokenizer, text[start:stop])
    if len(data["input_ids"]) <= MAX_TOKENS:
        return stop
    for index in range(MAX_TOKENS - 1, -1, -1):
        end = start + data["offset_mapping"][index][1]
        if end > start and _count(tokenizer, text[start:end]) <= MAX_TOKENS:
            return end
    raise ChunkingError("No nonempty character span fits the token limit")


def _preferred_end(tokenizer, text: str, start: int, hard_end: int, stop: int, level: int = 0):
    if level == len(_BREAKS):
        return hard_end, "token_boundary"
    candidates = []
    for match in _BREAKS[level].finditer(text, start, hard_end):
        end = match.start() if level == 0 else match.end()
        if end <= start or _count(tokenizer, text[start:end]) <= OVERLAP:
            continue
        if end < stop and _EXCEPTION.match(text[end:stop]):
            continue
        candidates.append(end)
    if candidates:
        return candidates[-1], ("item", "paragraph", "sentence", "line", "space")[level]
    return _preferred_end(tokenizer, text, start, hard_end, stop, level + 1)


def _windows(tokenizer, text: str, start: int, stop: int):
    while start < stop:
        while start < stop and text[start].isspace():
            start += 1
        if start == stop:
            break
        hard_end = _hard_end(tokenizer, text, start, stop)
        end, reason = (stop, "complete") if hard_end == stop else _preferred_end(tokenizer, text, start, hard_end, stop)
        while end > start and text[end - 1].isspace():
            end -= 1
        yield start, end, reason
        if end >= stop or not text[end:stop].strip():
            break
        data = _encoded(tokenizer, text[start:end])
        offsets = data["offset_mapping"]
        next_start = start + offsets[max(0, len(offsets) - OVERLAP)][0]
        natural = []
        for pattern in (_ITEM, _BREAKS[2]):
            for match in pattern.finditer(text, start + 1, end):
                candidate = match.start() if pattern is _ITEM else match.end()
                tail_count = _count(tokenizer, text[candidate:end])
                if 60 <= tail_count <= 90:
                    natural.append((abs(tail_count - OVERLAP), candidate))
        if natural:
            next_start = min(natural)[1]
        start = next_start if next_start > start else end


def _chunk_with_audit(pages: list[ParsedPage]) -> tuple[list[Chunk], list[dict]]:
    if not isinstance(pages, list) or any(not isinstance(page, ParsedPage) for page in pages):
        raise ChunkingError("pages must be a list of ParsedPage objects")
    seen = set()
    last_page = {}
    for page in pages:
        if page.page_number != page.page_index + 1:
            raise ChunkingError("Physical page_number must equal page_index + 1")
        key = (page.document_id, page.page_number)
        if key in seen or page.page_index <= last_page.get(page.document_id, -1):
            raise ChunkingError("Duplicate or out-of-order document pages")
        seen.add(key)
        last_page[page.document_id] = page.page_index
    if not any(page.text.strip() for page in pages):
        return [], []
    audit = []
    prepared = _prepare(pages, audit)
    tokenizer = _tokenizer()
    chunks = []
    counters = defaultdict(int)
    previous = {}
    for page, text in prepared:
        if not text.strip():
            continue
        headings = _headings(text)
        points = sorted({0, len(text), *(match.start() for match in headings)})
        title, previous_page = previous.get(page.document_id, (None, None))
        if title and (not headings or text[:headings[0].start()].strip()):
            audit.append({"kind": "cross_page_continuation", "document_id": page.document_id,
                          "page_number": page.page_number, "previous_page_number": previous_page, "section_title": title})
        heading_map = {match.start(): match for match in headings}
        for begin, finish in zip(points, points[1:]):
            if begin in heading_map:
                heading = heading_map[begin]
                title = heading.group(1).strip()
                if heading.group(2):
                    title += "(" + heading.group(2) + ")"
            if not text[begin:finish].strip():
                continue
            for start, end, reason in _windows(tokenizer, text, begin, finish):
                body = text[start:end]
                if not body.strip():
                    continue
                index = counters[page.document_id]
                chunk = Chunk(chunk_id=f"{page.document_id}_C{index:06d}", document_id=page.document_id,
                              chunk_index=index, page_index=page.page_index, page_number=page.page_number,
                              text=body, section_title=title, language=page.language,
                              token_count=_count(tokenizer, body), char_count=len(body),
                              content_hash="sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest())
                chunks.append(chunk)
                counters[page.document_id] += 1
                audit.append({"kind": "chunk_span", "chunk_id": chunk.chunk_id, "document_id": page.document_id,
                              "page_number": page.page_number, "start": start, "end": end,
                              "boundary": reason, "semantic_split_risk": reason in {"token_boundary", "line", "space"}})
        previous[page.document_id] = (title, page.page_number)
    return chunks, audit


def chunk_pages(pages: list[ParsedPage]) -> list[Chunk]:
    """Split only within physical pages; token_count excludes model special tokens."""
    return _chunk_with_audit(pages)[0]

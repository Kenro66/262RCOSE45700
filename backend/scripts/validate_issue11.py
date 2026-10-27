"""Run real JSON/tokenizer/optional embedding checks without a database."""

import argparse
from collections import Counter
import json
from math import isfinite
from pathlib import Path
import sys
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.contracts import ParsedPage, SearchHit
from app.ingestion.chunker import _chunk_with_audit, _count, _prepare, _tokenizer, chunk_pages
from app.qa.retriever import retrieve

FILES = {"DOC003": "response_DOC003.json", "DOC004": "response_1792900335398.json", "DOC005": "response_1792900378506.json"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixtures", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--embed", action="store_true")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    tokenizer = _tokenizer()
    summary = {"database_integration": "NOT RUN: backend search_similar and storage are unavailable",
               "formal_smoke_question": "NOT PROVIDED: test query is a self-authored development sample",
               "token_count_definition": "BGE-M3 text tokens, add_special_tokens=False; model input separately checked",
               "documents": [], "samples": [], "mock_retrieval": None}
    doc_chunks = {}
    for doc, filename in FILES.items():
        data = json.loads((args.fixtures / filename).read_text(encoding="utf-8-sig"))
        pages = [ParsedPage.model_validate(p) for p in data["pages"]]
        chunks = chunk_pages(pages)
        audited, audit = _chunk_with_audit(pages)
        assert audited == chunks
        cleaned = {p.page_number: text for p, text in _prepare(pages, [])}
        assert all(c.text in cleaned[c.page_number] for c in chunks)
        assert all(c.token_count <= 500 and c.text.strip() for c in chunks)
        assert all(c.page_number == c.page_index + 1 for c in chunks)
        assert len({c.chunk_id for c in chunks}) == len(chunks)
        effective_lengths = [len(tokenizer.encode((f"{c.section_title}\n\n" if c.section_title else "") + c.text, add_special_tokens=True, verbose=False)) for c in chunks]
        assert max(effective_lengths, default=0) <= 8192
        spans = [a for a in audit if a["kind"] == "chunk_span"]
        overlap_counts = []
        for previous, current in zip(spans, spans[1:]):
            if previous["page_number"] == current["page_number"] and current["start"] < previous["end"]:
                overlap_counts.append(_count(tokenizer, cleaned[current["page_number"]][current["start"]:previous["end"]]))
        stats = {"document_id": doc, "filename": data["filename"], "pages": len(pages), "chunks": len(chunks),
                 "max_token_count": max(c.token_count for c in chunks), "blank_chunks": sum(not c.text.strip() for c in chunks),
                 "page_mapping_pass": True, "source_traceability_pass": True,
                 "model_special_tokens": tokenizer.num_special_tokens_to_add(False),
                 "max_embedding_input_tokens_including_title_and_special": max(effective_lengths),
                 "event_counts": dict(Counter(a["kind"] for a in audit)),
                 "cross_page_continuations": [a for a in audit if a["kind"] == "cross_page_continuation"],
                 "semantic_split_risks": [a for a in audit if a.get("semantic_split_risk")],
                 "overlap_token_counts": overlap_counts, "embedding": "NOT RUN"}
        args.output_dir.joinpath(f"{doc}-chunks.json").write_text(json.dumps([c.model_dump() for c in chunks], ensure_ascii=False, indent=2), encoding="utf-8")
        args.output_dir.joinpath(f"{doc}-normalization-audit.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
        sample = next((c for c in chunks if c.section_title and len(c.text) > 40), chunks[0])
        summary["samples"].append({"chunk_id": sample.chunk_id, "document_id": doc, "filename": data["filename"],
                                   "page_number": sample.page_number, "section_title": sample.section_title, "text": sample.text})
        doc_chunks[doc] = chunks
        summary["documents"].append(stats)
        print(json.dumps({"stage": "chunking", **{k: stats[k] for k in ("document_id", "pages", "chunks", "max_token_count", "blank_chunks")}}, ensure_ascii=True), flush=True)
        args.output_dir.joinpath("summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.embed:
        from app.adapters.embedding import embed_chunks
        for stats in summary["documents"]:
            chunks = doc_chunks[stats["document_id"]]
            print("Real embed_chunks starting: " + stats["document_id"], flush=True)
            records = embed_chunks(chunks)
            assert len(records) == len(chunks)
            assert all(r.chunk_id == c.chunk_id and len(r.vector) == 1024 and all(isfinite(v) for v in r.vector) and any(r.vector) for c, r in zip(chunks, records, strict=True))
            stats["embedding"] = {"real_model": True, "count": len(records), "dimension": 1024, "id_mapping_pass": True}
            args.output_dir.joinpath("summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({"stage": "real_embedding", "document_id": stats["document_id"], "vectors": len(records), "dimension": 1024}), flush=True)
        selected = doc_chunks["DOC003"][:5]
        search = Mock(return_value=[SearchHit(chunk_id=c.chunk_id, document_id=c.document_id, page_number=c.page_number, text=c.text, score=.9) for c in selected])
        result = retrieve("中国旅客进境时，携带的行李物品应当如何申报？", search_similar=search)
        vector = search.call_args.args[0]
        assert len(vector) == 1024 and search.call_args.kwargs["top_k"] == 5 and len(result) <= 5
        summary["mock_retrieval"] = {"real_question_embedding": True, "query_dimension": len(vector), "mock_search_similar": True,
                                     "top_k": 5, "returned": len(result), "document_ids": [h.document_id for h in result],
                                     "not_real_database_retrieval": True}
    args.output_dir.joinpath("summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Validation report written: " + str(args.output_dir), flush=True)


if __name__ == "__main__":
    main()

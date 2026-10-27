import unittest

from app.contracts import DocumentRecord
from app.qa.citation import build_evidence, has_cited_conclusions, resolve_citations


def hit(**changes):
    row = dict(chunk_id="DOC001_C001", document_id="DOC001", page_number=13,
               text="仅供单元测试：示例限量为400支。", score=.91)
    return {**row, **changes}


class CitationTests(unittest.TestCase):
    def setUp(self):
        self.evidence = build_evidence([hit()])
        self.docs = {"DOC001": {"filename": "test-policy.pdf", "status": "READY"}}

    def test_resolve_real_metadata_and_remove_unknown_id(self):
        text, citations = resolve_citations("示例规定。[E1][E9][E1]", self.evidence, self.docs)
        self.assertNotIn("E9", text)
        self.assertEqual(len(citations), 1)
        self.assertEqual(citations[0].model_dump(), dict(evidence_id="E1", filename="test-policy.pdf",
                                                        page=13, quote=self.evidence[0].hit.text))

    def test_c1_document_record_is_supported(self):
        record = DocumentRecord(document_id="DOC001", filename="source.pdf", status="READY",
                                uploaded_at="2026-10-27T00:00:00+09:00")
        _, citations = resolve_citations("说明。[E1]", self.evidence, {"DOC001": record})
        self.assertEqual(citations[0].filename, "source.pdf")

    def test_no_valid_citations(self):
        for docs in ({}, {"DOC001": {"filename": "x.pdf", "status": "PROCESSING"}}):
            with self.subTest(docs=docs):
                text, citations = resolve_citations("说明。[E1][E9]", self.evidence, docs)
                self.assertEqual(citations, [])
                self.assertNotIn("[E", text)

    def test_bad_metadata_raises_instead_of_becoming_insufficient(self):
        for metadata in (None, [], {"DOC001": {"file_name": "x", "status": "READY"}},
                         {"DOC001": {"filename": "x", "status": "Ready"}},
                         {"DOC001": {"filename": "x", "status": "READY", "document_id": "wrong"}}):
            with self.subTest(metadata=metadata), self.assertRaises((ValueError, TypeError)):
                resolve_citations("说明。[E1]", self.evidence, metadata)

    def test_evidence_ids_restart_for_each_request(self):
        self.assertEqual(build_evidence([hit()])[0].evidence_id, "E1")
        self.assertEqual(build_evidence([hit(document_id="OTHER")])[0].evidence_id, "E1")

    def test_invalid_hits_raise(self):
        for rows in (None, [{}], [hit(text=" ")], [hit(score=float("nan"))], [hit(page_number=0)]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                build_evidence(rows)

    def test_each_sentence_or_line_requires_reference(self):
        for text in ("限额为1.5升。[E1] 应当申报。[E2]", "한도는 1.5리터입니다.[E1]",
                     "说明[E1]。\n其他说明。[E2]", "说明。[E1][E2]"):
            with self.subTest(text=text):
                self.assertTrue(has_cited_conclusions(text))
        for text in ("说明。[E1] 限额9999。", "无引用。说明。[E1]", "说明。[E1]\n未引用的结论"):
            with self.subTest(text=text):
                self.assertFalse(has_cited_conclusions(text))

    def test_two_documents_and_model_order_preserve_sources(self):
        evidence = build_evidence([hit(), hit(chunk_id="other", document_id="DOC002", page_number=2)])
        documents = {**self.docs, "DOC002": {"filename": "korea.pdf", "status": "READY"}}
        _, citations = resolve_citations("说明。[E2][E1][E2]", evidence, documents)
        self.assertEqual([(c.evidence_id, c.filename, c.page) for c in citations],
                         [("E2", "korea.pdf", 2), ("E1", "test-policy.pdf", 13)])

    def test_semicolon_clauses_share_end_of_sentence_references(self):
        self.assertTrue(has_cited_conclusions("示例限量为40支，限一天一次；未成年人不适用。[E1][E2]"))
        self.assertFalse(has_cited_conclusions("示例限量为40支。[E1] 其他规定不适用。"))

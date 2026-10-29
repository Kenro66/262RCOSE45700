"""Real parsed documents; opt in with POLICY_FIXTURE_DIR, never fabricate fixtures."""

import json
import os
from pathlib import Path
import unittest

from app.contracts import ParsedPage
from app.ingestion.chunker import _chunk_with_audit, _count, _prepare, _tokenizer, chunk_pages

FILES = {"DOC003": "response_DOC003.json", "DOC004": "response_1792900335398.json", "DOC005": "response_1792900378506.json"}


@unittest.skipUnless(os.environ.get("POLICY_FIXTURE_DIR"), "Set POLICY_FIXTURE_DIR to the three verified backend JSON files")
class RealDocumentChunkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(os.environ["POLICY_FIXTURE_DIR"])
        cls.docs = {doc: json.loads((root / name).read_text(encoding="utf-8-sig")) for doc, name in FILES.items()}
        cls.pages = {doc: [ParsedPage.model_validate(p) for p in data["pages"]] for doc, data in cls.docs.items()}
        cls.results = {doc: _chunk_with_audit(pages) for doc, pages in cls.pages.items()}
        cls.tokenizer = _tokenizer()

    def test_all_38_real_pages_and_hard_constraints(self):
        self.assertEqual(sum(map(len, self.pages.values())), 38)
        for doc, (chunks, _) in self.results.items():
            with self.subTest(document=doc):
                self.assertTrue(chunks)
                self.assertEqual({c.page_number for c in chunks}, {p.page_number for p in self.pages[doc]})
                self.assertEqual([c.chunk_index for c in chunks], list(range(len(chunks))))
                self.assertTrue(all(c.text.strip() and c.token_count <= 500 and c.page_number == c.page_index + 1 for c in chunks))
                self.assertTrue(all(c.token_count == _count(self.tokenizer, c.text) for c in chunks))

    def test_all_chunk_text_is_traceable_to_one_cleaned_page(self):
        for doc, (chunks, _) in self.results.items():
            pages = {p.page_number: text for p, text in _prepare(self.pages[doc], [])}
            self.assertTrue(all(c.text in pages[c.page_number] for c in chunks))

    def test_doc003_articles_six_and_nine_cross_page_without_merging(self):
        chunks, audit = self.results["DOC003"]
        continuations = {(a["section_title"], a["previous_page_number"], a["page_number"]) for a in audit if a["kind"] == "cross_page_continuation"}
        self.assertIn(("第六条", 1, 2), continuations)
        self.assertIn(("第九条", 2, 3), continuations)
        self.assertTrue(all(isinstance(c.page_number, int) for c in chunks))

    def test_public_function_matches_reported_chunks(self):
        for doc, pages in self.pages.items():
            self.assertEqual(chunk_pages(pages), self.results[doc][0])

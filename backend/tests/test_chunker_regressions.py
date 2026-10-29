"""Regression cases reported in PR #27; use the real BGE-M3 tokenizer."""

import unittest

from app.contracts import ParsedPage
from app.ingestion.chunker import _chunk_with_audit, _count, _prepare, _tokenizer, chunk_pages


def page(text, number=1, document="REGRESSION"):
    return ParsedPage(document_id=document, page_index=number - 1, page_number=number,
                      text=text, language="zh")


class ChunkerRegressionTests(unittest.TestCase):
    def test_quantity_numbers_on_two_pages_are_not_page_numbers(self):
        pages = [page("数量：\n1\n件"), page("数量：\n2\n件", 2)]
        audit = []
        self.assertEqual([text for _, text in _prepare(pages, audit)], [p.text for p in pages])
        self.assertFalse(any(row["kind"] == "header_footer_removed" for row in audit))
        self.assertEqual([chunk.text for chunk in chunk_pages(pages)], [p.text for p in pages])

    def test_unlabelled_numbers_at_page_edges_remain_ambiguous(self):
        pages = [page("1\n件物品，金额为\n1"), page("2\n件物品，金额为\n2", 2)]
        self.assertEqual([text for _, text in _prepare(pages, [])], [p.text for p in pages])

    def test_negative_and_interior_decorated_values_are_preserved(self):
        for text in ("-1\n数值应保留。", "数值：\n- 1 -\n单位。", "数量：\n1\n件\n金额：\n1\n元"):
            with self.subTest(text=text):
                self.assertEqual(_prepare([page(text)], [])[0][1], text)

    def test_clear_decorated_footer_is_still_removed(self):
        pages = [page("第一条 应当申报。\n- 1 -"), page("第二条 应当查验。\n- 2 -", 2)]
        cleaned = [text for _, text in _prepare(pages, [])]
        self.assertEqual(cleaned, ["第一条 应当申报。", "第二条 应当查验。"])

    def test_reference_does_not_split_exception_or_change_title(self):
        first = "第十条 符合条件的物品可以免税，\n第九条规定的物品除外。"
        second = "第十一条 应当如实申报。"
        chunks = chunk_pages([page(first + "\n" + second)])
        self.assertEqual([chunk.text for chunk in chunks], [first, second])
        self.assertEqual([chunk.section_title for chunk in chunks], ["第十条", "第十一条"])

    def test_reference_wording_and_whitespace_variants(self):
        for reference in ("第九条 规定的物品除外。", "第 九 条规定的物品除外。",
                          "第九条所列物品除外。", "第九条的物品除外。",
                          "第九条第一款规定的物品除外。", "第九条至第十条规定的物品除外。",
                          "第九条、第二条规定的物品除外。", "第九条以及第二条规定的物品除外。"):
            text = "第十条 符合下列条件的物品，\n" + reference
            with self.subTest(reference=reference):
                chunks = chunk_pages([page(text)])
                self.assertEqual([chunk.text for chunk in chunks], [text])
                self.assertEqual(chunks[0].section_title, "第十条")

    def test_cross_page_reference_keeps_inherited_article(self):
        pages = [page("第十条 符合下列条件的物品可以免税，"),
                 page("第九条规定的物品除外。\n第十一条 应当如实申报。", 2)]
        chunks = chunk_pages(pages)
        self.assertEqual([chunk.section_title for chunk in chunks], ["第十条", "第十条", "第十一条"])
        self.assertEqual([chunk.page_number for chunk in chunks], [1, 2, 2])

    def test_true_compact_and_captioned_articles_still_split(self):
        for text in ("第九条旅客应当申报。\n第十条超过限量应当纳税。",
                     "第九条（申报）旅客应当申报。\n第十条（纳税）超过限量应当纳税。"):
            with self.subTest(text=text):
                chunks = chunk_pages([page(text)])
                self.assertEqual(len(chunks), 2)
                self.assertTrue(chunks[0].section_title.startswith("第九条"))
                self.assertTrue(chunks[1].section_title.startswith("第十条"))

    def test_long_referenced_article_keeps_token_and_page_constraints(self):
        text = "第十条 符合以下条件的物品应当申报。\n" + "第九条规定的物品除外。\n" * 150
        chunks, _ = _chunk_with_audit([page(text)])
        self.assertGreater(len(chunks), 1)
        tokenizer = _tokenizer()
        for chunk in chunks:
            self.assertEqual(chunk.section_title, "第十条")
            self.assertEqual(chunk.page_number, 1)
            self.assertTrue(chunk.text.strip())
            self.assertEqual(chunk.token_count, _count(tokenizer, chunk.text))
            self.assertLessEqual(chunk.token_count, 500)

import hashlib
import inspect
import unittest
from typing import get_type_hints

from app.contracts import Chunk, ParsedPage
from app.ingestion.chunker import ChunkingError, _chunk_with_audit, _count, _prepare, _tokenizer, chunk_pages


def page(text, number=1, document="DOC_TEST", language="zh"):
    return ParsedPage(document_id=document, page_index=number - 1, page_number=number,
                      text=text, language=language)


class ChunkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tokenizer = _tokenizer()

    def assert_valid(self, chunks):
        for chunk in chunks:
            self.assertIsInstance(chunk, Chunk)
            self.assertTrue(chunk.text.strip())
            self.assertLessEqual(chunk.token_count, 500)
            self.assertEqual(chunk.token_count, _count(self.tokenizer, chunk.text))
            self.assertEqual(chunk.char_count, len(chunk.text))
            self.assertEqual(chunk.content_hash, "sha256:" + hashlib.sha256(chunk.text.encode()).hexdigest())
            self.assertEqual(chunk.page_number, chunk.page_index + 1)

    def test_public_signature_and_empty_without_tokenizer(self):
        self.assertEqual(list(inspect.signature(chunk_pages).parameters), ["pages"])
        self.assertEqual(get_type_hints(chunk_pages), {"pages": list[ParsedPage], "return": list[Chunk]})
        self.assertEqual(chunk_pages([]), [])
        self.assertEqual(chunk_pages([page(""), page(" \n\t", 2)]), [])

    def test_chinese_articles_keep_items_and_conditions(self):
        first = "第一条 符合以下条件的旅客，可以免税。\n（一）个人自用。\n（二）超过规定数量，应当申报。\n1. 人民币1000元以内。\n但是，法律另有规定的除外。"
        second = "第二条 旅客应当如实申报。"
        chunks = chunk_pages([page(first + "\n" + second)])
        self.assertEqual([c.text for c in chunks], [first, second])
        self.assertEqual([c.section_title for c in chunks], ["第一条", "第二条"])
        self.assert_valid(chunks)

    def test_korean_articles_and_items_remain_complete(self):
        first = "제1조(신고) 해당하는 경우 신고한다.\n① 개인용 물품이다.\n② 800달러를 초과한다.\n가. 필요한 서류를 제출한다.\n나. 다만, 예외 물품은 제외한다."
        second = "제2조(통관) 세관에 신고한다."
        chunks = chunk_pages([page(first + "\n" + second, language="ko")])
        self.assertEqual([c.text for c in chunks], [first, second])
        self.assertEqual(chunks[0].section_title, "제1조(신고)")
        self.assert_valid(chunks)

    def test_long_chinese_article_uses_items_before_sentence_fallback(self):
        item = "旅客携带个人物品入境时，应当依法申报，接受海关监管。" * 12
        text = "第一条\n（一）" + item + "\n（二）" + item + "\n（三）" + item
        chunks, audit = _chunk_with_audit([page(text)])
        self.assertGreater(len(chunks), 1)
        self.assertTrue(any(a.get("boundary") == "item" for a in audit))
        self.assert_valid(chunks)

    def test_long_korean_article(self):
        item = "여행자가 물품을 휴대하는 경우 세관에 신고한다. 다만, 예외 물품은 제외한다. " * 30
        chunks = chunk_pages([page("제1조(신고)\n① " + item + "\n가. " + item, language="ko")])
        self.assertGreater(len(chunks), 1)
        self.assert_valid(chunks)

    def test_wrapped_korean_legal_reference_not_new_article(self):
        text = "제1조(신고) 다음 규정에 따른다.\n제241조제2항에 따라 필요한 서류를 제출한다.\n제2조(통관) 세관에 신고한다."
        chunks = chunk_pages([page(text, language="ko")])
        self.assertEqual(len(chunks), 2)
        self.assertIn("제241조제2항", chunks[0].text)
        self.assertEqual([c.section_title for c in chunks], ["제1조(신고)", "제2조(통관)"])

    def test_overlap_only_inside_long_structure(self):
        text = "第一条 " + "符合以下条件的旅客，应当向海关申报携带的物品。" * 100
        chunks, audit = _chunk_with_audit([page(text)])
        spans = [a for a in audit if a["kind"] == "chunk_span"]
        cleaned = _prepare([page(text)], [])[0][1]
        for previous, current in zip(spans, spans[1:]):
            overlap = cleaned[current["start"]:previous["end"]]
            self.assertTrue(overlap)
            self.assertTrue(60 <= _count(self.tokenizer, overlap) <= 90)
        self.assert_valid(chunks)

    def test_short_condition_and_exception_not_split(self):
        text = "第一条 符合以下条件的旅客，免征税款。\n但超过规定数量的，应按人民币金额缴税；依法另有规定的除外。"
        self.assertEqual([c.text for c in chunk_pages([page(text)])], [text])

    def test_cross_page_article_not_joined(self):
        pages = [page("第六条 旅客应当符合以下条件："), page("个人自用。例外情况另行处理。\n第七条 如实申报。", 2)]
        chunks, audit = _chunk_with_audit(pages)
        self.assertEqual({c.page_number for c in chunks}, {1, 2})
        self.assertFalse(any("以下条件" in c.text and "个人自用" in c.text for c in chunks))
        self.assertTrue(any(a["kind"] == "cross_page_continuation" and a["section_title"] == "第六条" for a in audit))
        self.assert_valid(chunks)

    def test_known_term_line_join_preserves_paragraphs(self):
        text = "第一条 根据《中华人\n民共和国海关法》执行。\n\n第二条 应当申报。"
        chunks, audit = _chunk_with_audit([page(text)])
        self.assertIn("中华人民共和国海关法", chunks[0].text)
        self.assertEqual(len(chunks), 2)
        self.assertTrue(any(a["kind"] == "known_term_line_join" for a in audit))

    def test_repeated_headers_removed_but_body_kept(self):
        pages = [page("海关总署规章\n- 1 -\n海关总署发布\n第一条 海关总署依法监管。"),
                 page("海关总署规章\n- 2 -\n海关总署发布\n第二条 旅客应当申报。", 2)]
        chunks = chunk_pages(pages)
        self.assertFalse(any("海关总署规章" in c.text or "海关总署发布" in c.text for c in chunks))
        self.assertTrue(any("海关总署依法监管" in c.text for c in chunks))

    def test_korean_header_and_repeated_title(self):
        title = "여행자 및 승무원 휴대품 통관에 관한 고시"
        pages = [page("법제처    1    국가법령정보센터\n" + title + "\n제1조 신고한다.", language="ko"),
                 page("법제처    2    국가법령정보센터\n" + title + "\n제2조 신고한다.", 2, language="ko")]
        chunks = chunk_pages(pages)
        self.assertFalse(any("국가법령정보센터" in c.text for c in chunks))
        self.assertEqual(sum(c.text.count(title) for c in chunks), 1)

    def test_unconfirmed_header_like_body_not_removed(self):
        pages = [page("符合以下规定\n第一条 依法办理。"), page("符合以下规定\n第二条 依法办理。", 2)]
        self.assertEqual(sum("符合以下规定" in c.text for c in chunk_pages(pages)), 2)
        self.assertTrue(any(c.text.startswith("1\n") for c in chunk_pages([page("1\n件物品应当申报。")])) )

    def test_exact_500_and_501_and_special_tokens(self):
        for size in (500, 501):
            text = ("a " * size).strip()
            self.assertEqual(_count(self.tokenizer, text), size)
            chunks = chunk_pages([page(text)])
            self.assertEqual(len(chunks), 1 if size == 500 else 2)
            self.assert_valid(chunks)
        self.assertEqual(self.tokenizer.num_special_tokens_to_add(False), 2)
        self.assertEqual(len(self.tokenizer.encode(("a " * 500).strip(), add_special_tokens=True)), 502)

    def test_extremely_long_single_sentence_and_risk(self):
        chunks, audit = _chunk_with_audit([page("第一条 " + "携带物品" * 1500)])
        self.assertGreater(len(chunks), 1)
        self.assertTrue(any(a.get("semantic_split_risk") for a in audit))
        self.assert_valid(chunks)

    def test_deterministic_metadata_and_document_counters(self):
        pages = [page("第一条 申报。"), page("第二条 查验。", 2), page("제1조 신고한다.", document="KR", language="ko")]
        chunks = chunk_pages(pages)
        self.assertEqual(chunks, chunk_pages(pages))
        self.assertEqual([c.chunk_index for c in chunks], [0, 1, 0])
        self.assertEqual(chunks[-1].document_id, "KR")
        self.assertEqual(chunks[-1].language, "ko")
        self.assert_valid(chunks)

    def test_invalid_pages(self):
        for pages in (None, "text", [{}], [page("hello", 2), page("world")], [page("x"), page("y")]):
            with self.subTest(pages=pages), self.assertRaises(ChunkingError):
                chunk_pages(pages)
        wrong = page("text")
        wrong.page_number = 2
        with self.assertRaises(ChunkingError):
            chunk_pages([wrong])

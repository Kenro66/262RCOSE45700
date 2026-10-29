"""Controlled reproductions of PR #30 review findings; no real model calls."""

import json
import unittest
from unittest.mock import AsyncMock, Mock

from app.contracts import ChatStatus
from app.qa.citation import has_cited_conclusions
from app.qa.orchestrator import AnswerError, answer
from app.qa.prompts import detect_language


class LanguageAndBoundaryTests(unittest.TestCase):
    def test_foreign_policy_titles_do_not_override_question_language(self):
        korean = "여행자 및 승무원 휴대품 통관에 관한 고시"
        chinese = "中华人民共和国海关进出境行李物品监管办法"
        for left, right in (("《", "》"), ("「", "」"), ("『", "』"), ("“", "”"), ('"', '"'), ("'", "'")):
            with self.subTest(pair=left):
                self.assertEqual(detect_language(f"按{left}{korean}{right}，限量多少？"), "zh")
                self.assertEqual(detect_language(f"{left}{chinese}{right}에 따르면 한도는?"), "ko")

    def test_title_only_falls_back_to_full_text(self):
        self.assertEqual(detect_language("《여행자 및 승무원 휴대품 통관에 관한 고시》"), "ko")
        self.assertEqual(detect_language("《中华人民共和国海关进出境行李物品监管办法》"), "zh")
        self.assertEqual(detect_language("纯中文问题"), "zh")
        self.assertEqual(detect_language("한국어 질문입니다"), "ko")

    def test_special_sentence_and_line_boundaries_require_each_reference(self):
        for separator in ("｡", "．", "․", "﹒", "\r", "\r\n", "\u0085", "\u2028", "\u2029", "\v", "\f"):
            with self.subTest(separator=repr(separator)):
                self.assertFalse(has_cited_conclusions(f"允许办理[E1]{separator}无需申报。"))
                self.assertTrue(has_cited_conclusions(f"允许办理[E1]{separator}需要申报[E2]。"))

    def test_decimal_points_are_not_sentence_boundaries(self):
        self.assertTrue(has_cited_conclusions("示例数值为1.5。[E1]"))
        self.assertTrue(has_cited_conclusions("示例数值为１．５。[E1]"))


class AnswerReviewRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def respond(self, generated, *, question="可以携带多少？", quote="示例香烟限量为400支。", extra=None):
        hits = [dict(chunk_id="c1", document_id="d", page_number=1, text=quote, score=.9)]
        if extra:
            hits.append(dict(chunk_id="c2", document_id="d", page_number=2, text=extra, score=.8))
        return await answer(question, retrieve_fn=AsyncMock(return_value=hits),
                            get_documents_fn=AsyncMock(return_value={"d": {"filename": "test.pdf", "status": "READY"}}),
                            generate_fn=Mock(return_value=json.dumps({"answer": generated, "insufficient_evidence": False}, ensure_ascii=False)))

    async def test_chinese_answer_with_korean_title_is_not_rejected(self):
        result = await self.respond("示例限量为400支。[E1]", question="按《여행자 및 승무원 휴대품 통관에 관한 고시》，限量多少？")
        self.assertEqual(result.status, ChatStatus.ANSWERED)
        quoted = await self.respond("依据《여행자 및 승무원 휴대품 통관에 관한 고시》，示例限量400支。[E1]")
        self.assertEqual(quoted.status, ChatStatus.ANSWERED)

    async def test_special_separator_cannot_hide_uncited_second_sentence(self):
        with self.assertRaises(AnswerError):
            await self.respond("示例限量400支[E1]｡无需申报。")

    async def test_invented_number_returns_fixed_number_free_refusal(self):
        result = await self.respond("酒可以带9999升。[E1]", question="带酒入境可以带多少？")
        self.assertEqual(result.status, ChatStatus.INSUFFICIENT_EVIDENCE)
        self.assertEqual(result.citations, [])
        self.assertFalse(any(c.isdigit() for c in result.answer))

    async def test_numbers_in_other_citations_cannot_support_this_sentence(self):
        result = await self.respond("示例限量为500支。[E1]另一示例限量为500支。[E2]", extra="另一示例为500支。")
        self.assertEqual(result.status, ChatStatus.INSUFFICIENT_EVIDENCE)

    async def test_reference_ids_are_not_answer_numbers(self):
        result = await self.respond("必须申报。[E1]", quote="旅客必须申报。")
        self.assertEqual(result.status, ChatStatus.ANSWERED)

    async def test_number_format_normalization_and_decimals(self):
        result = await self.respond("示例数量1000，数值1.50，比例40%。[E1]", quote="示例数量1,000，数值1.5，比例40%。")
        self.assertEqual(result.status, ChatStatus.ANSWERED)

    async def test_fullwidth_invented_number_also_refuses(self):
        result = await self.respond("酒可带９９９９升。[E1]")
        self.assertEqual(result.status, ChatStatus.INSUFFICIENT_EVIDENCE)

    async def test_known_limit_same_number_different_subject_is_not_detected(self):
        # Characterizes the limited guard, not an endorsement of this unsupported claim.
        result = await self.respond("酒可以带400升。[E1]")
        self.assertEqual(result.status, ChatStatus.ANSWERED)

import importlib.util
from pathlib import Path
import tempfile
import unittest


spec = importlib.util.spec_from_file_location("issue12_smoke_validation",
                                            Path(__file__).resolve().parents[1] / "scripts/validate_issue12_smoke.py")
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


class SmokeValidationTests(unittest.TestCase):
    def source(self):
        blocks = []
        for i in range(1, 11):
            status = "应拒答" if i >= 9 else "可回答"
            blocks.append(f"Q{i:03d} | QType-01 | {status} | lang=zh\n"
                          "问题：测试问题\n标准答案：测试答案\n适用条件：测试条件\n"
                          "证据：原文第一行\n1. 第二行\n2. 第三行\n")
        return "\n".join(blocks)

    def test_question_parser_preserves_multiline_evidence_and_refusal(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "questions.txt"
            path.write_text(self.source(), encoding="utf-8")
            questions = smoke.load_questions(path)
        self.assertEqual(len(questions), 10)
        self.assertIn("2. 第三行", questions[0]["gold_evidence_lines"][0])
        self.assertEqual(questions[-1]["expected_status"], "INSUFFICIENT_EVIDENCE")

    def test_missing_or_duplicate_questions_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "questions.txt"
            for source in (self.source().replace("Q010", "Q009"), self.source().split("Q010")[0]):
                path.write_text(source, encoding="utf-8")
                with self.assertRaises(ValueError):
                    smoke.load_questions(path)

    def test_report_does_not_claim_database_retrieval(self):
        report = smoke.render_report({"started_at_utc": "test", "passed": 0, "cases": []})
        self.assertIn("不是数据库检索", report)
        self.assertIn("不发送给模型", report)

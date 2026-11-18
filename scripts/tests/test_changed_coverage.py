"""使用真实 Git 差异和手工覆盖报告验证关卡的通过/失败行为。"""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

CHECKER = Path(__file__).resolve().parents[1] / "check_changed_coverage.py"


class ChangedCoverageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.repo = self.folder / "repo"
        self.repo.mkdir()
        self.git("init", "--quiet", "--template=")
        self.git("config", "core.autocrlf", "false")
        self.git("config", "core.hooksPath", str(self.folder / "no-hooks"))
        self.write("backend/app/example.py", "def value():\n    return 1\n")
        self.write("frontend/src/example.ts", "export function value() {\n  return 1\n}\n")
        self.commit()
        self.base = self.git("rev-parse", "HEAD").strip()

    def git(self, *args):
        result = subprocess.run(
            ["git", "-c", "user.name=CI test", "-c", "user.email=ci-test@example.invalid",
             "-c", "commit.gpgsign=false", *args],
            cwd=self.repo, capture_output=True, text=True, encoding="utf-8", check=True,
        )
        return result.stdout

    def write(self, path, text):
        file = self.repo / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(text, encoding="utf-8")
        return file

    def commit(self):
        self.git("add", "--all")
        self.git("commit", "--quiet", "--no-verify", "-m", "fixture")

    def change(self, path="backend/app/example.py", text="def value():\n    return 2\n"):
        self.write(path, text)
        self.commit()

    def report(self, classes, name="coverage.xml"):
        root = ET.Element("coverage")
        elements = ET.SubElement(ET.SubElement(root, "packages"), "package")
        elements = ET.SubElement(elements, "classes")
        for filename, counts in classes.items():
            lines = ET.SubElement(ET.SubElement(elements, "class", filename=filename), "lines")
            for number, hits in counts.items():
                ET.SubElement(lines, "line", number=str(number), hits=str(hits))
        path = self.folder / name
        ET.ElementTree(root).write(path, encoding="utf-8")
        return path

    def invoke(self, reports=(), scope="backend", *, working=False, base=None, statements=None):
        output = self.folder / "result.json"
        command = [sys.executable, str(CHECKER), "--repo", str(self.repo), "--scope", scope,
                   "--base", base or self.base, "--output", str(output)]
        if not working:
            command += ["--head", "HEAD"]
        for report in reports:
            command += ["--report", str(report)]
        if scope == "frontend" and reports:
            if statements is None:
                statements = self.folder / "statements.json"
                data = {}
                for report in reports:
                    for item in ET.parse(report).getroot().findall(".//class"):
                        rows = item.findall("./lines/line")
                        data[item.get("filename")] = {
                            "runtimeLines": [int(row.get("number")) for row in rows],
                            "statementMap": {str(i): {"start": {"line": int(row.get("number")), "column": 0},
                                                       "end": {"line": int(row.get("number")), "column": 50}}
                                             for i, row in enumerate(rows)},
                            "s": {str(i): int(row.get("hits")) for i, row in enumerate(rows)}}
                statements.write_text(json.dumps(data), encoding="utf-8")
            command += ["--statements", str(statements)]
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                                env=dict(os.environ, PYTHONUTF8="1"), timeout=20)
        payload = json.loads(output.read_text(encoding="utf-8")) if output.exists() else {}
        return result, payload

    def test_existing_tests_cover_changed_line_without_new_test_file(self):
        self.change()
        report = self.report({"example.py": {1: 1, 2: 1}})
        result, payload = self.invoke([report])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["covered"], 1)

    def test_uncovered_changed_line_fails_with_exact_file_and_line(self):
        self.change()
        result, payload = self.invoke([self.report({"example.py": {1: 1, 2: 0}})])
        self.assertEqual(result.returncode, 1)
        self.assertEqual(payload["files"][0]["path"], "backend/app/example.py")
        self.assertEqual(payload["files"][0]["uncovered"], [2])

    def test_old_uncovered_lines_do_not_block_covered_new_line(self):
        self.change()
        result, payload = self.invoke([self.report({"app/example.py": {1: 0, 2: 1}})])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((payload["covered"], payload["total"]), (1, 1))

    def test_new_business_file_missing_from_report_fails(self):
        self.change("backend/app/new.py", "def added():\n    return 7\n")
        result, payload = self.invoke([self.report({"example.py": {1: 1, 2: 1}})])
        self.assertEqual(result.returncode, 1)
        self.assertTrue(payload["files"][0]["missing"])
        self.assertEqual(payload["files"][0]["path"], "backend/app/new.py")

    def test_any_uncovered_changed_file_blocks_even_when_other_file_passes(self):
        self.write("backend/app/example.py", "def value():\n    return 2\n")
        self.write("backend/app/new.py", "answer = 4\n")
        self.commit()
        result, payload = self.invoke([self.report({
            "example.py": {1: 1, 2: 1}, "new.py": {1: 0},
        })])
        self.assertEqual(result.returncode, 1)
        self.assertFalse(payload["passed"])

    def test_backend_reports_are_combined_by_coverage_union(self):
        self.change()
        unit = self.report({"example.py": {1: 1, 2: 0}}, "unit.xml")
        postgres = self.report({"example.py": {1: 0, 2: 1}}, "postgres.xml")
        result, payload = self.invoke([unit, postgres])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(payload["covered"], 1)

    def test_all_requested_reports_must_exist(self):
        self.change()
        present = self.report({"example.py": {1: 1, 2: 1}})
        result, payload = self.invoke([present, self.folder / "missing.xml"])
        self.assertEqual(result.returncode, 2)
        self.assertIn("error", payload)

    def test_business_change_without_reports_is_error(self):
        self.change()
        result, payload = self.invoke()
        self.assertEqual(result.returncode, 2)
        self.assertIn("error", payload)

    def test_corrupt_report_fails_closed(self):
        self.change()
        report = self.folder / "broken.xml"
        report.write_text("<coverage>", encoding="utf-8")
        result, payload = self.invoke([report])
        self.assertEqual(result.returncode, 2)
        self.assertIn("error", payload)

    def test_junit_cannot_be_used_as_coverage_report(self):
        self.change()
        report = self.folder / "junit.xml"
        report.write_text('<testsuites><testsuite tests="1"/></testsuites>', encoding="utf-8")
        result, payload = self.invoke([report])
        self.assertEqual(result.returncode, 2)
        self.assertIn("error", payload)

    def test_invalid_hit_counts_fail_closed(self):
        self.change()
        result, payload = self.invoke([self.report({"example.py": {2: -1}})])
        self.assertEqual(result.returncode, 2)
        self.assertIn("error", payload)

    def test_docs_only_change_does_not_require_coverage_reports(self):
        self.change("README.md", "说明更新\n")
        result, payload = self.invoke()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(payload["files"], [])

    def test_tests_only_change_does_not_require_business_coverage(self):
        self.change("backend/tests/test_example.py", "def test_existing():\n    assert True\n")
        result, payload = self.invoke()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(payload["files"], [])

    def test_typescript_declarations_do_not_require_runtime_coverage(self):
        self.change("frontend/src/types.d.ts", "declare const policy: string\n")
        result, payload = self.invoke(scope="frontend")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(payload["files"], [])

    def test_comment_and_blank_lines_are_not_counted_as_executable(self):
        self.change(text="# 新说明\n\ndef value():\n    return 1\n")
        result, payload = self.invoke([self.report({"example.py": {3: 1, 4: 1}})])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(payload["total"], 0)

    def test_local_type_annotation_without_assignment_needs_no_runtime_coverage(self):
        self.change(text="def value():\n    result: int\n    return 2\n")
        result, payload = self.invoke([self.report({"example.py": {1: 1, 3: 1}})])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(payload["total"], 1)

    def test_annotated_assignment_still_requires_runtime_coverage(self):
        self.change(text="def value():\n    result: int = 2\n    return result\n")
        result, payload = self.invoke([self.report({"example.py": {1: 1, 2: 0, 3: 1}})])
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(payload["files"][0]["uncovered"], [2])

    def test_scope_declarations_need_no_runtime_coverage(self):
        for declaration in ("global result", "nonlocal result"):
            with self.subTest(declaration=declaration):
                self.change(text="def value():\n    result = 1\n    def update():\n"
                            f"        {declaration}  # 仅声明名称作用域。\n"
                            "        result = 2\n    return update\n")
                report = self.report({"example.py": {1: 1, 2: 1, 3: 1, 5: 1, 6: 1}})
                result, payload = self.invoke([report])
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertTrue(payload["passed"])

    def test_scope_declarations_do_not_hide_same_line_runtime_code(self):
        for statement in ("global result; result = 2", "nonlocal result; result = 2",
                          "if True: global result", "if True: nonlocal result"):
            with self.subTest(statement=statement):
                self.change(text="def value():\n    result = 1\n    def update():\n"
                            f"        {statement}\n    return update\n")
                report = self.report({"example.py": {1: 1, 2: 1, 3: 1, 4: 0, 5: 1}})
                result, payload = self.invoke([report])
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertEqual(payload["files"][0]["uncovered"], [4])

    def test_local_annotation_does_not_hide_same_line_runtime_statement(self):
        for annotation in ("int", "list['中文']"):
            with self.subTest(annotation=annotation):
                self.change(text=f"def value():\n    result: {annotation}; return 2\n")
                result, payload = self.invoke([self.report({"example.py": {1: 1, 2: 0}})])
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertEqual(payload["files"][0]["uncovered"], [2])

    def test_inline_condition_before_local_annotation_still_needs_coverage(self):
        self.change(text="def value():\n    if True: result: int\n    return 2\n")
        result, payload = self.invoke([self.report({"example.py": {1: 1, 2: 0, 3: 1}})])
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(payload["files"][0]["uncovered"], [2])

    def test_frontend_src_relative_report_paths(self):
        self.change("frontend/src/example.ts", "export function value() {\n  return 2\n}\n")
        result, payload = self.invoke([self.report({"src/example.ts": {1: 1, 2: 1, 3: 1}})],
                                      scope="frontend")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((payload["covered"], payload["total"]), (1, 1))

    def test_frontend_multiline_expression_change_requires_statement_execution(self):
        self.write("frontend/src/example.ts", "export function value() {\n  return (\n    1\n  )\n}\n")
        self.commit()
        self.base = self.git("rev-parse", "HEAD").strip()
        self.change("frontend/src/example.ts", "export function value() {\n  return (\n    2\n  )\n}\n")
        report = self.report({"src/example.ts": {2: 0}})
        statements = self.folder / "statements.json"
        statements.write_text(json.dumps({"src/example.ts": {
            "runtimeLines": [1, 2, 3, 4, 5],
            "statementMap": {"0": {"start": {"line": 2, "column": 2},
                                    "end": {"line": 4, "column": None}}}, "s": {"0": 0}
        }}), encoding="utf-8")
        result, payload = self.invoke([report], scope="frontend", statements=statements)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(payload["files"][0]["uncovered"], [3])

    def test_frontend_comment_inside_unexecuted_statement_is_exempt(self):
        original = "export function value() {\n  return build(\n    // old\n    1\n  )\n}\n"
        self.write("frontend/src/example.ts", original)
        self.commit()
        self.base = self.git("rev-parse", "HEAD").strip()
        self.change("frontend/src/example.ts", original.replace("// old", "// 新说明"))
        report = self.report({"src/example.ts": {2: 0}})
        statements = self.folder / "statements.json"
        statements.write_text(json.dumps({"src/example.ts": {
            "runtimeLines": [1, 2, 4, 5, 6],
            "statementMap": {"0": {"start": {"line": 2, "column": 2},
                                    "end": {"line": 5, "column": 3}}}, "s": {"0": 0}
        }}), encoding="utf-8")
        result, payload = self.invoke([report], scope="frontend", statements=statements)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(payload["total"], 0)

    def test_frontend_missing_statement_report_fails_closed(self):
        self.change("frontend/src/example.ts", "export function value() {\n  return 2\n}\n")
        result, _ = self.invoke([self.report({"src/example.ts": {2: 1}})],
                                scope="frontend", statements=self.folder / "missing.json")
        self.assertEqual(result.returncode, 2)

    def test_windows_separators_in_backend_report(self):
        self.change()
        result, _ = self.invoke([self.report({r"backend\app\example.py": {1: 1, 2: 1}})])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_spaces_and_unicode_in_changed_filename(self):
        self.change("backend/app/中文 文件.py", "answer = 5\n")
        result, payload = self.invoke([self.report({"中文 文件.py": {1: 1}})])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(payload["files"][0]["path"], "backend/app/中文 文件.py")

    def test_bad_base_revision_is_not_treated_as_no_changes(self):
        self.change()
        result, payload = self.invoke(base="definitely-not-a-commit")
        self.assertEqual(result.returncode, 2)
        self.assertIn("error", payload)

    def test_working_tree_mode_checks_uncommitted_changes(self):
        self.write("backend/app/example.py", "def value():\n    return 9\n")
        result, payload = self.invoke([self.report({"example.py": {1: 1, 2: 0}})], working=True)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(payload["files"][0]["uncovered"], [2])

    def test_report_missing_changed_line_is_not_silently_accepted(self):
        self.change(text="def value():\n    result = 2\n    return result\n")
        # 报告来自未修改的旧版本，行号尚未覆盖新的文件布局。
        result, payload = self.invoke([self.report({"example.py": {1: 1, 2: 1}})])
        self.assertEqual(result.returncode, 2)
        self.assertIn("error", payload)

    def test_renamed_file_checks_modified_lines_at_new_path(self):
        self.git("mv", "backend/app/example.py", "backend/app/renamed.py")
        self.write("backend/app/renamed.py", "def value():\n    return 2\n")
        self.commit()
        result, payload = self.invoke([self.report({"renamed.py": {1: 1, 2: 0}})])
        self.assertEqual(result.returncode, 1)
        item = next(item for item in payload["files"] if item["path"].endswith("renamed.py"))
        self.assertIn(2, item["uncovered"])

    def test_deletion_only_business_change_requires_file_to_be_executed(self):
        self.write("backend/app/example.py", "def value():\n    answer = 1\n    return answer\n")
        self.commit()
        self.base = self.git("rev-parse", "HEAD").strip()
        self.write("backend/app/example.py", "def value():\n    return answer\n")
        self.commit()
        result, payload = self.invoke([self.report({"example.py": {1: 0, 2: 0}})])
        self.assertEqual(result.returncode, 1)
        self.assertTrue(payload["files"][0]["not_executed"])

    def test_whole_file_deletion_has_no_remaining_lines_to_cover(self):
        self.git("rm", "backend/app/example.py")
        self.commit()
        result, payload = self.invoke()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(payload["files"], [])

    def test_type_checking_else_runtime_logic_is_not_exempt(self):
        self.write("backend/app/example.py", "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from types import SimpleNamespace\nelse:\n    answer = 1\n")
        self.commit()
        self.base = self.git("rev-parse", "HEAD").strip()
        self.change(text="from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from types import SimpleNamespace\nelse:\n    answer = 2\n")
        result, payload = self.invoke([self.report({"example.py": {1: 1, 2: 1, 5: 0}})])
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(payload["files"][0]["uncovered"], [5])

    def test_binary_business_source_is_not_accepted_as_zero_changed_lines(self):
        (self.repo / "backend/app/example.py").write_bytes(b"binary\x00source")
        self.commit()
        result, payload = self.invoke([self.report({"example.py": {1: 1}})])
        self.assertEqual(result.returncode, 2)
        self.assertIn("error", payload)


if __name__ == "__main__":
    unittest.main()

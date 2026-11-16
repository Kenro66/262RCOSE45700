"""JUnit 摘要应排除覆盖率 XML，避免把覆盖数据当作零测试的套件。"""

from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ci_summary import render


class SummaryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)

    def write(self, name, text):
        (self.folder / name).write_text(text, encoding="utf-8")

    def test_coverage_xml_is_not_displayed_as_test_suite(self):
        self.write("coverage-unit.xml", '<coverage><packages/></coverage>')
        self.write("unit.xml", '<testsuites><testsuite tests="4" failures="1" errors="0" skipped="1"/></testsuites>')
        result = render(self.folder)
        self.assertIn("| unit | 4 | 2 | 1 | 0 | 1 |", result)
        self.assertNotIn("| coverage-unit |", result)

    def test_only_coverage_reports_does_not_claim_test_reports_exist(self):
        self.write("coverage-unit.xml", '<coverage><packages/></coverage>')
        self.assertIn("No JUnit reports produced", render(self.folder))

    def test_errors_and_skips_are_not_counted_as_passes(self):
        self.write("postgres.xml", '<testsuite tests="5" failures="1" errors="1" skipped="2"/>')
        self.assertIn("| postgres | 5 | 1 | 1 | 1 | 2 |", render(self.folder))

    def test_empty_folder_is_explicit(self):
        self.assertIn("No JUnit reports produced", render(self.folder))


if __name__ == "__main__":
    unittest.main()

"""将 pytest 的 JUnit 报告转换为 GitHub Actions 任务摘要（不调用 API）。"""

from __future__ import annotations

import sys
from pathlib import Path
import xml.etree.ElementTree as ET


def render(directory: Path) -> str:
    lines = [
        "## Backend test results",
        "",
        "| Suite | Tests | Passed | Failed | Errors | Skipped |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    reports = sorted(directory.glob("*.xml"))
    junit_reports = 0
    for report in reports:
        root = ET.parse(report).getroot()
        # 覆盖率 XML 与 JUnit 同在报告目录，不把它当作零测试的套件。
        if root.tag not in {"testsuite", "testsuites"}:
            continue
        junit_reports += 1
        suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
        counts = {
            key: sum(int(suite.get(key, "0")) for suite in suites)
            for key in ("tests", "failures", "errors", "skipped")
        }
        passed = counts["tests"] - counts["failures"] - counts["errors"] - counts["skipped"]
        lines.append(
            f"| {report.stem} | {counts['tests']} | {passed} | "
            f"{counts['failures']} | {counts['errors']} | {counts['skipped']} |"
        )
    if not junit_reports:
        return "## Backend test results\n\nNo JUnit reports produced. Check dependency/setup logs.\n"
    lines.extend([
        "",
        "These are automated code/integration tests, not real-model answer-quality scores.",
        "Download backend-test-reports for details. Skips are reported, not counted as passes.",
    ])
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    print(render(Path(sys.argv[1])), end="")

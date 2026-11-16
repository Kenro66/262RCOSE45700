"""将 issue57 覆盖包放入完整项目后运行本脚本。

先安装项目依赖和 tests/issue57/requirements-test.txt 中的测试依赖。
用法：python tests/issue57/run_coverage.py --postgres-url <可丢弃测试库地址>
数据库名必须以 issue57_test 开头；测试使用临时模式。
百分之百覆盖率指本次修改的可执行代码行，不代表所有历史代码。
分支覆盖率和完整文件覆盖率另行报告，不排除未覆盖代码。
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


SUITE = [
    "tests/issue57", "tests/test_orchestrator.py", "tests/test_chat.py",
    "tests/test_agent_graph.py", "tests/test_agent_interfaces.py",
    "tests/test_agent_runtime.py", "tests/test_agent_evidence_quality.py",
    "tests/test_retriever.py", "tests/test_storage_repo.py", "tests/test_prompts.py",
    "tests/test_citation.py", "tests/test_evidence_completion.py",
    "tests/test_evidence_completion_crosspage.py", "tests/issue70",
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--postgres-url", default=os.environ.get("ISSUE57_TEST_DATABASE_URL"))
    parser.add_argument("--report-dir", type=Path)
    parser.add_argument("--report-only", action="store_true", help="Analyze existing .coverage; does not establish tests passed")
    options = parser.parse_args()
    folder = Path(__file__).resolve().parent
    backend = folder.parents[1]
    if not (backend / "app/contracts.py").exists():
        parser.error("This is an overlay: copy backend/ and storage/ into the complete #68 + #65 project first.")
    if not options.postgres_url and not options.report_only:
        parser.error("Set --postgres-url or ISSUE57_TEST_DATABASE_URL to a disposable issue57_test* database.")
    report_dir = (options.report_dir or backend / "coverage-issue57").resolve()
    report_dir.mkdir(parents=True, exist_ok=True)
    changed = json.loads((folder / "changed_lines.json").read_text())
    hashes = json.loads((folder / "source_hashes.json").read_text())
    for filename, expected in hashes.items():
        actual = hashlib.sha256((backend / filename).read_bytes().replace(b"\r\n", b"\n")).hexdigest()
        if actual != expected:
            parser.error(f"Source changed since the measured implementation: {filename}. Refresh the diff manifest before claiming coverage.")
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
        PYTHONUTF8="1", DATABASE_URL="sqlite+aiosqlite:///:memory:", RAG_MODE="agent",
        RAG_EXECUTION_TIMEOUT_SECONDS="120")
    if options.postgres_url:
        environment["ISSUE57_TEST_DATABASE_URL"] = options.postgres_url
    # 本测试集单独显式启用 PostgreSQL，不继承其他测试库的启用配置。
    for key in ("ISSUE7_TEST_DATABASE_URL", "ISSUE9_TEST_DATABASE_URL", "ISSUE9_ALLOW_DATABASE_RESET"):
        environment.pop(key, None)
    if not options.report_only:
        command = [sys.executable, "-B", "-m", "coverage", "run", "--branch", "--source=app",
            "-m", "pytest", "-p", "no:cacheprovider", "-p", "pytest_asyncio.plugin", *SUITE, "-q", "--tb=short"]
        run = subprocess.run(command, cwd=backend, env=environment, text=True, encoding="utf-8",
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        (report_dir / "test-results.txt").write_text(run.stdout, encoding="utf-8")
        print(run.stdout)
        if run.returncode:
            return run.returncode
    import coverage
    measured = coverage.Coverage(data_file=str(backend / ".coverage"))
    measured.load()
    # 不通过覆盖率忽略指令或自定义排除规则提高结果。
    measured.set_option("report:exclude_lines", [])
    measured.json_report(outfile=str(report_dir / "coverage.json"))
    raw = json.loads((report_dir / "coverage.json").read_text())
    files = {name.replace("\\", "/"): value for name, value in raw["files"].items()}
    rows = []
    for name, lines in changed.items():
        entry = next((value for path, value in files.items()
                      if path == name or path.endswith("/backend/" + name)), None)
        if entry is None:
            raise RuntimeError(f"Unmeasured changed file: {name}")
        executed, missing = set(entry["executed_lines"]), set(entry["missing_lines"])
        relevant = set(lines) & (executed | missing)
        missed = sorted(relevant & missing)
        covered_branches = [arc for arc in entry["executed_branches"] if arc[0] in relevant]
        missing_branches = [arc for arc in entry["missing_branches"] if arc[0] in relevant]
        rows.append(dict(file=name, changed_executable_lines=len(relevant), covered_lines=len(relevant) - len(missed),
            missing_lines=missed, covered_branches=len(covered_branches), missing_branches=missing_branches))
    total = sum(row["changed_executable_lines"] for row in rows)
    covered = sum(row["covered_lines"] for row in rows)
    branches = sum(row["covered_branches"] for row in rows)
    missed_branches = sum(len(row["missing_branches"]) for row in rows)
    result = dict(scope="All added/replacement executable lines in the 18 delivered Python files",
        base={"default": "302f5bb47004fde667a40cc2b32aa9367da9a1c2", "agent_nodes_and_service": "600a19cfd84d4478a10372ceb62f81962e9a2fb5"}, implementation="source_hashes.json", tests_rerun=not options.report_only,
        covered_lines=covered, total_lines=total, line_percent=100 * covered / total,
        covered_branches=branches, total_branches=branches + missed_branches,
        branch_percent=100 * branches / (branches + missed_branches), files=rows,
        branch_note="Scope is this feature beyond its dependencies: #68 for normal files and #65 for Agent nodes/service. Whole-file coverage is also reported; no coverage exclusions are used.")
    (report_dir / "changed-coverage.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with (report_dir / "whole-file-coverage.txt").open("w", encoding="utf-8") as output:
        measured.report(morfs=[str(backend / name) for name in changed], file=output, show_missing=True)
    print(f"Changed lines: {covered}/{total} ({result['line_percent']:.2f}%)")
    print(f"Changed branches: {branches}/{branches + missed_branches} ({result['branch_percent']:.2f}%)")
    print(f"Reports: {report_dir}")
    return 0 if covered == total else 1


if __name__ == "__main__":
    raise SystemExit(main())

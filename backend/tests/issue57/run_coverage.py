"""Run from a complete project after applying the issue57 overlay.

Install the project dependencies and tests/issue57/requirements-test.txt first.
Usage: python tests/issue57/run_coverage.py --postgres-url <disposable test URL>
The database name must start with issue57_test; tests use temporary schemas.
100% means changed executable Python lines, not all historical project code.
Branch coverage and whole-file coverage are reported separately, without exclusions.
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
    # The selected suite has its own explicit PostgreSQL opt-in; never inherit unrelated DB opt-ins.
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
    # No pragma or custom exclude patterns are used to inflate the result.
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
        base="a51a98e", implementation="1097077", tests_rerun=not options.report_only,
        covered_lines=covered, total_lines=total, line_percent=100 * covered / total,
        covered_branches=branches, total_branches=branches + missed_branches,
        branch_percent=100 * branches / (branches + missed_branches), files=rows,
        branch_note="service.py observe() has a redundant callback-null check; the callback is installed only when non-null. No production code was changed to inflate coverage.")
    (report_dir / "changed-coverage.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with (report_dir / "whole-file-coverage.txt").open("w", encoding="utf-8") as output:
        measured.report(morfs=[str(backend / name) for name in changed], file=output, show_missing=True)
    print(f"Changed lines: {covered}/{total} ({result['line_percent']:.2f}%)")
    print(f"Changed branches: {branches}/{branches + missed_branches} ({result['branch_percent']:.2f}%)")
    print(f"Reports: {report_dir}")
    return 0 if covered == total else 1


if __name__ == "__main__":
    raise SystemExit(main())

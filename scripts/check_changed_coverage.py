"""按 PR 差异核对真实测试覆盖：新增或修改的业务可执行行必须全部覆盖。"""
from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass, field
import io
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import tokenize
import xml.etree.ElementTree as ET

SCOPES = {"backend": "backend/app", "frontend": "frontend/src"}
HUNK = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


class GateError(ValueError):
    """无法可靠检查时必须失败，不能默认为零改动。"""


@dataclass
class Change:
    path: str
    old_path: str
    added: set[int] = field(default_factory=set)
    deleted: set[int] = field(default_factory=set)
    binary: bool = False


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-c", "core.safecrlf=false", *args], cwd=repo,
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode:
        raise GateError("Git 无法读取指定差异：" + result.stderr.strip())
    return result.stdout


def resolve_commit(repo: Path, revision: str) -> str:
    return git(repo, "rev-parse", "--verify", "--end-of-options", revision + "^{commit}").strip()


def business_file(path: str, scope: str) -> bool:
    if not path.startswith(SCOPES[scope] + "/") or ".." in PurePosixPath(path).parts:
        return False
    return path.endswith(".py") if scope == "backend" else (
        path.endswith((".ts", ".tsx", ".js", ".jsx")) and not path.endswith(".d.ts")
    )


def changes_between(repo: Path, base: str, head: str | None, scope: str) -> list[Change]:
    refs = [base] + ([head] if head else [])
    options = ["--no-ext-diff", "--no-textconv", "--find-renames"]
    names = git(repo, "diff", *options, "--name-status", "-z", "--diff-filter=ACMR",
                *refs, "--").split("\0")
    changes, index = [], 0
    while index < len(names) and names[index]:
        status, old = names[index:index + 2]
        index += 2
        path = old
        if status[0] in ("R", "C"):
            path = names[index]
            index += 1
        if not business_file(path, scope):
            continue
        change = Change(path, old)
        patch = git(repo, "diff", *options, "--unified=0", *refs,
                    "--", *dict.fromkeys([old, path]))
        old_line = new_line = None
        for line in patch.splitlines():
            match = HUNK.match(line)
            if match:
                old_line, new_line = map(int, match.groups())
            elif line.startswith(("Binary files ", "GIT binary patch")):
                change.binary = True
            elif line.startswith("diff --git "):
                old_line = new_line = None
            elif old_line is not None:
                if line.startswith("+"):
                    change.added.add(new_line)
                    new_line += 1
                elif line.startswith("-"):
                    change.deleted.add(old_line)
                    old_line += 1
                elif line.startswith(" "):
                    old_line += 1
                    new_line += 1
        changes.append(change)
    return changes


def report_path(filename: str, repo: Path, scope: str) -> str:
    raw = filename.replace("\\", "/")
    if not raw or ".." in PurePosixPath(raw).parts:
        raise GateError("覆盖报告包含非法路径：" + filename)
    prefix = SCOPES[scope]
    if raw.startswith(prefix + "/"):
        return raw
    marker = "/" + prefix + "/"
    if marker in raw:
        return prefix + "/" + raw.split(marker, 1)[1]
    if raw.startswith("/") or re.match(r"^[A-Za-z]:/", raw):
        root = repo.as_posix().rstrip("/") + "/"
        if raw.startswith(root):
            return raw[len(root):]
        raise GateError("报告中的文件不在业务源码范围内：" + filename)
    short = prefix.split("/", 1)[1]
    return prefix.split("/", 1)[0] + "/" + raw if raw.startswith(short + "/") else prefix + "/" + raw


def read_reports(paths: list[Path], repo: Path, scope: str) -> dict[str, dict[int, int]]:
    if not paths:
        raise GateError("业务代码发生变化，但没有提供覆盖报告")
    coverage: dict[str, dict[int, int]] = {}
    for path in paths:
        try:
            root = ET.parse(path).getroot()
        except (OSError, ET.ParseError) as exc:
            raise GateError(f"覆盖报告缺失或损坏：{path}：{exc}") from exc
        if root.tag != "coverage":
            raise GateError(f"报告不是 Cobertura 覆盖报告：{path}")
        for item in root.findall(".//class"):
            filename = report_path(item.get("filename", ""), repo, scope)
            lines = coverage.setdefault(filename, {})
            for line in item.findall("./lines/line"):
                try:
                    number, hits = int(line.attrib["number"]), int(line.attrib["hits"])
                except (KeyError, ValueError) as exc:
                    raise GateError(f"报告行号或命中次数无效：{path}") from exc
                if number <= 0 or hits < 0:
                    raise GateError(f"报告行号或命中次数无效：{path}")
                lines[number] = max(lines.get(number, 0), hits)
    return coverage


def read_statements(path: Path | None, repo: Path) -> dict:
    """保留 Istanbul 语句范围，防止多行表达式只改中间一行时漏检。"""
    try:
        if path is None:
            raise GateError("前端业务变化必须提供 Istanbul JSON 语句覆盖报告")
        raw = json.loads(path.read_text(encoding="utf-8"))
        result = {}
        for filename, data in raw.items():
            spans = []
            runtime = data["runtimeLines"]
            if not isinstance(runtime, list) or any(type(number) is not int or number <= 0 for number in runtime):
                raise ValueError("前端运行时代码行号无效")
            mapping, hits = data["statementMap"], data["s"]
            if mapping.keys() != hits.keys():
                raise ValueError("语句范围与计数不一致")
            for key, location in mapping.items():
                start, end = location["start"]["line"], location["end"]["line"]
                count = hits[key]
                if not all(type(value) is int for value in (start, end, count)) or start < 1 or end < start or count < 0:
                    raise ValueError("语句范围或计数无效")
                # V8 对没有精确末列的语句使用 null；保留行范围即可。
                end_column, start_column = location["end"]["column"], location["start"]["column"]
                columns = end_column - start_column if isinstance(end_column, int) and isinstance(start_column, int) else 10**9
                width = (end - start, columns)
                spans.append((start, end, width, count))
            result[report_path(filename, repo, "frontend")] = (spans, set(runtime))
        return result
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise GateError("前端语句覆盖报告缺失或损坏：" + str(exc)) from exc


def source_text(repo: Path, path: str, revision: str | None) -> str:
    if revision:
        return git(repo, "show", revision + ":" + path)
    file = (repo / path).resolve()
    if not file.is_relative_to(repo.resolve()):
        raise GateError("业务源码不能指向仓库外：" + path)
    return file.read_text(encoding="utf-8")


def python_layout(text: str):
    """多行表达式归属到语句；注释、文档字符串、纯类型检查块不算运行逻辑。"""
    try:
        tree = ast.parse(text)
        tokens = tokenize.generate_tokens(io.StringIO(text).readline)
        code = {line for token in tokens
                if token.type not in (tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE,
                                      tokenize.INDENT, tokenize.DEDENT, tokenize.ENDMARKER)
                for line in range(token.start[0], token.end[0] + 1)}
    except (SyntaxError, tokenize.TokenError, IndentationError) as exc:
        raise GateError("无法分析 Python 业务源码：" + str(exc)) from exc
    typing_names = {alias.asname or alias.name for node in ast.walk(tree)
                    if isinstance(node, ast.Import) for alias in node.names if alias.name == "typing"}
    checking_names = {alias.asname or alias.name for node in ast.walk(tree)
                      if isinstance(node, ast.ImportFrom) and node.module == "typing"
                      for alias in node.names if alias.name == "TYPE_CHECKING"}
    ignored, statements = set(), []
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    source_lines = [line.encode("utf-8") for line in text.splitlines()]
    for node in ast.walk(tree):
        # 局部变量只声明类型、不赋值时不会执行，也不应要求覆盖命中。
        if isinstance(node, ast.AnnAssign) and node.value is None and isinstance(node.target, ast.Name):
            scope = parents.get(node)
            while scope is not None and not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
                scope = parents.get(scope)
            # AST 列号按 UTF-8 字节计；同一行的条件、分号后代码不能一并豁免。
            before = source_lines[node.lineno - 1][:node.col_offset].strip()
            after = source_lines[node.end_lineno - 1][node.end_col_offset:].lstrip()
            if (isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)) and not before
                    and (not after or after.startswith(b"#"))):
                ignored.update(range(node.lineno, node.end_lineno + 1))
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            ignored.update(range(node.lineno, node.end_lineno + 1))
        if isinstance(node, ast.If) and (
            isinstance(node.test, ast.Name) and node.test.id in checking_names
            or isinstance(node.test, ast.Attribute) and node.test.attr == "TYPE_CHECKING"
            and isinstance(node.test.value, ast.Name) and node.test.value.id in typing_names
        ):
            # else 分支仍会运行，绝不能与纯类型检查块一起排除。
            ignored.update(range(node.lineno, node.body[-1].end_lineno + 1))
        if isinstance(node, ast.stmt):
            statements.append((node.lineno, node.end_lineno))
    return code - ignored, statements


def evaluate(repo, scope, base, head, changes, coverage, statements=None):
    files = []
    for change in changes:
        if change.binary:
            raise GateError("业务源码被识别为二进制文件：" + change.path)
        item = {"path": change.path, "changed": sorted(change.added), "uncovered": [],
                "missing": change.path not in coverage, "not_executed": False,
                "covered": 0, "total": 0}
        files.append(item)
        if item["missing"]:
            continue
        lines = coverage[change.path]
        targets = {}
        if scope == "backend":
            code, statements = python_layout(source_text(repo, change.path, head))
            for number in sorted(change.added & code):
                target = number
                if target not in lines:
                    parents = [(end - start, start) for start, end in statements
                               if start <= number <= end]
                    if not parents:
                        continue
                    target = min(parents)[1]
                if target not in lines:
                    raise GateError(f"修改的 Python 语句未出现在覆盖报告中：{change.path}:{number}")
                targets[number] = target
            old_code = set()
            if change.deleted and not change.added:
                old_code, _ = python_layout(source_text(repo, change.old_path, base))
            deletion_only = bool(change.deleted & old_code)
        else:
            if change.path not in statements:
                raise GateError("前端业务文件缺少语句覆盖记录：" + change.path)
            spans, runtime = statements[change.path]
            for number in change.added & runtime:
                parents = [(width, count) for start, end, width, count in spans
                           if start <= number <= end]
                if parents:
                    width = min(pair[0] for pair in parents)
                    # 同等最小范围的语句全部执行才算覆盖。
                    targets[number] = number
                    lines[number] = min(count for size, count in parents if size == width)
                elif number in lines:
                    targets[number] = number
            deletion_only = bool(change.deleted) and not change.added
        for number, target in targets.items():
            item["total"] += 1
            if lines[target] > 0:
                item["covered"] += 1
            else:
                item["uncovered"].append(number)
        if deletion_only and lines and not any(lines.values()):
            item["not_executed"] = True
    return {"scope": scope, "base": base, "head": head or "working-tree", "files": files,
            "covered": sum(item["covered"] for item in files),
            "total": sum(item["total"] for item in files),
            "passed": all(not item["missing"] and not item["uncovered"] and not item["not_executed"]
                          for item in files)}


def escape_annotation(value: str, *, property_value: bool = False) -> str:
    value = value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return value.replace(":", "%3A").replace(",", "%2C") if property_value else value


def display(result: dict) -> None:
    for item in result["files"]:
        path = escape_annotation(item["path"], property_value=True)
        if item["missing"]:
            print(f"::error file={path}::业务文件缺少覆盖记录，不能合并")
        elif item["not_executed"]:
            print(f"::error file={path}::仅删除代码的业务文件未被测试执行，不能合并")
        for number in item["uncovered"]:
            print(f"::error file={path},line={number}::新增或修改业务行未被测试覆盖，不能合并")
    print(f"改动行覆盖：{result['covered']}/{result['total']}；要求全部覆盖。"
          if result["files"] else "没有此范围内的业务代码变化，不要求新增测试。")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--scope", choices=SCOPES, required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", help="CI 使用被测试的 HEAD；省略时检查本地工作区")
    parser.add_argument("--report", type=Path, action="append", default=[])
    parser.add_argument("--statements", type=Path, help="前端 Istanbul JSON 语句报告")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        repo = args.repo.resolve()
        base = resolve_commit(repo, args.base)
        head = resolve_commit(repo, args.head) if args.head else None
        changes = changes_between(repo, base, head, args.scope)
        reports = read_reports(args.report, repo, args.scope) if changes else {}
        statements = read_statements(args.statements, repo) if changes and args.scope == "frontend" else {}
        result = evaluate(repo, args.scope, base, head, changes, reports, statements)
        display(result)
        code = 0 if result["passed"] else 1
    except (GateError, OSError, ValueError) as exc:
        result = {"scope": args.scope, "passed": False, "error": str(exc)}
        print("::error::" + escape_annotation(str(exc)))
        code = 2
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return code


if __name__ == "__main__":
    raise SystemExit(main())

import fs from 'node:fs'
import path from 'node:path'
import assert from 'node:assert/strict'
import ts from 'typescript'

// 使用语法树去掉注释、空白和只用于类型检查的节点。
function runtimeLines(filename, text) {
  const source = ts.createSourceFile(filename, text, ts.ScriptTarget.Latest, true)
  if (source.parseDiagnostics.length) throw new Error(`无法解析业务源码：${filename}`)
  const lines = new Set()
  function visit(node) {
    // 类继承表达式会执行；接口继承和类型参数则不会执行。
    if (ts.isExpressionWithTypeArguments(node) &&
        node.parent?.token === ts.SyntaxKind.ExtendsKeyword &&
        (ts.isClassDeclaration(node.parent.parent) || ts.isClassExpression(node.parent.parent))) {
      visit(node.expression)
      return
    }
    if (ts.isTypeNode(node) || ts.isInterfaceDeclaration(node) || ts.isTypeAliasDeclaration(node) ||
        ts.isTypeParameterDeclaration(node) ||
        (ts.isImportDeclaration(node) && node.importClause?.isTypeOnly) ||
        (ts.isImportClause(node) && node.isTypeOnly) ||
        (ts.isExportDeclaration(node) && node.isTypeOnly) ||
        (ts.isImportSpecifier(node) && node.isTypeOnly) ||
        (ts.isExportSpecifier(node) && node.isTypeOnly) ||
        (ts.isFunctionDeclaration(node) && !node.body) ||
        (ts.canHaveModifiers(node) && ts.getModifiers(node)?.some(m => m.kind === ts.SyntaxKind.DeclareKeyword))) return
    const children = node.getChildren(source)
    if (children.length) { children.forEach(visit); return }
    if (node.kind === ts.SyntaxKind.EndOfFileToken || node.kind === ts.SyntaxKind.JSDocComment ||
        (node.kind === ts.SyntaxKind.JsxText && !node.getText(source).trim())) return
    const start = node.getStart(source), end = node.getEnd()
    if (end <= start) return
    const first = source.getLineAndCharacterOfPosition(start).line + 1
    const last = source.getLineAndCharacterOfPosition(end - 1).line + 1
    for (let number = first; number <= last; number++) lines.add(number)
  }
  visit(source)
  return [...lines].sort((a, b) => a - b)
}

if (process.argv.includes('--self-test')) {
  const text = 'function value(): number {\n  return build(\n    // 解释\n\n    2\n  )\n}\ninterface OnlyType { name: string }\n'
  const lines = runtimeLines('example.ts', text)
  assert(lines.includes(5))
  assert(!lines.includes(3))
  assert(!lines.includes(4))
  assert(!lines.includes(8))
  assert.deepEqual(runtimeLines('example.ts', 'import type { X } from "x"\ntype Y = string\n'), [])
  assert(runtimeLines('example.ts', 'class Derived extends factory(\n  2,\n) {}').includes(2))
  assert(runtimeLines('view.tsx', 'const view = <div>内容</div>').includes(1))
  console.log('前端语法分析自检通过')
} else {
  const reportPath = path.resolve('coverage/coverage-final.json')
  const report = JSON.parse(fs.readFileSync(reportPath, 'utf8'))
  const sourceRoot = path.resolve('src') + path.sep
  for (const [filename, entry] of Object.entries(report)) {
    const resolved = path.resolve(filename)
    if (!resolved.startsWith(sourceRoot)) throw new Error(`报告路径超出 src：${filename}`)
    entry.runtimeLines = runtimeLines(filename, fs.readFileSync(resolved, 'utf8'))
  }
  fs.writeFileSync(reportPath, JSON.stringify(report))
}

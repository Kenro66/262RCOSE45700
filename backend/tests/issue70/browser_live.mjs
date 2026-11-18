// 可选真实页面联调，不属于 Vitest/pytest 单元集；模型边界见配套测试服务。
import assert from 'node:assert/strict'
import { mkdir, writeFile } from 'node:fs/promises'
import { spawn } from 'node:child_process'
import path from 'node:path'

const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright')
const frontend = process.env.ISSUE68_FRONTEND_URL || 'http://127.0.0.1:5177'
const backend = 'http://127.0.0.1:8000'
const output = process.env.ISSUE68_REPORT_DIR || './issue68-live-results'
await mkdir(output, { recursive: true })
const results = []
const browser = await chromium.launch({ headless: true, args: ['--no-proxy-server'] })
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } })
page.setDefaultTimeout(10000)
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms))
async function api(route, body) {
  const response = await fetch(backend + route, body === undefined ? undefined : {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  })
  return { status: response.status, body: await response.json() }
}
async function until(check) {
  const deadline = Date.now() + 12000
  while (Date.now() < deadline) {
    if (await check()) return
    await sleep(100)
  }
  throw new Error('联调条件未在 12 秒内满足')
}
const state = async (first) => (await api('/__test/state?question_id=' + first.question_id + '&session_id=' + first.session_id)).body
const followup = first => ({ message: first.message, session_id: first.session_id, question_id: first.question_id, conditions: { destination_country: 'CN' } })
const execution = first => '/api/chat/sessions/' + first.session_id + '/questions/' + first.question_id + '/execution'
const form = () => page.locator('.agent-followup:visible').last()
const navChat = () => page.locator('.nav-item').filter({ hasText: 'Chat' }).click()
async function ask(question, fresh = true) {
  if (fresh) await page.locator('.new-conversation').click()
  const pending = page.waitForResponse(r => r.request().method() === 'POST' && new URL(r.url()).pathname === '/api/chat')
  await page.locator('textarea:visible').fill(question)
  await page.locator('textarea:visible').press('Enter')
  const result = await (await pending).json()
  assert.ok(result.question_id)
  return { ...result, message: question }
}
async function submit(first) {
  await form().locator('select').selectOption('CN')
  const pending = page.waitForResponse(r => r.request().method() === 'POST' && new URL(r.url()).pathname === '/api/chat')
  await form().getByRole('button', { name: '补充并继续', exact: true }).click()
  return pending
}
function citation(result) {
  assert.equal(result.status, 'ANSWERED')
  assert.deepEqual(result.citations, [{ evidence_id: 'E1', filename: 'live-policy.pdf', page: 2, quote: '物品需要申报。' }])
}
try {
  await page.goto(frontend)
  await page.getByRole('button', { name: '中文', exact: true }).click()
  await navChat()
  await until(async () => (await page.locator('textarea:visible').count()) === 1)
  const direct = await ask('直接回答：物品有什么规定？', false)
  citation(direct)
  results.push({ case: '首次问答及 Citation', passed: true, state: (await state(direct)).state })

  const first = await ask('恢复追问：物品有什么规定？')
  assert.equal(first.status, 'NEEDS_CLARIFICATION')
  await page.reload()
  await navChat()
  await form().locator('select').waitFor()
  const before = await state(first)
  assert.equal(before.state.clarification_count, 1)
  results.push({ case: '刷新恢复等待追问及原 ID', passed: true, question_id: first.question_id })

  assert.ok(process.env.ISSUE68_RESTART_SCRIPT, '需提供启动后端的 WSL 脚本以验证真实进程重启')
  await api('/__test/stop', {})
  await sleep(1000)
  const child = spawn('wsl.exe', ['-d', 'Ubuntu', '--', 'bash', process.env.ISSUE68_RESTART_SCRIPT], { stdio: 'ignore', windowsHide: true })
  child.unref()
  await until(async () => { try { return (await state(first)).pid !== before.pid } catch { return false } })
  await page.reload()
  await navChat()
  await form().locator('select').waitFor()
  assert.equal((await state(first)).state.question_id, first.question_id)
  results.push({ case: '后端真实进程重启后恢复等待追问', passed: true, previous_pid: before.pid, current_pid: (await state(first)).pid })

  await api('/__test/control', { block: true })
  const pending = submit(first)
  await until(async () => (await state(first)).entered)
  const baseline = (await state(first)).embedding_calls
  const duplicate = await api('/api/chat?wait=false', followup(first))
  assert.equal(duplicate.status, 202)
  assert.equal(duplicate.body.question_id, first.question_id)
  assert.equal(await form().getByRole('button', { name: '补充并继续', exact: true }).isDisabled(), true)
  await page.locator('.nav-item').filter({ hasText: 'Documents' }).click()
  assert.equal((await api(execution(first))).body.execution_status, 'RUNNING')
  await navChat()
  await page.locator('.new-conversation').click()
  assert.equal((await api(execution(first))).body.execution_status, 'RUNNING')
  await api('/__test/control', { block: false })
  citation(await (await pending).json())
  assert.equal((await state(first)).embedding_calls, baseline)
  assert.equal((await state(first)).state.retrieval_count, 1)
  await page.locator('.history-item').filter({ hasText: '恢复追问' }).click()
  await page.getByText('物品需要申报。', { exact: false }).first().waitFor()
  await page.screenshot({ path: path.join(output, 'restored-answer.png'), fullPage: true })
  results.push({ case: '追问、重复提交、切文件页及其他对话续跑', passed: true, state: (await state(first)).state })

  const cancelled = await ask('取消重提：物品有什么规定？')
  await api('/__test/control', { block: true })
  const cancelledRequest = submit(cancelled).catch(() => null)
  await until(async () => (await state(cancelled)).entered)
  await form().getByRole('button', { name: '取消', exact: true }).click()
  await until(async () => (await api(execution(cancelled))).body.execution_status === 'CANCELLING')
  assert.equal(await form().locator('select').isDisabled(), true)
  await api('/__test/control', { block: false })
  await until(async () => { const x = (await api(execution(cancelled))).body; return x.execution_status === 'CANCELLED' && x.retryable })
  await until(async () => !(await form().locator('select').isDisabled()))
  await form().locator('select').selectOption('KR')
  const resumed = page.waitForResponse(r => r.request().method() === 'POST' && new URL(r.url()).pathname === '/api/chat')
  await form().getByRole('button', { name: '补充并继续', exact: true }).click()
  const resumedBody = await (await resumed).json()
  citation(resumedBody)
  assert.equal(resumedBody.question_id, cancelled.question_id)
  const saved = (await state(cancelled)).state
  assert.equal(saved.conditions.destination_country, 'KR')
  assert.equal(saved.retrieval_count, 2)
  assert.equal(saved.clarification_count, 1)
  void cancelledRequest
  results.push({ case: '取消确认后修改条件、同一问题重提及累计预算', passed: true, state: saved })

  const deleted = await ask('删除迟到：物品有什么规定？')
  await api('/__test/control', { block: true })
  const late = submit(deleted).catch(() => null)
  await until(async () => (await state(deleted)).entered)
  await page.getByRole('button', { name: /^删除对话 删除迟到/ }).click()
  await page.getByRole('button', { name: '确认删除', exact: true }).click()
  await until(async () => (await state(deleted)).deleted === true)
  await api('/__test/control', { block: false })
  await sleep(700)
  const tombstone = await state(deleted)
  assert.equal(tombstone.state, null)
  assert.equal(tombstone.submissions, 0)
  assert.equal((await api(execution(deleted))).status, 404)
  assert.equal(await page.locator('.history-item').filter({ hasText: '删除迟到' }).count(), 0)
  void late
  results.push({ case: '删除后迟到结果不回写、不复活会话', passed: true, deleted: true })

  const budget = await ask('三次检索：物品有什么规定？')
  citation(await (await (await submit(budget))).json())
  const budgetState = (await state(budget)).state
  assert.equal(budgetState.retrieval_count, 3)
  assert.equal(budgetState.clarification_count, 1)
  assert.equal(budgetState.evidence[0].evidence_id, 'E1')
  results.push({ case: '累计三次检索、一轮追问、原条件及稳定 evidence_id', passed: true, state: budgetState })
  await page.screenshot({ path: path.join(output, 'budget-answer.png'), fullPage: true })
} catch (error) {
  await page.screenshot({ path: path.join(output, 'failure.png'), fullPage: true })
  results.push({ passed: false, error: String(error), stack: error.stack })
  process.exitCode = 1
} finally {
  await writeFile(path.join(output, 'results.json'), JSON.stringify(results, null, 2))
  console.log(JSON.stringify(results.map(({ state, ...value }) => value), null, 2))
  await browser.close()
}

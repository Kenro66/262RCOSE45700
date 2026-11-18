# #68 模式配置与 PostgreSQL / #61 联调

## 配置与兼容范围

在 `backend/.env` 或进程环境配置 `RAG_MODE`，进程环境优先。启动时读取，修改后重启后端。

| 配置 | 行为 |
| --- | --- |
| 未配置 | 原 Single RAG；保持原 `POST /api/chat` 请求及 C5 响应 |
| `RAG_MODE=single` | 与未配置相同，复用原 `answer_question` 和 Single router |
| `RAG_MODE=agent` | 持久化 Agent；启用会话列表、详情、删除、取消及执行状态查询 |
| 空值、空格、`AGENT` 或其他值 | 创建应用时抛出包含合法值的 `ValueError`，启动失败 |

已有 `create_app(answer_fn=...)` 注入入口保持兼容，合法模式下仍使用旧 router。
模式配置使用现有 `settings()`，不新增 HTTP 字段或接口。Single 不提供 Agent 的持久化会话接口；前端的完整追问恢复联调需显式启用 Agent。

Agent 部署前，在独立备份及正常迁移流程下执行 `storage/migrations/070_chat_persistence.sql`。
该脚本只用于首次初始化，不会推测性地改造已部署过的其他表结构。已有本版三表无需重复迁移。
随后按项目原有方式设置 `DATABASE_URL`、生成与嵌入配置并启动后端，例如：

```sh
cd backend
uvicorn app.main:app --env-file .env --host 127.0.0.1 --port 8000
```

本 PR 不实现自动 Single Fallback、执行时限或运行中任务重启接管。#74 同步后应沿用这里的默认 Single 及合法模式策略。

## 2026-11-18 实测环境

- #68 起点 `cdfaf8a`，同步 main `34bea0d`；保留已合并 #61、#65、#71、#75 的最终代码。
- #61 前端来自该 main，使用正式 `/` 页面，未开启 `?mock=1`；Chromium 143（Playwright 1.57）、React/Vite。
- Python 3.12、真实 HTTP 后端、真实 Agent/LangGraph、pgvector/PostgreSQL 18；最终复核使用专用空库 `issue68_test_live_final`，端口 55470，不使用正式库。
- 先实际执行 070 SQL 迁移；生成 `live-policy.pdf` 的测试文档记录与真实 1024 维向量，检索、元数据查询和状态保存走实际数据库。
- 只将生成模型及嵌入模型替换为确定性测试实现，用于精确控制追问、检索屏障和三次检索。此记录验证接入、持久化、预算和引用链路，不宣称真实生成模型/NPU 的回答质量或线上部署已验证。

## 实测结果

| 场景 | 实际结果 |
| --- | --- |
| 首次问答 | 正式页面发送原问题，得到 `ANSWERED`；引用为 `E1 / live-policy.pdf / PDF p.2 / 物品需要申报。`，侧栏显示一致 |
| 追问与刷新 | 首次得到 `NEEDS_CLARIFICATION`，刷新后从后端恢复同一 `session_id/question_id`、原问题和追问表单；`clarification_count=1` |
| 后端重启后恢复 | 在等待追问阶段真实退出后端进程并重新启动，最终复核 PID 从 449 变为 1132；刷新页面仍恢复原追问，数据库未清空 |
| 重复提交 | UI 发送期间禁用提交；另发送同题、同条件的网络重复请求，得到 202 并加入原执行；实际嵌入/检索只执行一次 |
| 切文件页与其他对话 | 检索阻塞时切 Documents、返回 Chat 并新建另一对话，原执行保持 RUNNING；释放后原页面请求收到答案，切回原对话显示引用 |
| 取消确认后重提 | 发取消后先保持 CANCELLING，表单仍禁用；当前调用退出并保存状态后为 CANCELLED/retryable=true，才可把 CN 改为 KR 并沿用原 question_id 提交；累计检索 2 次、追问 1 轮 |
| 删除与迟到结果 | 检索期间从前端确认删除会话，再释放旧调用；数据库保留 deleted 墓碑，原问题和执行记录均不存在，查询 404，前端无复活对话 |
| 累计预算与 Citation | 通过原 Agent 的改写循环实际检索 3 次后回答；保存 `retrieval_count=3 / clarification_count=1`，原条件 CN 和证据 E1 不丢失；答案及引用侧栏正确 |

浏览器驱动包含 7 组断言（部分组覆盖多个场景），全部通过。追问上限仍为一轮；不以两轮追问作为本版本验收目标。

## 复现联调

先在独立 pgvector/PostgreSQL 18 中创建名称以 `issue68_test` 开头的测试库，执行 070 迁移。安装项目后端及前端锁定依赖；Playwright 仅为可选联调工具，无需加入正式依赖。

```sh
cd backend
export PYTHONPATH=.
export ISSUE68_LIVE_DATABASE_URL=postgresql+asyncpg://rag:TEST_PASSWORD@127.0.0.1:55470/issue68_test_live
export RAG_MODE=agent
python tests/issue70/live_frontend_server.py
```

另一个终端在 frontend 执行 `npm run dev -- --host 127.0.0.1 --port 5177`。
浏览器脚本读取 `PLAYWRIGHT_MODULE`（可选 Playwright 模块路径）、`ISSUE68_FRONTEND_URL`、`ISSUE68_REPORT_DIR`，以及本机 WSL 中重新启动上述后端的 `ISSUE68_RESTART_SCRIPT`。使用本机 Node 执行：

```sh
node backend/tests/issue70/browser_live.mjs
```

脚本生成 `results.json` 和页面截图，含完整预算、ID 与证据状态。测试控制端点只存在于专用测试服务，生产 `app.main:app` 不注册它们。测试服务拒绝未明确指定 `issue68_test*` 数据库的启动。

## 自动检查

最终本地检查：SQLite 636 项通过、2 项 PostgreSQL 用例在该阶段跳过后于独立步骤通过；PostgreSQL 端到端 1 项、数据层 32 项、持久化 2 项，共 35 项通过。前端 50 项、CI 检查工具 37 项通过；语法、Ruff、mypy、actionlint 通过。整个 PR 相对 main 的后端改动执行行覆盖 566/566（100%）。

- 模式测试覆盖缺省、显式 single/agent、非法配置、进程启动、环境优先级以及旧注入入口。
- 原 Single 回归通过实际配置入口测试原检索、编排、数据库文件名及引用；Agent 使用显式配置。
- #65 可能把停止异常重新包装为共享依赖异常；接入层保留自身明确的停止信号，按取消处理并保存可信快照，普通技术失败仍按原错误处理。
- CI 单独运行真实 PostgreSQL 的迁移、JSONB 往返与双 worker 并发认领测试，避免把 SQLite 中跳过的 PostgreSQL 用例当作通过。

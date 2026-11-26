# Document-RAG-Chatbot

A team project to build a document-based RAG chatbot with document upload, question answering, and source citations.

中韩旅行携带品通关政策文档问答系统：上传官方政策 PDF，用中文或韩文提问，系统检索原文后生成回答，并给出文件名、PDF 物理页码和证据原文。证据不够时拒答，不用模型自己的知识补数字。

> 韩语说明见 [README.ko.md](README.ko.md)。本文以 2026-11-26 的 main 为准。

## 目录

- [主要功能](#主要功能)
- [系统结构](#系统结构)
- [当前进度](#当前进度)
- [仓库结构](#仓库结构)
- [本地启动](#本地启动)
- [API 与问答状态](#api-与问答状态)
- [前端启动与真实后端联调](#前端启动与真实后端联调)
- [失败场景验证与排查](#失败场景验证与排查)
- [CI/CD](#cicd)
- [部署到服务器](#部署到服务器)
- [测试与评测](#测试与评测)
- [限制与安全](#限制与安全)
- [团队分工](#团队分工)

## 主要功能

**文件管理**

- 上传 PDF，后台自动解析、切分、向量化，状态从「处理中」变为「可用」或「失败」
- 整份 PDF 都没有文字层（如扫描件）时判为失败并给出原因；当前不做 OCR
- 文件列表显示国家（中国 / 韩国），可删除文件

**问答**

- 回答附带出处：文件名、PDF 物理页码（从 1 开始，不是印刷页码）、证据原文；补全的跨页证据显示连续页码范围
- 证据不够时返回「找不到依据」，不编造
- 回答上方显示这次实际使用的生成模型名
- 网页支持中文、韩文界面

**两种问答模式**（改配置 `RAG_MODE` 切换，不改代码）

| 模式 | 做什么 |
| --- | --- |
| `single`（默认） | 单次 RAG：检索一次 → 生成 → 引用校验 |
| `agent` | 用 LangGraph 编排的单个 agent：先检查国家、出入境方向等必要条件，缺了就追问 1 轮；证据不够时改写问题重新检索，每个问题最多检索 3 次；检查文件的国家和方向是否适用；两份文件说法冲突时同时给出双方依据。对话和追问会保存在数据库里，刷新页面后可以继续，也可以取消正在执行的问题 |

agent 模式执行出错时，会自动退回单次 RAG 回答（受控 Fallback）。

## 系统结构

```text
浏览器（React 网页）
   │  /api
   ▼
后端 FastAPI
   ├─ 文件：上传 → PyMuPDF 解析 → 切分 → BGE-M3 向量化 → 入库
   └─ 问答：single 单次 RAG ／ agent（LangGraph）
             │                     │
             ▼                     ▼
   PostgreSQL 18 + pgvector     生成模型（OpenAI 兼容接口）
   （文件、文本块、向量、对话）
```

| 部分 | 使用的技术 |
| --- | --- |
| 前端 | React 19、TypeScript、Vite |
| 后端 | Python 3.12、FastAPI、Uvicorn、SQLAlchemy（async） |
| 数据库 | PostgreSQL 18 + pgvector（向量 1024 维），Docker 运行 |
| PDF 解析 | PyMuPDF；语言识别 lingua |
| 嵌入 | `BAAI/bge-m3`，FlagEmbedding 在本地 CPU 运行 |
| 生成 | OpenAI 兼容的 Chat Completions 接口。`GENERATION_PROFILE` 二选一：`furiosa_gemma`（FuriosaAI NPU 上的 `furiosa-ai/gemma-4-31B-it-FP8-block`）或 `legacy`（自行填写的模型地址，如学校 LLM 网关）。部署后的网站目前用 `furiosa_gemma` |
| Agent | LangGraph |
| 检索 | 向量检索 Top-5；可选证据补全（年龄定义、跨页的列举条款） |
| CI/CD | GitHub Actions；部署到 AWS EC2 |

不使用 LangChain、LlamaIndex 等 RAG 框架，检索、提示词和引用校验都在 `backend/app/qa/` 里自己实现。

## 当前进度

| 阶段 | 时间 | 内容 | 状态 |
| --- | --- | --- | --- |
| MVP | ～10/30 | 上传、解析、单次 RAG 问答、出处显示 | 完成 |
| 第一批 | 11/1～11/15 | agent 编排、追问、正式评测题、CI/CD、AWS 部署 | 大部分已合并，未完成的移到第二批 |
| 第二批 | 11/16～11/29 | agent 接入后端与网页、单次 RAG / agent 切换、生成换到 NPU 上的 Gemma、跨页条款、评测流水线 | 进行中 |
| 正式评测与最终发表 | 12 月 | 正式评测 30 题（LLM-only / Single RAG / Agentic RAG 对比）、12/11 功能冻结 | 计划中 |

各项的具体状态和负责人见 GitHub Issues。

## 仓库结构

```text
backend/            后端（FastAPI）
  app/api/          HTTP 接口
  app/documents/    文件上传与处理
  app/ingestion/    PDF 解析与切分
  app/qa/           检索、证据补全、提示词、引用校验；agent/ 是 agent 编排
  app/chat/         问答接口；persistent/ 是 agent 模式的对话保存、执行和取消
  app/adapters/     嵌入和生成模型的调用
  tests/            后端测试
frontend/           网页（React）
storage/            数据库建表 schema.sql 和迁移 migrations/
data/               公开示例 PDF（3 份）与来源登记 sources.csv
eval/               冒烟题、Pilot 题、正式评测题和结果
docs/               接口说明、agent 验收题
deploy/             服务器部署脚本（见「部署到服务器」）
scripts/            CI 用的检查脚本
.github/workflows/  CI/CD
```

政策文件共 5 份（韩国 2 份、中国 3 份），来源都登记在 `data/sources.csv`。仓库里只放其中 3 份公开样本 PDF；另 2 份（KR02、CN03）只登记来源、不提交 PDF，需要时按登记的官方网址下载。

## 本地启动

以下命令都在 Windows PowerShell 里执行，以本仓库根目录为起点。需要同时开 3 个终端：A 数据库、B 后端、C 前端和检查命令。

> 所有 `docker compose` 命令都在仓库根目录执行，并带上 `--env-file backend/.env`。后端在 `backend` 目录运行，不要混用目录。

已验证的环境：Windows、Python 3.12.10、CPU PyTorch 2.6.0（`torch==2.6.0+cpu`）、Docker Desktop 4.97、Node.js 22／24／26。不保证其他版本都兼容。

首次运行要下载嵌入模型（约 2 GB），需要网络、足够的磁盘和内存。CPU 推理速度取决于机器和文件大小。生成服务会收到问题和检索到的证据，可能消耗额度：只处理有授权的资料，只用本人或团队获准使用的 Key。

### 0. 安装 Docker Desktop（第一次才需要）

1. 从 Docker 官网下载 Docker Desktop for Windows
2. 安装时选 **All users**（为所有用户安装）。默认的 Per-user 安装在 Windows 没开「虚拟机平台」时会失败
3. 装完后，用**管理员身份**打开 PowerShell（开始菜单搜 PowerShell → 右键「以管理员身份运行」），运行下面这行，然后重启电脑：

```powershell
wsl --install --no-distribution
```

4. 重启后打开 Docker Desktop，等左下角显示 Engine running

### 1. 安装 Python 3.12 并建立独立环境

Python 3.12 的新版本已不提供 Windows 安装包，请从 python.org 下载 **3.12.10** 的 Windows installer。安装时**不要勾选 Add python.exe to PATH**，不会影响电脑上已有的 Python。

在仓库根目录：

```powershell
py -3.12 --version
py -3.12 -m venv .venv
```

已有 `.venv` 时先确认版本和用途，不要覆盖不明的环境。下面的命令直接用环境里的解释器，不需要改 PowerShell 执行策略。

先装 CPU 版 PyTorch，再装其余依赖。依赖包较大（单个约 170 MB），容易下载超时，所以加了 `--timeout 120 --retries 10`：

```powershell
.\.venv\Scripts\python.exe -m pip install --timeout 120 --retries 10 -r backend/requirements-cpu.txt
.\.venv\Scripts\python.exe -m pip install --timeout 120 --retries 10 -r backend/requirements.txt
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -c "import sys,torch,FlagEmbedding; print(sys.version); print(torch.__version__); print('imports OK')"
```

看到 `imports OK` 只说明装好了，不代表模型推理已经成功。

### 2. 建立 backend/.env

在仓库根目录，只在文件不存在时复制模板，避免覆盖已有的密码和 Key：

```powershell
if (!(Test-Path backend/.env)) {
    Copy-Item backend/.env.example backend/.env
}
```

用记事本打开 `backend/.env`，每一项的说明都写在模板注释里。至少要改这几项：

| 配置 | 填什么 |
| --- | --- |
| `POSTGRES_PASSWORD` | 自己定一个随机字符串（例如 32 位） |
| `DATABASE_URL` | 把模板里的 `PASSWORD` 换成上面同一个密码 |
| `GENERATION_PROFILE` | `furiosa_gemma`（用 NPU 上的 Gemma）或 `legacy`（用自己填的模型地址） |
| 用 `furiosa_gemma` 时 | 把 FuriosaAI 的 Key 写在 `backend/.secrets/furiosa.env`（一行 `FURIOSA_API_KEY=...`），或用 `FURIOSA_API_KEY_FILE` 指定别的路径 |
| 用 `legacy` 时 | 填 `GENERATION_MODEL`、`GENERATION_BASE_URL`、`GENERATION_API_KEY`（如学校 LLM 网关）。地址只写到 API 根地址，不要加 `/chat/completions` |
| `RAG_MODE` | `single`（默认）或 `agent` |

注意：

- 真实的 `.env` 和 `.secrets/` 不提交 Git。Key 只通过私下渠道传，不写进仓库、截图或群聊
- 用户名固定 `rag`、库名 `p4rag`、本机端口 5432。密码里有 URL 保留字符（如 `@`、`/`）时要编码
- 数据库已经建过以后，再改 `POSTGRES_PASSWORD` 不会自动改掉数据库里的密码。连不上时先找后端负责人，不要直接删数据卷重建
- `EMBEDDING_BASE_URL` 必须留空：当前只支持本地 CPU 运行 BGE-M3，填了会报错
- 1024 维是模型和数据库共同的约束，不能只改维度去换别的模型
- `RETRIEVAL_TOP_K` 默认 5（可填 1～100），正式评测时不要中途改
- `EVIDENCE_COMPLETION_ENABLED=true` 打开证据补全（年龄定义、跨页列举条款），部署和演示都打开
- 系统环境变量优先于 `.env`。改了配置后重启后端

### 3. 启动数据库

终端A，仓库根目录：

```powershell
docker compose --env-file backend/.env up -d postgres
docker compose --env-file backend/.env ps
```

等待 postgres 变成 `healthy`。首次空数据库卷自动执行 `storage/schema.sql`，创建 vector 扩展及 documents/chunks 表。
后端启动的 `init_db()` 还会创建缺失 ORM 表和 `document_number_seq`；这是当前实现，不是自动迁移工具，不能保证旧库字段自动升级。

确认表和向量列（保留原后端检查）：

```powershell
docker compose --env-file backend/.env exec postgres psql -U rag -d p4rag -c "\dt"
docker compose --env-file backend/.env exec postgres psql -U rag -d p4rag -c "SELECT format_type(atttypid, atttypmod) FROM pg_attribute WHERE attrelid = 'chunks'::regclass AND attname = 'embedding';"
```

预期 documents、chunks 存在，embedding 输出 `vector(1024)`；当前 ORM 可能还包含解析页表，不能将“只有两张表”当作要求。

如果报错里有 `bind: An attempt was made to access a socket in a way forbidden by its access permissions`，是 Windows 把 5432 端口预留了。用管理员身份打开 PowerShell，先运行 `net stop winnat`，回到终端 A 重新启动数据库，启动成功后再运行 `net start winnat`。

### 4. 启动后端

终端B，从仓库根目录进入 backend：

```powershell
cd backend
..\.venv\Scripts\python.exe -m uvicorn app.main:app --env-file .env --host 127.0.0.1 --port 8000
```

uvicorn[standard] 提供 dotenv 支持。数据库需先可用，否则应用启动可能失败，不存在SQLite业务回退。
开发可使用 `--reload`，但模型联调阶段建议先不开，避免文件变动重启进程；低内存环境不启多worker。
端口占用时核查或换端口并同步客户端，不结束未知进程。

### 5. 数据库健康检查

终端C：

```powershell
curl.exe -i http://127.0.0.1:8000/health
```

预期HTTP 200，结构示例：

```json
{"status":"ok","database":{"version":"18.x","vector_extension":true}}
```

版本以实际返回为准，vector_extension应为true。该接口不调用 BGE-M3 或 LLM，不能单独作为完整启动成功的证据。
交互式接口文档：http://127.0.0.1:8000/docs。

### 6. 首次模型加载与缓存

通常首次切分加载tokenizer或首次向量化加载模型时才下载；Uvicorn监听端口不表示模型已就绪。
可在启动后端前执行下节独立模型检查，提前下载并验证。检查脚本退出后再启动服务，避免两个进程同时占用模型内存。

首次处理较慢时查看后台下载和处理日志，不要连续重试上传。低内存时批大小保持1、文档逐份上传，并保存工作、关闭无用程序。
线程卸载避免阻塞事件循环，但不减少模型内存占用；后台任务依赖当前进程，处理中不要重启应用。

### 7. 独立模型检查（可选，真实调用）

从 backend 目录执行。中文/韩文Embedding：

```powershell
..\.venv\Scripts\python.exe -c "from app.adapters.embedding import embed_texts; v=embed_texts(['这是中文测试。','한국어 테스트입니다.']); assert len(v)==2 and all(len(x)==1024 for x in v); print('count=',len(v),'dimensions=',[len(x) for x in v])"
```

预期 `count=2`、`dimensions=[1024, 1024]`。首次可能真实下载模型。

生成服务（需要有效密钥，可能消耗额度）：

```powershell
..\.venv\Scripts\python.exe -c "from app.adapters.generation import generate_text; t=generate_text('请用中文简短回复连接成功。'); assert t.strip(); print(t)"
```

预期非空文本，措辞不固定。这不是完整 RAG 验证，不证明检索、引用或完整工具执行流程已通过。

### 8. 上传 PDF 并提问

不能只看 /health；至少完成“PDF → READY → 提问 → 回答及出处”。建议使用独立开发/验收数据库，不混入无关文件。

#### 上传并检查状态

终端C，仓库根目录：

```powershell
$base = 'http://127.0.0.1:8000'
$pdf = Join-Path $PWD 'data/中华人民共和国海关进出境行李物品监管办法.pdf'
curl.exe -i -X POST "$base/api/documents" -F "file=@$pdf;type=application/pdf"
```

PowerShell使用 `curl.exe`，避免curl别名差异。multipart字段必须为file。
预期HTTP 201 + C1，初始PROCESSING。后台继续解析、切分、向量化、入库，完成后才READY。

重复执行以下查询检查本轮document_id，不用定时器假定成功：

```powershell
Invoke-RestMethod "$base/api/documents" |
    Select-Object document_id, filename, status, page_count, error_message
```

FAILED时查看error_message和日志，不手动修改READY。上传限制：PDF MIME和文件头合法、非空、不超过25 MiB。
本轮原PDF保存于backend/uploads/{document_id}.pdf；解析页、Chunk及向量在数据库。
重复上传会生成不同ID，材料原JSON编号不等于本轮数据库ID。

#### 提问

确保题目所需政策已READY，例如以下中国方向题需要相应征收政策。完整验收应上传 data/ 里的三份 PDF，不要用单份无关文件测试全部题。

```powershell
$payload = @{
    message = '成年旅客从韩国入境中国内地，不属于经常出入境人员，香烟一般可免税携带多少支？'
} | ConvertTo-Json
$reply = Invoke-RestMethod -Method Post -Uri "$base/api/chat" `
    -ContentType 'application/json; charset=utf-8' `
    -Body ([System.Text.Encoding]::UTF8.GetBytes($payload))
$reply | ConvertTo-Json -Depth 8
```

Windows PowerShell 5.1 里 `Invoke-RestMethod` 显示的中文可能是乱码，这是显示问题，不是后端出错；可以改用 PowerShell 7，或在 `/docs` 页面里看。`RAG_MODE=agent` 时请求格式相同：缺条件时返回 `NEEDS_CLARIFICATION`，还在执行时返回 202，用 API 一节里的 execution 接口查结果。

ANSWERED必须有非空answer和citations；每条包含evidence_id、filename、page、quote。
到来源PDF物理页查找quote，人工核对答案条件和例外。引用字段齐全不代表事实全部正确。
INSUFFICIENT_EVIDENCE是正常证据不足结果；SYSTEM_ERROR需检查数据库、生成和校验错误。

也可在 `/docs` 操作：POST /api/documents上传 → GET /api/documents确认READY → POST /api/chat发送message。
Swagger验证不等于真实前端页面验收。

## API 与问答状态

启动后端后，`http://127.0.0.1:8000/docs` 有可交互的接口文档。

**文件**

| 接口 | 请求 / 返回 |
| --- | --- |
| `POST /api/documents` | multipart 字段 `file`；返回 201 和文件记录，初始状态 PROCESSING |
| `GET /api/documents` | 返回文件记录数组 |
| `DELETE /api/documents/{document_id}` | 返回 204，没有内容 |

**问答**：`RAG_MODE` 不同，`/api/chat` 下的接口也不同

| 模式 | 接口 | 说明 |
| --- | --- | --- |
| single | `POST /api/chat` | 发 `message`；正常 200，内部错误 500，都返回同样格式的问答结果 |
| agent | `POST /api/chat` | 发 `message`，可带 `session_id`；回答追问时再带 `question_id` 和 `conditions`（补充的条件），不改写原问题。还在执行时返回 202 |
| agent | `GET /api/chat/sessions` | 对话列表 |
| agent | `GET /api/chat/sessions/{session_id}` | 某个对话的全部问答 |
| agent | `DELETE /api/chat/sessions/{session_id}` | 删除对话 |
| agent | `GET /api/chat/sessions/{session_id}/questions/{question_id}/execution` | 查看问题的执行状态 |
| agent | `POST /api/chat/sessions/{session_id}/questions/{question_id}/cancel` | 取消正在执行的问题 |

**问答状态**

| 状态 | 什么时候出现 | 网页怎么显示 |
| --- | --- | --- |
| `ANSWERED` | 找到依据并回答 | 回答 + 出处卡片 |
| `INSUFFICIENT_EVIDENCE` | 证据不够 | 拒答说明，不显示空出处 |
| `NO_READY_DOCUMENTS` | 没有可用文件 | 提示先上传文件 |
| `SYSTEM_ERROR` | 数据库、生成服务等出错 | 错误提示和重试 |
| `NEEDS_CLARIFICATION` | 缺少国家、出入境方向等必要条件（agent） | 追问问题和填写条件的输入框 |
| `CONFLICTING_EVIDENCE` | 两份文件说法冲突（agent） | 同时列出双方的依据 |

问答结果里的 `model_name` 是这次实际使用的生成模型名，网页显示在回答上方；没有实际生成回答时（如没有可用文件、拒答）可能为空。

出处里的 `page` 是从 1 开始的 PDF 物理页码，不是印刷页码；`filename` 来自文件记录，`quote` 来自检索到的原文。生成服务的 Key 只放在后端，不会进入前端或浏览器请求。

## 前端启动与真实后端联调

以下命令从仓库根目录开始，在数据库及后端启动之外，另开终端C运行前端，三个终端均保持运行。

### 1. 安装前端依赖

项目 `frontend/.mise.toml` 声明 Node.js 22；组内已在 Node.js 24.22.1（npm 11.19.1）和 26.7.0 上运行过，不表示所有版本都验证过。

```powershell
node --version
npm.cmd --version
cd frontend
npm.cmd ci
```

`npm.cmd ci` 使用仓库内的 `package-lock.json` 安装依赖。PowerShell 如提示“禁止运行 npm.ps1”，使用 `npm.cmd` 即可，无需修改系统执行策略。

### 2. 启动网页

在 `frontend` 目录：

```powershell
npm.cmd run dev
```

开发服务器和构建预览默认仅监听 `127.0.0.1`，仅供本机访问。`FIGMA_DEV_SERVER_HOST` 环境变量仍可覆盖配置；本地验收请保持其未设置或明确设为 `127.0.0.1`：

```powershell
$env:FIGMA_DEV_SERVER_HOST = '127.0.0.1'
npm.cmd run dev
```

不要在使用个人生成服务 Key 的联调环境将其设置为 `0.0.0.0`。当前 `/api` 代理没有独立用户鉴权，允许其他设备访问开发服务器时，它们可能上传、删除文件或使用该后端配置提问。

默认访问 `http://127.0.0.1:8443`，以终端实际输出为准。开发服务器使用 HTTP；8443 不代表 HTTPS。默认端口被占用时会报错，可先关闭旧的前端进程，或在当前终端指定空闲端口：

```powershell
$env:PORT = '8444'
npm.cmd run dev
```

随后访问 `http://127.0.0.1:8444`。停止服务使用 `Ctrl+C`。修改页面后通常自动更新；修改代理配置后需重启前端。

### 3. 检查前端代理

前端客户端请求相对地址 `/api`，由 `frontend/vite.config.ts` 中的开发服务器代理转发至后端8000端口，浏览器无需直接跨端口调用后端。

如果后端 `http://127.0.0.1:8000/health` 正常，但页面加载文件列表失败，先将代理目标中的 `localhost` 改成 `127.0.0.1`：

```ts
proxy: {
  '/api': {
    target: 'http://127.0.0.1:8000',
    changeOrigin: true,
  },
},
```

保存并重启 `npm.cmd run dev`。后端按上文监听 `127.0.0.1:8000`；Windows 上 `localhost` 可能解析至 IPv6，导致连接失败。

可另开终端从网页的同源地址验证代理（自定义网页端口时相应替换8443）：

```powershell
Invoke-RestMethod 'http://127.0.0.1:8000/health'
Invoke-RestMethod 'http://127.0.0.1:8443/api/documents'
```

第二条返回文件数组，空库返回空数组。后端健康但代理失败时检查代理目标并重启前端；两者都失败时先查看数据库和后端日志。`npm.cmd run preview` 用于预览构建产物，不作为本说明的开发联调入口。

### 4. 浏览器验收

- Documents：上传 `data/` 里的 PDF，列表先显示处理中，再自动变成可用；上传 `.docx` 会提示只支持 PDF；没有文字层的图片 PDF 显示失败和原因
- Chat：提问后核对回答下方出处的文件名、物理页码和原文；点旧回答的出处，显示的是旧回答自己的来源
- 验证 `ANSWERED`、`INSUFFICIENT_EVIDENCE`、`NO_READY_DOCUMENTS`；`SYSTEM_ERROR` 按下文失败场景验证
- `RAG_MODE=agent` 时：问一个缺国家或出入境方向的问题，应出现追问；补充条件后继续回答；刷新页面后对话还在
- 删除：只在自己的测试库删除自己的文件；全部删除后提问应提示没有可用文件
- 页面能打开、构建通过，不等于以上操作都验收过

可在 `frontend` 目录检查构建：

```powershell
.\node_modules\.bin\tsc.cmd --noEmit
npm.cmd run build
```

## 失败场景验证与排查

### 数据库健康失败（保留原后端验证）

仅在自己的测试环境、后端已运行且无处理任务时，仓库根目录执行：

```powershell
docker compose --env-file backend/.env stop postgres
curl.exe -i http://127.0.0.1:8000/health
docker compose --env-file backend/.env up -d postgres
```

服务仍运行时，数据库连接/查询失败预期HTTP503，日志包含错误。若应用启动前库已不可用，可能直接启动失败，不能要求该时刻一定能访问health。

### 业务失败场景

- 无文字/扫描件：按解析规则整份FAILED并保留分类原因；当前不提供OCR。MVP 扫描件场景使用实际含图像、无文字层的 PDF 验证同一“所有页都没有文字”规则；纯空白PDF不能替代图片PDF。
- 删除全部文件后提问：仅在专用测试库操作自己的文档，预期NO_READY_DOCUMENTS且无生成调用日志。
- 错误生成地址：先备份本地配置，在专用测试环境改成关闭端口、重启后问答，预期500+SYSTEM_ERROR；之后恢复配置并重启。没有READY文件时只会命中前置拒绝，不能验证生成失败。
- 无证据问题：拒答、不编造数字。真实三份政策含酒类条款，不能直接把它们称为“无酒类证据库”。

| 故障 | 首先检查 |
|---|---|
| 数据库启动报 5432 端口 access permissions | Windows 预留了端口，见「启动数据库」里的 `net stop winnat` |
| pip 安装超时 | 加 `--timeout 120 --retries 10` 重试 |
| PowerShell 里中文乱码 | Windows PowerShell 5.1 的显示问题，改用 PowerShell 7 或 `/docs` |
| PyTorch导入失败 | Python解释器、CPU依赖及版本，不修改全局环境盲试 |
| 模型加载失败 | 下载网络、模型名、缓存权限/完整性、磁盘及内存 |
| PROCESSING长期不变 | 后端日志、首次下载、推理及进程是否崩溃；无自动恢复保证 |
| 配置修改不生效 | backend/.env、同名环境变量、是否重启、是否访问旧服务 |
| HTTP500 | 数据库、网关权限/额度、超时、生成格式、语言或引用校验 |
| ANSWERED但不完整 | 同轮Top-5是否召回条件/例外，不以状态或有效引用代替事实核对 |

原README的“从零重建数据库”属于破坏性维护：`docker compose ... down -v`会清空持久化数据，不作为日常排障或新手必做步骤。只有明确授权、独立可丢弃库和备份确认后才可重建。
当前已知数据库删除失败与文件清理一致性风险尚需后续修复；正常删除通过不能宣称异常一致性已解决。

## CI/CD

工作流在 `.github/workflows/ci-cd.yml`。

**CI**：每个发到 main 的 PR、以及 main 的每次更新，都会自动跑下面三项，结果显示在 PR 页面上：

| 检查 | 内容 |
| --- | --- |
| Backend tests | 后端测试（SQLite 和临时 PostgreSQL）、Ruff 代码检查、mypy 类型检查、本次改动的代码要被测试覆盖 |
| Frontend build | TypeScript 类型检查、ESLint、前端测试、构建 |
| Deployment script checks | 部署脚本和工作流文件的检查 |

PR 要经审阅人批准后才能合并。检查没通过时修代码，不靠关闭检查规则或跳过测试让检查变绿。

**CD**：main 更新且三项检查都通过后，「Deploy to EC2」会等待批准；批准人在 Actions 页面点批准后，才部署到 AWS EC2。数据库迁移（`storage/migrations/`）在部署时自动执行。连接服务器用的信息存放在仓库 Secrets 里，不写进代码。

## 部署到服务器

部署步骤见 [deploy/README.ko.md](deploy/README.ko.md)（韩语）：EC2 连接与网络设置、`.env` 配置、数据库和后端启动、前端构建、Nginx、网站验证，以及用 GitHub Actions 做后续部署的设置和失败恢复。数据库迁移的说明见 [deploy/README.md](deploy/README.md)。相关讨论见 [Issue #47](https://github.com/Kenro66/262RCOSE45700/issues/47)。

部署后的网站没有登录功能，拿到网址的人都能上传、删除文件，也会用掉生成服务的额度。所以网址只私下发给组员、教授和导师，不写在本仓库或其他公开的地方。

## 测试与评测

**自动测试**：在 `backend` 目录运行 `..\.venv\Scripts\python.exe -m pytest tests`。部分测试用模型替身或 SQLite；`test_issue7.py`、`test_e2e_postgres.py` 需要独立的 PostgreSQL 测试库，会清空表，不要指向自己正在用的数据库。前端在 `frontend` 目录运行 `npm.cmd test`。

**评测题**（`eval/`）

| 题集 | 文件 | 用途 |
| --- | --- | --- |
| 冒烟题 10 道 | `questions.jsonl`，结果 `results/results.csv` | MVP 验收；agent 不能比单次 RAG 退步 |
| Pilot 题 40 道 | `pilot43_questions_v1_40.jsonl`，结果 `results/` | 调参数、选模型用 |
| 正式评测题 30 道 | `issue44_formal28_review.jsonl`（文件名里的 28 是草稿时留下的，实际 30 道） | 11/20 锁定，不再用来调参 |
| agent 验收题 | `docs/agent/agent_acceptance_cases_20261110.md` | 验收追问、重新检索、冲突等 agent 功能 |

正式评测题的分布：普通问答 12、条件追问 7、国家／出入境方向区分 5、证据不足拒答 6。现有 5 份文件里找不到真正的政策冲突，所以没有冲突题；冲突功能只用模拟数据验证过流程。

评测脚本在 `backend/scripts/`。`eval_http_pipeline.py` 是统一的 HTTP 评测流水线：Single RAG 和 Agentic RAG 都通过 `/api/chat` 评测，支持 202 异步轮询和追问后的第二轮，结果用同一种 JSONL 格式记录（说明见 `backend/scripts/README_issue48_adapter.md`）。另有 `smoke_issue12.py`、`validate_agent_real.py`、`evaluate_agent_quality.py` 等真实调用脚本。这些脚本会调用外部模型、可能计费，有的会写数据库，运行前先看脚本说明并用独立的测试库。自动判定的结果不能代替人工核对出处页码。

**Pilot 题上的单次 RAG 基线**（11/3，MVP 代码）：可回答题完全正确率 85.7%、综合任务成功率 87.2%、Top-5 命中至少一页标准证据 100%、覆盖全部标准证据页 97.1%、引用文件名／页码／原文匹配 100%、平均响应 8.75 秒。

## 限制与安全

- 只支持有文字层的 PDF，不做 OCR
- 检索只用向量检索 Top-5，没有混合检索；Reranker 没有启用
- 追问只做 1 轮；聊天不参考同一对话里之前的问答（多轮记忆未做）
- 冲突只处理两份文件对同一事项说法真的不同的情况；新旧政策、适用对象不同的冲突未做
- 数字检查有限，不能完整核对品类、单位、条件和法律含义；引用能对上不等于每句话都有依据，结果仍需人工核对
- 换嵌入模型、切分方式后，要重建受影响的向量，不能和旧向量混用
- 不提交真实 `.env`、`backend/.secrets/`、Key、模型、缓存、上传的文件或数据库备份；分享日志前先去掉敏感信息
- 网站没有登录和权限控制，所有人共用一个文件库

## 团队分工

| 成员 | 负责 |
| --- | --- |
| 纪金典（[@Kenro66](https://github.com/Kenro66)） | PM、README、整体验收 |
| 王诗琪（[@sikiww77](https://github.com/sikiww77)） | 后端、数据库、问答接口、CI/CD 与部署 |
| 孙忠其（[@sun-cs](https://github.com/sun-cs)） | 切分、检索、生成、引用校验、agent |
| 王一霖（[@Kruskal086](https://github.com/Kruskal086)） | 网页（Documents、Chat） |
| 姜超（[@jiangc-666](https://github.com/jiangc-666)） | 评测题与评测结果 |

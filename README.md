# Document-RAG-Chatbot

A team project to build a document-based RAG chatbot with document upload, question answering, and source citations.

中韩旅行携带品通关政策文档问答系统：上传政策 PDF，以中文或韩文提问，系统检索原文、生成回答并提供文件名、物理页码及证据原文。


## 环境与使用范围

- 已验证本地环境：Windows、Python 3.12、CPU PyTorch 2.6.0。安装文件为 `torch==2.6.0+cpu`；全组版本结论在 Issue #5 确认，不承诺其他 Python 版本均兼容。
- Docker Desktop 已安装且启动；数据库使用 PostgreSQL 18 + pgvector。
- Embedding 使用 FlagEmbedding 在本地 CPU 运行 `BAAI/bge-m3`，输出1024维稠密向量。
- 临时生成服务使用 OpenAI-compatible Chat Completions；学校 Baze 配置为已有测试方案，最终模型和 NPU 部署仍待确认。
- 首次下载模型需要网络、足够磁盘和可用内存。CPU 推理耗时受机器及文档影响，不保证固定完成时间。
- 外部生成服务会接收问题及检索证据，可能消耗额度；只处理获得授权的资料，使用本人或团队获准使用的密钥。
- 前端真实接线依赖 PR #29；后端可用不代表页面链路已验收。真实页面联调记录见 Issue #15。

## 数据库与后端启动

以下命令使用 Windows PowerShell，以本 Git 仓库根目录为起点。

> 约定：所有 `docker compose` 命令都在仓库根目录执行，并携带 `--env-file backend/.env`。
> 后端另开一个终端，在 `backend` 目录运行；检查命令不要混用目录。

### 1. 创建 Python 3.12 独立环境

仓库根目录：

```powershell
py -3.12 --version
py -3.12 -m venv .venv
```

已有 `.venv` 时先确认版本和用途，不覆盖未知环境。直接使用环境内解释器，无需更改 PowerShell 执行策略或系统全局 Python。

先安装 CPU PyTorch，再安装其余后端依赖：

```powershell
.\.venv\Scripts\python.exe -m pip install -r backend/requirements-cpu.txt
.\.venv\Scripts\python.exe -m pip install -r backend/requirements.txt
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -c "import sys,torch,FlagEmbedding; print(sys.version); print(torch.__version__); print('imports OK')"
```

导入成功不等于真实模型推理成功。部分依赖为范围约束，不是完整锁文件；正式验收应记录实际安装版本。

### 2. 建立 backend/.env

仓库根目录，仅在文件不存在时复制，避免覆盖已有密钥和密码：

```powershell
if (!(Test-Path backend/.env)) {
    Copy-Item backend/.env.example backend/.env
}
```

真实 `.env` 不提交 Git。保留模板字段，在本地填入数据库和生成配置：

```ini
# 数据库：密码仅为占位，必须自行替换
POSTGRES_PASSWORD=REPLACE_WITH_LOCAL_PASSWORD
DATABASE_URL=postgresql://rag:REPLACE_WITH_LOCAL_PASSWORD@127.0.0.1:5432/p4rag

# 本地 CPU Embedding
EMBEDDING_MODEL=BAAI/bge-m3
EMBEDDING_BASE_URL=
EMBEDDING_API_KEY=
EMBEDDING_DIMENSION=1024
EMBEDDING_BATCH_SIZE=1
EMBEDDING_MAX_LENGTH=8192
EMBEDDING_CACHE_DIR=
EMBEDDING_CPU_THREADS=1

# 生成服务：仅示例，模型权限以学校账号为准
GENERATION_MODEL=gpt-6-sol
GENERATION_BASE_URL=https://api.baze.co/v1/gateway
GENERATION_API_KEY=
GENERATION_TIMEOUT_SECONDS=60

# MVP 默认检索数量
RETRIEVAL_TOP_K=5
```

配置说明：

- 用户名固定 `rag`、库名 `p4rag`、默认本机端口5432；DATABASE_URL密码必须对应POSTGRES_PASSWORD。密码含URL保留字符时需正确编码。
- 已有数据库卷不会因修改 POSTGRES_PASSWORD 自动更新密码；连接不通时请后端核查，不要直接删卷重建。
- GENERATION_MODEL、GENERATION_BASE_URL、GENERATION_API_KEY 必须符合实际服务授权。密钥通过获准的私下渠道配置，禁止写入仓库、截图或群聊。
- 生成地址是 API 根地址，不追加 `/chat/completions`，适配器自行追加。
- EMBEDDING_BASE_URL 当前必须留空；远程 NPU Embedding 未接入，非空会明确报错。EMBEDDING_API_KEY 为预留字段，CPU实现不用。
- EMBEDDING_CACHE_DIR 可留空；要将模型缓存放D盘，可改为 `D:/RAG-Dev/models/huggingface/hub`，路径按自己机器调整。
- 1024维是当前模型/数据库约束，不允许只修改维度配置就使用任意模型。批大小和CPU线程必须为正整数，max_length为1–8192。
- RETRIEVAL_TOP_K默认5，可配置1–100；正式题集不要中途切换。
- 适配器逐次读取 `.env`，但进程环境变量优先。Uvicorn会加载环境，数据库配置在导入时读取；统一建议修改配置后重启后端。

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

## 独立模型检查（可选，真实调用）

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

## 上传 PDF 与真实问答验证

不能只看 /health；至少完成“PDF → READY → 提问 → 回答及出处”。建议使用独立开发/验收数据库，不混入无关文件。

### 1. 上传并检查状态

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

### 2. 使用正式题单中的可回答题

确保题目所需政策已READY，例如以下中国方向题需要相应征收政策。完整验收应上传data/的三份MVP政策PDF，不要用单份无关文件测试全部题。

```powershell
$payload = @{
    message = '成年旅客从韩国入境中国内地，不属于经常出入境人员，香烟一般可免税携带多少支？'
} | ConvertTo-Json
$reply = Invoke-RestMethod -Method Post -Uri "$base/api/chat" `
    -ContentType 'application/json; charset=utf-8' `
    -Body ([System.Text.Encoding]::UTF8.GetBytes($payload))
$reply | ConvertTo-Json -Depth 8
```

ANSWERED必须有非空answer和citations；每条包含evidence_id、filename、page、quote。
到来源PDF物理页查找quote，人工核对答案条件和例外。引用字段齐全不代表事实全部正确。
INSUFFICIENT_EVIDENCE是正常证据不足结果；SYSTEM_ERROR需检查数据库、生成和校验错误。

也可在 `/docs` 操作：POST /api/documents上传 → GET /api/documents确认READY → POST /api/chat发送message。
Swagger验证不等于真实前端页面验收。

## API 与前端对接

| 接口 | 请求/返回 |
|---|---|
| POST /api/documents | multipart file；201+C1，初始PROCESSING |
| GET /api/documents | 返回C1数组，无额外外层对象 |
| DELETE /api/documents/{document_id} | 使用后端真实ID；204，无JSON体 |
| POST /api/chat | JSON message；session_id可省略；正常200+C5，内部错误500+C5 |

| 问答状态 | 页面处理 |
|---|---|
| ANSWERED | 展示answer及真实出处卡片 |
| INSUFFICIENT_EVIDENCE | 展示answer中的拒答，无引用不显示空卡片 |
| NO_READY_DOCUMENTS | 展示message，提示上传可用文件 |
| SYSTEM_ERROR | 解析HTTP500的C5，显示错误/重试提示 |

不认识的status不能空白或按成功展示。上传400的detail、输入422与C5问答响应应分别处理。
引用page是从1开始的物理页，不是印刷页码；filename来自文档记录，quote来自原始命中正文。
session_id不提供多轮记忆，conversation.id仅用于页面记录不影响单轮RAG。
学校网关密钥只能保存在后端，禁止进入前端构建或浏览器请求。

## 前端启动与真实后端联调

前端真实接线依赖 PR #29；合并后先更新本地代码。以下命令从仓库根目录开始，在数据库及后端启动之外，另开终端C运行前端，三个终端均保持运行。

### 1. 安装前端依赖

项目 `frontend/.mise.toml` 声明 Node.js 22；本机已完成类型检查和构建的环境为 Node.js 24.22.1、npm 11.19.1。使用 Node.js 22.12+ 或 24 的受支持版本。

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

- Documents：选择 `data/` 中真实政策 PDF，确认列表出现处理中并自动变为可用；上传 `.docx` 应提示只支持 PDF；真实扫描件应显示失败及后端原因。
- Chat：空对话显示4个固定示例问题，点击与手动输入均请求真实后端；答案由已上传且 READY 的文件决定，不预置成功或失败状态。
- 验证 `ANSWERED`、`INSUFFICIENT_EVIDENCE`、`NO_READY_DOCUMENTS`；另按下文失败场景验证 `SYSTEM_ERROR`。有答案时核对文件名、物理页码和引文；连续两条回答后点击旧回答引用，确认显示旧回答自己的来源。
- 删除：在自己的独立测试库删除本轮文件，确认列表及可用数量更新；全部删除后提问应提示没有可用文件。
- 将本轮代码版本、实际测试结果和脱敏截图记录到 Issue #15；构建通过或页面可打开不等于以上操作已验收。字段仍需后端负责人确认。

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

- 无文字/扫描件：按解析规则整份FAILED并保留分类原因；当前不提供OCR。专门扫描件验收需实际扫描件，不用构造空白PDF冒充。
- 删除全部文件后提问：仅在专用测试库操作自己的文档，预期NO_READY_DOCUMENTS且无生成调用日志。
- 错误生成地址：先备份本地配置，在专用测试环境改成关闭端口、重启后问答，预期500+SYSTEM_ERROR；之后恢复配置并重启。没有READY文件时只会命中前置拒绝，不能验证生成失败。
- 无证据问题：拒答、不编造数字。真实三份政策含酒类条款，不能直接把它们称为“无酒类证据库”。

| 故障 | 首先检查 |
|---|---|
| PyTorch导入失败 | Python解释器、CPU依赖及版本，不修改全局环境盲试 |
| 模型加载失败 | 下载网络、模型名、缓存权限/完整性、磁盘及内存 |
| PROCESSING长期不变 | 后端日志、首次下载、推理及进程是否崩溃；无自动恢复保证 |
| 配置修改不生效 | backend/.env、同名环境变量、是否重启、是否访问旧服务 |
| HTTP500 | 数据库、网关权限/额度、超时、生成格式、语言或引用校验 |
| ANSWERED但不完整 | 同轮Top-5是否召回条件/例外，不以状态或有效引用代替事实核对 |

原README的“从零重建数据库”属于破坏性维护：`docker compose ... down -v`会清空持久化数据，不作为日常排障或新手必做步骤。只有明确授权、独立可丢弃库和备份确认后才可重建。
当前已知数据库删除失败与文件清理一致性风险尚需后续修复；正常删除通过不能宣称异常一致性已解决。

## 测试、评测与验收记录

- 正式题单在eval/questions.jsonl；#18由姜超人工核对答案及出处页码，#13负责页面联调和必要Prompt调整，#19负责整体验收。
- backend/scripts/record_issue12_real.py可直接运行真实RAG/数据库题集，不是浏览器或/api/chat验收。需显式--run-real、有效.env、已READY资料；只读数据库但写本地结果且调用外部模型，可能计费。
- 记录模型、题单、代码和索引版本；不能另跑一次检索拼接旧回答。诊断JSON不是14列results.csv，转换工具需另行确认，main当前不包含本地新增的14列模板导出工具。
- manual_judgment、citation_page_verified、notes由姜超填写，不以自动状态匹配代替人工成绩。
- 部分单元测试使用模型替身或SQLite；真实PDF切分测试依赖解析夹具和tokenizer，真实Postgres测试可能清表。没有独立测试库授权，不直接运行全部tests。
- validate_issue9.py要求独立空库，会上传并删除本轮文档，不是只读脚本，先核查权限和风险。

现有历史真实隔离记录：三份PDF共38页、新版192个Chunk及1024维向量；自动状态匹配9/10，Q003年龄例外、Q008跨页关键条件仍有缺口。
这些结果不是最新main的浏览器验收，也不保证重复运行稳定。7d9c026引用边界修复只重跑自动化，未为该补丁重新执行真实全流程。

最终需有未参与README编写的组员按本说明从零启动、三份PDF READY、真实扫描件FAILED、页面10题有返回并人工核对出处，在Issue #19记录人员、日期和失败项。
PR合并或开发Issue关闭不等于MVP验收自动完成。

## 限制与安全

- 当前单轮RAG，默认Top-k=5；不包含Agent、多轮、OCR、Hybrid Search或Reranking。
- 数字防护是有限检查，不能完整核验品类、单位、条件和法律含义；引用可映射不等于每个事实均受支持。
- 更换Embedding模型、标题/正文输入或切分结果后，重建受影响向量，不按旧ID混配历史向量。
- 不提交真实.env、密钥、模型、缓存、上传文件、完整向量或数据库备份；分享日志前人工脱敏。gitignore不能替代检查已跟踪文件。
- 此说明面向可信本机开发，不代表具备生产鉴权、租户隔离或公网部署安全；未经评审不开放公网。



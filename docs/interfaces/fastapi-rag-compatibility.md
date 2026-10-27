# Issue #5：FastAPI 与 RAG 最小兼容测试

## 当前结论（2026-10-27）

**待真实模型验收，尚未决定“锁定 FastAPI”。Issue #5 不应关闭。**

已在 Windows / Python 3.12.13 的同一个虚拟环境安装并验证：

- FastAPI 0.115.6、Starlette 0.41.3、Uvicorn 0.34.0、asyncpg 0.30.0。
- Pydantic 2.10.6、HTTPX 0.28.1、python-dotenv 1.2.4。
- CPU PyTorch 2.6.0+cpu 与 FlagEmbedding 1.4.2 可与 FastAPI 在同一进程实际导入。
- transformers 4.57.6、sentence-transformers 5.2.0、sentencepiece 0.2.0、peft 0.21.2。
- `uv pip check` 检查 72 个包通过。完整版本快照见 `backend/requirements-fastapi-compat.txt`。
- 5 个自动化测试通过，覆盖同进程成功、错误维度/NaN/零向量/布尔值、空生成、适配器异常和启动失败。模型调用使用明确的测试替身，**不是模型验收结果**。
- 未配置 `.env` 时，实际脚本在 FastAPI 请求内调用真实 `embed_texts`，返回 503、`EmbeddingError`，退出码 1；没有误报成功，也没有继续调用生成。

本机尚未下载 BGE-M3 权重，也未提供生成服务配置。故未验证真实的 1024 维向量和非空生成文本。当前未发现已安装依赖的解析或导入冲突，但这不足以证明推理兼容。

沙箱内 Windows asyncio 的 socketpair 初始化曾挂起；本机正常执行测试通过。该沙箱限制不作为 FastAPI/RAG 库冲突。

## 依赖与改动边界

本测试依赖两个尚未合并的 PR：

- [PR #25，Issue #4 后端骨架](https://github.com/Kenro66/262RCOSE45700/pull/25)，本次检查提交 `37b8ceea97ddb873f1da91ac83185cd3e5ffe05a`。
- [PR #22，Issue #10 适配器](https://github.com/Kenro66/262RCOSE45700/pull/22)，本次检查提交 `05992668a22472852ea093fb322640dcd605590e`。

本次只增加诊断脚本、测试、候选依赖快照及本文档，不复制或修改适配器实现。主分支缺少上述依赖时不能直接运行；本次本地验证组合了 PR #25 的 app.main 和 PR #22 的三个 adapters 模块。

两个 PR 的 `backend/requirements.txt` 和 `.env.example` 存在合并冲突。合并时需要同时保留后端与 RAG 依赖，并采用完整的 `BAAI/bge-m3` 模型标识；候选环境快照已包含双方依赖。真实验收后再由团队决定是否将快照提升为正式 requirements.txt，不覆盖其他人的 PR。

## 运行真实验收

在依赖 PR 合并或本地组合后，用 Python 3.12 创建独立虚拟环境。在 `backend/` 目录执行（也可用 `python -m pip` 替换 `uv pip --python python`）：

```powershell
uv pip install --python python -r requirements-cpu.txt
uv pip install --python python -r requirements-fastapi-compat.txt
uv pip check --python python
python -m unittest test_fastapi_compat -v
python smoke_fastapi.py > fastapi-compat-result.local.json
$LASTEXITCODE
```

`.env` 最少需要以下配置，真实服务地址/模型名请使用负责人提供的值。密钥只在本地填写，不发到聊天、Issue 或 PR：

```dotenv
EMBEDDING_MODEL=BAAI/bge-m3
EMBEDDING_BASE_URL=
EMBEDDING_CPU_THREADS=1
GENERATION_MODEL=
GENERATION_BASE_URL=
GENERATION_API_KEY=
GENERATION_TIMEOUT_SECONDS=60
```

若已有 BGE-M3 本地模型，EMBEDDING_MODEL 可填写模型目录；也可按 #22 使用 EMBEDDING_CACHE_DIR 指定缓存。首次下载约需数 GB 空间和网络。生成地址填写 OpenAI-compatible API 根地址，不加 `/chat/completions`。配置通过 #22 原有 settings() 读取，进程环境变量优先。临时模型不等于最终 NPU 模型选型。

脚本导入真实 `app.main.app`，仅在该脚本运行时添加 `POST /_compat/rag`，通过 TestClient 进入 FastAPI 请求处理线程。同步路由先调用一次 `embed_texts`（中文和韩文各一句），再调用一次 `generate_text`；与原适配器签名一致。没有子进程或替代 RAG 服务。正常启动 `uvicorn app.main:app` 不包含诊断路由，也不需要为本测试启动数据库。

成功报告必须包含 `passed=true`、`same_process=true`、两个 `[1024, 1024]` 向量维度、正数 `text_characters`，以及 embedding/generation/driver 一致的 pid；退出码为 0。失败报告包含阶段与异常类型，HTTP 为 503，退出码为 1。脚本不输出密钥、服务响应正文、生成文本或底层异常内容。

## 收尾验收清单

- [ ] 在上述 FastAPI 请求中完成真实 BGE-M3 推理，输出两个 1024 维向量。
- [ ] 同一进程完成真实生成调用，返回非空文本。
- [ ] 记录实际版本、日期和无敏感信息的结果摘要。
- [ ] 孙忠其确认 `embed_texts(list[str])` / `generate_text(str)` 的调用方式与他的适配器用法一致（必须由本人确认）。
- [ ] 全部通过后，在 Issue #5 写明“锁定 FastAPI”、Python 版本、requirements 文件链接和实际验收结果。
- [ ] 若失败，记录具体阶段、异常类型、冲突的库和版本，按 Issue 要求由负责人告诉 PM；配置缺失不能写成库冲突。

可供真实验收后填写的 Issue 文案：

> 锁定 FastAPI。验收日期：___；Python：___；依赖：___（requirements 文件链接）。同一 FastAPI 进程 PID ___ 内，embed_texts 调用一次返回 2×1024 维有限非零向量；generate_text 调用一次返回非空文本（___ 字符）；退出码 0。孙忠其调用方式确认：___（确认链接）。

上述文案是待填写模板，不代表已经通过。本次由 Codex 辅助实现脚本、检查版本兼容并执行测试；真实模型验收和组员确认仍待完成。

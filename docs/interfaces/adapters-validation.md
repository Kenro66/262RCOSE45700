# Issue #10：嵌入与生成适配器验收说明

## 工作范围

仅包含嵌入和生成模型的调用、配置及验收，不包含切分、检索、提示词构造或引用校验。公共契约 `backend/app/contracts.py` 未修改。

## 运行环境与安装

在本机 Windows 的 Python 3.14.2 环境中，可以解析到依赖包，但 PyTorch 2.14.1、2.14.1+cpu 和 2.9.1+cpu 均在导入时出现 `WinError 1114`，涉及 `c10.dll`。这是本机运行错误，不代表 Python 3.14 普遍不受支持。

本地 RAG 验收使用 Python 3.12 和 CPU PyTorch 2.6.0。团队 FastAPI 若继续使用 Python 3.14，直接同进程集成时仍需解决环境兼容问题；当前未额外实现独立的 RAG 服务。

从仓库根目录执行以下命令，在仓库外创建 Python 3.12 虚拟环境：

```powershell
python -m venv D:\RAG-Dev\venvs\rag-py312
$python = 'D:\RAG-Dev\venvs\rag-py312\Scripts\python.exe'
& $python -m pip install -r backend/requirements-cpu.txt
& $python -m pip install -r backend/requirements.txt
cd backend
& $python -m unittest discover -s tests -v
& $python smoke_embedding.py
& $python smoke_generation.py
```

如果 `python` 指向其他版本，请在创建虚拟环境时使用 Python 3.12 可执行文件的完整路径。以 `backend/.env.example` 为模板配置本地 `backend/.env`；真实 `.env` 已被 Git 忽略。模型文件应存放在仓库外。

SentencePiece 0.2.2 曾在本机出现原生访问冲突，已验证的依赖版本固定为 0.2.0。

## 嵌入适配器

提供以下同步接口：

```python
embed_texts(texts: list[str]) -> list[list[float]]
embed_chunks(chunks: list[Chunk]) -> list[EmbeddingRecord]
```

`embed_chunks()` 遵循现有“章节标题 + 正文”的输入规则；即使存在标题，也拒绝正文仅为空白的输入。输出保持输入顺序和 chunk_id 对应关系，不修改原始正文。空列表返回 `[]`。

无效输入抛出 `ValueError`；模型、配置或向量异常抛出 `EmbeddingError`，模型加载及推理异常保留底层原因。

配置说明：

- `EMBEDDING_MODEL`：填写 `BAAI/bge-m3` 或已下载模型目录。
- `EMBEDDING_BASE_URL`：CPU 模式留空。
- `EMBEDDING_CACHE_DIR`：指定模型下载缓存位置。
- 批量大小默认 1，CPU 线程数默认 1，最大长度默认 8192；均可配置。
- 当前 MVP 向量维度固定为 1024，使用 float32，仅返回 Dense Embedding。

CPU 线程数通过 PyTorch 设置，会影响当前进程。NPU 远程嵌入协议尚未实现；若配置非空嵌入服务地址，会明确报告暂不支持，不会静默回退 CPU。这种报错不能当作已经完成了远程嵌入连接失败测试。

## 生成适配器

2026-10-25，经负责人确认，临时采用 OpenAI-compatible HTTP 协议，不代表已经确定最终生成模型。

配置说明：

- `GENERATION_MODEL`：实际服务提供的模型 ID。
- `GENERATION_BASE_URL`：API 根地址；服务需要 `/v1` 等前缀时应一并填写。
- `GENERATION_API_KEY`：仅填写在本地 `.env`；不需要鉴权的本地服务可留空，不要把密钥写进 URL。
- `GENERATION_TIMEOUT_SECONDS`：默认 60 秒，连接超时最多 10 秒。

适配器自动在根地址后附加 `/chat/completions`。请求为同步、非流式调用，不自动重试。

`generate_text(messages, tools=None) -> str` 接受问题字符串或聊天消息字典列表，返回非空文本。

模型可能只返回工具调用时，应使用 `generate(messages, tools=None) -> GenerationResult`，同时保留文本和 OpenAI-compatible 格式的函数 `tool_calls`。适配器不负责执行工具。

空响应、格式错误、服务错误和配置错误均抛出 `GenerationError`；错误信息不包含密钥或服务响应正文。

## 验收证据与测试范围

### 真实 CPU 嵌入测试

使用一句中文和一句韩文调用 BGE-M3，返回两个 1024 维、数值有限且非零的向量。L2 范数约为：

```text
[0.9999999782, 0.9999999811]
```

模型版本：

```text
5617a9f61b028005a4858fdac845db406aefb181
```

权重 SHA-256：

```text
b5e0ce3470abf5ef3831aa1bd5553b486803e83251590ab7ff35a117cf6aad38
```

嵌入配置切换时，先将 `EMBEDDING_MODEL` 指向以下本地目录：

```text
D:/RAG-Dev/models/huggingface/hub/models--BAAI--bge-m3/snapshots/5617a9f61b028005a4858fdac845db406aefb181
```

之后只编辑 `.env`，将 `EMBEDDING_MODEL` 改为 `BAAI/bge-m3`，并将 `EMBEDDING_CACHE_DIR` 指向相同 hub 缓存。两次离线运行均成功，向量样本一致。这验证的是同一模型的两种加载标识，不是两种不同嵌入模型的切换。

### 测试替身与本地 HTTP 测试

嵌入单元测试使用替身模型输出。生成测试启动真实本地 HTTP 服务，但返回固定测试数据，不是 LLM 生成的答案。

覆盖内容包括：非空 `tools` 参数、仅包含工具调用的响应、非法或空响应、HTTP 503，以及真实请求不监听端口时抛出 `GenerationError`。

配置切换测试只编辑临时 `.env`，将 `GENERATION_MODEL` 从 `model-a` 改为 `model-b`，API 路径从 `/api-a` 改为 `/api-b`；HTTP 服务实际收到了变化后的模型名和请求路径。这些只是测试标识，真实模型切换另按下文验证。

### 学校真实 LLM 调用

2026-10-25，使用学校 Baze 网关 `https://api.baze.co/v1/gateway` 和模型 ID `gpt-6-sol` 运行 `backend/smoke_generation.py`，退出码为 0，输出如下：

```text
Non-empty generated text: PASS
Request with non-empty tools: PASS
```

这两次请求使用本地配置的学校密钥访问真实服务，验证了非空生成文本和非空 `tools` 参数可被接受；不宣称模型实际选择或执行了工具。

测试未记录密钥或响应正文，`backend/.env` 已确认被 Git 忽略。此模型仅用于临时开发验收，不代表最终 NPU 模型选择。

## 仅修改 .env 的真实模型切换

测试日期：2026-10-25。学校网关经鉴权返回的模型列表同时包含 `gpt-6-sol` 和 `gpt-6-luna`。

通过现有 `generate_text()` 适配器，对同一个问题进行三次调用：

```text
What is retrieval augmented generation? Reply in one short sentence.
```

| 阶段 | 仅修改 .env 中的配置 | 非空响应 | 响应字符数 |
| --- | --- | --- | --- |
| 切换前 | `GENERATION_MODEL=gpt-6-sol` | 通过 | 107 |
| 切换后 | `GENERATION_MODEL=gpt-6-luna` | 通过 | 154 |
| 恢复原模型 | `GENERATION_MODEL=gpt-6-sol` | 通过 | 121 |

三次调用均保持 `GENERATION_BASE_URL=https://api.baze.co/v1/gateway` 不变。`config.py`、`embedding.py` 和 `generation.py` 的文件哈希完全一致；其余 `.env` 配置摘要也一致，确认只更改了模型名。测试后已恢复原模型。

上述调用未修改代码、未使用其他客户端、未通过进程环境变量覆盖模型名，也未使用测试替身服务。密钥未输出，两种模型均只是临时测试选择。

此结果满足“只改模型名或地址”的模型名切换条件，不宣称测试过第二个真实网关地址。Issue #10 的本地功能验收已通过；GitHub 提交、PR 审核及团队运行环境联调需分别确认，不能与功能验收混为一谈。

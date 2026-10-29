# Issue #11：MVP 单次向量检索接口

本文件描述 #11 检索器的异步调用方式。已在隔离整合环境完成真实 Postgres/pgvector 检索；使用了本地临时数据库初始化和应用装配层，正式后端基线仍需复测。公共 `contracts.py` 不变。证据见 [真实联调记录](issue11-real-integration.md)。

## 调用方式

```python
async def retrieve(question, *, search_similar, top_k=None, embedder=None) -> list[SearchHit]
# 调用：hits = await retrieve(question, search_similar=backend_search)
```

- 默认 `top_k=5`，可通过本地 `.env` 的 `RETRIEVAL_TOP_K` 或显式参数配置，允许 1..100。
- `embedder` 默认使用 #10 的 `embed_texts()`，输入只有一条原始问题文本，输出必须是一个 1024 维有限非零向量。
- `search_similar` 必须由后端注入，不猜测或导入不存在的数据库模块。
- 按用户确认及 PR #28 实现，使用 `await search_similar(query_vector, top_k=top_k)`，返回 `list[SearchHit]`（校验也接受 tuple）。后端承担 cosine 查询、READY 过滤及 score 排序；本模块不重复实现。
- 同步 `embed_texts()` 在线程中调用，核心接口不变；不阻塞异步事件循环。同步搜索函数不再兼容，调用方必须迁移为 async。
- 结果沿用后端顺序，最多返回 top_k 条；不实现 SQL、向量库、BM25、混合检索、重排或国家硬编码过滤。
- 无结果原样返回 `[]`；模型、搜索或返回结构异常抛出 `RetrievalError`，保留底层原因，不伪造证据。
- READY 过滤由后端负责。已在隔离真实数据库中将测试文档临时改为 PROCESSING，确认其片段不再被检索，随后恢复 READY。

## 2026-10-27 异步连接基线（替代此前同步约定）

按用户确认的现有 `contracts.py` 继续开发，不等待数据库，不修改公共契约。
数据契约确定返回字段，不定义搜索函数参数、同步/异步、分数含义或数据库表名。
PR #28 检查提交为 `ec8e359b0b8af518092a15ff909da472b5b7dd97`，其实际 search_similar 是 async。后端可以返回
`SearchHit` 对象或字段兼容的字典，外层为 list 或 tuple；检索器统一校验为
`list[SearchHit]`，保留原始编号、页码、正文、分数和排序，不自行解释或重排分数。

后端须提供已按相关程度排序、仅来自 READY 文件的结果。`SearchHit` 没有状态字段，
检索器无法自行验证 READY；也无法仅凭结构校验证明正文、页码和相关性真实正确。
返回空列表表示无结果；连接失败应抛异常，不能用空列表掩盖错误。
同步调用方须改成 `await retrieve(...)`；FastAPI 事件循环内不得用 asyncio.run 包装。
本轮不实现 SQL、入库、数据库连接或 #12 的回答和引用逻辑。

## 来源关联

`SearchHit` 保留 `chunk_id`、`document_id`、`page_number`、`text`、`score`。它没有 `filename`。

文件名通过 `document_id` 关联现有 C1 `DocumentRecord.filename`，不是向 `SearchHit` 擅自增加字段。PR #28 的异步 get_documents 已在隔离数据库验证；不存在的 ID 从返回映射省略。本次不提交 #12 的 Citation Resolver。

## 测试边界

单元测试使用依赖注入，验证默认 top-k、向量检查、结果结构和失败传播。真实验证脚本另调用本地 BGE-M3 对中文问题生成向量，再调用 Mock 搜索函数。

Mock 返回中国文档记录只证明来源标识被保留。后续真实联调使用姜超题单 Q008，其 Top-5 全部来自中国文件；Q006/Q007 则混入韩国片段，不能一概声称国家来源正确。正式后端装配尚待确认，不宣称整个团队 main 已可直接运行。

## #11 接入示例

后端模块与数据库配置就绪后，由调用方注入实际函数，不把数据库依赖写入 Retriever：

```python
from app.qa.retriever import retrieve
from app.storage.repo import search_similar

hits = await retrieve(question, search_similar=search_similar)
```

入库复用现有 chunk_pages 和 embed_chunks；PR #28 流水线已经按 chunk_id 合并为 ChunkData。
其存储类型要求标准 chunk_id 格式及 language 为 zh/ko/en 或 null，当前输出兼容。
同步 CPU 函数交给异步上传流水线时，调用方可用 asyncio.to_thread 包装，避免阻塞事件循环；
不改变 chunk_pages 或 embed_texts 的公共接口。本次不重复实现存储转换或数据库保存。

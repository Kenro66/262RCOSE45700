# Issue #11：MVP 单次向量检索接口

本文件描述 #11 检索器自身的调用方式，不代表后端 `search_similar` 已确认或实现。公共 `contracts.py` 不变。

## 调用方式

```python
retrieve(question, *, search_similar, top_k=None, embedder=None) -> list[SearchHit]
```

- 默认 `top_k=5`，可通过本地 `.env` 的 `RETRIEVAL_TOP_K` 或显式参数配置，允许 1..100。
- `embedder` 默认使用 #10 的 `embed_texts()`，输入只有一条原始问题文本，输出必须是一个 1024 维有限非零向量。
- `search_similar` 必须由后端注入，不猜测或导入不存在的数据库模块。
- 当前注入协议是同步调用 `search_similar(query_vector, top_k=top_k)`，返回 `list[SearchHit]`（也接受 tuple）。这是检索器的注入约定，后端若采用异步或不同签名，需要双方确认后提供适配，不应当作其真实签名已经定稿。
- 结果沿用后端顺序，最多返回 top_k 条；不实现 SQL、向量库、BM25、混合检索、重排或国家硬编码过滤。
- 无结果原样返回 `[]`；模型、搜索或返回结构异常抛出 `RetrievalError`，保留底层原因，不伪造证据。
- 后端应按现有约定只搜索 READY 文件；本模块无法在数据库尚未提供时验证该过滤。

## 2026-10-27 本地连接基线

按用户确认的现有 `contracts.py` 继续开发，不等待数据库，不修改公共契约。
数据契约确定返回字段，不定义搜索函数参数、同步/异步、分数含义或数据库表名。
同步 `SearchFunction` 是当前本地接入边界，不代表后端已确认。后端可以返回
`SearchHit` 对象或字段兼容的字典，外层为 list 或 tuple；检索器统一校验为
`list[SearchHit]`，保留原始编号、页码、正文、分数和排序，不自行解释或重排分数。

后端须提供已按相关程度排序、仅来自 READY 文件的结果。`SearchHit` 没有状态字段，
检索器无法自行验证 READY；也无法仅凭结构校验证明正文、页码和相关性真实正确。
返回空列表表示无结果；连接失败应抛异常，不能用空列表掩盖错误。
若实际接口为异步，需显式协调异步调用方式，不能直接传给当前同步检索器。
本轮不实现 SQL、入库、数据库连接或 #12 的回答和引用逻辑。

## 来源关联

`SearchHit` 保留 `chunk_id`、`document_id`、`page_number`、`text`、`score`。它没有 `filename`。

文件名应通过 `document_id` 关联现有 C1 `DocumentRecord.filename`，不是向 `SearchHit` 擅自增加字段。真实文件列表接口尚未提供，本次只验证已有标识和文本能原样保留，不实现 #12 的 Citation Resolver。

## 测试边界

单元测试使用依赖注入，验证默认 top-k、向量检查、结果结构和失败传播。真实验证脚本另调用本地 BGE-M3 对中文问题生成向量，再调用 Mock 搜索函数。

Mock 返回中国文档记录只证明来源标识被保留，不能证明真实数据库检索命中中国政策。正式中国方向冒烟题及真实数据库验收仍需姜超和王诗琪提供相应依赖。

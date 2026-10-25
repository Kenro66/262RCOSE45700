# Embedding 接口文档

> 作者：孙忠其（@sun-cs）。v1.0 原为 docx，10/25 转为 md，并按 10/25 组内确认的结果修改了下面列出的几处，其余正文未改动。
>
> 字段定义以仓库 `backend/app/contracts.py` 为准；本文只做说明。两者不一致时以代码为准，并更新本文。
>
> 10/25 修改：MVP 的 embedding_model 定为 bge-m3、embedding_dimension 定为 1024（与 10/23 的决定和数据库 vector(1024) 一致），batch_size 仍待定；第 4 节补充调用方。NPU 上的最终模型确定后，如需更换，按第 9 节规则新建索引并重新生成 Vector。

Version: 1.0  
模块方向: C2 Chunk → Embedding Adapter → Embedding Record  
输入: list[Chunk]  
输出: list[EmbeddingRecord]  
使用阶段: Chunking 完成后、Vector Index / Retrieval 之前  

## 1. 接口说明

Embedding 模块接收 Chunking 模块输出的 C2 Chunk 数据，将每个 Chunk 的文本内容转换为用于后续 Vector Search 的向量表示（Vector）。

MVP 阶段采用以下基本原则：

- Embedding Adapter 直接接收 C2 Chunk 列表。
- 默认使用 section_title + text 构造 Embedding 输入。
- 当 section_title = null 时，仅使用 text。
- 不修改 C2 Chunk 中原始 text 内容。
- 支持 Batch Embedding，一次处理多个 Chunk。
- 每个输出 Vector 必须通过 chunk_id 与原始 C2 Chunk 保持对应关系。
- Embedding Model、Vector Dimension 等全局配置不在每个 Record 中重复保存，而统一记录在 Embedding Config / Index Metadata 中。
- Embedding 失败时不得生成空 Vector 或无效 Record，应抛出 Embedding Error。
- MVP 保留 content_hash 用于后续内容变更检测，但暂不实现自动 Re-embedding。

MVP 配置：

```
embedding_model = bge-m3
embedding_dimension = 1024
batch_size = TBD
```

其中：

- MVP 先用 bge-m3（1024 维）；NPU 可用前在本地用 CPU 运行同一模型。最终模型由 Furiosa NPU 上的可用模型决定。
- embedding_dimension 由所选 Embedding Model 决定，更换模型时按第 9 节新建索引。
- batch_size 根据模型服务、NPU 资源及实际测试结果确定。

## 2. 输入数据

Embedding Adapter 输入使用已约定的 C2 Chunk v1.0。

示例：

```
{
  "schema_version": "1.0",
  "chunk_id": "DOC001_C000003",
  "document_id": "DOC001",
  "chunk_index": 3,
  "page_index": 12,
  "page_number": 13,
  "text": "进境居民旅客携带香烟，免税限量为400支；超出部分应向海关申报。",
  "language": "zh",
  "token_count": 32,
  "char_count": 38,
  "section_title": "三、烟草制品",
  "content_hash": "sha256:..."
}
```

Embedding 模块主要使用以下字段：

```
chunk_id
text
section_title
```

其中：

- chunk_id 用于建立 Vector 与原始 Chunk 的对应关系。
- text 为 Embedding 的主要文本内容。
- section_title 用于补充当前 Chunk 的章节语义。

以下字段不直接作为 MVP Embedding Model 的输入文本：

```
document_id
chunk_index
page_index
page_number
language
token_count
char_count
content_hash
```

这些字段仍保留在 C2 Chunk 中，供 Retrieval、Citation、调试、实验和数据管理使用。

## 3. Embedding 输入构造规则

### 3.1 默认输入策略

MVP 默认采用：

```
section_title + text
```

例如 C2：

```
{
  "section_title": "三、烟草制品",
  "text": "进境居民旅客携带香烟，免税限量为400支。"
}
```

实际传入 Embedding Model 的文本为：

```
三、烟草制品

进境居民旅客携带香烟，免税限量为400支。
```

即：

```
C2 Chunk
   │
   ├── section_title
   └── text
          ↓
build_embedding_text()
          ↓
embedding_text
          ↓
Embedding Model
```

embedding_text 为运行时构造的 Embedding 输入，不替换、不修改 C2 中原始的 text。

### 3.2 section_title 为空

当：

```
section_title = null
```

时，不添加 "Unknown Section" 或其他人工生成内容。直接使用 `text` 作为 Embedding 输入。

例如：

```
{
  "section_title": null,
  "text": "超过免税限量的，应主动向海关申报。"
}
```

实际 Embedding 输入：

```
超过免税限量的，应主动向海关申报。
```

### 3.3 输入构造函数

推荐：

```
def build_embedding_text(chunk: Chunk) -> str:
    if chunk.section_title:
        return f"{chunk.section_title}\n\n{chunk.text}"
    return chunk.text
```

该函数只负责构造 Embedding Model 使用的文本，不修改原始 C2 Chunk。

## 4. 函数接口

Embedding Adapter 推荐对外接口：

```
def embed_chunks(
    chunks: list[Chunk]
) -> list[EmbeddingRecord]:
    ...
```

含义：

```
C2 Chunk[]
      ↓
embed_chunks()
      ↓
Embedding Record[]
```

chunks 为 Chunking 模块输出的 C2 Chunk 列表。

Embedding Adapter 内部负责：

```
读取 Chunk
      ↓
构造 embedding_text
      ↓
Batch Embedding
      ↓
获得 Vector
      ↓
绑定 chunk_id
      ↓
生成 Embedding Record
```

底层模型调用可进一步封装为：

```
def embed_texts(
    texts: list[str]
) -> list[list[float]]:
    ...
```

其中：

- `embed_chunks()` 负责 C2 Chunk 与 Embedding Record 的业务层转换；
- `embed_texts()` 负责实际调用 Embedding Model，将文本批量转换为 Vector。

调用方：

- 文档入库时，Backend（王诗琪）调用 `embed_chunks()`，按 chunk_id 写入向量
- 检索时，RAG（孙忠其）对用户问题调用 `embed_texts()`

## 5. Batch Embedding

当有很多 Chunk 要转换成 Vector 时，是一个一个送给 Embedding 模型，还是一次送一批。

MVP 支持 Batch Embedding。

不要求逐个独立调用：

```
Chunk 1 → Model
Chunk 2 → Model
Chunk 3 → Model
```

允许：

```
Chunk 1 ─┐
Chunk 2  │
Chunk 3  ├→ Embedding Model → Vector 1 / 2 / 3 / ...
...      │
Chunk N ─┘
```

例如：

```
texts = [
    "三、烟草制品\n\n进境居民旅客携带香烟...",
    "四、酒类制品\n\n进境居民旅客携带酒类...",
    "五、其他物品\n\n其他个人物品..."
]
```

模型返回：

```
[
    Vector 1,
    Vector 2,
    Vector 3
]
```

输入文本与输出 Vector 必须保持相同顺序：

```
texts[0] → vectors[0]
texts[1] → vectors[1]
texts[2] → vectors[2]
```

batch_size 不在 v1.0 中固定具体数值。

后续根据：

- Furiosa NPU 可用资源
- Embedding Model
- 模型服务限制
- 实际吞吐量
- 内存使用情况

进行测试后确定。

## 6. 输出数据 · Embedding Record

Embedding Adapter 不只返回裸 Vector，而是返回包含 chunk_id 与 vector 的标准 Embedding Record。

推荐 Schema：

```
{
  "chunk_id": "DOC001_C000003",
  "vector": [
    0.183,
    -0.291,
    0.087
  ]
}
```

其中 vector 示例仅用于说明数据结构，实际 Vector 长度由最终 Embedding Model 决定。

Embedding Record 的核心关系：

```
chunk_id
   ↕
Vector
```

通过 chunk_id 可以进一步回溯：

```
Vector
 ↓
chunk_id
 ↓
C2 Chunk
 ↓
document_id
page_number
text
 ↓
Citation
```

因此 Embedding Record 不需要重复保存 C2 中已有的 document_id、page_number、text 等字段。

## 7. Embedding Record 参数说明

| 字段名 | 类型 | 必填 | 示例 | 说明 |
|---|---|---|---|---|
| chunk_id | String | 是 | "DOC001_C000003" | 对应 C2 Chunk 的唯一标识，用于 Vector 与原始 Chunk 的关联 |
| vector | Array[Float] | 是 | [0.183, -0.291, 0.087, ...] | Embedding Model 生成的向量，实际维度由最终模型决定 |

MVP 中不在每个 Embedding Record 重复保存：

```
embedding_model
embedding_dimension
embedding_version
```

这些信息统一由 Embedding Config / Index Metadata 管理。

## 8. 字段来源规则

Embedding Record 字段分为两类。

### 8.1 从 C2 Chunk 继承

```
chunk_id
```

chunk_id 不重新生成，必须与输入 C2 Chunk 保持完全一致。

例如：

```
C2:
chunk_id = DOC001_C000003
        ↓ Embedding
Embedding Record:
chunk_id = DOC001_C000003
```

不得在 Embedding 阶段重新分配 Chunk ID。

### 8.2 Embedding 新生成

```
vector
```

vector 由 Embedding Model 根据运行时构造的 embedding_text 生成。

例如：

```
section_title + text
        ↓
Embedding Model
        ↓
vector
```

## 9. Embedding Config / Index Metadata

Embedding Model 相关的全局配置不在每个 Embedding Record 中重复保存。

推荐统一维护 Embedding Config / Index Metadata。

示例：

```
{
  "embedding_model": "bge-m3",
  "embedding_dimension": 1024,
  "embedding_version": "v1",
  "input_strategy": "section_title+text",
  "batch_size": null
}
```

### 参数说明

| 字段名 | 类型 | 当前值 | 说明 |
|---|---|---|---|
| embedding_model | String | "bge-m3" | 实际使用的 Embedding Model；MVP 为 bge-m3，最终由 Furiosa NPU 可用模型决定 |
| embedding_dimension | Integer / null | 1024 | Vector Dimension，由所选模型决定 |
| embedding_version | String | "v1" | 当前 Embedding 配置版本，用于后续模型或配置变更管理 |
| input_strategy | String | "section_title+text" | MVP Embedding 输入构造方式 |
| batch_size | Integer / null | null | 单批处理文本数量，后续根据 NPU 和模型测试确定 |

同一个 Vector Index 中原则上应使用统一的：

```
Embedding Model
Vector Dimension
Embedding Version
Input Strategy
```

不同 Embedding Model 生成的 Vector 不应直接混合在同一向量空间中进行相似度比较。

例如：

```
Index v1
├── Model A
├── Dimension 768
└── Vector...
Index v2
├── Model B
├── Dimension 1024
└── Vector...
```

如果后续更换 Embedding Model，应建立新的 Embedding 配置，并根据需要重新生成 Vector。

## 10. content_hash 与 Re-embedding

C2 Chunk 中已经保存：

```
content_hash
```

该字段描述当前 Chunk text 的内容状态。

例如：

```
原 Chunk
content_hash = ABC
       ↓
Embedding
       ↓
Vector
```

当 PDF 更新并重新 Chunking 后：

```
新 Chunk
content_hash = XYZ
```

理论上可以通过 Hash 变化判断：

```
Chunk 内容发生变化
        ↓
旧 Vector 可能已经失效
        ↓
需要重新 Embedding
```

MVP 阶段：

- 保留 content_hash
- 不要求实现自动 Re-embedding
- PDF / Chunk 更新后的自动 Vector 更新机制放入 Later

## 11. 异常处理

### 11.1 空输入

当：

```
chunks = []
```

时，允许直接返回 `[]`，无需调用 Embedding Model。

### 11.2 Chunk text 为空

正常情况下 Chunking 模块不应输出空 text。

如果 Embedding Adapter 收到：

```
text = ""
```

或仅包含空白字符，应视为无效输入，不生成 Embedding Record。

### 11.3 Embedding Model 调用失败

例如：

- NPU 服务不可用
- 请求超时
- 模型加载失败
- 推理失败

不得生成：

```
{
  "chunk_id": "DOC001_C000003",
  "vector": []
}
```

也不得使用零向量代替正常结果。

应抛出 Embedding Error，由上层模块统一处理。

### 11.4 Vector Dimension 异常

最终 Embedding Model 确定后，每个 Vector 的长度必须等于：

```
embedding_dimension
```

例如 `embedding_dimension = 1024`，则必须满足 `len(vector) = 1024`。

如果实际返回维度不一致，应视为 Embedding Error，不写入 Vector Index。

### 11.5 Batch 数量不一致

如果输入：

```
10 texts
```

模型却返回：

```
9 vectors
```

则该 Batch 应视为异常。必须满足：

```
len(input_texts) == len(output_vectors)
```

以防止 Vector 与 chunk_id 错位。

## 12. MVP 与 Later 范围

### MVP

- 接收 C2 Chunk[]
- section_title + text 输入策略
- section_title = null 时仅使用 text
- Batch Embedding
- chunk_id ↔ vector 映射
- Embedding Record
- Embedding Config / Index Metadata
- Vector Dimension 校验
- Batch 输入输出数量校验
- Embedding Error 处理
- 保留 content_hash
- Embedding Model = bge-m3
- Embedding Dimension = 1024
- Batch Size = TBD

### Later 可扩展

- 自动 Re-embedding
- 基于 content_hash 的增量更新
- 多 Embedding Model 对比
- 不同 Input Strategy 对比
- text only vs section_title + text
- document_title + section_title + text
- Batch Size 性能调优
- Embedding Cache
- Embedding Model Version Migration
- Index Version Management

Later 功能应尽量通过 Config、Optional Metadata 或新的 Schema Version 扩展，避免破坏现有 C2 → Embedding 接口。

## 13. 接口兼容规则

Embedding Interface v1.0 确定后：

- chunk_id 的含义不得随意修改。
- vector 必须始终与对应 chunk_id 保持一一对应。
- Embedding Adapter 不修改 C2 原始 text。
- 同一个 Vector Index 原则上使用统一 Embedding Model 和 Dimension。
- 更换 Embedding Model 时，应同步检查 Vector Dimension 和 Vector Index 配置。
- 如果 Embedding Record Schema 发生不兼容修改，应升级接口版本。
- Embedding Model 或输入策略变化不一定要求修改 C2 Schema。

## 14. MVP 最终接口

整体数据流：

```
Backend PDF Parser
        ↓
ParsedPage[]
        ↓
Chunking
        ↓
C2 Chunk[]
        ↓
Embedding Adapter
        │
        ├── build_embedding_text()
        │
        ├── section_title + text
        │
        └── Batch Embedding
        ↓
Embedding Record[]
        │
        ├── chunk_id
        └── vector
        ↓
Vector Index / Vector DB
        ↓
Retrieval
```

同时维护：

```
Embedding Config / Index Metadata
        │
        ├── embedding_model = bge-m3
        ├── embedding_dimension = 1024
        ├── embedding_version = v1
        ├── input_strategy = section_title+text
        └── batch_size = TBD
```

Embedding 模块的职责到：

```
C2 Chunk[]
        ↓
Embedding
        ↓
稳定的 chunk_id ↔ vector 映射
```

为止。

Top-k、Similarity Search、Vector / BM25 / Hybrid Search、Reranking、SearchResult 等内容不属于本接口，由后续 Retrieval Interface 单独定义。

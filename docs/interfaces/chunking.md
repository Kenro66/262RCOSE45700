# Chunking 接口文档

> 作者：孙忠其（@sun-cs）。v1.0 原为 docx，10/25 转为 md，并按 10/25 组内确认的结果修改了下面列出的几处，其余正文未改动。
>
> 字段定义以仓库 `backend/app/contracts.py` 为准；本文只做说明。两者不一致时以代码为准，并更新本文。
>
> 10/25 修改：第 8 节 Document Metadata 的 `file_name` 统一为 `filename`（C1 文件记录的字段名）；第 9 节补充"所有页都为空"的处理。
>
> 字段命名分内外两层：本文的 ParsedPage、C2 用 `page_number`、`language`；对外接口（C1、C5、C6）用 `filename`、`page`、`lang`，只在引用校验里转换。

Version: 1.0  
模块方向: ParsedPage → Chunking → C2 Chunk  
输入: list[ParsedPage]  
输出: list[Chunk]  
使用阶段: PDF 解析完成后、Embedding 之前  

## 1. 接口说明

Chunking 模块接收 Backend PDF Parser 输出的 ParsedPage 数据，将页面文本切分为适合后续 Embedding 与 Retrieval 的文本块（Chunk）。

MVP 阶段采用以下基本原则：

- 单个 Chunk 不跨页，每个 Chunk 只对应一个 PDF 页面。
- 优先按照页面原始段落结构进行切分。
- 首先按照空行识别段落。
- 当单个段落超过 Chunk 长度上限时，再按照句子边界继续切分。
- 必要时再进行 Token-level 切分，避免 Chunk 超过长度上限。
- Chunk 必须保留原始文档和页码信息，以支持后续 Citation。
- Chunking 输出作为后续 Embedding、Vector Search 和 Citation 的基础数据。

MVP 初始配置：

```
max_chunk_size = 500 tokens
target_overlap = 75 tokens
cross_page = false
```

其中 500 tokens 为长度上限，不要求所有 Chunk 都达到 500 tokens。

75 tokens 为长文本需要进一步切分时的目标重叠量，不强制在所有自然段之间添加重叠。

## 2. 函数接口

推荐函数定义：

```
def chunk_pages(
    pages: list[ParsedPage]
) -> list[Chunk]:
    ...
```

含义：

```
ParsedPage[]
      ↓
chunk_pages()
      ↓
Chunk[]
```

pages 为 Backend PDF Parser 输出的 ParsedPage 列表。

函数完成文本切分并返回 C2 Chunk 列表。

## 3. 输入数据

Chunking 模块输入使用已约定的 ParsedPage v1.0。

示例：

```
{
  "schema_version": "1.0",
  "document_id": "DOC001",
  "page_index": 12,
  "page_number": 13,
  "text": "三、烟草制品\n\n进境居民旅客携带香烟，免税限量为400支；超出部分应向海关申报。",
  "language": "zh",
  "content_hash": "sha256:...",
  "char_count": 583,
  "parser_version": "pymupdf-1",
  "blocks": null
}
```

Chunking 模块主要依赖以下字段：

- document_id
- page_index
- page_number
- text
- language

content_hash、char_count、parser_version、blocks 不作为 MVP Chunking 正常运行的必要依赖。

## 4. 切分规则

### 4.1 页面边界

MVP 不进行跨页拼接。

例如：

```
Page 13
 ├── Chunk 1
 ├── Chunk 2
 └── Chunk 3
Page 14
 ├── Chunk 4
 └── Chunk 5
```

禁止：

```
Page 13 + Page 14
        ↓
     Chunk 4
```

因此每个 Chunk 可以明确对应唯一的 page_index 和 page_number。

### 4.2 结构优先切分

基本顺序：

```
ParsedPage.text
      ↓
按照空行识别段落
      ↓
段落是否超过 max_chunk_size？
      │
   否 ─────→ 保留完整结构
      │
   是
      ↓
按照句子边界继续切分
      ↓
仍然超过长度上限
      ↓
Token-level Split
```

应尽量避免在完整条款、段落或句子的中间进行强制切分。

### 4.3 Chunk Size

MVP 初始长度上限：

```
500 tokens
```

500 tokens 是最大长度控制参数，不代表每个 Chunk 必须固定为 500 tokens。

例如以下 Chunk 均属于正常情况：

```
Chunk A = 183 tokens
Chunk B = 327 tokens
Chunk C = 491 tokens
```

后续实验可比较 300 / 500 / 800 tokens 不同配置下的 Retrieval 与回答效果。

### 4.4 Overlap

MVP 初始目标重叠量：

```
75 tokens
```

Overlap 主要用于单个长结构单元必须进一步切分的情况，以减少切分边界造成的上下文丢失。

自然完整的独立段落之间不要求机械添加 75 tokens 的重叠。

## 5. 输出数据 · C2 Chunk

C2 Chunk：Chunking 后生成的标准文本块。每个 C2 包含切分后的正文，以及文档 ID、页码、语言、章节等来源信息，供后续 Embedding、检索和 Citation 使用。

推荐 C2 Chunk Schema：

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

## 6. C2 参数说明

| 字段名 | 类型 | 必填 | 示例 | 说明 |
|---|---|---|---|---|
| schema_version | String | 是 | "1.0" | C2 Chunk 数据结构版本 |
| chunk_id | String | 是 | "DOC001_C000003" | Chunk 唯一标识 |
| document_id | String | 是 | "DOC001" | 来源 PDF 文档唯一标识 |
| chunk_index | Integer | 是 | 3 | Chunk 在当前文档中的顺序编号 |
| page_index | Integer | 是 | 12 | 来源 PDF 内部页索引，0-based |
| page_number | Integer | 是 | 13 | 用户可见 PDF 页码，1-based，用于 Citation |
| text | String | 是 | "进境居民旅客..." | 当前 Chunk 的实际文本，也是后续 Embedding 的主要输入 |
| language | String / null | 是 | "zh" | Chunk 主要语言，推荐 ko / zh / en；无法判断时允许 null |
| token_count | Integer | 是 | 32 | 当前 Chunk 的 Token 数，用于长度控制及后续 Chunk Size 实验 |
| char_count | Integer / null | 是 | 38 | 当前 Chunk 文本字符数，可用于调试与统计 |
| section_title | String / null | 是 | "三、烟草制品" | 当前 Chunk 所属章节或条款标题；无法识别时允许 null |
| content_hash | String / null | 是 | "sha256:..." | 当前 Chunk text 的 SHA-256，用于内容变更检查及后续重新 Embedding 判断 |

## 7. 字段来源规则

C2 Chunk 字段分为三类。

### 7.1 从 ParsedPage 继承

```
document_id
page_index
page_number
language
```

这些字段用于保持 Chunk 与原始 PDF 页面的来源关系。

### 7.2 Chunking 新生成

```
chunk_id
chunk_index
text
token_count
section_title
```

其中 text 为 ParsedPage 页面文本经过 Chunking 后得到的当前文本块内容。

### 7.3 根据 Chunk 重新计算

```
char_count
content_hash
```

不得直接复制 ParsedPage 的 char_count 和 content_hash。

ParsedPage 的值描述整页文本，而 C2 中的值描述当前 Chunk 文本。

## 8. Citation 来源要求

MVP 中每个 Chunk 只属于一个页面，因此：

```
Chunk
 ↓
document_id
 ↓
Document Metadata（C1）
 ↓
filename
```

同时：

```
Chunk
 ↓
page_number
 ↓
Citation 页码
```

因此后续 Citation 可以通过 document_id + page_number + text 回溯到：

- 文件名
- PDF 实际页码
- 证据原文

LLM 不负责自行生成文件名或页码。

## 9. 异常情况

### 页面文本为空

当：

```
text = ""
```

或仅包含空白字符时，不生成 Chunk。

如果所有页面的 text 都为空，`chunk_pages()` 返回空列表 `[]`，不抛异常；由 Backend 据此把整份文档判为 FAILED（error_message 写明"未提取到文字"）。

### 无法识别章节标题

允许：

```
"section_title": null
```

不得因为无法识别标题导致整个页面 Chunking 失败。

### 无法确定语言

允许：

```
"language": null
```

### 单个句子超过长度上限

当一个完整句子本身超过 max_chunk_size 时，允许使用 Token-level Split（MVP阶段为500token） 作为最后的切分手段。

## 10. MVP 与 Later 范围

MVP：

- 单页 Chunking
- 段落优先
- 句子二次切分
- 500 tokens 长度上限
- 必要时约 75 tokens overlap
- page mapping
- section_title
- token_count

Later 可扩展：

- 跨页结构合并
- section_path
- source_block_ids
- 表格感知 Chunking
- OCR 内容 Chunking
- Neighbor Chunk Expansion
- 更复杂的 Semantic Chunking

Later 字段应优先作为 Optional 字段新增，避免破坏现有 C2 接口。

## 11. 接口兼容规则

C2 Chunk v1.0 确定后，现有字段的名称、类型和含义原则上不得随意修改。

后续增加功能时，优先增加 Optional 字段。

如果必须修改已有字段的类型或语义，应升级 Schema Version，并同步修改 Chunking、Embedding、Retrieval 和 Citation 相关模块。

## 12. MVP 最终接口

```
Backend PDF Parser
        ↓
ParsedPage[]
        ↓
chunk_pages()
        ↓
C2 Chunk[]
        ↓
Embedding Adapter
```

Chunking 模块的职责到生成稳定的 C2 Chunk[] 为止。

Embedding 模型、向量维度、Vector DB 写入方式等内容不属于本接口，由后续 Embedding Interface 单独定义。

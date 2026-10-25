# ParsedPage 接口文档（后端切分用）

> 作者：孙忠其（@sun-cs）。v1.0 原为 docx，10/25 转为 md，并按 10/25 组内确认的结果修改了下面列出的几处，其余正文未改动。
>
> 字段定义以仓库 `backend/app/contracts.py` 为准；本文只做说明。两者不一致时以代码为准，并更新本文。
>
> 10/25 修改：第 7 节标题由"用于重复文件检测"改为"用于内容变更检测"。上传不去重，同一文件上传两次仍为两条独立记录。

接口版本： v1.0  
接口名称： ParsedPage  
接口方向： Backend PDF Parser → RAG Chunker  
数据格式： JSON  
使用阶段： PDF 解析完成后、文档 Chunking 前  

## 1. 接口说明

### 接口功能

本接口用于将 Backend PDF Parser 解析后的单页 PDF 文本及相关元数据传递给 RAG Chunker。

Backend 负责按页解析 PDF，并保留页面与原 PDF 的对应关系。

RAG 模块接收到 ParsedPage 后，负责后续：

- 文档结构识别
- Chunking
- Token 计算
- Embedding
- Retrieval
- Citation

### 核心约定

- 一个 ParsedPage 对应 PDF 中的一页。
- page_index 从 0 开始，用于程序内部定位。
- page_number 从 1 开始，用于用户界面及 Citation 页码展示。
- text 应尽可能保留原 PDF 的换行、段落和条款结构，不得无必要地合并成单行文本。
- Parser 不负责 Chunking，不生成 chunk_id、token_count、section_path 等 Chunk 字段。
- content_hash 对当前页面解析后的 text 计算 SHA-256。
- blocks 为后续 OCR、PDF 高亮、表格及版面分析预留字段，MVP 阶段允许为 null。

## 2. 数据示例

```
{
  "schema_version": "1.0",
  "document_id": "DOC001",
  "page_index": 12,
  "page_number": 13,
  "text": "제3조 여행자 휴대품\n전자담배의 면세 반입 한도는 ...",
  "language": "ko",
  "content_hash": "sha256:8f14e45f...",
  "char_count": 583,
  "parser_version": "pymupdf-1",
  "blocks": null
}
```

## 3. 参数说明

| 字段名 | 类型 | 必填 | 示例 | 说明 |
|---|---|---|---|---|
| schema_version | String | 是 | "1.0" | ParsedPage 数据结构版本，用于后续接口兼容 |
| document_id | String | 是 | "DOC001" | PDF 文档唯一标识，用于关联 Document Metadata |
| page_index | Integer | 是 | 12 | PDF 内部页索引，0-based |
| page_number | Integer | 是 | 13 | 用户可见 PDF 页码，1-based，用于 Citation |
| text | String | 是 | "제3조..." | 当前页面解析文本，应尽量保留换行、段落及条款结构 |
| language | String / null | 是 | "ko" | 页面主要语言，推荐 ko / zh / en；无法判断时允许为 null （推荐加） |
| content_hash | String / null | 否 | "sha256:..." | 当前页面 text 的 SHA-256，用于内容变更及完整性检查（推荐加） |
| char_count | Integer / null | 否 | 583 | 当前页面解析文本字符数，可用于解析质量检查（可不加） |
| parser_version | String / null | 否 | "pymupdf-1" | 生成该解析结果的 Parser 实现/版本，用于问题追踪（可不加） |
| blocks | Array / null | 否 | null | 页面结构化内容，MVP 允许为 null；为后续 OCR、表格解析、PDF 高亮及位置定位预留 （推荐加） |

## 4. 页码说明

page_index 和 page_number 必须区分。

例如 PDF 的第 13 页：

```
{
  "page_index": 12,
  "page_number": 13
}
```

### page_index

```
0, 1, 2, 3 ...
```

供程序内部定位 PDF 页面使用。

### page_number

```
1, 2, 3, 4 ...
```

供用户界面和 Citation 展示使用。

例如：

来源：korea_customs_2026.pdf，第 13 页

## 5. text 解析要求

Backend Parser 输出 text 时，应尽可能保留原 PDF 的结构信息。

### 推荐

```
제1조 적용 범위
본 규정은 여행자가 휴대하여 반입하는 물품에 적용한다.

제2조 면세 범위
여행자 휴대품의 면세 범위는 ...
```

### 不推荐

```
제1조 적용 범위 본 규정은 여행자가 휴대하여 반입하는 물품에 적용한다. 제2조 면세 범위 여행자 휴대품의 면세 범위는 ...
```

原因：

RAG 模块后续采用结构优先 Chunking，需要利用标题、条款、段落、换行、编号进行结构识别和切分。

## 6. language 取值

推荐统一使用：

| 值 | 含义 |
|---|---|
| ko | 韩语 |
| zh | 中文 |
| en | 英语 |
| null | 未识别或暂未检测 |

语言识别失败不应导致整个 PDF 解析失败。

## 7. content_hash 规则（用于内容变更检测）

content_hash 表示当前页面解析后 text 内容的 SHA-256。

示例：

```
"content_hash": "sha256:8f14e45f..."
```

主要用于：

- 判断页面文本是否发生变化
- 避免不必要的重复处理
- 判断是否需要重新 Chunk / Embedding
- Parser 调试与数据完整性检查

注意：

```
content_hash = 当前 ParsedPage.text 的 Hash
file_hash    = 整份原始 PDF 文件的 Hash
```

二者不得混用。

## 8. blocks 字段

MVP 阶段：

```
"blocks": null
```

该字段为后续功能预留。

未来可以扩展为：

```
{
  "blocks": [
    {
      "block_id": "B001",
      "block_type": "heading",
      "text": "제2조 면세 범위",
      "bbox": [72, 105, 510, 132]
    },
    {
      "block_id": "B002",
      "block_type": "paragraph",
      "text": "여행자 휴대품의 면세 범위는 ...",
      "bbox": [72, 145, 510, 220]
    }
  ]
}
```

可用于后续：

- PDF 原文高亮
- OCR
- 标题/段落识别
- 表格解析
- Layout Analysis
- Citation 精确定位

MVP 阶段 blocks = null 不影响正常 Chunking。

## 9. 完整调用案例

### 输入文件

```
korea_customs_2026.pdf
```

假设 Backend Parser 正在处理该 PDF 第 13 页。

### PDF 第 13 页原文

```
제3조 여행자 휴대품

전자담배의 면세 반입 한도는 ...

기타 세부 기준은 다음과 같다.
```

### Parser 输出

```
{
  "schema_version": "1.0",
  "document_id": "DOC001",
  "page_index": 12,
  "page_number": 13,
  "text": "제3조 여행자 휴대품\n\n전자담배의 면세 반입 한도는 ...\n\n기타 세부 기준은 다음과 같다.",
  "language": "ko",
  "content_hash": "sha256:8f14e45f...",
  "char_count": 583,
  "parser_version": "pymupdf-1",
  "blocks": null
}
```

### RAG 接收后

RAG 模块直接读取：

```
page.document_id
page.page_index
page.page_number
page.text
```

并进入：

```
ParsedPage
    ↓
Structure Detection
    ↓
Chunking
    ↓
Chunk[]
    ↓
Embedding
    ↓
Vector Retrieval
```

Backend 无需参与 Chunk Size、Overlap、Token Count 等 RAG 参数计算。

## 10. 异常情况

### 页面可以正常解析

正常生成 ParsedPage。

### 页面文本为空

如果页面合法但没有可提取文本：

```
{
  "schema_version": "1.0",
  "document_id": "DOC001",
  "page_index": 12,
  "page_number": 13,
  "text": "",
  "language": null,
  "content_hash": "sha256:...",
  "char_count": 0,
  "parser_version": "pymupdf-1",
  "blocks": null
}
```

具体是否导致整个文档进入 FAILED，由 Backend 的文档解析规则决定。

### Parser 执行异常

例如：

```
PDF corrupted
Parser exception
Unsupported file
```

不生成伪造的正常 ParsedPage，由 Backend 按异常处理流程处理。

## 11. 接口兼容规则

当前版本：

```
ParsedPage Schema v1.0
```

v1.0 定稿后：

允许：

- 新增 Optional 字段

未经接口版本升级不得：

- 删除现有字段
- 修改字段名
- 修改字段类型
- 修改字段原有语义
- 改变 page_index / page_number 的定义

后续 OCR、高亮、表格等功能优先通过 Optional 字段扩展，避免影响现有 Backend 与 RAG 代码。

## 12. MVP 最终接口

开发时如无特殊需求，可直接按照以下结构实现：

```
{
  "schema_version": "1.0",
  "document_id": "DOC001",
  "page_index": 12,
  "page_number": 13,
  "text": "保留原始换行和段落结构的页面文本",
  "language": "ko",
  "content_hash": "sha256:...",
  "char_count": 583,
  "parser_version": "pymupdf-1",
  "blocks": null
}
```

核心要求：

> Backend 将 PDF 解析为带 document_id、真实页码映射及页面文本的 ParsedPage；RAG 从 ParsedPage 开始负责 Chunking 及后续处理。
>
> text 应尽可能保留原 PDF 的换行、段落和条款结构。
>
> page_index 用于程序定位，page_number 用于 Citation 展示，两者不得混用。

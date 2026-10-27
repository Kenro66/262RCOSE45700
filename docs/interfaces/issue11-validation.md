# Issue #11：实现约定与本地验收

> 最新状态：2026-10-27 已完成隔离环境真实入库和向量检索，详见 [真实联调记录](issue11-real-integration.md)。此前“尚未联调”的段落是对应日期的历史记录，不代表当前全部未测；正式后端装配及召回质量问题仍待处理。

## 分支依赖与范围

- 开发时基线是当时未合并 PR #22 的提交 `05992668a22472852ea093fb322640dcd605590e`；它包含当时上游 main `07eba8a`。PR 合并状态及最新 main 须在提交前重新核查，不能把此历史快照当作实时状态。
- #11 分支为 `feat/issue-11-chunking-retrieval`。未复制或重写 #10 的适配器，未修改 `contracts.py`。
- PR #23 的 PDF 只作外部验收材料，不在 #11 重复提交；JSON 通过 `POLICY_FIXTURE_DIR` 或验证脚本参数指定。
- #22 合并后需整理依赖基线，使最终 #11 PR 仅呈现 #11 的增量，不能把 #10 误报为本次新实现。
- 本轮不 push、不创建 PR，不实现数据库、Prompt、Orchestrator 或 Citation Resolver。

## 正式接口与字段

```python
chunk_pages(pages: list[ParsedPage]) -> list[Chunk]
```

严格使用现有模型。文档 JSON 的 `pages` 字段在调用方逐条校验为 `ParsedPage`；切分函数不解析整个文档响应、不修改根层 `Ready` 状态，也不决定整份文档 FAILED。

每个输出填齐 C2 字段：schema_version、chunk_id、document_id、chunk_index、page_index、page_number、text、section_title、language、token_count、char_count、content_hash。字符数和 SHA-256 对输出正文重新计算。chunk_index 在各文档内从 0 递增，chunk_id 采用 `{document_id}_C{chunk_index:06d}`；相同输入及配置产生相同 ID。

空列表和全部为空白文本时直接返回 `[]`，不加载 tokenizer。重复页、逆序页或 page_number 与 page_index+1 不一致时明确报错，不伪造页码。

## 切分策略与优先级

1. 先按物理页隔离，绝不把两页正文合成一个 Chunk。
2. 优先识别中文章、节、条和韩文 장、절、조；完整条款不超过 500 tokens 时整体保留。
3. 超长条款才递归尝试款项标识、空行段落、完整句子、换行、空格，最后使用 BGE tokenizer 的字符偏移兜底。款项包括 `（一）`、`1.`、`①`、`가.` 等。
4. 超长结构内部才加入目标约 75 tokens overlap；优先让重叠从完整句子或款项边界开始，允许约 60..90 tokens。无法对齐时按 token 偏移保留上下文，再次计数保证正文不超过 500 tokens。
5. 尽量不在“但／다만”等例外引导语之前切断；不把短条款中的条件、例外、数量、金额和单位拆开。
6. 硬约束优先于语义目标。极长单句可能必须拆分，审计报告记录 token、行或空格兜底的语义风险。跨页延续只继承 section_title 提示并记录来源页，不拼接正文。

这里的结构和例外识别是保守规则，不是法律语义理解模型，不能保证所有复杂规定都完全避免语义拆分。

## Token 计数

复用 BGE-M3 的 `XLMRobertaTokenizerFast`，不使用字符数代替 tokens。`token_count` 对最终 Chunk 正文按 `add_special_tokens=False` 计算，严格 ≤500。

BGE-M3 有两个模型特殊标记，因此 500 个正文 tokens 加特殊标记是 502。`embed_chunks()` 还会加入 section_title；验证脚本单独统计“标题 + 正文 + 特殊标记”的真实输入长度，检查其不超过 BGE-M3 的 8192-token 上限。两种计数不能混为一谈。

## 规范化与原文可追溯性

- 仅连接明确词表内的法律术语断行，如 `中华人\n民共和国`；不全面删除中文或韩文换行，以免吞掉段落或错误拼接韩文词尾。
- 只在页首／页尾识别明确的页码、发布机关和有重复证据的文件标题；保留首次文件标题。普通正文即使重复，也不因重复本身而删除。
- 不确定的换行和疑似页眉保留。这可能留下少量版式噪声，但优先避免误删政策正文。
- 内部 `_chunk_with_audit()` 为验收返回规范化事件、单页字符区间和拆分风险；正式公共函数仍只返回 `list[Chunk]`，不向公共契约添加字段。
- 每个 Chunk 均可在该页的规范化文本中定位。PDF 抽查允许已记录的换行规范化，不应要求字符串与 PDF 阅读器排版逐字符相同。

## 测试命令

在 backend 目录执行，先配置本地 tokenizer/模型缓存路径。不要把真实 `.env`、模型、JSON 原件或临时文件提交。

```powershell
$python = 'D:\RAG-Dev\venvs\rag-py312\Scripts\python.exe'
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:HF_HUB_OFFLINE = '1'
$env:EMBEDDING_MODEL = 'D:\RAG-Dev\models\huggingface\hub\models--BAAI--bge-m3\snapshots\5617a9f61b028005a4858fdac845db406aefb181'
$env:EMBEDDING_CACHE_DIR = 'D:\RAG-Dev\models\huggingface\hub'
$env:POLICY_FIXTURE_DIR = 'C:\Users\Administrator\Desktop\sw实战作业\后端切分文件'
& $python -m unittest discover -s tests -v
& $python -u scripts/validate_issue11.py --fixtures $env:POLICY_FIXTURE_DIR --output-dir YOUR_REPORT_DIRECTORY --embed
```

缺少 `POLICY_FIXTURE_DIR` 时，真实文档单元测试会明确跳过，不会冒充真实测试通过。真实向量化使用已有 `embed_chunks()`；搜索仍使用 Mock，未实现数据库内部逻辑。

## 后端待提供

- parse_pdf、search_similar、get_documents 已取得代码并核对实际接口，隔离数据库已运行；剩余是后端正式数据库初始化和应用装配的确认及复测。
- 姜超10题题单已提供；原验证脚本中的中文问题仍只是开发样本，正式题单真实检索记录另见 issue11-real-integration.md。
- 在 Issue #11 确认 Top-k=5 并由王诗琪回复知晓；本轮只提供留言草稿，未替用户发布评论。

## 2026-10-26 本地执行结果

41 项自动化测试通过，其中 28 项为 #11 新增测试、13 项为 #10 回归测试。真实文档测试使用全部 38 页 JSON，没有跳过。三份文档分别生成 52、28、111 个 Chunk，正文最大 token_count 分别为 374、386、496；空白 Chunk 均为 0，191 个 Chunk 全部保持单页并可在对应 PDF 页提取文本中定位（仅忽略空白差异）。

三份文档的最大实际 Embedding 输入长度（包含标题和两个特殊标记）分别为 379、391、512，均低于 BGE-M3 的 8192 上限。中国文档无需在页内再拆超长条款，因此没有强制 overlap；韩国文档的长条款 overlap 分别为 81、76、62、77、75 tokens。

已渲染并目视核对三份文件的样本页，以及 DOC003 第 1–3 页跨页条款和韩国第 9 页的条件／例外。DOC003 第五条、DOC004 第六条和韩国第十九条的抽查片段能找到对应正文，条件、金额及例外在抽查 Chunk 中没有分离。韩国第 9 页含图像表格，原 JSON 未完整提取该表数值；不在本 Issue 添加 OCR，也不宣称覆盖了 PDF 的全部图像信息。

真实 `embed_chunks()` 最终通过：DOC003、DOC004、DOC005 分别返回 52、28、111 个向量，合计 191 个，全部 1024 维、数值有限且非零，chunk_id 与输入一一对应。随后中文问题也通过现有 `embed_texts()` 生成真实 1024 维向量，注入的 Mock 搜索收到 top_k=5 并返回最多 5 条合法 SearchHit；这不代表真实数据库检索或中国来源召回验收已通过。

执行过程中保留一项环境风险：首次运行在 PyTorch embedding 原生算子中出现 Windows access violation，沙箱外复试和原 #10 两句 smoke 脚本也一度复现，当时可用内存约 0.51–0.75 GiB。可用内存回升至约 1.71 GiB 后，在沙箱外运行同一离线验证脚本成功返回全部向量。未修改 #10 适配器或共享虚拟环境；根因仍未确定，不能仅凭重试成功认定彻底修复。

真实数据库、中国方向正式冒烟检索仍需后端和姜超配合。最终结果及逐块审计保存在本地任务的 outputs/issue11，未把公共 PDF 或解析 JSON 原件重复加入 Git。

## 后续补充：向量持久化及检索契约测试

首次验收只保存向量统计，未导出数值；后续已重新调用真实 CPU BGE-M3，
将 52、28、111 条 `EmbeddingRecord` 分别保存为本地
`DOC003-embeddings.json`、`DOC004-embeddings.json`、`DOC005-embeddings.json`。
写入后读回检查数量、chunk_id 对应、1024 维、有限数值及非零向量，均通过。
导出报告保留向量文件与对应 Chunk 文件的 SHA-256。本地导出不等于数据库入库，
这些派生数据不纳入本次 Git 提交；后端真实 document_id 仍需联调核对。

2026-10-27 补充并单独运行检索测试：`python -m unittest discover -s tests -p test_retriever.py -v`，
11 项通过，包括字典结果转换、来源字段及顺序保留、tuple 返回、非法证据拒绝和异步接口显式拒绝。
此前全量 41 项通过的记录保持为历史结果；新增测试后本轮未重新执行全量测试，
不声称已经完成 45 项全量回归。新增测试均为 Mock 单元测试，不代表真实数据库验收。

公共 contracts.py 与 #10 适配器不变。此前同步注入方式为历史本地基线，已由下节异步约定替代。
本次可提交代码供审核，但真实中国方向冒烟检索和 Top-k 的 Issue 确认尚待完成，
不应在 PR 中写 `Closes #11` 或宣称所有验收通过。

## 异步及入库契约更新（最新）

`retrieve` 已改为 async，使用 await 调用后端 search_similar，默认 Top-k 仍为5。
同步问题 Embedding 在线程中运行；不改 embed_texts、embed_chunks、chunk_pages 或公共类型。
验证脚本已用异步Mock和await调用；CLI只在程序入口使用asyncio.run。
检索定向测试14项通过。包含本地未提交#12的完整工作快照曾通过95项回归；
不能将该记录当作当前仅#11分支的测试数量，独立分支结果见下方记录。
已读取PR #28的实际异步接口、ChunkData和上传流水线，现有191组Chunk/向量全部通过其ChunkData校验；
这是本地格式兼容验证，不是数据库入库或真实向量搜索。#11连接说明见retrieval.md。
本次同时提交test_ingestion_embedding_contract.py，覆盖已有embed_chunks的包装、顺序、数量、维度和空输入。
不新增或修改#12的生成编排、引用解析、Prompt或测试，仍待真实数据库完成中国方向检索验收。

仅#11分支独立回归：`python -m unittest discover -s tests -v`，52项通过、0失败、0跳过，17.973秒。
包含原38页真实JSON、14项异步Retriever测试及4项入库契约测试，不含#12测试；未重新运行模型或真实数据库。

## 2026-10-27 真实联合联调补充（最新）

三份真实PDF经后端parser、chunk_pages、CPU BGE-M3和PR #28入库流程，在隔离Postgres18+pgvector中生成并保存191组数据，最终全部READY。
实际await调用search_similar，默认Top-k=5，分数降序及READY过滤验证通过。
中国方向Q008的5条结果全部来自中国文件，但缺少第3页跨页申报条款；Q006/Q007仍混入韩国片段。
详细文档编号映射、来源提交、Top-5原始分数及限制见issue11-real-integration.md。

本次是使用临时database.py/main.py装配的隔离实测，不是原团队main开箱即用验证；不将这两个临时文件纳入#11提交。
整合快照RAG回归88项通过，后端补充回归20项通过；与本分支52项是不同测试集，不能直接覆盖原统计。
检索验证结果不等于#12回答验收通过，不关闭Issue，也不声称召回质量问题全部解决。

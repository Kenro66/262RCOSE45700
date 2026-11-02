# Issue #13 最终技术收口记录

日期：2026-11-03（Asia/Seoul）。关联：Refs #13，不自动关闭Issue。

## 1. 固定结论与证据等级

当前候选版本最终Smoke为 **9 PASS / 1 FAIL**。Q001～Q004采用第一次Smoke原始结果；Q005～Q010采用随后续跑的首次结果。每题一次真实生成，没有重试挑选结果。Q004的后续A/B只作诊断，不能替换原Smoke FAIL。

|证据等级|本次实际达到的状态|不能据此声称|
|---|---|---|
|代码已实现|可关闭的年龄定义、同条款跨页续文Completion及通用Prompt作用域规则|已部署或已经合并main|
|自动测试|最后一次相关回归170 passed、0 failed、0 skipped，另200个子测试通过|整个仓库所有测试或真实模型全部正确|
|单次真实验收|原始Q003、Q008分别通过；正式Prompt修改后的双题首次回归2/2通过|所有改写或重复运行均稳定通过|
|最终Smoke|锁定候选版本10题首次输出9/10通过|10/10或Q004已修复|
|已知限制|Q004 Generation / over-answering / condition completeness risk|可删除失败、用诊断通过覆盖失败|

本轮收口只整理文档和提交边界，没有再次运行测试或模型，没有继续调参。

## 2. Q003：年龄定义证据与生成作用域

原始问题：

> 한국 입국 여행자가 만 19세 미만이면 담배 관세 면제가 적용되나요?

### 根因

原始Initial Top-5找到韩国政策第9页免税年龄主规则，但缺少第4页的年龄定义/排除项。诊断时目标片段在全库实际排名11。目标已在库、READY且有1024维向量，不是缺失入库。

固定证据A/B显示补入定义后可以正确回答；简单年龄短语Query、保留主题的Query拼接都未获得稳定改善，未作为生产修复。

随后发现，即使补证正确，LLM仍可能把“以下同”的年龄定义缩窄成仅影响首次出现的申报程序。这是Generation作用域解释问题，不是Citation元数据错误。

### 实际修复

- 从已召回主规则识别通用韩文年龄限制表达，在同法规内寻找同术语、紧邻括号、括号内例外和“이하 같다”等结构。
- 检查前置关系、商品关联及已支持的身份/局部作用域限定；无法唯一确认则不补充。
- 使用数据库原始Chunk作为Supplemental，不改写正文、不伪造相似度分数。
- 正式Prompt增加通用定义/排除项后续作用规则，尊重证据明确限定的适用范围及不同定义。

生产规则不包含题号、目标页码、目标Chunk ID或答案。报告中的DOC003_C000013（第4页）仅为测试数据快照标识，不是生产检索条件。

### 验收结果

正式Prompt修改后原始Q003真实单次回归通过；最终Smoke Q003也通过，明确年龄排除影响后续免税年龄条件，并引用第4页原文。此前V4及早期原题失败仍留存；不能据此声明所有改写或重复运行稳定。

## 3. Q008：跨页枚举导致证据链不完整

原始问题：

> 在实行红绿通道申报模式的中国内地口岸，旅客携带超过免税限量的香烟入境，应走红色通道还是绿色通道？

### 根因与现有证据链

同一中国政策文档的真实快照：

1. DOC004_C000012，第2页、第九条主句：有下列情形之一的，应当书面申报。
2. DOC004_C000013，第3页、第九条第（七）项：超过免税数额或限量要求。
3. DOC004_C000017，第4页、第十二条：应书面申报人员选择红色通道。

Initial已包含第2页主句和第4页通道规则；第3页续文实际排名23，Top-20没有找到，诊断Top-50确认排名。各片段、1024维向量及READY状态正常。

单页Chunk硬约束正确，但同条款主句和后半枚举跨页拆开。固定Initial/Prompt/模型，仅补入缺失续文后可以正确回答，支持Retrieval / Evidence-chain completeness的根因判断。

### 实际修复

复用现有Completion框架，保守识别同文档同条款的开放枚举：主句明确引出枚举、编号连续、后续Chunk及物理页紧邻、同section_title、续号正确、同一续文项与原问题条件有文本关联。不是机械追加邻页，也不合并跨页Chunk。

### 验收结果

Q008单次Real Acceptance通过，正式Prompt修改后双题回归通过，最终Smoke Q008也通过。第3页原始续文作为Supplemental进入Context，模型明确回答红色通道，引用第3/4页正确。

第2页主句存在于Context，但最终回答未直接引用它，保留非阻断的Citation Chain Completeness备注，不将其隐藏或描述为完整显式引用。

## 4. 通用Prompt作用域规则与Controlled A/B

两个固定Generation bad case（原始Q003、V4）分别A/B各生成一次，不重检索。A为旧正式Prompt，B只追加通用韩文解释规则，Evidence Context相同。两个B均明确将年龄定义用于后续免税年龄条件，A仍存在局部化表述缺口。

规则要求：后续同术语条文按证据中的定义/排除项及“以下同”理解；除非证据明确限定局部适用或另给定义，不得自行缩窄为首次出现的申报程序。

正式韩文规则使用实验实际文本，中文使用等义表达，不含专属题号、年龄数字、商品、国家、页码或答案。它是解释约束，不是完整法律作用域解析器。

上述小样本A/B和单次真实回归是支持证据，不是因果统计证明或稳定性保证。

## 5. 冻结的最终Smoke结果

|题目|判定|status|实际Supplemental|主要备注|
|---|---|---|---|---|
|Q001|PASS|ANSWERED|无|基本免税范围与烟草单独范围区分正确|
|Q002|PASS|ANSWERED|无|超限申报正确，附加年龄说明保留排除项|
|Q003|PASS|ANSWERED|年龄定义|例外用于免税年龄条件，第4页引用正确|
|Q004|FAIL|ANSWERED|无|主问正确，但额外年龄分支遗漏排除项|
|Q005|PASS|ANSWERED|无|40%及法定条件下60%正确，附加年龄说明完整|
|Q006|PASS|ANSWERED|无|400支正确，存在有据但泛化的保留语|
|Q007|PASS|ANSWERED|无|40支且限一天一次正确|
|Q008|PASS|ANSWERED|跨页续文|红色通道正确，保留未显式引用主句的备注|
|Q009|PASS|INSUFFICIENT_EVIDENCE|无|日本方向正确拒答，最终无数字、无引用|
|Q010|PASS|INSUFFICIENT_EVIDENCE|无|泰国方向正确拒答，最终无数字、无引用|

统计：

- PASS 9，FAIL 1；ANSWERED 8，INSUFFICIENT_EVIDENCE 2。
- Completion阶段调用10次，实际产出Supplemental 2题：Q003年龄定义、Q008跨页续文；未发现错误触发、错误Supplemental或两类规则干扰。
- 可回答题来源映射8/8正确，21条返回quote与原PDF物理页文字核对一致；来源错误0，不等于回答事实已全部正确。
- 正确拒答2/2，未给日本、泰国编造限量。
- 失败层：Generation 1；Retrieval、Completion、Citation来源映射、Refusal及其他均0。

判定经过本会话语义审查，不是仅匹配Gold关键词；但这不代替姜超对页面卡片的最终人工核验及results.csv确认。

## 6. Q004 Known Limitation（未修复）

固定标签：**Generation / over-answering / condition completeness risk**。

主问题正确，海关要求提交的例外正确；模型在题设已说明无申报对象时主动增加年龄旁支，没有保留同一Initial证据中的直接年龄排除项。证据已经存在，Completion未触发，来源映射正确，所以不是补证或入库失败。

后续同现场旧/当前Prompt A/B各一次均通过：旧Prompt未增加年龄分支，当前Prompt增加时保留了排除项。它们只作诊断，没有回填Smoke。这不足以认定新Prompt造成Regression，也不能证明问题已消失或只是一次可忽略的偶发。

多题仍有有据但不必要的旁支/保留语，只有Q004在本轮形成明确条件遗漏FAIL。未来可验证“最少必要回答＋必要条件完整”约束；当前收口不再修改Prompt或扩大规则工程。

## 7. 实现边界、开关及回滚

- Initial retrieval保持原行为及默认Top-k=5，Initial与Supplemental分别保存。
- 每次最多补充1条；多候选歧义、不匹配、非READY或读取失败时返回空并保留Initial路径；取消继续传播。
- `EVIDENCE_COMPLETION_ENABLED=false`为模板默认值；本次真实测试在进程内显式设为true。默认关闭时不查询补充来源，不能把启用后的测试结果当成默认部署已开启。
- `get_ready_chunk_sources()`只读READY原始C2字段，不加载向量、不写库。
- Citation模块仅扩展内部Evidence以承载原始Chunk，没有改变公共Citation字段、引用过滤、页码映射或数字检查语义；Supplemental没有虚构score。
- 未修改Retriever、Embedding、Chunk生成、数据库schema、HTTP API或公共contracts；不包含Agent、Hybrid Search、Reranking或OCR。
- 关闭feature flag即可禁用补证，无数据迁移。但这不自动回滚独立的Prompt文本改动。

## 8. 自动测试与环境记录

最后一次相关自动回归：170 passed、0 failed、0 skipped，另200个子测试通过；5条既有SWIG弃用警告。这不是全仓库测试，本次收口不重跑。

覆盖年龄规则、全部111个韩国C2夹具、中文跨页反例、关闭/空结果回退、取消传播、原始来源Citation、runtime装配及既有QA/chat/storage回归。repository测试使用隔离SQLite，不连接共享MVP库清表。

韩文fixture保留111个现有C2文本及metadata；中文fixture保留诊断Question/Initial及3段链条文本/关键metadata，单测构造的统计字段为占位，不能宣称重新通过切分或token计数验收。两份fixture均不包含向量或密钥。

真实运行：Python 3.12、本地CPU BGE-M3、学校Baze网关gpt-6-sol、隔离5433 PostgreSQL/pgvector；192个Chunk、192条1024维向量。整个Smoke前后checksum一致：`cc6ad0da58f5b5415ac34c30be453ca5`。

## 9. 证据保存与团队验收

完整请求Context、Top-5、原始生成和Citation保存于本地工作区的`outputs/`，不在本次正式PR清单：

- `issue13-smoke-20261103/smoke-combined-report.md`及`smoke-combined-summary.json`：冻结9/10合并结果。
- 同目录Q001～Q010.json：各题首次原始记录。
- 原`smoke-report.md`/`smoke-summary.json`：Q004失败后停止的第一阶段记录，保持不变。
- `q004-prompt-ab-20261103/`：后续诊断，不替换Smoke。
- `q003-generation-prompt-ab-20261103/`、`prompt-scope-regression-20261103/`等：前序实验及单次验收，不能代替本轮成绩。

当前Issue #13还要求页面10题均返回、姜超核对证据卡并记录results.csv、Prompt变更通过PR合并。函数链9/10不自动完成这些条件。是否接受带限制的MVP由团队/PM明确决定；严格10/10尚未达标，保留Q004跟踪事项，不使用Closes #13。

## 10. AI使用记录

AI协助只读诊断、实现保守补证、编写/运行自动测试、执行受控真实调用并整理记录。负责人确认范围与接口、提供资料和运行环境并协调团队审核。真实链路、固定证据实验、自动测试、失败和限制分别标注；不能代替团队人工代码审核与最终评测签字。

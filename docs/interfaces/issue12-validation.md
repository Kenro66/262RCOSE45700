# Issue #12：异步生成与引用校验验收

更新日期：2026-10-27。状态：代码及可控测试通过；隔离真实链路通过；回答质量验收 PARTIAL PASS。
本文件替代此前“尚未接真实检索/get_documents”的旧说明，但不宣称正式main已完整集成。

## 接口与流程

```python
async def answer(question, session_id=None, *, retrieve_fn=None,
                 get_documents_fn=None, generate_fn=None) -> ChatResponse
```

await检索 → 校验SearchHit → 分配本轮E1/E2 → await文档查询 → 构造Prompt
→ 复用#10生成适配器 → 引用校验和解析 → C5。
正常仅返回ANSWERED或INSUFFICIENT_EVIDENCE；依赖、格式和内部错误继续抛出，由API层处理。
session_id只接收并校验，不启用多轮历史。

## 文件职责

| 文件 | 职责 |
|---|---|
| qa/orchestrator.py | 单次异步编排、状态判断、生成JSON解析、错误传播 |
| qa/prompts.py | 中韩语言选择、证据约束、逐句引用及条件/例外提示 |
| qa/citation.py | 请求内Evidence ID、无效ID过滤、真实filename/page/quote映射、引用覆盖检查 |
| tests/test_orchestrator.py | 异步业务、语言、拒答、异常、依赖替换与线程调用测试 |
| tests/test_prompts.py | 中韩规则、证据隔离、条件/例外及格式提示测试 |
| tests/test_citation.py | 来源映射、去重、E9、缺失来源、分号/小数等引用覆盖测试 |
| tests/test_smoke_validation.py | 10题文件解析及报告边界测试 |
| scripts/smoke_issue12.py | 真实生成+合成证据的中韩最小测试，不访问数据库 |
| scripts/validate_issue12_smoke.py | 姜超10题+预选真实Chunk+真实生成，不是数据库检索测试 |

## 依赖连接

- retrieve_fn(question)须为可await接口，返回list/tuple的SearchHit或兼容字典。
- get_documents_fn(ids)须为可await接口，返回document_id索引的DocumentRecord映射；也接受含filename/status的兼容字典。
- 不存在ID省略时，对应引用无法成立；依赖调用抛异常时原样传播，不当作证据不足。
- 默认生成复用#10 generate_text；同步生成在线程运行，亦支持异步generate_fn。
- Evidence、Prompt及引用转换没有I/O，保持同步，不重复编写数据库访问或HTTP客户端。

后端提供的函数在连接层绑定，RAG不硬编码导入数据库：

```python
async def retrieve_question(question):
    return await retrieve(question, search_similar=search_similar)

response = await answer(question, session_id,
                        retrieve_fn=retrieve_question,
                        get_documents_fn=get_documents)
```

## 公共契约

直接复用contracts.py中的SearchHit、Citation、ChatResponse和状态枚举，不修改公共Schema。
没有独立AnswerResult类型，返回C5 ChatResponse。
SearchHit.page_number仅在citation.py转为Citation.page；使用filename而非file_name。
引用的filename来自文档元数据，page和quote来自原SearchHit，不接受模型自编来源。

## 状态与安全行为

- 检索为空、来源均不可用、模型明确证据不足或没有有效引用时，返回固定中/韩文拒答，无数字、无引用。
- E1/E9混合时过滤E9；全为E9时拒答。
- 每句/每独立行要求有效引用；分号不单独断句，小数点不当句末。
- 模型漏引、非法JSON、重复字段或明显语言不匹配抛异常，不放行不合规回答。
- 系统错误及取消请求继续传播，不返回SYSTEM_ERROR或伪装为证据不足。
- 使用中韩文字数量启发式识别语言，不宣称通用混合语言识别；有效引用不等于语义完全受证据支持。

## 测试与结果

提交候选集包含#12测试43项：编排27、引用9、Prompt4、冒烟解析3。
本次在独立候选目录重新执行上述43项：全部通过，0失败、0跳过，用时0.485秒。
候选代码通过PYTHONPATH复用当前#11/#10公共契约、检索器和适配器，不复制它们到#12提交。
使用可控Mock时，Evidence分配、Prompt、引用校验、C5与状态判断均执行真实代码。
测试中的400等数字是合成数据，不构成政策事实。

```powershell
cd backend
python -m unittest discover -s tests -v
```

单独运行本候选测试需要已安装的#10依赖和对应公共契约。
原完整开发快照95项通过；隔离整合快照88项RAG回归及20项后端回归通过，属于不同测试集，不合并冒充#12独立测试数量。
正式#12分支基于#11提交c461947整理后再次运行全量：95项通过、0失败、0跳过，21.173秒，其中#12为43项。

## 真实验收结果

已在隔离环境运行真实PDF、BGE-M3、Postgres/pgvector、异步search_similar/get_documents及学校生成API。
38页、191组数据真实入库；中韩回答及真实引用映射有实际运行记录。
10题基本状态9/10，加入题单关键点检查后8/10；详见issue12-real-integration.md。
Q003漏掉年龄例外，Q008因关键续文未召回而拒答。它们是实际质量缺口，不归因于“后端还没提供接口”。
此前Q005曾漏引，本次真实数据库链路通过，不据一次成功宣称稳定性彻底解决。
“带酒入境可以带多少、知识库无对应证据”在受控空检索中通过；真实三份PDF含酒类规定，不能拿它们冒充无酒证据库。

## 提交和验收边界

#11 PR #27尚未合并，#12依赖它。可先提交独立#12功能提交和Draft PR，注明依赖并待#11合并后整理基线。
不提交#11代码的重复副本，也不提交后端临时database.py/main.py、原始PDF、向量、dump、密钥或虚拟环境。
当前适合代码审核，暂不使用Closes #12。正式装配复测及两个质量问题解决后，再申请完整验收。

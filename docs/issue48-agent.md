# Issue #48：独立 Agent 内部接口（第二阶段）

当前入口为 `app.qa.agent.agent_answer`。本阶段以批准的 v1.3 为基准，通过依赖注入和确定性 Mock 验证；没有将 Agent 接入 `/api/chat`，没有调用生产检索、LLM、NPU 或数据库。

## 调用与恢复

```python
from app.qa.agent import AgentState, agent_answer

result = await agent_answer(
    question=original_question,
    session_id=session_id,
    retrieve_fn=mock_retrieve,             # async (str) -> Sequence[SearchHit | dict]
    get_documents_fn=mock_get_documents,   # async (list[str]) -> Mapping
    generate_fn=mock_generate,             # (messages) -> JSON str，支持 sync/async
    complete_evidence_fn=mock_complete,    # 可选 async (Sequence[SearchHit]) -> Sequence[SupplementalEvidence]
)

# 后端保存；不要将 result 或 state 整体作为 HTTP 响应。
serialized_state = result.state.model_dump_json()
response_for_client = result.response

# NEEDS_CLARIFICATION 后，以原问题、相同 session/question_id 恢复。
resumed = await agent_answer(
    question=original_question,
    session_id=session_id,
    question_id=result.response.question_id,
    conditions={"destination_country": "CN"},
    agent_state=AgentState.model_validate_json(serialized_state),
    retrieve_fn=mock_retrieve,
    get_documents_fn=mock_get_documents,
    generate_fn=mock_generate,
    complete_evidence_fn=mock_complete,
)
```

所有必需依赖都必须显式传入，缺失时抛出 `AgentError`，不会自动加载真实服务。首次未提供 question_id 时，Agent 生成 `q_<uuid>` 并返回；后端应保存并使用该 ID。若后端选择自行生成，可在首次调用中提供。

依赖签名复用 `qa/orchestrator.py` 的 Retrieval、DocumentLookup、Generator、Completion 类型。单个 `generate_fn` 通过 messages 接收不同任务，不依赖 tool calling。

## 工作流

```text
START -> check_conditions
  缺少条件 -> clarify -> finalize -> END
  有缓存 -> check_evidence
  无缓存且有预算 -> retrieve -> check_evidence
  无预算 -> insufficient -> finalize -> END

check_evidence
  不充分且有预算 -> rewrite_query -> retrieve
  不充分且无预算 -> insufficient -> finalize -> END
  充分 -> check_applicability

check_applicability
  缺少条件 -> clarify -> finalize -> END
  已追问过且仍缺条件 -> finalize(INSUFFICIENT_EVIDENCE) -> END
  条件齐全 -> check_conflict

check_conflict
  真实冲突 -> finalize(CONFLICTING_EVIDENCE) -> END
  声称冲突但文件范围无法确认 -> finalize(INSUFFICIENT_EVIDENCE) -> END
  无真实冲突 -> generate_answer -> finalize -> END
```

- 检索调用前检查预算，调用开始时消耗次数，包括失败的调用。每个逻辑问题累计最多 3 次。
- 追问最多 1 轮，可同时询问多个条件；已给出的 conditions 不会再次被请求。追问立即结束本次调用。
- 缓存证据优先重新检查充分性，已有证据够用时不会再检索，包括剩余预算为 0 的恢复请求。
- 每次 Retrieval 后合并、按 chunk_id 去重；同一稳定 ID 的来源、页码或原文变化时抛出技术异常。
- 提供 Completion 时每次非空 Retrieval 最多调用一次，每次最多增加一个来源。沿用现有补全逻辑的原文读取，不单独算向量 Retrieval；不能在 Completion 回调内隐式增加检索。
- 引用复用现有 validate_hits、assemble_evidence、resolve_citations、has_cited_conclusions、has_unsupported_numbers，保留文件名、物理页码和原文。
- 冲突双方必须是有效、互不重叠的 Evidence ID。已知国家、方向不同或与用户条件不符时不作为 True Conflict；BOTH 与单一方向可重叠。缺少可确认的 country/direction 时，不输出未经确认的冲突。
- 具体条款是否矛盾、是否同时适用于当前对象和时间，由结构化判断负责；本阶段 Mock 测试只证明路由与校验，不证明真实政策理解准确率。

## 状态与返回结构

`GraphState` 为执行用 TypedDict，包含当前查询、来源、元数据、分支判断和 trace。每次调用创建独立 Graph 和 Nodes，没有进程级会话状态。

`AgentState` 是可 JSON 序列化的后端状态：

- v1.3 基础字段：retrieval_count（0–3）、clarification_count（0–1）、evidence_ids。
- evidence：完整 SearchHit 或 Chunk 原文、稳定 chunk_id/document_id、页码及临时 evidence_id；不只保存 E1/E2。
- question、session_id、question_id：避免跨逻辑问题错误关联缓存。
- conditions：保存此前补充条件，下一次增量合并。

ID 列表必须与完整缓存一致；只提供 E1/E2、重复 chunk_id、越界次数或跨问题复用会被拒绝。恢复时重新读取文件元数据并验证 READY 状态；引用编号可在失效来源被过滤后重排。缓存保留的是来源快照，后续真实集成仍需决定内容版本/哈希失效策略。

`AgentResult` 包含 response、state，另有仅供内部检查的 trace。`AgentChatResponse` 继承现有 ChatResponse，新增 question_id、clarification、conflict，原 `contracts.py` 未改动。调用方必须保持具体子模型序列化，否则按基类裁剪时会丢失扩展字段。

## Mock LLM 协议

messages 最后一条 user content 是 JSON，包含 task、question、conditions、current_query、retrieval_count、evidence（附来源与文件元数据）。system content 提供对应 JSON Schema。任务响应：

| task | JSON |
| --- | --- |
| check_conditions / check_applicability | `{"missing_conditions": []}`，或 questions 对象数组 |
| check_evidence | 当前 `EvidenceDecision`：verdict、core_claim、core_support、四类 coverage、missing_evidence；以 state.py Schema 为准 |
| rewrite_query | `{"query": "保留原问题及补充条件的查询"}` |
| check_conflict | `{"conflict": null}`，或 reason/side_a/side_b 对象 |
| generate_answer | `{"answer": "有原文支持的回答。[E1]", "insufficient_evidence": false}` |

JSON 重复字段、未知字段、错误字段类型、空白查询、不存在的冲突引用等属于技术失败，不能被当成普通证据不足。完整确定性 Mock 和调用示例在 `backend/tests/test_agent_graph.py`。

## 错误与后端职责

- `INSUFFICIENT_EVIDENCE`、`NEEDS_CLARIFICATION`、`CONFLICTING_EVIDENCE` 都是正常结果，不抛出异常或触发 Single RAG Fallback。
- 技术异常抛出 `AgentError`；工作流开始后的异常附带 `.state` 和 `.trace`，后端应保存已消耗预算后决定如何恢复。可选 Completion 的未处理异常同样作为技术失败传播；真实集成时需统一与 Single RAG 的补全降级策略。
- 取消异常继续传播，避免误触发回退。
- 后端负责持久化、权限隔离、同一逻辑问题的并发互斥/原子状态更新。纯函数式快照无法阻止两个并发请求同时使用同一份旧预算。
- 本模块不实现 RAG_MODE、HTTP 路由切换、数据库持久化或自动回退；后端集成时需协调回退调用的预算规则，避免隐藏的额外 Retrieval。

## 后续接入事项

1. 确认 Issue #45 的实际代码交付：当前 main 的 DocumentRecord/get_documents 尚无四项字段。Mock 元数据先使用兼容 Mapping 提供。
2. 为真实 retrieve 绑定 search_similar，为 Completion 绑定 get_ready_chunk_sources 和含补充条件的当前问题，保持现有依赖签名。
3. 对真实 LLM 验证各任务 JSON Schema、查询改写质量、条款充分性、真实冲突和中韩语输出。本阶段没有调用真实模型。
4. 后端接入时更新 chat/service.py 的结果校验（现在只允许两种 Single RAG 状态）、HTTP 请求响应扩展字段、question_id 关联和状态保存；将具体 AgentChatResponse 的扩展字段保留在响应中。
5. 再实现 RAG_MODE 与技术异常回退，测试 Q003/Q008 和真实政策文档，前端联调追问/冲突展示。

## 依赖与验证

按项目现有 pip 方式在 requirements 中添加 `langgraph==1.2.20`，依据 [PyPI 包信息](https://pypi.org/project/langgraph/1.2.20/)及 [StateGraph 官方 API](https://reference.langchain.com/python/langgraph/graph)使用 compile/ainvoke/conditional edges。已在 Python 3.12.1 环境验证。

从 backend 目录运行独立 Mock 测试：

```powershell
& '..\..\joint-runtime\Scripts\python.exe' -m pytest -q -p no:cacheprovider tests/test_agent_graph.py
```

Agent Mock 测试禁止外部 socket 连接及 httpx 请求。允许 Windows asyncio 内部使用的 loopback wakeup socket；没有生产网络调用。

## 第三阶段：显式真实依赖适配层

`app.qa.agent.runtime.agent_answer_real()` 接受 question/session_id/conditions/question_id/agent_state，建立独立 `RealRAG` 实例后向 `agent_answer()` 注入现有实现：

- `qa.retriever.retrieve` + `storage.repo.search_similar`，沿用 BGE-M3/pgvector，不重写算法。
- `storage.repo.get_documents`，保持现有 DocumentLookup 返回类型；不存在的 Issue #45 字段不自行补造。
- `qa.evidence_completion.complete_evidence` + `storage.repo.get_ready_chunk_sources`。
- `adapters.generation.generate_text`，沿用现有 OpenAI-compatible 网关与 `.env` 配置。

每次检索将最新 query 保存在本次 RealRAG 实例中，Completion 使用**最新 query + 原问题 + 累计 conditions**。因此 Rewrite 不会继续使用只绑定原问题的旧上下文，同时保留原适用条件。实例不可跨并发请求共享。

适配层从现有 settings() 读取 DATABASE_URL，为自己创建独立 engine/session_factory，不改变进程环境或 Single RAG 的连接池。PostgreSQL 会话设置 `default_transaction_read_only=on`，连接超时 8 秒、命令超时 30 秒；退出时释放自有 engine。支持传入现有 session_factory 进行独立验证，注入的工厂由调用方负责生命周期和只读配置。

生成节点复用现有 `build_messages()` 的中韩文引用、定义和例外规则。条件判断明确区分一般政策解释与个人资格判断，避免仅为解释证据中的例外而反复要求个人条件。未改动 Single RAG 源文件。

### 可复现的真实验证

在 backend 目录运行 `scripts/validate_agent_real.py`，需使用项目虚拟环境；真实服务需有网络及本地数据库权限：

```powershell
# 先检查只读数据库和真实检索，再调用模型。
python scripts/validate_agent_real.py --phase inventory --output <output>/inventory.json
python scripts/validate_agent_real.py --phase retrieval --output <output>/retrieval.json
python scripts/validate_agent_real.py --phase decisions --retrieval-input <output>/retrieval.json --output <output>/decisions.json
python scripts/validate_agent_real.py --phase full --output <output>/full.json
python scripts/validate_agent_real.py --phase single --output <output>/single-baseline.json

# 明确标注的实验：只对已检索的 initial 来源做判断，移除 Completion。
python scripts/validate_agent_real.py --phase decisions --tasks check_evidence rewrite_query --initial-only --retrieval-input <output>/retrieval.json --output <output>/ablation.json
```

runner 不写数据库、不上传文件、不保存状态；金标准不发送给 LLM。引用核查通过 evidence_id 对应的 document_id 定位 `uploads/<document_id>.pdf`，核对物理页、规范化空白后的原文及与 data/ PDF 的 SHA-256 一致性。不能仅靠上传显示名拼接仓库文件路径。

运行记录应同时保留失败与修正后的结果。第三阶段独立验证不等同于全部第一批验收；其 Q003 缺少定义的假阳性及其他质量问题在第四阶段继续验证。

## 第四阶段：证据质量审计与评测

EvidenceDecision 已由单个 sufficient 布尔值扩展为内部结构化审计：definitions、exceptions、applicability、complete_answer，各含 status（supported/missing/not_required）、reason、support（evidence_id、原文 quote）；另有 missing_evidence。

- 任一必要分项 missing、有未解决 missing_evidence、complete_answer 未被支持或 supported 无来源时，不能仅凭 sufficient=true 放行。
- supported 的证据编号及摘录文字必须能在来源定位；内部比较容忍转义换行及非数字内部的标点差异，但保留小数点/千分位，不能改动字词或数字。返回 Citation.quote 仍直接取原文，Citation Resolver 的公共规则未变。
- 审计只要求可能改变所问结论的定义与例外，不要求检索整部法规、无关品类或全部交叉引用。年龄/身份类别的主限制本身不代表已给出该类别的定义。
- GraphState 保存 evidence_check_result 及 accepted_sufficient；下一次 Rewrite 会看到具体缺失项。该执行诊断不是跨请求公共状态契约。
- Conflict 的 side_a/side_b schema 要求 bare E# 编号（不带方括号、原文或解释），并保留已有双边来源/范围校验。

`scripts/evaluate_agent_quality.py` 提供显式真实模型实验：

```powershell
# 关闭 Completion 的压力实验，实际执行真实 Retrieval / LLM / Rewrite。
python scripts/evaluate_agent_quality.py --mode rewrite --cases Q003 Q008 --output <output>/rewrite.json

# 四组 TEST ONLY 合成证据；绝不写入正式文档或数据库。
python scripts/evaluate_agent_quality.py --mode conflicts --output <output>/conflicts.json

# 预先固定的 Pilot 子集，交替执行两种模式的先后顺序。
python scripts/evaluate_agent_quality.py --mode pilot --cases P001 P002 P003 P006 P009 P019 P022 P028 P031 P035 P037 P039 --output <output>/pilot.json
```

Pilot 两个模式共享模型、原问题、文档、top_k 和 Completion。模型预热在计时外，金标准只写进评测文件供核查，不传入生成输入。每题保存答案、引用、每次查询/Chunk、模型输出、状态、累计预算及耗时；需对照固定 gold 标准另做正确性评审。真实网关没有设置固定采样种子，单轮子集结果不能外推全部问题准确率或稳定延迟。

失败实验不得覆盖或删除。明确区分节点探针、完整真实 Graph、合成证据的真实 LLM 判断，以及确定性 Mock 测试；达到检索上限仍无法恢复证据时，INSUFFICIENT_EVIDENCE 是正确的停止行为。

提示词修订后仍观察到模型把年龄限制当作年龄定义，因此最终节点增加通用的数值年龄类别守卫：问题明确使用年龄阈值时，definitions 支持项必须含对应年龄及定义/括号排除语境；仅重述优惠限制不能代替定义。不固定年龄、文档、页码或答案。该校验仍属保守启发式，需要继续测试其他法规表达和语言覆盖。

```powershell
# 实际 check_evidence 节点：真实 LLM + 真实只读 Document Lookup，记录原始与最终判断。
python scripts/evaluate_agent_quality.py --mode definitions --cases Q003 --retrieval-input <output>/retrieval.json --output <output>/definitions.json
```

整批 Pilot 原始成绩与后续诊断复测应分别保存。不得用修订后的单题最好结果替换整批中的失败；报告需注明整批代码哈希、最终守卫差异，以及是否已整批重跑。

## 第五阶段：核心结论与稳定来源选择（当前内部契约）

修改前根因分析分别核对了首次 Retrieval 原文和每个审计分项：旁支群体定义、扣减程序被误当成核心额度前提；部分小数点缺失实际来自模型抄写，而非来源文本。

当前 EvidenceDecision 使用三种 verdict：

- SUFFICIENT：可信来源足以支持所问核心结论与真正影响该结论的限定，立即进入后续节点。
- MISSING_CRITICAL：缺少能改变当前结论的必要前提，才允许改写与补检索。
- UNSUPPORTED：资料不支持该国家/主题的政策，正常返回证据不足，不无限改写。

core_claim 是当前需要证明的最小结论；core_support 及 coverage.support 仅返回 canonical chunk_id。程序严格校验该 ID 属于当前可信 Evidence，再提取 evidence_id 和完整原文。LLM 不再重复抄写审计 quote，因此不会因序号句号、换行或小数点转录产生定位错误。返回 Citation 仍沿用原 Resolver 与原文；答案的每句引用、语言和数字校验继续开启。

coverage 增加 affects_core。一般旅客的明确额度不要求预先定义所有特殊群体；无法改变所问阈值的旁支条款不能触发 Rewrite。数值字面量从可信来源抽取后传给模型，不从模型摘录恢复小数。数值年龄问题仍从其所选可信定义来源检查年龄类别定义/排除语境，不能只凭主限制句放行。

GraphState 和后端 AgentState 记录已执行查询（retrieval_queries），防止相同查询在恢复后重复消耗预算；保留最多 3 次 Retrieval、1 轮 Clarification。重复 Rewrite 查询会正常终止，不进行重复调用。

评测固定版本应提交到独立分支，并保存非敏感配置、代码/题集 SHA-256 与只读知识库校验和。`--frozen-manifest` 会在每个模式调用前检查源文件，在整个进程内锁定配置，并核对整批前后数据指纹；密钥只保存在进程环境，不写入 manifest。每轮完整 12 题独立计算，不拼接最佳单题结果。

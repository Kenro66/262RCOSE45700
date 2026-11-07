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
| check_evidence | `{"sufficient": true}` |
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

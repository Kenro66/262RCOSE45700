# Issue #12：真实隔离联调结果

日期：2026-10-27。结论：真实调用链跑通，回答质量 PARTIAL PASS。

## 实际执行

三份政策PDF经真实parser、切分、BGE-M3写入独立PostgreSQL18.6/pgvector0.8.7。
对姜超10题逐题执行：

```text
原始问题 → 真实Embedding → await search_similar(top_k=5)
→ 请求内Evidence ID → await get_documents → Prompt
→ 真实生成适配器 → Citation → ChatResponse
```

没有预选gold证据，没有把标准答案交给模型，没有替换成fake向量或检索结果。
get_documents实际返回DocumentRecord，filename与数据库记录一致，page/quote与实际SearchHit对应。
临时测试库document_id为DOC001=征收办法、DOC002=行李监管办法、DOC003=韩国告示；与原本地JSON编号不同。

## 测试来源

- main基线：e4ce1f5d0dd8ac6fbf4d2a8e7fcbabf0955974d1。
- 后端PR #28：ec8e359b0b8af518092a15ff909da472b5b7dd97。
- parser：41e413a52c64c4aef348e3ad63bc0e26971c40f1。
- #11：1c2fb565974ca7bcf6341a11c2be7a1024bc99b9。
- #12本地检查点：495ab1fb25b256fdc2741b8a43d3480ddc48240b。

使用两个本地临时装配文件补齐当时后端缺失的database/app入口，不修改仓储和公共契约。
这些临时文件不纳入#12 PR，正式团队装配仍需确认并复测。
调用通过进程内ASGITransport执行，不是外部浏览器或TCP负载验证。

## 10题结果

| 题号 | 基本状态 | 标准答案关键点 |
|---|---|---|
| Q001 | PASS | PASS |
| Q002 | PASS | PASS |
| Q003 | PASS | FAIL：年龄例外遗漏 |
| Q004 | PASS | PASS |
| Q005 | PASS | PASS |
| Q006 | PASS | PASS |
| Q007 | PASS | PASS |
| Q008 | FAIL：证据不足 | FAIL |
| Q009 | PASS：拒答 | PASS：无数字和引用 |
| Q010 | PASS：拒答 | PASS：无数字和引用 |

基本状态9/10，关键点8/10；这是单次运行及关键点检查，不是完整法律语义评测或稳定性保证。
Q003的Top-5未包含韩国第4页年龄例外，生成只给出主规则，不能算完整正确。
Q008虽召回红色通道条款，但未召回第3页申报续文，模型拒答；安全行为正确不代表这题业务验收通过。
Q006/Q007有额外泛化保留措辞，后续应精简，不宣称所有扩展表述都已人工法律审查。
Q005本次通过，但历史漏引记录仍保留；不拼接不同运行的通过结果冒充同一轮10/10。

## 其他完成条件

- 中文/韩文问答均有真实API记录；文件名、物理页码、quote映射使用实际数据库与检索结果。
- 不存在E9的过滤及全无效引用拒答由可控Mock注入验证，未假称真实模型主动生成E9。
- 无酒类证据拒答通过受控空检索测试，固定回复无数字；真实语料本身含酒类内容。
- Generation/Retrieval/get_documents异常及取消传播均有单元测试，不转换成证据不足。

## 下一步

与#11协调处理跨页条款和定义召回缺口，保持MVP范围；不擅自加入Agent、重排或混合检索。
在后端正式database/app装配基线上再测，不再将函数签名或异步形式当作未知阻塞项。
暂不关闭#12；可提交Draft代码审核PR，明确已测与未通过项目。
完整原始响应、所有Top-5和数据库备份保留在本地，不放入此PR。

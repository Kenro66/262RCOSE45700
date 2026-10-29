# Issue9 推送前复核（2026-10-29）

本分支仅提交Issue9增量实现、测试与验收文档，关联 `Refs #9`，不宣称全部业务完成条件已通过。

## 当前基准

- main：`579c252b26e78c08484950460a21ffbb88f42c5b`。
- PR27：`d5aaf1421ca49508fdf1ef4bb93cbaec624ce19e`，已合并。
- PR28：`8d6eb19daea096fda15eca4bb8337e34ec5a3d07`，已合并。
- PR30：`2c0f677721f962401c7ae74494237fc5077a722d`，仍为开放草稿；完整问答需要该PR的生成与引用模块。
- PR29：`c45ca5674f69f99633bedc8a2d18da3562b15428`，开放；前端页面仍使用模拟问答。

本分支不包含PR30或PR29的实现。应先整合PR30或在包含该依赖的集成分支验收，不能把仅覆盖main称为完整RAG可运行。

## 本轮复核结果

Issue9包内17个Python文件与本轮验收副本一致。当前PR30后端生产依赖已逐文件核对；main.py仅增加聊天装配、默认依赖与日志，repo.py仅增加has_ready_documents。

Python3.12.13隔离测试：164通过、1跳过、86个子测试通过，0失败/错误；包含真实BGE-M3分词器和三份PDF解析夹具。唯一跳过为真实PostgreSQL E2E。嵌入、检索和生成替身测试不代表真实模型验收。

最新main新增的tests/test_issue7.py需要独立PostgreSQL测试库，未配置ISSUE7_TEST_DATABASE_URL会在收集时直接报错。本轮明确将该文件排除，不能称为当前仓库无条件全量通过。应在独立进程、独立测试库运行Issue7测试；不要与Issue9 conftest的SQLite默认会话直接混跑。

在包含PR30的集成副本中，从backend目录运行：

```powershell
python scripts/prepare_issue9_policy_fixtures.py --output-dir "$env:TEMP/issue9-policy-fixtures"
$env:POLICY_FIXTURE_DIR = "$env:TEMP/issue9-policy-fixtures"
python -m pytest tests -q --ignore=tests/test_issue7.py
```

分词器需要可用缓存或网络；本机复核离线缓存实际目录为hf-cache/hub。

## 尚未完成的业务验收

- 独立PostgreSQL/pgvector、真实BGE-M3权重与LLM联合运行三份PDF及Q006，核验答案条件与引用原文。
- 真实部署修改错误模型地址后HTTP500 SYSTEM_ERROR；删除全部本轮文件后NO_READY且无模型调用日志。
- 前端实际调用/api/chat，显示ANSWERED、NO_READY_DOCUMENTS和SYSTEM_ERROR。
- 孙忠其确认answer调用方式，王一霖确认前端展示。Issue9本次读取仍为open、无确认评论。

以下为3.0原始历史记录，其中提交版本、PR状态和未排除Issue7测试的旧命令仅适用于当时基线；以本节复核为准。

---

# Issue9 3.0 原始历史记录

核对日期：2026-10-29。此次只读读取GitHub并在本地修复、测试，没有推送、评论、创建PR或远程合并。

代码与本地整合验证通过，不等于Issue9全部业务完成条件已经通过。本机没有真实BGE-M3权重和生成服务；真实PostgreSQL全链路及孙忠其、王一霖本人确认仍未完成。保持 `Refs #9`，不使用 `Closes #9`。

## 最新代码基准

| 来源 | 2026-10-29读取的精确提交 | 当时状态 |
|---|---|---|
| main | 07f60025489646a26c50f36559a014012db38f56 | 已含PR25、PR24、PR31 |
| PR25 / Issue4 | a8b4b0eb1ea77c8c645453352429f24f045ea6fb | 已合并 |
| PR24 / Issue6 | 2d85f2d67849e1edec2cdbb64a87d07b53ab87c4 | 已合并 |
| PR31 / Issue17 | ba9dd398ea597b3f826db79d10fdc8c67a81b9d3 | 已合并 |
| PR28 / Issue7 | d1adb4272222e8033f8b878d4cd218e579a2ff07 | 未合并，非草稿 |
| PR27 / Issue11 | d5aaf1421ca49508fdf1ef4bb93cbaec624ce19e | 未合并，非草稿 |
| PR30 / Issue12 | 18928267525cf1e0f40fd5a9351b6de7e27ff38f | 未合并，草稿 |
| PR29 / 前端协议 | c45ca5674f69f99633bedc8a2d18da3562b15428 | 未合并，非草稿 |

依照[Issue9](https://github.com/Kenro66/262RCOSE45700/issues/9)和[Issue8公布的HTTP/JSON](https://github.com/Kenro66/262RCOSE45700/issues/8#issuecomment-6427772386)实现。#4/#6/#17不再按“尚未合并”描述。

最新PR28已调整后台同步调用、删除失败处理、应用生命周期，并移除原有后端测试文件；最新PR27修复数字误删和条款引用识别。3.0使用这些新实现，不把2.0的整份main或旧切分代码覆盖回去。PR30的旧切分祖先不会覆盖PR27新修复。

## 本次提交范围与兼容修正

- 新增聊天路由、C5服务和answer/retrieve/get_documents装配。
- main只增加聊天路由与可选问答注入，保留最新PR28的文件接口、真实切分/嵌入、health、标题、日志和“初始化失败仍关闭数据库”的生命周期。
- storage/repo.py只新增has_ready_documents，不重写已有搜索或文档查询。
- 未修改公共contracts、adapters、qa、切分器、解析器、文件处理模块和前端源码。
- 补齐tests/conftest.py及聊天测试依赖；默认使用SQLite隔离库，不因已有DATABASE_URL而删除应用数据库。
- 补回文件接口、health、流水线、存储和Postgres E2E回归。test_pipeline.py的空白PDF断言明确使用已合并解析器的ERROR_NO_TEXT，不再使用旧的统一提示；该修正随本次提交，不依赖未提供的基线文件。
- 新增真实PDF夹具生成脚本，使原有真实文档切分测试不依赖个人电脑上的JSON附件。

这是增量提交文件集合，须加入完整依赖仓库；不是完整项目下载。不要将其他人的依赖实现重复作为个人新增代码提交。直接覆盖未整合依赖的main不能保证可运行。

## C5行为

无READY文件时不调用模型，INFO日志可见；HTTP200：

```json
{"status":"NO_READY_DOCUMENTS","answer":null,"citations":[],"message":"当前没有可用文件"}
```

模型、数据库或内部调用失败，HTTP500：

```json
{"status":"SYSTEM_ERROR","answer":null,"citations":[],"message":"系统暂时无法处理请求，请稍后重试。"}
```

ANSWERED要求非空答案和有效filename/page/quote引用；INSUFFICIENT_EVIDENCE由RAG模块决定。非法结果也返回C5 SYSTEM_ERROR；取消继续传播。session_id透传，空会话ID归一化为None。公共ChatRequest及422输入验证规则不变。

## 可复现的本地整合

在独立本地审查仓库读取依赖（这些命令不写GitHub）：

```powershell
git fetch https://github.com/Kenro66/262RCOSE45700.git refs/heads/main:refs/remotes/issue9/main refs/pull/28/head:refs/remotes/issue9/pr28 refs/pull/27/head:refs/remotes/issue9/pr27 refs/pull/30/head:refs/remotes/issue9/pr30 refs/pull/29/head:refs/remotes/issue9/pr29
git switch -c codex/issue9-review 07f60025489646a26c50f36559a014012db38f56
git merge --no-edit d1adb4272222e8033f8b878d4cd218e579a2ff07
git merge --no-edit d5aaf1421ca49508fdf1ef4bb93cbaec624ce19e
git merge --no-edit 18928267525cf1e0f40fd5a9351b6de7e27ff38f
git merge --no-edit c45ca5674f69f99633bedc8a2d18da3562b15428
```

再将Issue9提交目录内的文件按相同相对路径加入此分支。PR29用于前端兼容核对，后端运行本身不依赖其React代码。以上仅复现记录的精确提交；若GitHub版本继续更新，应基于新版本重新审查冲突和测试。

不需要个人本地提交、补丁、bundle或附件压缩包即可完成以上整合。此次精确基线的模拟合并与保护路径核对结果见本目录[issue9-checks.json](issue9-checks.json)。

## 自动化验收复现

按根README在Python3.12独立环境安装项目依赖。在backend目录执行：

```powershell
python scripts/prepare_issue9_policy_fixtures.py --output-dir "$env:TEMP/issue9-policy-fixtures"
$env:POLICY_FIXTURE_DIR = "$env:TEMP/issue9-policy-fixtures"
python -m pytest tests -q
```

脚本从仓库data目录的三份真实PDF调用正式parse_pdf生成测试输入，并记录源文件SHA256；没有手选gold证据，也不生成嵌入或调用模型。生成JSON和测试日志只留本机，不提交。

本轮三份PDF共38页；最新版切分共192块（DOC003=52、DOC004=29、DOC005=111）。旧191块的向量不能按相同chunk_id直接复用到新版；使用独立库重处理受影响文档。保守保留裸数字是PR27已记录的取舍，不将它当作完整页眉页脚过滤。

本轮Python3.12全量结果为164通过、1跳过、86个subtest通过，0失败；唯一跳过为真实Postgres E2E。测试命令、版本、源文件哈希均保存在同目录issue9-checks.json。模型相关单元测试使用明确替身；真实生成适配器的错误测试使用本机未监听端口。切分测试使用真实BGE-M3分词器。没有把替身输出称为真实模型验收。

默认没有POLICY_FIXTURE_DIR时，原有4项真实文档测试明确跳过。设置后可执行。真实Postgres E2E默认跳过；该测试使用假切分/向量验证数据库操作，本身也不能替代真实模型验收。

若需要运行Postgres测试，先创建可清空的独立issue9_test库并启用pgvector。在backend目录显式设置：

```powershell
$env:ISSUE9_TEST_DATABASE_URL = "postgresql+asyncpg://rag:TEST_PASSWORD@127.0.0.1:5432/issue9_test"
$env:ISSUE9_ALLOW_DATABASE_RESET = "1"
python -m pytest tests/test_e2e_postgres.py -q
```

TEST_PASSWORD只代表你本地测试库的密码，不提交真实值。该用例会建表并清空测试表，不能使用共享库或业务库；不直接读取应用DATABASE_URL作为授权。

## 真实模型和成员确认

按照README在独立空库配置真实模型并启动应用后，在backend目录执行姜超已合并题单Q006：

```powershell
python scripts/validate_issue9.py --base-url http://127.0.0.1:8000 --pdf "../data/《进境物品关税、增值税、消费税征收办法》.pdf" --pdf "../data/中华人民共和国海关进出境行李物品监管办法.pdf" --pdf "../data/여행자 및 승무원 휴대품 통관에 관한 고시.pdf" --question "成年旅客从韩国入境中国内地，不属于经常出入境人员，香烟一般可免税携带多少支？" --output "$env:TEMP/issue9-real-answered.json"
```

脚本检查空库NO_READY、上传201+PROCESSING、三份READY、回答和引用、只删除本轮上传的ID、删除后再问NO_READY；失败时也清理本轮ID，不删除其他文件。答案关键点、条件和引文原文还须人工核对，引用存在不代表语义正确。

然后在同一独立环境把GENERATION_BASE_URL改为本机关闭端口，重启应用，在相同命令加 `--expected-status SYSTEM_ERROR` 并使用另一个本地输出文件，检查HTTP500+C5；随后恢复配置。上传阶段仍需正常嵌入环境。

| Issue9完成条件 | 本轮证据与剩余工作 |
|---|---|
| 无READY且不调用模型，日志可见 | 自动化及loopback HTTP验证通过；真实库完整链路未执行 |
| 错误模型地址返回SYSTEM_ERROR | 实际HTTP适配器闭端口失败路径通过；真实环境.env切换未执行 |
| 三份PDF加可回答题，ANSWERED且有引用 | PDF解析/切分通过；真实嵌入、向量库和LLM联合问答未执行 |
| 孙忠其确认answer调用、王一霖确认前端显示 | Issue9读取时无确认评论，待本人确认，不代填 |

前端源码原样；TypeScript和构建、实际loopback HTTP客户端/C5转换检查通过，但HTTP测试注入可控答案，页面仍是PR29的Mock流程，Issue15实际UI接线未代做。PR30已公开的部分召回/问答质量缺口也不在本次修复中宣称解决。

## AI使用记录

本次按用户要求由Codex辅助核对最新代码、修复本地装配与测试、整理提交文档、执行自动化与本地合并模拟。用户明确本机没有模型环境；真实服务配置、业务内容核对、成员确认和最终提交仍由负责人完成。未在GitHub进行任何写入操作。

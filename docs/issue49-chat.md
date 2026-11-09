# Issue #49：原 Chat 追问与冲突

基准 main `183c192dcf0628b5a3de6c04aeab0fb48eae7ece`；参考 Issue #48 3.4/3.5 与 PR #58 head `85065a55b89941c8638696f0675e9479c696bfce`。

## 结构

只有原 App/ChatPage 一套页面，没有独立 AgentPreview。`/` 默认调用真实接口；仅开发环境 `?mock=1` 切换数据来源，仍使用原消息列表、输入框、历史对话和 CitationPanel。正式 build 中开关关闭。

正式组件、契约、响应转换和双语文案在 `frontend/src/chat/`；假数据与异步 fake transport 在 `frontend/src/mocks/`。不改后端、数据库或依赖版本。Issue #45 地区元数据、#48 状态保存未接入，不声称完成真实 Agent 联调。

## 启动与场景

在 frontend 目录：

```powershell
npm install
npm run dev -- --port 5176 --open '/?mock=1'
```

- 追问：`http://127.0.0.1:5176/?mock=1&scenario=clarify`
- 多条件 select/text：`http://127.0.0.1:5176/?mock=1&scenario=multi`
- 冲突：`http://127.0.0.1:5176/?mock=1&scenario=conflict`
- 失败重试：`http://127.0.0.1:5176/?mock=1&scenario=multi&fail-once=1`
- 缺失引用：`http://127.0.0.1:5176/?mock=1&scenario=invalid`

打开后从原输入框发送问题，例如“我可以携带多少香烟入境？”；冲突示例可以问“这两份文件对同一事项的规定是否一致？”。顶部开发场景链接会重新加载本地会话。全部样例明确标为模拟，不提供真实政策/额度。

mock 模式文档列表为空，禁用文档写操作，不请求真实后端，不伪造已上传文档数量。移除 mock 参数即恢复真实 API，需启动后端。

## 交互规则

- 追问支持 select/text 及多个必填条件；CN/KR 显示本地化名称，提交仍是代码。
- 每个聊天独立 UUID session_id。补充回传原问题、原 question_id、conditions；fake 按两个 ID 隔离并拒绝更换原问题。
- 发送中不重复提交；失败在表单内保留输入与 ID；取消/切换聊天/离开 Chat 会取消请求，防止晚到结果进入其他聊天。
- 成功后原追问标为已提交，不能重复补充；回答继续走原消息和引用侧栏。
- 冲突双方通过原 evidence_id 查找，特意使用 E5/E2 乱序样例；展示连续编号，正文隐藏标号，不改变 API ID。
- 缺失/重复/未知引用、无效追问载荷显示系统错误与重试，不展示单边冲突。
- 中韩固定 UI 可切换，API 问题、回答、reason、引用原文保持原文；切换语言后的新请求使用相应语言样例。
- 沿用原桌面三列布局，没有另建响应式页面。

## 验证

```powershell
npm run test:chat
npx tsc --noEmit
npm run build
git diff --check
```

13 项自动测试覆盖：旧四状态兼容、新状态字段、select/text 校验、原选项代码（含首尾空格）的精确匹配、关联标识、原问题、失败重试、取消、会话隔离、冲突引用完整性、连续编号和中韩键一致。

浏览器检查原 Chat：输入问题→多条件表单→模拟失败保留 CN/物品文字→重试成功→已提交和新回答/引用；冲突双方点击正确切换原侧栏文件名/PDF 物理页码/原文；中韩固定 UI 切换，证据原文保持不变。

纯 mock 验证不等同于 #48 真实 HTTP/Agent 验收，不能据此关闭 #49。

## 后续删除假数据

1. 用真实后端在正常模式通过追问/冲突和旧状态回归。
2. 删除 `src/mocks/`、`src/chat/mock-mode.ts`，移除 api/client.ts 的 mock 分支，以及 App.tsx 的开发提示、场景链接和文档空态开关。
3. 删除测试中 fake 专属用例，保留正式转换/引用/条件校验测试。
4. 保留 AgentMessages.tsx、agent-messages.css、contracts.ts、response.ts、双语文案和 App 正式状态分支，无需重写页面。

本 PR 等待 @sikiww77 / @sun-cs 审核；不合并 main，不宣称后端已接线。

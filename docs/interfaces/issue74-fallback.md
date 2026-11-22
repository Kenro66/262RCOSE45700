# Agent 受控 Single Fallback

未配置 `RAG_MODE` 时继续使用原 Single RAG。可显式设置 `single` 或 `agent`，空值或其他值会在应用创建时被拒绝。`RAG_EXECUTION_TIMEOUT_SECONDS` 默认 120 秒，显式配置须为正整数。时限针对单次 Agent 执行，包含同一次执行内的 Fallback。检索预算按原问题累计，失败的检索也消耗预算。

EC2 的 CD 会在重启后端前自动应用迁移，详见 [EC2 数据库迁移](../../deploy/README.md)。手动部署时，先执行 `070_chat_persistence.sql`，再对同一数据库执行 `071_chat_mode_fallback.sql`，每个文件使用 `psql --single-transaction -v ON_ERROR_STOP=1 -f`。已经应用 070 的数据库只需补跑 071。071 可重复执行。历史记录不会补填模式或检索预算。已完成的回复仍可查看，缺少受控执行所需数据的历史问题需要重新发起。

真实 PostgreSQL 检查使用名称以 `issue57_test` 开头的专用测试库。覆盖率复现命令在 `backend` 运行 `python tests/issue57/run_coverage.py --postgres-url <测试库地址>`。

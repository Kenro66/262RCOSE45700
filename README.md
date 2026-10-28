# Document-RAG-Chatbot
A team project to build a document-based RAG chatbot with document upload, question answering, and source citations.

## 本地启动（后端骨架 + 数据库，Issue #4）

前置：已安装 Docker Desktop 并已启动。

> 约定：**所有 docker compose 命令都在项目根目录执行、并携带 `--env-file backend\.env`**；
> 后端应用单独开一个终端，在 `backend` 目录里运行（两个终端互不影响）。

1. **建 `backend/.env`**（模板是 `backend/.env.example`；`.env` 不要提交）

   ```bat
   copy backend\.env.example backend\.env
   ```

   填两个值：`POSTGRES_PASSWORD`（任意随机字符串，例如 32 位）和
   `DATABASE_URL`（URL 里的密码必须与 `POSTGRES_PASSWORD` 一致）。

2. **启动数据库**（首次启动会自动执行 `storage/schema.sql`：
   建 `vector` 扩展和 `documents` / `chunks` 两张表）

   ```bat
   docker compose --env-file backend\.env up -d postgres
   docker compose --env-file backend\.env ps
   ```

   等 postgres 变成 `healthy`。

3. **装后端依赖、起应用**（在 `backend` 目录；`--env-file .env` 让 uvicorn 从
   当前目录的 `.env` 读 `DATABASE_URL`，uvicorn[standard] 自带 dotenv 依赖，无需改代码）

   ```bat
   cd backend
   pip install -r requirements.txt
   uvicorn app.main:app --reload --env-file .env
   ```

4. **验证**（compose 命令回根目录执行）

   - 健康检查（数据库在 → 200）：

     ```bat
     curl http://127.0.0.1:8000/health
     ```

     期望：`{"status":"ok","database":{"version":"18.x","vector_extension":true}}`

   - 确认两张表和 embedding 列：

     ```bat
     docker compose --env-file backend\.env exec postgres psql -U rag -d p4rag -c "\dt"
     docker compose --env-file backend\.env exec postgres psql -U rag -d p4rag -c "SELECT format_type(atttypid, atttypmod) FROM pg_attribute WHERE attrelid = 'chunks'::regclass AND attname = 'embedding';"
     ```

     期望：`documents`、`chunks` 两张表存在；embedding 列输出 `vector(1024)`。

5. **失败情况验证（可选）**：把数据库停掉再查健康检查，
   应该不再是 200，且 uvicorn 日志里有明确的连接错误：

   ```bat
   docker compose --env-file backend\.env stop postgres
   curl http://127.0.0.1:8000/health
   ```

   期望：HTTP 503，响应体和日志都写明连接失败原因。

> 重新从零建库（会清空数据）：先 `docker compose --env-file backend\.env down -v`，再重复上面步骤。

-- Issue #4：MVP 数据库 schema（documents、chunks、ParsedPage 三张表）
-- 2026-10-28：并入 Issue #7 存储层（backend/app/storage/models.py）的表结构——
-- 新增 ParsedPage 表，列类型/约束/索引与 ORM 逐一对齐（表名 documents / chunks / ParsedPage）。
-- 字段对照 backend/app/contracts.py：documents=C1（DocumentRecord）、chunks=C2（Chunk）。
--
-- 执行方式：
--   1. 自动：首次 docker compose up 时，本文件挂载到 /docker-entrypoint-initdb.d/ 自动执行
--   2. 手动（数据库已初始化过、需要补执行时，在 repo 根目录执行）：
--      docker compose --env-file backend/.env exec -T postgres psql -U rag -d p4rag -f /docker-entrypoint-initdb.d/01-schema.sql

CREATE EXTENSION IF NOT EXISTS vector;

-- 文档编号序列：Issue #7 repo.py 的 next_document_id() 用 nextval 分配 DOC###
-- （应用启动时 init_db() 也会以 IF NOT EXISTS 自动创建；此处建全，
-- 使 schema.sql 单独执行后库即完整可用）
CREATE SEQUENCE IF NOT EXISTS document_number_seq START WITH 1;


-- documents：文件记录（王诗琪提供，王一霖使用）
CREATE TABLE IF NOT EXISTS documents (
    document_id   VARCHAR(64) PRIMARY KEY,        -- 例：DOC001
    filename      VARCHAR(255) NOT NULL,          -- 原始文件名
    status        VARCHAR(16) NOT NULL
        CHECK (status IN ('PROCESSING', 'READY', 'FAILED')),
    error_message TEXT,                           -- FAILED 时写原因
    page_count    INTEGER,                        -- READY 后才有
    uploaded_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_documents_status ON documents (status);



-- ParsedPage：单页解析结果（parse_pdf 输出，王诗琪存储，孙忠其使用）
CREATE TABLE IF NOT EXISTS "ParsedPage" (
    id             SERIAL PRIMARY KEY,
    document_id    VARCHAR(64) NOT NULL
        REFERENCES documents (document_id) ON DELETE CASCADE,
    page_index     INTEGER NOT NULL,              -- 从 0 开始
    page_number    INTEGER NOT NULL,              -- 物理页码，从 1 开始
    text           TEXT NOT NULL,
    language       VARCHAR(16),
    content_hash   VARCHAR(71),
    char_count     INTEGER,
    schema_version VARCHAR(16) NOT NULL DEFAULT '1.0',
    parser_version VARCHAR(64) NOT NULL DEFAULT 'unknown',
    blocks         JSONB,
    CONSTRAINT uq_document_page_index UNIQUE (document_id, page_index)
);

CREATE INDEX IF NOT EXISTS "ix_ParsedPage_document_id" ON "ParsedPage" (document_id);

-- chunks：文本块（chunk_pages 输出，王诗琪存储）
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id       VARCHAR(64) PRIMARY KEY,       -- 例：DOC001_C000003
    schema_version VARCHAR(16) NOT NULL,
    document_id    VARCHAR(64) NOT NULL
        REFERENCES documents (document_id) ON DELETE CASCADE,
    chunk_index    INTEGER NOT NULL CHECK (chunk_index >= 0),
    page_index     INTEGER NOT NULL CHECK (page_index >= 0),   -- 从 0 开始（程序内部用）
    page_number    INTEGER NOT NULL CHECK (page_number >= 1),  -- 物理页码，从 1 开始（用于引用）
    text           TEXT NOT NULL CHECK (text <> ''),           -- 空页不生成 chunk
    section_title  VARCHAR(512),
    language       VARCHAR(16) CHECK (language IS NULL OR language IN ('ko', 'zh', 'en')),
    token_count    INTEGER NOT NULL CHECK (token_count >= 0),
    char_count     INTEGER CHECK (char_count IS NULL OR char_count >= 0),
    content_hash   VARCHAR(71),
    embedding      VECTOR(1024),                  -- bge-m3，1024 维【待确认：NPU 上是否可用】
    CONSTRAINT uq_chunks_document_chunk_index UNIQUE (document_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS ix_chunks_document_page ON chunks (document_id, page_index);
CREATE INDEX IF NOT EXISTS ix_chunks_document_id ON chunks (document_id);

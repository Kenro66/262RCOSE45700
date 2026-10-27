-- Issue #4：MVP 数据库 schema（documents、chunks 两张表）
-- 字段按 backend/app/contracts.py 的 C1（DocumentRecord）与 C2（Chunk）。
--
-- 执行方式：
--   1. 自动：首次 docker compose up 时，本文件挂载到 /docker-entrypoint-initdb.d/ 自动执行
--   2. 手动（数据库已初始化过、需要补执行时，在 repo 根目录执行）：
--      docker compose --env-file backend/.env exec -T postgres psql -U rag -d p4rag -f /docker-entrypoint-initdb.d/01-schema.sql

CREATE EXTENSION IF NOT EXISTS vector;

-- C1：文件记录（王诗琪提供，王一霖使用）
CREATE TABLE IF NOT EXISTS documents (
    document_id   TEXT PRIMARY KEY,             -- 例：DOC001
    filename      TEXT NOT NULL,                -- 原始文件名
    status        TEXT NOT NULL
        CHECK (status IN ('PROCESSING', 'READY', 'FAILED')),
    error_message TEXT,                         -- FAILED 时写原因
    page_count    INTEGER,                      -- READY 后才有
    uploaded_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_documents_status ON documents (status);

-- C2：文本块（孙忠其提供，王诗琪存储）
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id       TEXT PRIMARY KEY,            -- 例：DOC001_C000003
    schema_version TEXT NOT NULL DEFAULT '1.0',
    document_id    TEXT NOT NULL
        REFERENCES documents (document_id) ON DELETE CASCADE,
    chunk_index    INTEGER NOT NULL CHECK (chunk_index >= 0),
    page_index     INTEGER NOT NULL CHECK (page_index >= 0),   -- 从 0 开始（程序内部用）
    page_number    INTEGER NOT NULL CHECK (page_number >= 1),  -- 物理页码，从 1 开始（用于引用）
    text           TEXT NOT NULL CHECK (text <> ''),
    section_title  TEXT,
    language       TEXT CHECK (language IS NULL OR language IN ('ko', 'zh', 'en')),
    token_count    INTEGER NOT NULL CHECK (token_count >= 0),
    char_count     INTEGER CHECK (char_count IS NULL OR char_count >= 0),
    content_hash   TEXT,
    embedding      vector(1024),                -- bge-m3，1024 维【待确认：NPU 上是否可用】
    UNIQUE (document_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS ix_chunks_document_page ON chunks (document_id, page_index);

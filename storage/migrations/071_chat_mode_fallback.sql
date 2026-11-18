-- 在 070_chat_persistence.sql 之后执行；不推测历史记录的模式或检索预算。
ALTER TABLE chat_questions ADD COLUMN IF NOT EXISTS mode VARCHAR(8);
ALTER TABLE chat_questions ADD COLUMN IF NOT EXISTS document_ids JSONB;
ALTER TABLE chat_questions ADD COLUMN IF NOT EXISTS retrieval_used INTEGER;
ALTER TABLE chat_questions ADD COLUMN IF NOT EXISTS state_reliable BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE chat_submissions ADD COLUMN IF NOT EXISTS run_id VARCHAR(64);
ALTER TABLE chat_submissions ADD COLUMN IF NOT EXISTS deadline_at TIMESTAMPTZ;
ALTER TABLE chat_submissions ADD COLUMN IF NOT EXISTS fallback_started BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE chat_submissions ADD COLUMN IF NOT EXISTS stop_reason VARCHAR(24);
ALTER TABLE chat_submissions ADD COLUMN IF NOT EXISTS inflight_call BOOLEAN NOT NULL DEFAULT FALSE;
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_chat_retrieval_used'
                   AND conrelid='chat_questions'::regclass) THEN
        ALTER TABLE chat_questions ADD CONSTRAINT ck_chat_retrieval_used
            CHECK (retrieval_used IS NULL OR retrieval_used BETWEEN 0 AND 3);
    END IF;
END $$;
CREATE TABLE IF NOT EXISTS chat_retrieval_attempts (
    question_id VARCHAR(128) NOT NULL REFERENCES chat_questions(question_id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL CHECK (ordinal BETWEEN 1 AND 3),
    run_id VARCHAR(64) NOT NULL,
    query TEXT NOT NULL,
    source VARCHAR(8) NOT NULL CHECK (source IN ('AGENT', 'FALLBACK')),
    outcome VARCHAR(24) NOT NULL DEFAULT 'RESERVED',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (question_id, ordinal)
);

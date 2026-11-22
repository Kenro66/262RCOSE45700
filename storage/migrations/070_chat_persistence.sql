-- 仅用于未部署版本初始化；旧数据库需单独迁移，重复 CREATE 不会删除旧列或变更主键。

CREATE TABLE IF NOT EXISTS chat_sessions (
    session_id VARCHAR(128) PRIMARY KEY,
    title VARCHAR(100) NOT NULL DEFAULT '',
    deleted BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS chat_questions (
    question_id VARCHAR(128) PRIMARY KEY,
    session_id VARCHAR(128) NOT NULL REFERENCES chat_sessions(session_id) ON DELETE CASCADE,
    question TEXT NOT NULL,
    state JSONB NOT NULL,
    clarification JSONB,
    status VARCHAR(24) NOT NULL DEFAULT 'NEW',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_chat_questions_session_id ON chat_questions(session_id);

CREATE TABLE IF NOT EXISTS chat_submissions (
    session_id VARCHAR(128) NOT NULL REFERENCES chat_sessions(session_id) ON DELETE CASCADE,
    question_id VARCHAR(128) PRIMARY KEY REFERENCES chat_questions(question_id) ON DELETE CASCADE,
    conditions JSONB NOT NULL DEFAULT '{}',
    status VARCHAR(24) NOT NULL DEFAULT 'RUNNING',
    response JSONB,
    error_code VARCHAR(64),
    cancel_requested BOOLEAN NOT NULL DEFAULT FALSE,
    worker_id VARCHAR(64) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_chat_submissions_session_id ON chat_submissions(session_id);
CREATE INDEX IF NOT EXISTS ix_chat_submissions_question_id ON chat_submissions(question_id);
CREATE INDEX IF NOT EXISTS ix_chat_submissions_status ON chat_submissions(status);

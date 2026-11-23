-- Retain execution counters and outcomes while removing legacy retrieval text.
UPDATE chat_questions
SET state = state - 'evidence' - 'evidence_ids' - 'retrieval_queries'
                  - 'question' - 'session_id' - 'question_id',
    clarification = CASE WHEN status = 'COMPLETED' THEN NULL ELSE clarification END;

UPDATE chat_submissions SET response = NULL WHERE response IS NOT NULL;
UPDATE chat_retrieval_attempts SET query = '';

-- Run once before starting the updated backend; safe to repeat.
BEGIN;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS country VARCHAR(2);
ALTER TABLE documents ADD COLUMN IF NOT EXISTS direction VARCHAR(8);
ALTER TABLE documents ADD COLUMN IF NOT EXISTS effective_date DATE;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS applicable_persons JSONB;
COMMIT;

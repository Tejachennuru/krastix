-- ============================================
-- MIGRATION 005: Phase 0 Stabilization
-- Aligns registry/task schemas, normalizes task lifecycle states,
-- and adds secure callback idempotency persistence.
-- ============================================

-- Ensure UUID helper is available before adding defaults using gen_random_uuid().
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- 1) Agent Registry alignment (runtime expects these fields)
ALTER TABLE agent_registry ADD COLUMN IF NOT EXISTS agent_key VARCHAR(100);
ALTER TABLE agent_registry ADD COLUMN IF NOT EXISTS display_name VARCHAR(255);
ALTER TABLE agent_registry ADD COLUMN IF NOT EXISTS queue_or_url VARCHAR(255);
ALTER TABLE agent_registry ADD COLUMN IF NOT EXISTS dispatch_method VARCHAR(50) DEFAULT 'celery';
ALTER TABLE agent_registry ADD COLUMN IF NOT EXISTS enabled BOOLEAN DEFAULT true;
ALTER TABLE agent_registry ADD COLUMN IF NOT EXISTS version INTEGER DEFAULT 1;
ALTER TABLE entity_definitions ADD COLUMN IF NOT EXISTS display_name VARCHAR(255);

-- Backfill modern columns from legacy schema
UPDATE agent_registry
SET agent_key = COALESCE(NULLIF(agent_key, ''), agent_id)
WHERE agent_key IS NULL OR agent_key = '';

UPDATE agent_registry
SET queue_or_url = COALESCE(NULLIF(queue_or_url, ''), queue)
WHERE queue_or_url IS NULL OR queue_or_url = '';

UPDATE agent_registry
SET display_name = COALESCE(
    NULLIF(display_name, ''),
    NULLIF(description, ''),
    COALESCE(agent_key, agent_id)
)
WHERE display_name IS NULL OR display_name = '';

CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_registry_agent_key ON agent_registry(agent_key);
CREATE INDEX IF NOT EXISTS idx_agent_registry_enabled_domains ON agent_registry(enabled) WHERE enabled = true;

-- Phase 0 migration must be non-destructive for registry semantics:
-- align schema and normalize existing rows only; do not create/overwrite
-- canonical agent definitions here.
UPDATE agent_registry
SET dispatch_method = CASE
    WHEN queue_or_url ILIKE 'http%' THEN 'http'
    ELSE 'celery'
END
WHERE dispatch_method IS NULL OR dispatch_method = '';

UPDATE agent_registry
SET enabled = true
WHERE enabled IS NULL;

UPDATE agent_registry
SET version = 1
WHERE version IS NULL;

-- 2) Task lifecycle + observability metadata
ALTER TABLE agent_tasks ADD COLUMN IF NOT EXISTS error_code VARCHAR(100);
ALTER TABLE agent_tasks ADD COLUMN IF NOT EXISTS error_detail TEXT;
ALTER TABLE agent_tasks ADD COLUMN IF NOT EXISTS attempt_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE agent_tasks ADD COLUMN IF NOT EXISTS last_heartbeat_at TIMESTAMPTZ;
ALTER TABLE agent_tasks ADD COLUMN IF NOT EXISTS correlation_id UUID DEFAULT gen_random_uuid();
ALTER TABLE agent_tasks ADD COLUMN IF NOT EXISTS queued_at TIMESTAMPTZ;
ALTER TABLE agent_tasks ADD COLUMN IF NOT EXISTS dispatched_at TIMESTAMPTZ;
ALTER TABLE agent_tasks ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ;
ALTER TABLE agent_tasks ADD COLUMN IF NOT EXISTS callback_received_at TIMESTAMPTZ;
ALTER TABLE agent_tasks ADD COLUMN IF NOT EXISTS callback_idempotency_key VARCHAR(255);
ALTER TABLE agent_tasks ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();

UPDATE agent_tasks
SET status = 'queued'
WHERE status = 'pending';

UPDATE agent_tasks
SET status = 'running'
WHERE status = 'processing';

UPDATE agent_tasks
SET status = 'completed'
WHERE status = 'success';

WITH bad AS (
    SELECT task_id, status
    FROM agent_tasks
    WHERE status IS NULL
       OR status NOT IN ('created', 'queued', 'dispatched', 'running', 'completed', 'failed', 'cancelled', 'timed_out', 'stale')
)
UPDATE agent_tasks t
SET status = 'failed',
    error_code = COALESCE(t.error_code, 'legacy_status_normalized'),
    error_detail = COALESCE(t.error_detail, 'Normalized from unsupported status: ' || COALESCE(bad.status, 'NULL')),
    error_message = COALESCE(t.error_message, 'Task status normalized during Phase 0 migration'),
    completed_at = COALESCE(t.completed_at, NOW()),
    updated_at = NOW()
FROM bad
WHERE t.task_id = bad.task_id;

UPDATE agent_tasks
SET status = COALESCE(status, 'failed');

UPDATE agent_tasks
SET correlation_id = gen_random_uuid()
WHERE correlation_id IS NULL;

ALTER TABLE agent_tasks
    ALTER COLUMN status SET DEFAULT 'created';

ALTER TABLE agent_tasks
    ALTER COLUMN status SET NOT NULL;

ALTER TABLE agent_tasks
    DROP CONSTRAINT IF EXISTS agent_tasks_status_check;

ALTER TABLE agent_tasks
    ADD CONSTRAINT agent_tasks_status_check
    CHECK (status IN ('created', 'queued', 'dispatched', 'running', 'completed', 'failed', 'cancelled', 'timed_out', 'stale'));

CREATE INDEX IF NOT EXISTS idx_agent_tasks_correlation_id ON agent_tasks(correlation_id);

-- 3) Callback idempotency registry
CREATE TABLE IF NOT EXISTS agent_task_callbacks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id UUID NOT NULL REFERENCES agent_tasks(task_id) ON DELETE CASCADE,
    idempotency_key VARCHAR(255) NOT NULL UNIQUE,
    payload_hash VARCHAR(64) NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_agent_task_callbacks_task_id ON agent_task_callbacks(task_id);

ALTER TABLE agent_task_callbacks ENABLE ROW LEVEL SECURITY;

DO $$ BEGIN
    CREATE POLICY "user_owns_task_callbacks" ON agent_task_callbacks
        FOR ALL USING (
            auth.uid() = (
                SELECT user_id
                FROM agent_tasks
                WHERE task_id = agent_task_callbacks.task_id
            )
        );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE POLICY "service_full_access_task_callbacks" ON agent_task_callbacks
        FOR ALL USING (auth.role() = 'service_role');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- ============================================
-- MIGRATION 005 COMPLETE
-- ============================================

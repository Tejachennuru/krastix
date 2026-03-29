-- ============================================
-- MIGRATION 004: Communication Agent + Email Draft HITL
-- Adds:
--   1) communication agent registry entry
--   2) domain queue enablement for HR/PERSONAL
--   3) email_draft entity definition for accept/modify/reject flow (EAV)
-- ============================================

-- Ensure newer agent_registry columns exist (idempotent)
ALTER TABLE agent_registry ADD COLUMN IF NOT EXISTS agent_key VARCHAR(100);
ALTER TABLE agent_registry ADD COLUMN IF NOT EXISTS display_name VARCHAR(255);
ALTER TABLE agent_registry ADD COLUMN IF NOT EXISTS queue_or_url VARCHAR(255);
ALTER TABLE agent_registry ADD COLUMN IF NOT EXISTS dispatch_method VARCHAR(50) DEFAULT 'celery';
ALTER TABLE agent_registry ADD COLUMN IF NOT EXISTS version INTEGER DEFAULT 1;
ALTER TABLE agent_registry ADD COLUMN IF NOT EXISTS enabled BOOLEAN DEFAULT true;

-- Back-fill agent_key from agent_id when needed
UPDATE agent_registry SET agent_key = agent_id WHERE agent_key IS NULL;

-- Register communication agent
INSERT INTO agent_registry (
    agent_id, agent_key, display_name, queue, queue_or_url,
    dispatch_method, capabilities, supported_domains,
    description, health_endpoint, enabled, version
) VALUES (
    'communication_gmail_v1',
    'communication_gmail_v1',
    'Communication Agent (Gmail)',
    'communication_queue',
    'communication_queue',
    'celery',
    '{
        "actions": ["send_email"],
        "description": "Sends approved emails using the signed-in Google account via Gmail API.",
        "celery_task_name": "agents.communication_worker.execute_task"
    }'::jsonb,
    '["HR_RECRUITER", "PERSONAL_ASSISTANT"]'::jsonb,
    'Communication agent for approved outbound email delivery through Gmail OAuth.',
    NULL,
    true,
    1
)
ON CONFLICT (agent_id) DO UPDATE SET
    agent_key = EXCLUDED.agent_key,
    display_name = EXCLUDED.display_name,
    queue = EXCLUDED.queue,
    queue_or_url = EXCLUDED.queue_or_url,
    dispatch_method = EXCLUDED.dispatch_method,
    capabilities = EXCLUDED.capabilities,
    supported_domains = EXCLUDED.supported_domains,
    description = EXCLUDED.description,
    enabled = EXCLUDED.enabled,
    version = EXCLUDED.version,
    updated_at = NOW();

-- Enable queue for HR_RECRUITER
UPDATE domain_configs
SET allowed_agent_queues = allowed_agent_queues || '["communication_queue"]'::jsonb,
    updated_at = NOW()
WHERE domain_key = 'HR_RECRUITER'
  AND NOT (allowed_agent_queues @> '"communication_queue"'::jsonb);

-- Enable queue for PERSONAL_ASSISTANT
UPDATE domain_configs
SET allowed_agent_queues = allowed_agent_queues || '["communication_queue"]'::jsonb,
    updated_at = NOW()
WHERE domain_key = 'PERSONAL_ASSISTANT'
  AND NOT (allowed_agent_queues @> '"communication_queue"'::jsonb);

-- Email draft entity definition for deterministic HITL review (EAV)
INSERT INTO entity_definitions (entity_type, description, validation_schema) VALUES
(
    'email_draft',
    'Email draft awaiting user review with accept/modify/reject lifecycle',
    '{
        "type": "object",
        "properties": {
            "domain_key": {"type": "string"},
            "session_id": {"type": "string"},
            "draft_payload": {
                "type": "object",
                "properties": {
                    "to": {"type": "array", "items": {"type": "string"}},
                    "cc": {"type": "array", "items": {"type": "string"}},
                    "bcc": {"type": "array", "items": {"type": "string"}},
                    "subject": {"type": "string"},
                    "body": {"type": "string"}
                },
                "required": ["to", "subject", "body"]
            }
        },
        "required": ["session_id", "draft_payload"]
    }'::jsonb
)
ON CONFLICT (entity_type) DO NOTHING;

CREATE INDEX IF NOT EXISTS idx_entities_email_draft_session_status
    ON entities (user_id, status, created_at DESC)
    WHERE entity_type = 'email_draft';

-- Compatibility cleanup for earlier experimental schema that created a dedicated table.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'email_drafts'
    ) THEN
        INSERT INTO entities (id, user_id, entity_type, display_name, status, data, created_at, updated_at)
        SELECT
            id,
            user_id,
            'email_draft',
            'Email Draft',
            status,
            jsonb_build_object(
                'domain_key', domain_key,
                'session_id', session_id,
                'draft_payload', draft_payload
            ),
            created_at,
            updated_at
        FROM email_drafts
        ON CONFLICT (id) DO NOTHING;

        DROP TABLE email_drafts;
    END IF;
END $$;

-- ============================================
-- MIGRATION 004 COMPLETE
-- ============================================

-- ============================================
-- MIGRATION 003: Document Agent (VDU)
--
-- Adds:
--   1. doc_vdu_v1 to agent_registry
--   2. document_artifacts table for parsed results
--   3. Updates domain_configs to allow doc_queue
--   4. document entity_definition for generic parsed docs
-- ============================================

-- 1. REGISTER THE DOCUMENT AGENT
-- Uses the newer agent_registry schema with agent_key, display_name,
-- queue_or_url, dispatch_method, capabilities (dict), etc.
-- If your DB still uses the old schema (agent_id, queue), run the
-- ALTER TABLEs below first.

-- Ensure newer columns exist (idempotent)
ALTER TABLE agent_registry ADD COLUMN IF NOT EXISTS agent_key VARCHAR(100);
ALTER TABLE agent_registry ADD COLUMN IF NOT EXISTS display_name VARCHAR(255);
ALTER TABLE agent_registry ADD COLUMN IF NOT EXISTS queue_or_url VARCHAR(255);
ALTER TABLE agent_registry ADD COLUMN IF NOT EXISTS dispatch_method VARCHAR(50) DEFAULT 'celery';
ALTER TABLE agent_registry ADD COLUMN IF NOT EXISTS version INTEGER DEFAULT 1;
ALTER TABLE agent_registry ADD COLUMN IF NOT EXISTS enabled BOOLEAN DEFAULT true;

-- Back-fill agent_key from agent_id if NULL
UPDATE agent_registry SET agent_key = agent_id WHERE agent_key IS NULL;

-- Ensure entity_definitions has display_name
ALTER TABLE entity_definitions ADD COLUMN IF NOT EXISTS display_name VARCHAR(255);

-- Insert the Document Agent
INSERT INTO agent_registry (
    agent_id, agent_key, display_name, queue, queue_or_url,
    dispatch_method, capabilities, supported_domains,
    description, health_endpoint, enabled, version
) VALUES (
    'doc_vdu_v1',
    'doc_vdu_v1',
    'Document Agent (VDU)',
    'doc_queue',
    'http://doc_agent:8002',
    'http',
    '{
        "actions": ["extract", "ocr", "table_parsing", "visual_verification"],
        "description": "Extracts structured data from PDFs and images using Vision-Language Models. Supports schema-guided extraction, table reconstruction, and visual grounding with bounding boxes.",
        "http_endpoint": "/document/run"
    }'::jsonb,
    '["HR_RECRUITER", "PERSONAL_ASSISTANT", "SALES_LEAD_GEN", "FINANCE", "LEGAL"]'::jsonb,
    'Document Agent — multi-stage VDU pipeline using Qwen VL for OCR, table parsing, schema-guided extraction, and visual verification with Reflect-Refine loops.',
    'http://doc_agent:8002/health',
    true,
    1
)
ON CONFLICT (agent_id) DO UPDATE SET
    agent_key = EXCLUDED.agent_key,
    display_name = EXCLUDED.display_name,
    queue_or_url = EXCLUDED.queue_or_url,
    dispatch_method = EXCLUDED.dispatch_method,
    capabilities = EXCLUDED.capabilities,
    supported_domains = EXCLUDED.supported_domains,
    description = EXCLUDED.description,
    health_endpoint = EXCLUDED.health_endpoint,
    enabled = EXCLUDED.enabled,
    version = EXCLUDED.version,
    updated_at = NOW();


-- 2. UPDATE DOMAIN CONFIGS to allow the doc agent
-- HR_RECRUITER: add the doc agent URL to allowed_agent_queues
UPDATE domain_configs
SET allowed_agent_queues = allowed_agent_queues || '["http://doc_agent:8002"]'::jsonb,
    updated_at = NOW()
WHERE domain_key = 'HR_RECRUITER'
  AND NOT (allowed_agent_queues @> '"http://doc_agent:8002"'::jsonb);

-- PERSONAL_ASSISTANT: add the doc agent URL
UPDATE domain_configs
SET allowed_agent_queues = allowed_agent_queues || '["http://doc_agent:8002"]'::jsonb,
    updated_at = NOW()
WHERE domain_key = 'PERSONAL_ASSISTANT'
  AND NOT (allowed_agent_queues @> '"http://doc_agent:8002"'::jsonb);


-- 3. DOCUMENT ARTIFACTS TABLE
-- Stores full extraction results with visual grounding data.
-- Enables "click-to-source" UI features.
CREATE TABLE IF NOT EXISTS document_artifacts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    task_id UUID REFERENCES agent_tasks(task_id),
    file_id TEXT NOT NULL,
    entity_type VARCHAR(100),
    parsed_markdown TEXT,
    extracted_data JSONB,
    bounding_boxes JSONB,  -- [ {field, page, x, y, w, h, confidence} ]
    page_count INTEGER DEFAULT 0,
    status VARCHAR(50) DEFAULT 'pending',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_doc_artifacts_user ON document_artifacts(user_id);
CREATE INDEX IF NOT EXISTS idx_doc_artifacts_task ON document_artifacts(task_id);
CREATE INDEX IF NOT EXISTS idx_doc_artifacts_file ON document_artifacts(file_id);

-- RLS
ALTER TABLE document_artifacts ENABLE ROW LEVEL SECURITY;

DO $$ BEGIN
    CREATE POLICY "user_owns_doc_artifacts" ON document_artifacts
        FOR ALL USING (auth.uid() = user_id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE POLICY "service_full_access_doc_artifacts" ON document_artifacts
        FOR ALL USING (auth.role() = 'service_role');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;


-- 4. ENTITY DEFINITION for generic parsed documents
INSERT INTO entity_definitions (entity_type, display_name, description, validation_schema) VALUES
(
    'document',
    'Parsed Document',
    'A parsed document with extracted text, tables, and metadata',
    '{
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "document_type": {"type": "string"},
            "page_count": {"type": "integer"},
            "raw_text": {"type": "string"},
            "sections": {"type": "array", "items": {"type": "object"}},
            "tables": {"type": "array", "items": {"type": "object"}},
            "source_file_id": {"type": "string"}
        },
        "required": []
    }'::jsonb
)
ON CONFLICT (entity_type) DO NOTHING;


-- ============================================
-- MIGRATION 003 COMPLETE
-- ============================================

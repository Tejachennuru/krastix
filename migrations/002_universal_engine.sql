-- ============================================
-- MIGRATION 002: The Universal Agentic Engine
-- Adds: Optimistic Concurrency, Agent Registry,
--        Contextual Memory Indexing
-- ============================================

-- 1. OPTIMISTIC CONCURRENCY on entities
ALTER TABLE entities ADD COLUMN IF NOT EXISTS version INTEGER DEFAULT 1;

-- 2. AGENT REGISTRY: Dynamic agent discovery
CREATE TABLE IF NOT EXISTS agent_registry (
    agent_id VARCHAR(100) PRIMARY KEY,
    queue VARCHAR(100) NOT NULL,
    capabilities JSONB NOT NULL DEFAULT '[]'::jsonb,
    supported_domains JSONB NOT NULL DEFAULT '[]'::jsonb,
    description TEXT,
    health_endpoint VARCHAR(255),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- RLS for agent_registry
ALTER TABLE agent_registry ENABLE ROW LEVEL SECURITY;

DO $$ BEGIN
    CREATE POLICY "public_read_agent_registry" ON agent_registry FOR SELECT USING (true);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE POLICY "service_full_access_agent_registry" ON agent_registry FOR ALL USING (auth.role() = 'service_role');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- 3. CONTEXTUAL INDEXING: Prevent cross-domain data leaks in RAG
CREATE INDEX IF NOT EXISTS idx_memories_user_domain ON memories(user_id, domain_key);

-- 4. SEED AGENT REGISTRY
INSERT INTO agent_registry (agent_id, queue, capabilities, supported_domains, description, health_endpoint) VALUES
(
    'crm_universal_v1',
    'crm_queue',
    '["upsert_entity", "get_entities"]'::jsonb,
    '["HR_RECRUITER", "SALES_LEAD_GEN"]'::jsonb,
    'Universal CRM agent — manages entities (candidates, leads, contacts) with schema validation and optimistic concurrency.',
    NULL
),
(
    'form_tally_v1',
    'form_queue',
    '["create_form", "list_forms"]'::jsonb,
    '["HR_RECRUITER"]'::jsonb,
    'Form builder agent — creates and manages Tally.so forms for applications and surveys.',
    NULL
),
(
    'research_firecrawl_v1',
    'research_queue',
    '["web_search", "scrape_url", "linkedin_profile", "linkedin_company", "site_map"]'::jsonb,
    '["HR_RECRUITER", "PERSONAL_ASSISTANT", "SALES_LEAD_GEN"]'::jsonb,
    'Research agent — performs web searches, scrapes pages, maps sites, and retrieves LinkedIn data.',
    'http://research_agent:8001/health'
)
ON CONFLICT (agent_id) DO UPDATE SET
    capabilities = EXCLUDED.capabilities,
    supported_domains = EXCLUDED.supported_domains,
    description = EXCLUDED.description,
    updated_at = NOW();

-- 5. SEED additional entity definition for LEAD (Sales domain)
INSERT INTO entity_definitions (entity_type, description, validation_schema) VALUES
(
    'lead',
    'A potential sales lead or prospect',
    '{
        "type": "object",
        "properties": {
            "email": {"type": "string"},
            "company": {"type": "string"},
            "phone": {"type": "string"},
            "source": {"type": "string"},
            "deal_value": {"type": "number"},
            "notes": {"type": "string"}
        },
        "required": ["email", "company"]
    }'::jsonb
),
(
    'contact',
    'A general contact entry',
    '{
        "type": "object",
        "properties": {
            "email": {"type": "string"},
            "phone": {"type": "string"},
            "company": {"type": "string"},
            "role": {"type": "string"},
            "notes": {"type": "string"}
        },
        "required": ["email"]
    }'::jsonb
)
ON CONFLICT (entity_type) DO NOTHING;

-- ============================================
-- MIGRATION 002 COMPLETE
-- ============================================

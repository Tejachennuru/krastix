-- ============================================
-- MIGRATION 006: Phase 1 Planner + Scheduler
-- Persisted DAG plans, node execution state, and event logs.
-- ============================================

CREATE TABLE IF NOT EXISTS plans (
    plan_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    domain_key VARCHAR(100) NOT NULL,
    session_id UUID,
    source_message TEXT,
    status VARCHAR(50) NOT NULL DEFAULT 'created',
    summary TEXT,
    error_detail TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);

ALTER TABLE plans
    DROP CONSTRAINT IF EXISTS plans_status_check;
ALTER TABLE plans
    ADD CONSTRAINT plans_status_check
    CHECK (status IN ('created', 'running', 'completed', 'completed_with_failures', 'failed', 'cancelled'));

CREATE INDEX IF NOT EXISTS idx_plans_user_created ON plans(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_plans_status ON plans(status);
CREATE INDEX IF NOT EXISTS idx_plans_session ON plans(session_id);

CREATE TABLE IF NOT EXISTS plan_nodes (
    node_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    plan_id UUID NOT NULL REFERENCES plans(plan_id) ON DELETE CASCADE,
    node_key VARCHAR(120) NOT NULL,
    node_type VARCHAR(50) NOT NULL DEFAULT 'agent_task',
    status VARCHAR(50) NOT NULL DEFAULT 'pending',
    agent_queue VARCHAR(255) NOT NULL,
    instruction TEXT NOT NULL,
    task_action VARCHAR(120),
    parameters JSONB NOT NULL DEFAULT '{}'::jsonb,
    priority INTEGER NOT NULL DEFAULT 1,
    dependencies JSONB NOT NULL DEFAULT '[]'::jsonb,
    task_id UUID REFERENCES agent_tasks(task_id) ON DELETE SET NULL,
    result JSONB,
    error_code VARCHAR(100),
    error_detail TEXT,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(plan_id, node_key)
);

ALTER TABLE plan_nodes
    DROP CONSTRAINT IF EXISTS plan_nodes_status_check;
ALTER TABLE plan_nodes
    ADD CONSTRAINT plan_nodes_status_check
    CHECK (status IN ('pending', 'ready', 'queued', 'dispatched', 'running', 'completed', 'failed', 'cancelled', 'blocked'));

CREATE INDEX IF NOT EXISTS idx_plan_nodes_plan_status ON plan_nodes(plan_id, status);
CREATE INDEX IF NOT EXISTS idx_plan_nodes_task_id ON plan_nodes(task_id);

CREATE TABLE IF NOT EXISTS plan_events (
    event_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    plan_id UUID NOT NULL REFERENCES plans(plan_id) ON DELETE CASCADE,
    node_id UUID REFERENCES plan_nodes(node_id) ON DELETE CASCADE,
    event_type VARCHAR(100) NOT NULL,
    event_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_plan_events_plan_created ON plan_events(plan_id, created_at);

ALTER TABLE agent_tasks ADD COLUMN IF NOT EXISTS plan_id UUID REFERENCES plans(plan_id) ON DELETE SET NULL;
ALTER TABLE agent_tasks ADD COLUMN IF NOT EXISTS plan_node_id UUID REFERENCES plan_nodes(node_id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS idx_agent_tasks_plan_id ON agent_tasks(plan_id);

-- RLS
ALTER TABLE plans ENABLE ROW LEVEL SECURITY;
ALTER TABLE plan_nodes ENABLE ROW LEVEL SECURITY;
ALTER TABLE plan_events ENABLE ROW LEVEL SECURITY;

DO $$ BEGIN
    CREATE POLICY "user_owns_plans" ON plans FOR ALL USING (auth.uid() = user_id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE POLICY "user_owns_plan_nodes" ON plan_nodes
        FOR ALL USING (
            auth.uid() = (SELECT user_id FROM plans WHERE plan_id = plan_nodes.plan_id)
        );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE POLICY "user_owns_plan_events" ON plan_events
        FOR ALL USING (
            auth.uid() = (SELECT user_id FROM plans WHERE plan_id = plan_events.plan_id)
        );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE POLICY "service_full_access_plans" ON plans FOR ALL USING (auth.role() = 'service_role');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE POLICY "service_full_access_plan_nodes" ON plan_nodes FOR ALL USING (auth.role() = 'service_role');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE POLICY "service_full_access_plan_events" ON plan_events FOR ALL USING (auth.role() = 'service_role');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;


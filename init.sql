-- ============================================
-- KRASTIX ORCHESTRATOR – MASTER PRODUCTION SCHEMA
-- Architecture: Data-Driven, Event-Sourced, Microservices-Ready
-- Status: HARDENED & SCALABLE
-- ============================================

-- --------------------------------------------
-- 1. EXTENSIONS
-- --------------------------------------------
CREATE EXTENSION IF NOT EXISTS vector;

-- --------------------------------------------
-- 2. CORE IDENTITY & BILLING
-- --------------------------------------------
CREATE TABLE IF NOT EXISTS profiles (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email VARCHAR(255) UNIQUE NOT NULL,
    tier VARCHAR(50) DEFAULT 'free', -- 'free', 'pro', 'enterprise'
    credits INTEGER DEFAULT 100,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- --------------------------------------------
-- 3. THE "STEERING" (Configuration & Rules)
-- --------------------------------------------

-- Domain Registry: Defines "HR", "Sales", "Personal" behaviors
CREATE TABLE IF NOT EXISTS domain_configs (
    domain_key VARCHAR(100) PRIMARY KEY,
    display_name VARCHAR(255) NOT NULL,
    system_prompt TEXT NOT NULL,
    allowed_agent_queues JSONB NOT NULL, -- e.g. ["research", "email"]
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Entity Registry: Defines the "Shape" of data (The Rulebook)
-- Agents cannot invent new types; they must follow these schemas.
CREATE TABLE IF NOT EXISTS entity_definitions (
    entity_type VARCHAR(100) PRIMARY KEY,
    description TEXT,
    -- JSON Schema to validate data upstream in Python
    validation_schema JSONB NOT NULL, 
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- --------------------------------------------
-- 4. THE "FERRARI ENGINE" (Flexible Data)
-- --------------------------------------------

-- Helper Function: Extracts text array from JSONB (Immutable for Generated Columns)
CREATE OR REPLACE FUNCTION extract_skills_from_data(data jsonb)
RETURNS text[] AS $$
BEGIN
    -- Returns null if 'skills' key doesn't exist or isn't an array
    IF jsonb_typeof(data->'skills') = 'array' THEN
        RETURN ARRAY(SELECT jsonb_array_elements_text(data->'skills'));
    ELSE
        RETURN NULL;
    END IF;
END;
$$ LANGUAGE plpgsql IMMUTABLE;

-- The Universal Data Table
CREATE TABLE IF NOT EXISTS entities (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    
    -- Foreign Key enforces that 'candidate' or 'flight' must exist in definitions
    entity_type VARCHAR(100) NOT NULL REFERENCES entity_definitions(entity_type), 
    
    display_name TEXT, -- Human readable label
    status VARCHAR(50), -- High-level status
    
    -- THE PAYLOAD: Stores specific fields (skills, prices, dates)
    data JSONB DEFAULT '{}'::jsonb, 
    
    -- PERFORMANCE BOOSTER: Uses the function above
    derived_skills TEXT[] GENERATED ALWAYS AS (
        extract_skills_from_data(data)
    ) STORED,

    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- GIN Indexes (Critical for JSONB Performance)
CREATE INDEX idx_entities_data ON entities USING gin (data);
CREATE INDEX idx_entities_skills ON entities USING gin (derived_skills);
CREATE INDEX idx_entities_user_type ON entities(user_id, entity_type);

-- --------------------------------------------
-- 5. THE "BLACK BOX" (Audit History)
-- --------------------------------------------

-- Tracks every change an agent makes. Crucial for AI accountability.
CREATE TABLE IF NOT EXISTS entity_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id UUID NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    
    -- "status_change", "data_update", "email_sent"
    event_type VARCHAR(100) NOT NULL, 
    
    -- The "Diff": {"old_status": "new", "new_status": "hired"}
    payload JSONB, 
    
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX idx_entity_events_entity ON entity_events(entity_id);

-- --------------------------------------------
-- 6. ORCHESTRATION & MEMORY
-- --------------------------------------------

-- Agent Task Queue (State management)
CREATE TABLE IF NOT EXISTS agent_tasks (
    task_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    domain_key VARCHAR(100) NOT NULL,
    agent_queue VARCHAR(100) NOT NULL,
    status VARCHAR(50) DEFAULT 'pending',
    input_payload JSONB NOT NULL,
    output_result JSONB,
    error_message TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);
CREATE INDEX idx_agent_tasks_status ON agent_tasks(status);
CREATE INDEX idx_agent_tasks_user ON agent_tasks(user_id, created_at DESC);

-- Chat History & Planning
CREATE TABLE IF NOT EXISTS conversations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    domain_key VARCHAR(100) NOT NULL,
    conversation_history JSONB NOT NULL DEFAULT '[]',
    current_plan TEXT,
    active_microservice VARCHAR(100),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Vector Memory (RAG)
CREATE TABLE IF NOT EXISTS memories (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    domain_key VARCHAR(100) NOT NULL,
    content TEXT NOT NULL,
    embedding vector(768), -- Dimensions for Gemini/OpenAI
    metadata JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX idx_memories_user_domain ON memories(user_id, domain_key);

-- OAuth Integrations
CREATE TABLE IF NOT EXISTS integrations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    provider VARCHAR(50) NOT NULL,
    access_token TEXT NOT NULL,
    refresh_token TEXT,
    expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(user_id, provider)
);

-- --------------------------------------------
-- 7. SECURITY (Row Level Security)
-- --------------------------------------------
ALTER TABLE profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE domain_configs ENABLE ROW LEVEL SECURITY;
ALTER TABLE entity_definitions ENABLE ROW LEVEL SECURITY;
ALTER TABLE entities ENABLE ROW LEVEL SECURITY;
ALTER TABLE entity_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_tasks ENABLE ROW LEVEL SECURITY;
ALTER TABLE conversations ENABLE ROW LEVEL SECURITY;
ALTER TABLE memories ENABLE ROW LEVEL SECURITY;
ALTER TABLE integrations ENABLE ROW LEVEL SECURITY;

-- User Policies (Can only see their own data)
CREATE POLICY "user_owns_profile" ON profiles FOR ALL USING (auth.uid() = id);
CREATE POLICY "user_owns_entities" ON entities FOR ALL USING (auth.uid() = user_id);
CREATE POLICY "user_owns_events" ON entity_events FOR ALL USING (auth.uid() = (SELECT user_id FROM entities WHERE id = entity_events.entity_id));
CREATE POLICY "user_owns_tasks" ON agent_tasks FOR ALL USING (auth.uid() = user_id);
CREATE POLICY "user_owns_conversations" ON conversations FOR ALL USING (auth.uid() = user_id);
CREATE POLICY "user_owns_memories" ON memories FOR ALL USING (auth.uid() = user_id);
CREATE POLICY "user_owns_integrations" ON integrations FOR ALL USING (auth.uid() = user_id);

-- Public Read Policies (Configs are shared)
CREATE POLICY "public_read_domain_configs" ON domain_configs FOR SELECT USING (true);
CREATE POLICY "public_read_entity_definitions" ON entity_definitions FOR SELECT USING (true);

-- Service Role Policies (Backend/Workers have full access)
CREATE POLICY "service_full_access_profiles" ON profiles FOR ALL USING (auth.role() = 'service_role');
CREATE POLICY "service_full_access_entities" ON entities FOR ALL USING (auth.role() = 'service_role');
CREATE POLICY "service_full_access_events" ON entity_events FOR ALL USING (auth.role() = 'service_role');
CREATE POLICY "service_full_access_tasks" ON agent_tasks FOR ALL USING (auth.role() = 'service_role');
CREATE POLICY "service_full_access_conversations" ON conversations FOR ALL USING (auth.role() = 'service_role');
CREATE POLICY "service_full_access_memories" ON memories FOR ALL USING (auth.role() = 'service_role');
CREATE POLICY "service_full_access_integrations" ON integrations FOR ALL USING (auth.role() = 'service_role');

-- --------------------------------------------
-- 8. SEED DATA (The Default Setup)
-- --------------------------------------------

-- 8.1 Define the Domain Logic
INSERT INTO domain_configs (domain_key, display_name, system_prompt, allowed_agent_queues) VALUES
('HR_RECRUITER', 'HR Recruitment Assistant', 
'You are an AI HR Recruitment Assistant responsible for managing and supporting end-to-end recruitment operations.
Core Function
Your primary function is to assist with candidate sourcing, screening, coordination, and communication while ensuring a professional and efficient hiring process.

Responsibilities
Create, manage, and optimize candidate application forms using Tally
Screen resumes and evaluate candidates based on role-specific requirements
Coordinate and schedule interviews efficiently
Communicate with candidates professionally via Gmail
Track, update, and manage candidate status across the hiring pipeline

Agent Orchestration & Task Delegation
You have access to multiple specialized agents, each with defined capabilities
Maintain awareness of what each agent can and cannot do
Analyze incoming requests and recruitment requirements
Plan, allocate, and delegate tasks to the most appropriate agent
When a task requires execution (e.g., candidate research, resume screening, interview scheduling, status updates), explicitly delegate it to the relevant agent

Operational Behavior
Always remain professional, respectful, and neutral
Be proactive, structured, and detail-oriented
Ensure clarity, accuracy, and efficiency in all actions
Maintain consistent and high-quality candidate communication
Optimize for a positive candidate experience and smooth hiring workflows

Operating Principle
Autonomously manage recruitment workflows by coordinating agents, executing delegated tasks, and ensuring candidates move smoothly through each stage of the hiring process.',
'["research_queue", "crm_queue", "form_queue"]'::jsonb),

('PERSONAL_ASSISTANT', 'Personal Assistant',
'You are a Personal AI Assistant (PA) designed to manage daily life operations, scheduling, communication, and task execution on behalf of the user.
Core Function
Your primary function is to act as a reliable, proactive personal assistant that organizes time, manages communication, executes tasks, and serves as a long-term second brain for information storage and retrieval.

Scheduling & Time Management
Plan, organize, and manage schedules using Google Calendar
Schedule, reschedule, and cancel meetings and appointments
Optimize daily, weekly, and long-term timetables
Set reminders, deadlines, and follow-ups
Resolve scheduling conflicts proactively

Communication Management
Manage communication across all available communication tools
Draft, send, and respond to messages on behalf of the user when authorized
Maintain professional, polite, and context-aware communication
Track conversations and ensure timely follow-ups

Task Planning & Execution
Break down goals into actionable tasks
Plan and prioritize tasks based on urgency, importance, and user preferences
Track task progress and completion
Proactively suggest optimizations and next steps

Shopping, Orders & Bookings
Execute shopping and service orders via MCP servers such as Zomato and Swiggy
Book appointments (medical, personal, professional, or services)
Handle reservations and confirmations
Ensure accuracy, timing, and cost-awareness for all bookings

Payments & Transactions
Perform payments securely via Razorpay MCP server
Confirm transaction details before execution
Track payment history and confirmations
Ensure financial actions are deliberate, accurate, and transparent

Knowledge Capture & Second Brain
Capture and store information from:
Text, Voice, Images, Files, PDFs
Organize notes with context, timestamps, and relevance
Retain memory persistently and retrieve information accurately when requested
Act as a searchable, reliable second brain for the user

Agent Orchestration & Tool Usage
You have access to multiple tools, MCP servers, and agents
Maintain awareness of each tool’s capabilities and limitations
Analyze user requests and determine the best execution path
Delegate tasks to the appropriate agent or MCP server when required
Coordinate multi-step tasks autonomously

Behavioral Guidelines
Always remain professional, respectful, and user-centric
Be proactive, discreet, and detail-oriented
Minimize friction and cognitive load for the user
Prioritize accuracy, privacy, and efficiency
Adapt to user habits, preferences, and routines over time

Operating Principle
Autonomously manage personal workflows, communications, and life logistics by planning intelligently, executing reliably, and acting as an extension of the user’s memory and decision-making.',
'["research_queue"]'::jsonb)
ON CONFLICT (domain_key) DO NOTHING;

-- 8.2 Define the Data Rules (The Brakes)
INSERT INTO entity_definitions (entity_type, description, validation_schema) VALUES
(
    'candidate', 
    'A potential hire for a job role',
    '{
        "type": "object", 
        "properties": {
            "email": {"type": "string"}, 
            "phone": {"type": "string"}, 
            "skills": {"type": "array", "items": {"type": "string"}},
            "linkedin_url": {"type": "string"}
        }, 
        "required": ["email", "skills"]
    }'::jsonb
),
(
    'flight_booking',
    'A travel reservation',
    '{
        "type": "object", 
        "properties": {
            "pnr": {"type": "string"}, 
            "airline": {"type": "string"}, 
            "price": {"type": "number"},
            "departure_time": {"type": "string"}
        }, 
        "required": ["pnr", "price"]
    }'::jsonb
)
ON CONFLICT (entity_type) DO NOTHING;

-- 8.3 Seed Test User
INSERT INTO profiles (email, tier, credits)
VALUES ('test@example.com', 'pro', 1000)
ON CONFLICT (email) DO NOTHING;





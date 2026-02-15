-- ============================================
-- KRASTIX TEST SEED DATA
-- Run this in Supabase SQL Editor ONCE before testing
-- ============================================

-- 1. Ensure test user exists (same as init.sql)
INSERT INTO profiles (id, email, tier, credits)
VALUES ('a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11', 'test@example.com', 'pro', 1000)
ON CONFLICT (email) DO NOTHING;

-- 2. Create a test conversation (needed for audit log saves)
INSERT INTO conversations (id, user_id, domain_key, conversation_history)
VALUES (
    'b1eebc99-9c0b-4ef8-bb6d-6bb9bd380a22',
    'a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11',
    'HR_RECRUITER',
    '[]'::jsonb
)
ON CONFLICT (id) DO NOTHING;

-- 3. Mock Tally integration token (Form Agent needs this)
INSERT INTO integrations (user_id, provider, access_token)
VALUES (
    'a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11',
    'tally',
    'mock-tally-token-for-testing'
)
ON CONFLICT (user_id, provider) DO NOTHING;

-- 4. Verify seed data
SELECT 'profiles' AS "table", count(*) FROM profiles
UNION ALL
SELECT 'domain_configs', count(*) FROM domain_configs
UNION ALL
SELECT 'conversations', count(*) FROM conversations
UNION ALL
SELECT 'integrations', count(*) FROM integrations;

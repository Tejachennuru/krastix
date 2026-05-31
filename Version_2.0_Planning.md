# Krastix Version 2.0 Planning

## 1) Why V2 is Needed

Krastix already has a strong base: orchestrator + specialized agents, task callbacks, SSE chat streaming, semantic memory, and a flexible entity model.  
But there is a gap between the current implementation and the target architecture:

- Target docs describe dynamic DAG planning, MCP-first integrations, and stronger platform guarantees.
- Current runtime is mostly single-step delegation (planner -> dispatcher -> one agent task).
- Data model flexibility is high, but governance and query strategy need hardening before enterprise scale.

Version 2.0 should focus on **making the platform reliable, governable, and enterprise-configurable**, while keeping your flexibility goals.

---

## 2) Current-State Assessment (What is Good vs What is Risky)

## 2.1 What is working well

- Registry-driven agent discovery and domain scoping are in place.
- Task callback + stale task watcher gives a useful reliability safety net.
- Hybrid data model (`entities` + JSONB + `entity_definitions`) allows fast feature additions.
- Frontend already supports streaming + delegated task polling.
- Multi-agent split (CRM, Form, Doc, Research, Communication) is clear and extensible.

## 2.2 Current issues and wrong-direction signals

### A) Orchestration depth mismatch
- Architecture intent says DAG and parallel/sequential multi-task execution.
- Runtime graph is mostly planner/dispatcher with single-step delegation.
- Result: complex tasks are harder to coordinate, retry, and audit.

### B) Registry contract drift risk
- `init.sql` and migrations show old/new registry schema overlap.
- Runtime expects newer fields (`agent_key`, `queue_or_url`, `dispatch_method`, `enabled`).
- Result: fresh environments can be fragile or require strict migration ordering.

### C) Dispatch reliability gaps
- HTTP dispatch failure behavior can appear as "dispatched" even when target call fails.
- Callback pipeline is mostly best-effort; stronger idempotency/auth/retry strategy is needed.

### D) Task lifecycle inconsistency
- Status values are not fully normalized (`pending`, `processing`, `success`, `completed`, `failed`, `stale`).
- Result: monitoring, retry policy, and UI behavior are less deterministic.

### E) EAV governance/query pressure
- JSONB flexibility works, but hot-field querying/indexing is uneven.
- Runtime schema insertion (from workers) can bypass migration governance.
- Result: long-term performance and data consistency risk.

### F) MCP described, but not yet a concrete runtime layer
- Code mostly uses direct HTTP/Celery/provider SDK paths.
- No unified MCP connection manager, capability negotiation, or policy enforcement.

---

## 3) Version 2.0 Product Goals

1. **Reliable orchestration engine** with explicit multi-step execution plans.
2. **MCP-enabled tool layer** so agents can use user-integrations via a standard protocol.
3. **Enterprise data strategy** that balances flexibility and performance.
4. **Operational maturity** (state model, retries, observability, SLOs).
5. **Configurable domain onboarding** so new customer requirements do not require frequent DB rewrites.

---

## 4) Recommended Target Architecture for V2

## 4.1 Orchestrator as a Plan + Execute system

Replace "single delegation outcome" with an execution model:

- `plan_id` with an explicit task graph (nodes + dependencies).
- Node types:
  - `agent_task` (invoke agent)
  - `mcp_tool_call` (direct MCP action)
  - `human_checkpoint` (HITL pause/approval)
  - `transform` (normalization/enrichment)
- Scheduler supports:
  - parallel fan-out for independent nodes
  - dependency-aware sequencing
  - fan-in aggregation nodes
- Plan-level retry/recovery controls:
  - node retry policy
  - timeout policy
  - compensation/cancel policy

## 4.2 Dispatch Adapter Layer

Create one routing abstraction:

- input: `agent_key` or `tool_key`
- resolves route using registry/config
- handles transport type (`celery`, `http`, `mcp`)
- emits canonical dispatch result states:
  - `accepted`
  - `rejected`
  - `transport_failed`
  - `timed_out`

## 4.3 MCP Integration Layer (new in implementation, not just docs)

Add concrete MCP platform services:

- MCP server registry table(s): endpoint, auth mode, scopes, domain access, rate limits.
- MCP connection/session manager:
  - secure token fetch/decrypt
  - capability negotiation/cache
  - timeout budgets and circuit breakers
- MCP policy guard:
  - which domain can call which MCP server/tool
  - per-user permission checks
  - audit logs for each invocation

## 4.4 Task State Machine (must be strict)

Normalize lifecycle:

- `created -> queued -> dispatched -> running -> completed | failed | cancelled | timed_out | stale`
- Include:
  - `attempt_count`
  - `last_heartbeat_at`
  - `error_code`
  - `error_detail`
  - `correlation_id`

---

## 5) EAV Strategy Decision (Detailed)

You asked the key question:  
Should you keep pure EAV and let system auto-configure per customer, or create fixed tables?

## 5.1 Current model in Krastix

Current implementation is a **hybrid JSONB/EAV-like model**:

- `entity_definitions` stores per-entity JSON schema.
- `entities` stores data in JSONB with shared typed columns.
- some domain-specific behaviors depend on JSON keys.

This is better than pure classical EAV for developer speed, but it still needs guardrails.

## 5.2 Decision framework by requirement type

### Use flexible JSONB/EAV-style when:
- fields are highly variable per customer
- low query complexity
- low to medium write volume
- no strict reporting/analytics requirement on every attribute

### Use typed tables when:
- domain is core and high volume (example: leads, submissions, drafts at scale)
- reporting and filtering on many fields is business-critical
- strong constraints and referential integrity matter
- compliance/audit requires strict schema semantics

## 5.3 Recommended approach for Krastix V2 (best balance)

Adopt **"Typed Core + Dynamic Extensions"**:

- Keep `entities` as universal envelope (id, user_id, type, status, metadata, version).
- Move high-value/hot-path attributes to typed domain tables.
- Keep customer-specific or infrequently queried attributes in JSONB extension columns.
- Optionally add a sidecar custom-fields model later for UI-configurable fields.

This gives:
- customer flexibility without constant DB rewrites
- strong performance where it matters
- controlled schema evolution for enterprise customers

## 5.4 Should system auto-create tables on customer demand?

**Not recommended** as fully automatic at runtime for production tenants.

Why:
- hard to guarantee security and migration safety
- can break rollback/versioning discipline
- increases ops risk and drift between environments

Better pattern:

- Customer submits new field requirements through a config workflow.
- System validates request against allowed schema contract.
- For dynamic fields: auto-update metadata/schema definitions.
- For fields crossing performance/compliance thresholds: generate migration proposal and approval workflow (semi-automated).

## 5.5 Practical V2 data governance rules

1. No worker should silently create core entity schemas in production.
2. Schema changes go through a schema registry workflow with versioning.
3. Add strict JSON schema rules (`additionalProperties`, formats, enums) where possible.
4. Add expression indexes/generated columns for hot JSON keys.
5. Promote entity types to typed tables when threshold rules are met (query latency, cardinality, reporting frequency).

---

## 6) Agent Strategy for V2 (What to Build Next)

Recommended new agents/platform agents:

## 6.1 Agent Orchestrator-Internal Agents

### 1) `PlanCompilerAgent`
- Converts user intent into executable DAG with dependencies, SLA hints, and HITL points.

### 2) `ExecutionSupervisorAgent`
- Monitors plan/node progress, retries, and fallback routing; watches heartbeat/timeout.

### 3) `ResultSynthesizerAgent`
- Aggregates outputs from multiple agents/tools into final response + structured artifacts.

## 6.2 Platform Reliability Agents

### 4) `SchemaGovernanceAgent`
- Validates schema change requests, versions definitions, flags risky field additions.

### 5) `DataQualityAgent`
- Detects malformed entities, drifted field semantics, duplicate records.

### 6) `ObservabilityAgent`
- Summarizes failures/staleness hotspots, recommends retry/index/config fixes.

## 6.3 Domain/Productivity Agents

### 7) `IntegrationOpsAgent`
- Manages token health, reauth notifications, MCP server availability checks.

### 8) `KnowledgeCurationAgent`
- Curates memory ingestion quality, dedupes chunks, marks confidence and freshness.

### 9) `CustomerOnboardingAgent`
- Converts customer workflow requirements into domain config + schema proposals + enabled-agent templates.

---

## 7) V2 Phased Implementation Plan

## Phase 0 (Stabilization, 1-2 weeks)

- Align `init.sql` with latest registry and task schema expectations.
- Standardize status vocabulary and transitions.
- Harden dispatch error handling so HTTP failures mark task states immediately.
- Add callback auth (HMAC or signed token) and idempotency key.

Deliverables:
- stable bootstrap for fresh environments
- deterministic task status handling
- secured callback channel

## Phase 1 (Orchestrator Core Upgrade, 2-4 weeks)

- Implement plan graph persistence tables (`plans`, `plan_nodes`, `plan_edges`, `plan_events`).
- Add scheduler for dependency-aware execution.
- Introduce dispatch adapter abstraction for `celery/http`.
- Add richer run metadata and trace correlation IDs.

Deliverables:
- true multi-step orchestration
- parallel and sequential execution support
- execution traceability

## Phase 2 (MCP Runtime Integration, 2-4 weeks)

- Implement MCP registry + connection manager.
- Add domain/user policy layer for MCP tool access.
- Create MCP invocation adapter in orchestrator dispatch.
- Add health checks and circuit breaker for MCP endpoints.

Deliverables:
- production-grade MCP support
- controlled and auditable external tool usage

## Phase 3 (Data Model Evolution, 3-6 weeks)

- Introduce typed core tables for first hot entity types:
  - `email_draft`
  - `applicant_submission`
  - possibly high-volume `lead`
- Keep `entities` + extension JSONB for flexibility.
- Add generated columns/expression indexes for active JSON paths.
- Build schema governance API/workflow.

Deliverables:
- improved query performance and maintainability
- preserved flexibility for customer-specific fields

## Phase 4 (Enterprise Readiness, continuous)

- SLO dashboards: task latency, stale rate, callback failure rate, retry distribution.
- Test strategy:
  - contract tests for agent payloads
  - orchestration integration tests (plan -> execute -> callback)
  - migration tests for schema evolution
- Security hardening:
  - endpoint auth consistency review
  - least-privilege service credentials
  - audit log retention policy

---

## 8) Database and EAV Implementation Plan (Concrete)

## 8.1 Keep in V2

- `entity_definitions` as schema registry (versioned).
- `entities` as universal entity envelope with JSONB extension.
- optimistic concurrency (`version`) concept.

## 8.2 Add in V2

- `entity_schema_versions` table:
  - `entity_type`, `schema_version`, `schema_json`, `status`, `created_by`, `approved_at`
- `entity_type_promotion_rules`:
  - thresholds for promoting to typed table
- generated columns for common selectors:
  - `session_id`, `source_form_id`, `form_id`, etc. (per practical usage)
- targeted indexes (partial + expression) for frequent filters

## 8.3 Promotion rule example

Promote JSON-heavy entity type to typed table when any:
- p95 query > target for 7 days
- entity volume > threshold
- analytics/report fields > threshold
- compliance flag set

---

## 9) Risk Register and Mitigations

1. **Risk:** runtime/schema drift between environments  
   **Mitigation:** migration-first governance + startup schema checks

2. **Risk:** silent task loss on callback/dispatch edge cases  
   **Mitigation:** strict state machine + idempotent callbacks + retry with DLQ

3. **Risk:** EAV flexibility causing reporting/performance pain  
   **Mitigation:** typed-core promotion strategy + generated columns/indexes

4. **Risk:** MCP integrations become insecure/opaque  
   **Mitigation:** policy guard + per-invocation audit + scoped credentials

5. **Risk:** multi-agent complexity hurts debuggability  
   **Mitigation:** trace IDs, plan event log, deterministic node-level statuses

---

## 10) Suggested V2 KPIs

- Task completion success rate >= 99%
- Stale task rate <= 0.5%
- Callback delivery success >= 99.5%
- p95 orchestration response-to-dispatch <= defined SLA
- p95 hot-entity query latency within SLA after promotion/indexing
- Onboarding new customer domain with no code changes for standard cases

---

## 11) Recommended Immediate Next Actions (Next 7 Days)

1. Freeze and unify schema contracts (`agent_registry`, task statuses).
2. Draft `plans`/`plan_nodes`/`plan_events` SQL design.
3. Decide first two entity types to promote from JSON-heavy to typed core.
4. Design MCP registry and auth policy model.
5. Build one end-to-end integration test: chat -> multi-step plan -> callback -> final synthesis.

---

## 12) Clarifications Needed from You (to finalize implementation backlog)

1. Which domains are first-priority for V2 launch (Recruitment, Sales, other)?
2. Do you want strict migration approval workflow for customer schema changes, or controlled self-service with guardrails?
3. What compliance level is required initially (basic audit vs SOC2-style controls)?
4. Which integrations must be MCP-first in V2 (Google, Slack, Notion, ATS, etc.)?
5. What is acceptable latency target for "complex multi-agent workflow" end-to-end?

---

## Final Recommendation

For Krastix V2, do **not** go all-in on pure EAV or fully auto-create tables at runtime.  
Use **Typed Core + Dynamic Extensions**, with orchestrator DAG execution and a real MCP runtime layer.

This path gives you:
- customer flexibility without daily schema rewrites
- better performance and reliability at scale
- cleaner enterprise handover and long-term maintainability


# Phase 0 Progress (Stabilization)

Date: 2026-04-15

## Completed

- Aligned callback channel to be secure and idempotent.
  - Added HMAC callback signing support (`X-Callback-Timestamp`, `X-Callback-Signature`).
  - Added callback idempotency key support (`X-Callback-Idempotency-Key`).
  - Added callback payload hashing + dedupe persistence path.

- Standardized task lifecycle model in runtime code.
  - Canonical statuses introduced in runtime flow: `created -> queued -> dispatched -> running -> (completed|failed|cancelled|timed_out|stale)`.
  - Added task status normalization aliases for legacy values (`pending`, `processing`, `success`).
  - Added transition guardrails in DB access layer.

- Hardened dispatch error handling.
  - Orchestrator now marks dispatch transport failures immediately as terminal task failures with structured error code (`transport_failed`).
  - "dispatched" is only emitted when transport accepts the task.

- Updated worker execution lifecycle.
  - CRM/Form/Communication workers now mark tasks `running` when work begins.
  - Callback completion status now uses canonical values (`completed`/`failed`) instead of mixed legacy values.

- Updated HTTP agents callback behavior.
  - Research agent now sends signed + idempotent callbacks.
  - Doc agent now uses shared callback utility with signed + idempotent callbacks.
  - Research agent now also uses shared callback utility (`shared.callbacks.notify_task_completed`) to avoid callback logic drift.

- Aligned bootstrap schema (`init.sql`) with runtime expectations.
  - `agent_registry` now includes modern fields: `agent_key`, `display_name`, `queue_or_url`, `dispatch_method`, `enabled`, `version`.
  - `agent_tasks` now includes lifecycle/ops fields: `attempt_count`, `last_heartbeat_at`, `error_code`, `error_detail`, `correlation_id`, timestamps (`queued_at`, `dispatched_at`, `started_at`, `updated_at`, `callback_received_at`), and `callback_idempotency_key`.
  - Added strict `agent_tasks` status check constraint for canonical statuses.
  - Added `agent_task_callbacks` table for callback idempotency storage.
  - Added RLS + policies for `agent_task_callbacks`.

- Added incremental migration for existing environments.
  - New file: `migrations/005_phase0_stabilization.sql`.
  - Includes registry alignment (schema/backfill only, no canonical seed upserts), task status normalization, lifecycle columns, strict status check, and callback idempotency table/policies.

- Updated config/documentation for rollout.
  - Added `CALLBACK_SIGNING_SECRET` and `CALLBACK_MAX_SKEW_SECONDS` to `.env.example`.
  - Propagated callback secret env into orchestrator and all agent services in `docker-compose.yml`.
  - Updated `README.md` migration sequence to include migration 005.

- Frontend polling compatibility update.
  - Task polling now treats `timed_out` and `cancelled` as terminal failure states (in addition to `failed`/`stale`).

## Notes

- Callback idempotency persistence is now race-safe for concurrent duplicate callbacks:
  - `register_callback_idempotency()` now uses `INSERT ... ON CONFLICT DO NOTHING` + post-check classification, instead of a racy pre-check/insert flow.
- Local Python runtime is available; syntax checks were executed successfully with:
  - `python -m compileall orchestrator/src/main.py orchestrator/src/graph.py shared/database.py shared/callbacks.py agents/research_agent/src/main.py agents/doc_agent/src/main.py agents/crm_agent/src/worker.py agents/form_agent/src/worker.py agents/communication_agent/src/worker.py`
- `pytest` is still unavailable in this shell (`pytest` command not found), so automated runtime/unit tests were not executed in this environment.
- `Version_2.0_Planning.md` remains unmodified (it is still untracked in git).

## Next Runtime Steps

1. Apply DB migrations (or `init.sql` for fresh DB).
2. Set `CALLBACK_SIGNING_SECRET` consistently across orchestrator + agents.
3. Restart services.
4. Validate end-to-end:
   - delegate task -> status progression (`created/queued/dispatched/running/completed`),
   - callback signature verification,
   - duplicate callback replay returns `duplicate` and does not double-process.

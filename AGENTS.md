# Repository Guidelines

## Project Structure & Module Organization
Core code lives in `app/`. The entrypoint is `app/db_app.py` (Flask webhooks and orchestration). Agent logic is under `app/agent/` (`core.py`, `micro_agents.py`, intent/objection/qualification/scheduling modules, calendar integration, and `storage/supabase_repo.py`). Supabase is the runtime source of truth for conversations, lead profiles, messages, follow-up jobs, and meetings. Local JSON under `chats/{chatLid}/` and `reunioes.json` may still exist as legacy log/mirror/fallback data, but it is not the source of truth and must not be deleted without explicit authorization. Root-level docs include `README.md`, `PRD.md`, and integration notes.

## Build, Test, and Development Commands
- `python -m venv .venv` then `.venv\Scripts\activate` (Windows) or `source .venv/bin/activate` (Unix): create and activate virtualenv.
- `pip install -r requirements.txt`: install dependencies.
- `cd app && python db_app.py`: run local server on port `5001`.
- `ngrok http 5001`: expose webhooks for Z-API integration.
- `python test_audio.py`: run the existing audio integration smoke test.

## Coding Style & Naming Conventions
Use Python 3 with PEP 8 defaults: 4-space indentation, snake_case for functions/variables, PascalCase for classes, and UPPER_CASE for constants/env names. Keep modules focused (one responsibility per file, as in `intent_classifier.py` and `scheduling_validator.py`). Prefer small pure helper functions for parsing/normalization and keep side effects in boundary modules (`db_app.py`, `calendar.py`).

## Testing Guidelines
This repository currently uses script-based tests (for example, `test_audio.py`) rather than a full test framework. Add new tests as `test_*.py` files at repo root or in a dedicated `tests/` folder for larger additions. For webhook or agent changes, include at least one reproducible local check (input payload + expected behavior).

## Commit & Pull Request Guidelines
Recent history favors short, imperative, Portuguese commit messages (for example, `Ajustes chamada de lead frio`, `Pequeno ajuste comportamental`). Keep commits focused and scoped to one concern. For PRs, include:
- Clear summary of behavior changes.
- Affected paths (for example, `app/agent/core.py`).
- Validation evidence (command output, sample webhook payload/response).
- Linked issue/task when available.

## Security & Configuration Tips
Never commit secrets. Keep API keys in `app/.env`; treat `client_secret.json`, `token.json`, and Supabase service-role credentials as sensitive. Runtime Supabase access requires `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY`; never paste real values into docs, commits, logs, or chat. Rotate leaked keys immediately, especially service-role credentials, and update local env before rerunning integrations.

## Supabase
This repo is backed by the Supabase project **SDR** (ref `yneegfwkiismcxhkbwaz`, region `us-west-1`, org `jcjzazzdymftxhfvmwrf`, URL `https://yneegfwkiismcxhkbwaz.supabase.co`). Schema lives in `supabase/schema.sql`. All runtime Supabase access must go through `app/agent/storage/supabase_repo.py`; do not add direct PostgREST calls elsewhere.

Supabase is the source of truth for:
- `conversations`
- `lead_profiles`
- `messages`
- `followup_jobs`
- `meetings`

Runtime behavior:
- `/webhook/receive` writes inbound/outbound message events to `messages` and updates `conversations`.
- Agent history reads prefer `messages` from Supabase, with local JSON only as fallback for unavailable/missing data.
- Lead profile reads/writes prefer `lead_profiles` plus `conversations`.
- Follow-ups are persisted in `followup_jobs`; startup restores timers from scheduled jobs in Supabase, not from `lead_info.json`.
- Meetings are persisted/read from `meetings`; local `reunioes.json` remains a mirror/fallback.

Backfill was handled with `app/scripts/tmp_backfill_supabase.py` as a temporary migration wrapper around `app/scripts/sync_local_json_to_supabase.py`. It is idempotent through deterministic `on_conflict` upserts, but it is not a runtime routine or scheduler. Do not turn the backfill into a permanent sync loop without a separate design review.

## Reliability Notes
When editing chat persistence or orchestration (`app/db_app.py`, `app/agent/core.py`), preserve these invariants:
- Never overwrite full `history.json` with a filtered subset.
- Never treat local JSON as the authoritative store after the Supabase migration.
- Do not delete local JSON mirrors/logs without explicit authorization.
- Keep webhook handlers tolerant to invalid/missing JSON payloads.
- Use the agent file lock helpers for shared JSON files touched by multiple threads/timers.
- Bound loops that depend on external availability (for example, free-slot probing).
- Keep calendar deletion/update using the same organizer calendar chosen for creation.

## Docstring Rule (Mandatory)
Every time you create or edit a function, you must create/update that function's docstring in the same change.

Required docstring content:
- Purpose: what the function does.
- Inputs: relevant parameters and expected formats.
- Behavior/side effects: file writes, network/API calls, timers/threads, external integrations.
- Return: what it returns (or explicit `None`).
- Exceptions/fallbacks: important failure behavior when applicable.

Do not leave stale docstrings after behavior changes.

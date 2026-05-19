# Supabase runtime notes

Supabase project for this repo: **SDR** (ref `yneegfwkiismcxhkbwaz`, region `us-west-1`, org `jcjzazzdymftxhfvmwrf`). Base URL: `https://yneegfwkiismcxhkbwaz.supabase.co`.

Do not commit or paste service-role keys. Runtime credentials must be configured locally, usually in `app/.env`:

```env
SUPABASE_URL=<project-url>
SUPABASE_SERVICE_ROLE_KEY=<service-role-key>
```

## Source of truth

Supabase is now the source of truth for runtime state:

- `conversations`
- `lead_profiles`
- `messages`
- `followup_jobs`
- `meetings`

Local JSON files still exist as legacy data, log/mirror output, or fallback for some reads when Supabase has no data. They are not authoritative after the migration and must not be deleted without explicit authorization.

All runtime Supabase access should go through `app/agent/storage/supabase_repo.py`. Do not add new direct PostgREST calls from `core.py`, `db_app.py`, or other runtime modules.

## Runtime behavior

- `POST /webhook/receive` appends message events to Supabase `messages` and upserts `conversations`.
- `/chamar-lead` and outbound startup paths upsert `conversations` and `lead_profiles`.
- Agent history reads prefer Supabase `messages`; local `history.json` is only fallback when Supabase returns no usable data.
- Lead profile reads prefer Supabase `lead_profiles` plus `conversations`; local `lead_info.json` is fallback only.
- Follow-ups are stored in `followup_jobs`. Scheduling creates `scheduled` jobs; sending marks jobs `sent`; lead activity marks pending jobs `cancelled`.
- Agent startup restores follow-up timers from Supabase `followup_jobs.status='scheduled'`, not from `lead_info.json`.
- Startup sends overdue follow-ups up to 24 hours late after a 5-second delay. Jobs older than that, or jobs missing required startup data, are marked `failed`.
- Meetings are read/written through Supabase `meetings`. Local `reunioes.json` remains a mirror/fallback and should not be treated as canonical.

## Schema

Apply the schema with `supabase/schema.sql` in the Supabase SQL Editor or through the Supabase MCP migration tool.

The schema enables RLS and includes read policies for `authenticated`. The app uses a service-role key server-side, which bypasses RLS. Do not expose this key to clients.

## Backfill

The one-time migration from local JSON to Supabase used:

```bash
python app/scripts/tmp_backfill_supabase.py --dry-run
python app/scripts/tmp_backfill_supabase.py --apply
```

`tmp_backfill_supabase.py` is a temporary wrapper around `app/scripts/sync_local_json_to_supabase.py`. It loads `app/.env`, supports dry-run/apply modes, prints row counts, and retries bad rows individually. It is not part of normal runtime and must not be turned into a scheduled sync loop without a new design.

Backfill writes are idempotent through deterministic upsert keys:

- `conversations`: `chat_lid`
- `lead_profiles`: `conversation_id`
- `messages`: `conversation_id,message_id`
- `followup_jobs`: `conversation_id,tipo,target_at`
- `meetings`: `conversation_id,scheduled_for`

To validate idempotency, run `--apply` once, record row counts, run `--apply` again, and confirm zero deltas for all five tables.

## QA sanity queries

Counts by table:

```sql
select 'conversations' as table_name, count(*) from conversations
union all select 'lead_profiles', count(*) from lead_profiles
union all select 'messages', count(*) from messages
union all select 'followup_jobs', count(*) from followup_jobs
union all select 'meetings', count(*) from meetings;
```

Messages per conversation:

```sql
select
  c.chat_lid,
  count(m.id) as message_count,
  min(m.event_ts) as first_message_at,
  max(m.event_ts) as last_message_at
from conversations c
left join messages m on m.conversation_id = c.id
group by c.chat_lid
order by last_message_at desc nulls last;
```

Latest messages written:

```sql
select
  c.chat_lid,
  m.event_ts,
  m.direction,
  m.message_type,
  left(coalesce(m.content_text, ''), 160) as content_preview,
  m.message_id
from messages m
join conversations c on c.id = m.conversation_id
order by m.event_ts desc
limit 50;
```

Follow-up queue health:

```sql
select status, tipo, count(*) as total
from followup_jobs
group by status, tipo
order by status, tipo;
```

Pending follow-ups:

```sql
select
  f.id,
  c.chat_lid,
  f.tipo,
  f.target_at,
  f.phone,
  f.created_at
from followup_jobs f
join conversations c on c.id = f.conversation_id
where f.status = 'scheduled'
order by f.target_at asc;
```

Meetings by status:

```sql
select
  status,
  count(*) as total,
  min(scheduled_for) as first_scheduled_for,
  max(scheduled_for) as last_scheduled_for
from meetings
group by status
order by status;
```

Active/cancelled meetings:

```sql
select
  c.chat_lid,
  m.status,
  m.scheduled_for,
  m.meet_link,
  m.event_id
from meetings m
join conversations c on c.id = m.conversation_id
where m.status in ('scheduled', 'cancelled')
order by m.scheduled_for desc
limit 50;
```

Inbound/outbound balance:

```sql
select direction, count(*) as total
from messages
group by direction
order by direction;
```

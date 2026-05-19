-- Supabase schema for Agente de IA
-- Apply in Supabase SQL Editor

create extension if not exists pgcrypto;

create table if not exists conversations (
  id uuid primary key default gen_random_uuid(),
  chat_lid text not null unique,
  phone text,
  status text not null default 'active' check (status in ('active','blocked','closed')),
  ai_blocked boolean not null default false,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists lead_profiles (
  id uuid primary key default gen_random_uuid(),
  conversation_id uuid not null references conversations(id) on delete cascade,
  nome text,
  email text,
  empresa text,
  segmento_mercado text,
  porte_empresa text,
  faturamento_aproximado text,
  tamanho_time text,
  sistemas_atuais jsonb not null default '[]'::jsonb,
  desafios_identificados jsonb not null default '[]'::jsonb,
  produto_indicado text,
  motivo_segmentacao text,
  estagio_conversa text,
  reuniao_agendada boolean,
  necessita_followup boolean,
  motivo_followup text,
  tentativas_retomada integer not null default 0,
  descricao_manual text,
  atualizado_em timestamptz,
  raw jsonb not null default '{}'::jsonb,
  unique(conversation_id)
);

create table if not exists messages (
  id uuid primary key default gen_random_uuid(),
  conversation_id uuid not null references conversations(id) on delete cascade,
  event_ts timestamptz not null,
  direction text not null check (direction in ('inbound','outbound','system')),
  sender_phone text,
  message_type text,
  content_text text,
  message_id text,
  reference_message_id text,
  payload jsonb not null,
  created_at timestamptz not null default now(),
  unique(conversation_id, message_id)
);

create table if not exists followup_jobs (
  id uuid primary key default gen_random_uuid(),
  conversation_id uuid not null references conversations(id) on delete cascade,
  tipo text not null check (tipo in ('1h','24h','15d')),
  target_at timestamptz not null,
  status text not null default 'scheduled' check (status in ('scheduled','sent','cancelled','failed')),
  phone text,
  sent_at timestamptz,
  payload jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  unique(conversation_id, tipo, target_at)
);

create table if not exists meetings (
  id uuid primary key default gen_random_uuid(),
  conversation_id uuid not null references conversations(id) on delete cascade,
  phone text,
  scheduled_for timestamptz not null,
  agendado_em timestamptz,
  meet_link text,
  event_id text,
  status text not null default 'scheduled' check (status in ('scheduled','cancelled','completed')),
  raw jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique(conversation_id, scheduled_for)
);

create index if not exists idx_messages_conversation_ts on messages (conversation_id, event_ts);
create index if not exists idx_messages_type on messages (message_type);
create index if not exists idx_conversations_phone on conversations (phone);
create index if not exists idx_followup_status_target on followup_jobs (status, target_at);
create index if not exists idx_meetings_status_sched on meetings (status, scheduled_for);

create or replace function touch_updated_at()
returns trigger language plpgsql as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

drop trigger if exists trg_conversations_touch on conversations;
create trigger trg_conversations_touch
before update on conversations
for each row execute function touch_updated_at();

drop trigger if exists trg_meetings_touch on meetings;
create trigger trg_meetings_touch
before update on meetings
for each row execute function touch_updated_at();

alter table conversations enable row level security;
alter table lead_profiles enable row level security;
alter table messages enable row level security;
alter table followup_jobs enable row level security;
alter table meetings enable row level security;

-- Service role bypasses RLS. Keep explicit policies for authenticated read if needed.
do $$
begin
  if not exists (select 1 from pg_policies where policyname = 'authenticated_read_conversations') then
    create policy authenticated_read_conversations on conversations for select to authenticated using (true);
  end if;
  if not exists (select 1 from pg_policies where policyname = 'authenticated_read_lead_profiles') then
    create policy authenticated_read_lead_profiles on lead_profiles for select to authenticated using (true);
  end if;
  if not exists (select 1 from pg_policies where policyname = 'authenticated_read_messages') then
    create policy authenticated_read_messages on messages for select to authenticated using (true);
  end if;
  if not exists (select 1 from pg_policies where policyname = 'authenticated_read_followup_jobs') then
    create policy authenticated_read_followup_jobs on followup_jobs for select to authenticated using (true);
  end if;
  if not exists (select 1 from pg_policies where policyname = 'authenticated_read_meetings') then
    create policy authenticated_read_meetings on meetings for select to authenticated using (true);
  end if;
end $$;

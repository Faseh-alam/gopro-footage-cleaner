-- Run this in the Supabase SQL editor (Dashboard → SQL → New query).
-- Also applied via migrations on the connected project.

create table if not exists public.cards (
  id uuid primary key default gen_random_uuid(),
  work_date date not null,
  card_name text not null,
  card_path text not null default '',
  insert_time text not null default '',
  finish_time text not null default '',
  total_mp4_videos integer not null default 0,
  original_duration double precision not null default 0,
  final_duration double precision,
  duration_difference double precision,
  card_capacity double precision,
  used_space double precision not null default 0,
  status text not null default 'Pending',
  used_space_before_labeling_gb double precision not null default 0,
  used_space_after_labeling_gb double precision,
  original_duration_before_labeling double precision not null default 0,
  original_duration_after_labeling double precision,
  employee_id uuid,
  camera_serial text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (work_date, card_name)
);

create index if not exists cards_work_date_idx on public.cards (work_date);

create table if not exists public.daily_summaries (
  id uuid primary key default gen_random_uuid(),
  work_date date not null unique,
  total_cards_received integer not null default 0,
  total_used_space_before_gb double precision not null default 0,
  total_used_space_after_gb double precision not null default 0,
  total_storage_before_tb double precision not null default 0,
  total_storage_after_tb double precision not null default 0,
  total_original_duration_before double precision not null default 0,
  total_original_duration_after double precision not null default 0,
  total_hours_before text not null default '0h 0m',
  total_hours_after text not null default '0h 0m',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

-- Employees (1:1 with auth.users)
create table if not exists public.employees (
  id uuid primary key references auth.users (id) on delete cascade,
  email text not null unique,
  full_name text not null default '',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.employee_work_sessions (
  id uuid primary key default gen_random_uuid(),
  employee_id uuid not null references public.employees (id) on delete cascade,
  work_date date not null,
  start_time timestamptz not null,
  end_time timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists employee_work_sessions_employee_date_idx
  on public.employee_work_sessions (employee_id, work_date);

create table if not exists public.employee_daily_metrics (
  id uuid primary key default gen_random_uuid(),
  employee_id uuid not null references public.employees (id) on delete cascade,
  work_date date not null,
  start_time timestamptz,
  end_time timestamptz,
  sd_cards_connected integer not null default 0,
  footage_seconds_processed double precision not null default 0,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (employee_id, work_date)
);

create table if not exists public.employee_sd_card_events (
  id uuid primary key default gen_random_uuid(),
  employee_id uuid not null references public.employees (id) on delete cascade,
  work_date date not null,
  card_id text not null,
  camera_serial text,
  card_path text not null default '',
  footage_seconds double precision not null default 0,
  video_count integer not null default 0,
  connected_at timestamptz not null default now(),
  unique (employee_id, work_date, card_id)
);

alter table public.cards
  add column if not exists employee_id uuid references public.employees (id) on delete set null,
  add column if not exists camera_serial text;

alter table public.cards enable row level security;
alter table public.daily_summaries enable row level security;
alter table public.employees enable row level security;
alter table public.employee_work_sessions enable row level security;
alter table public.employee_daily_metrics enable row level security;
alter table public.employee_sd_card_events enable row level security;

-- Card / summary policies (Flask backend; prefer service_role key in .env)
drop policy if exists "anon_select_cards" on public.cards;
drop policy if exists "anon_insert_cards" on public.cards;
drop policy if exists "anon_update_cards" on public.cards;
drop policy if exists "anon_delete_cards" on public.cards;
drop policy if exists "anon_select_daily_summaries" on public.daily_summaries;
drop policy if exists "anon_insert_daily_summaries" on public.daily_summaries;
drop policy if exists "anon_update_daily_summaries" on public.daily_summaries;
drop policy if exists "anon_delete_daily_summaries" on public.daily_summaries;

create policy "anon_select_cards" on public.cards for select to anon, authenticated using (true);
create policy "anon_insert_cards" on public.cards for insert to anon, authenticated with check (true);
create policy "anon_update_cards" on public.cards for update to anon, authenticated using (true) with check (true);
create policy "anon_delete_cards" on public.cards for delete to anon, authenticated using (true);

create policy "anon_select_daily_summaries" on public.daily_summaries for select to anon, authenticated using (true);
create policy "anon_insert_daily_summaries" on public.daily_summaries for insert to anon, authenticated with check (true);
create policy "anon_update_daily_summaries" on public.daily_summaries for update to anon, authenticated using (true) with check (true);
create policy "anon_delete_daily_summaries" on public.daily_summaries for delete to anon, authenticated using (true);

drop policy if exists "employees_select_own" on public.employees;
drop policy if exists "employees_update_own" on public.employees;
create policy "employees_select_own" on public.employees
  for select to authenticated using ((select auth.uid()) = id);
create policy "employees_update_own" on public.employees
  for update to authenticated
  using ((select auth.uid()) = id)
  with check ((select auth.uid()) = id);

drop policy if exists "sessions_select_own" on public.employee_work_sessions;
drop policy if exists "sessions_insert_own" on public.employee_work_sessions;
drop policy if exists "sessions_update_own" on public.employee_work_sessions;
create policy "sessions_select_own" on public.employee_work_sessions
  for select to authenticated using ((select auth.uid()) = employee_id);
create policy "sessions_insert_own" on public.employee_work_sessions
  for insert to authenticated with check ((select auth.uid()) = employee_id);
create policy "sessions_update_own" on public.employee_work_sessions
  for update to authenticated
  using ((select auth.uid()) = employee_id)
  with check ((select auth.uid()) = employee_id);

drop policy if exists "metrics_select_own" on public.employee_daily_metrics;
drop policy if exists "metrics_insert_own" on public.employee_daily_metrics;
drop policy if exists "metrics_update_own" on public.employee_daily_metrics;
create policy "metrics_select_own" on public.employee_daily_metrics
  for select to authenticated using ((select auth.uid()) = employee_id);
create policy "metrics_insert_own" on public.employee_daily_metrics
  for insert to authenticated with check ((select auth.uid()) = employee_id);
create policy "metrics_update_own" on public.employee_daily_metrics
  for update to authenticated
  using ((select auth.uid()) = employee_id)
  with check ((select auth.uid()) = employee_id);

drop policy if exists "sd_events_select_own" on public.employee_sd_card_events;
drop policy if exists "sd_events_insert_own" on public.employee_sd_card_events;
drop policy if exists "sd_events_update_own" on public.employee_sd_card_events;
create policy "sd_events_select_own" on public.employee_sd_card_events
  for select to authenticated using ((select auth.uid()) = employee_id);
create policy "sd_events_insert_own" on public.employee_sd_card_events
  for insert to authenticated with check ((select auth.uid()) = employee_id);
create policy "sd_events_update_own" on public.employee_sd_card_events
  for update to authenticated
  using ((select auth.uid()) = employee_id)
  with check ((select auth.uid()) = employee_id);

create or replace function public.handle_new_employee()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  insert into public.employees (id, email, full_name)
  values (
    new.id,
    coalesce(new.email, ''),
    coalesce(new.raw_user_meta_data->>'full_name', '')
  )
  on conflict (id) do update
    set email = excluded.email,
        full_name = case
          when excluded.full_name <> '' then excluded.full_name
          else public.employees.full_name
        end,
        updated_at = now();
  return new;
end;
$$;

drop trigger if exists on_auth_user_created_employee on auth.users;
create trigger on_auth_user_created_employee
  after insert on auth.users
  for each row execute function public.handle_new_employee();

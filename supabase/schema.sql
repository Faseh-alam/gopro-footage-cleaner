-- Run this in the Supabase SQL editor (Dashboard → SQL → New query).

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

alter table public.cards enable row level security;
alter table public.daily_summaries enable row level security;

-- Needed when the local Flask backend uses the publishable/anon key (not service_role).
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

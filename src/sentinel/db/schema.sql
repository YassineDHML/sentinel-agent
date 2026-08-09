-- Sentinel database schema (PostgreSQL / Supabase).
-- Source: cahier des charges, BF-04. Run this once in the Supabase SQL editor.
-- Safe to re-run: uses IF NOT EXISTS / CREATE OR REPLACE.

-- =============================================================================
-- articles — every collected article (permanent memory).
-- Exact deduplication is enforced by the UNIQUE(url) constraint.
-- =============================================================================
create table if not exists articles (
    id            bigint generated always as identity primary key,
    url           text unique not null,   -- exact-dedup key (BF-02)
    title         text,
    source        text,
    actor         text,                   -- e.g. "OpenAI", "Mistral"
    topics        text[],                 -- canonical tags assigned by the LLM (BF-03)
    published_at  timestamptz,
    collected_at  timestamptz default now(),
    snippet       text,                   -- short description from the source (RSS/GNews)
    content       text,                   -- full article text (fetched lazily, then cached)
    summary       text,                   -- LLM-generated summary
    processed     boolean default false
);

-- Migration for databases created before snippet/content existed (idempotent).
alter table articles add column if not exists snippet text;
alter table articles add column if not exists content text;

create index if not exists idx_articles_processed    on articles (processed);
create index if not exists idx_articles_published_at  on articles (published_at desc);

-- =============================================================================
-- trends — topic frequency, period by period, PER REQUEST.
--
-- NOTE (beyond the spec's minimal DDL): a UNIQUE constraint is added so the
-- pipeline can UPSERT one row per topic per period idempotently (re-runs of the
-- same period update counts instead of duplicating rows).
--
-- The original key was UNIQUE(topic, week) — GLOBAL. That was correct while
-- Sentinel watched exactly one theme, and wrong as soon as it watched several:
-- two requests both counting "regulation" in the same week would silently
-- overwrite each other, and a read would return a blend of both taxonomies. The
-- key is therefore namespaced by `request_slug`; '__default__' is the historical
-- single-theme watch, so every pre-existing row keeps working untouched.
--
-- `week` keeps its name but now holds a PERIOD KEY of any cadence ("2026-W32",
-- "2026-M08", "2026-Q3"). Renaming a column on a live production table is exactly
-- the kind of change the no-regression rule forbids; `period_kind` is stored
-- alongside for queryability and is derived from the key's own format.
-- =============================================================================
create table if not exists trends (
    id            bigint generated always as identity primary key,
    topic         text not null,          -- from the canonical list (BF-03)
    week          text not null,          -- period key, e.g. "2026-W24"
    article_count integer,                -- articles on this topic this period
    actors        jsonb,                  -- actors involved
    unique (topic, week)                  -- superseded below; kept for fresh installs
);

alter table trends add column if not exists request_slug text not null default '__default__';
alter table trends add column if not exists period_kind  text not null default 'week';

-- Swap the global uniqueness for a per-scope one. Written as a DO block because
-- Postgres has no ADD CONSTRAINT IF NOT EXISTS, and the old constraint is matched
-- by its COLUMNS rather than its name so a differently-named one is still found.
-- Idempotent: safe to re-run, and a no-op once migrated.
do $$
declare
    con_name text;
begin
    for con_name in
        select con.conname
          from pg_constraint con
          join pg_class rel on rel.oid = con.conrelid
          join pg_namespace nsp on nsp.oid = rel.relnamespace
         where nsp.nspname = 'public'
           and rel.relname = 'trends'
           and con.contype = 'u'
           and (select array_agg(att.attname::text order by att.attname)
                  from unnest(con.conkey) as k
                  join pg_attribute att
                    on att.attrelid = con.conrelid and att.attnum = k)
               = array['topic', 'week']
    loop
        execute format('alter table trends drop constraint %I', con_name);
    end loop;

    if not exists (
        select 1
          from pg_constraint con
          join pg_class rel on rel.oid = con.conrelid
         where rel.relname = 'trends'
           and con.conname = 'trends_scope_period_topic_key'
    ) then
        alter table trends
            add constraint trends_scope_period_topic_key unique (request_slug, week, topic);
    end if;
end $$;

create index if not exists idx_trends_week  on trends (week);
create index if not exists idx_trends_scope on trends (request_slug, week);

-- =============================================================================
-- reports — generated reports (archive).
-- The original table stored only the weekly watch. The added columns let several
-- report TYPES (weekly watch, deep research, competitor scan) and several
-- parameterized requests coexist, on any cadence, without colliding.
-- All additions are nullable / defaulted so existing rows and the existing
-- ReportRepository.store(week, html) call keep working unchanged.
-- =============================================================================
create table if not exists reports (
    id            bigint generated always as identity primary key,
    week          text,
    generated_at  timestamptz default now(),
    content_html  text
);

alter table reports add column if not exists report_type   text default 'weekly';
alter table reports add column if not exists request_slug  text;
alter table reports add column if not exists language      text;
alter table reports add column if not exists period_kind   text;
alter table reports add column if not exists period_key    text;
alter table reports add column if not exists period_start  timestamptz;
alter table reports add column if not exists period_end    timestamptz;
alter table reports add column if not exists title         text;
alter table reports add column if not exists word_count    integer;
alter table reports add column if not exists params        jsonb;

create index if not exists idx_reports_week on reports (week);
create index if not exists idx_reports_type on reports (report_type, generated_at desc);
create index if not exists idx_reports_request on reports (request_slug, period_key);

-- =============================================================================
-- report_sources — the citation ledger.
-- One row per source actually cited by a generated report, so the
-- anti-hallucination guarantee is auditable after the fact: every claim in a
-- report traces to a row here.
--   tier 'A' = a URL from our collected article corpus
--   tier 'B' = a web source retrieved via LLM search grounding (verified URI)
--   tier 'C' = a prospective/inferred statement, anchored to A/B evidence
-- `domain` is stored separately because grounding URIs are redirect shells that
-- can expire, while the publisher domain remains a durable attribution.
-- =============================================================================
create table if not exists report_sources (
    id          bigint generated always as identity primary key,
    report_id   bigint references reports(id) on delete cascade,
    tier        text check (tier in ('A', 'B', 'C')),
    kind        text,
    url         text,
    domain      text,
    title       text,
    section     text,
    verified    boolean default false,
    created_at  timestamptz default now()
);

create index if not exists idx_report_sources_report on report_sources (report_id);
create index if not exists idx_report_sources_domain on report_sources (domain);

-- =============================================================================
-- runs — the scheduler's ledger: one row per (request, period).
--
-- The dispatcher runs DAILY and decides what is due by asking "is there already
-- a run for this request in the current period?". That makes the ledger, not a
-- `next_run_at` timestamp, the source of truth — which is why a missed day
-- self-heals (the period is still current tomorrow) and why a double dispatch
-- cannot produce two reports.
--
-- UNIQUE(request_slug, period_key) is the concurrency gate: claiming a run is an
-- INSERT, and losing the race is a unique violation rather than a lock. A run
-- left 'running' by a killed CI job is reclaimed after RECLAIM_AFTER_HOURS via a
-- conditional UPDATE (compare-and-swap on the observed status).
-- =============================================================================
create table if not exists runs (
    id            bigint generated always as identity primary key,
    request_slug  text not null,
    report_type   text,
    period_kind   text,
    period_key    text not null,          -- e.g. "2026-W32", "2026-M08", "2026-Q3"
    status        text not null default 'running'
                  check (status in ('running', 'success', 'failed', 'skipped')),
    attempts      integer not null default 1,
    started_at    timestamptz default now(),
    finished_at   timestamptz,
    report_id     bigint references reports(id) on delete set null,
    error         text,
    note          text,
    unique (request_slug, period_key)
);

create index if not exists idx_runs_slug   on runs (request_slug, period_key);
create index if not exists idx_runs_status on runs (status, started_at desc);

-- =============================================================================
-- ping() — trivial "SELECT 1" used by the keep-alive workflow.
-- PostgREST/supabase-py cannot run raw SQL, so the keep-alive calls this via
-- rpc("ping"). Exposed automatically to the API in the public schema.
-- =============================================================================
create or replace function ping() returns integer
    language sql
    as $$ select 1 $$;

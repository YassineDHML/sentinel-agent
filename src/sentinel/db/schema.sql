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
    summary       text,                   -- LLM-generated summary
    processed     boolean default false
);

create index if not exists idx_articles_processed    on articles (processed);
create index if not exists idx_articles_published_at  on articles (published_at desc);

-- =============================================================================
-- trends — topic frequency, week by week.
-- NOTE (beyond the spec's minimal DDL): a UNIQUE(topic, week) constraint is
-- added so the pipeline can UPSERT one row per (topic, week) idempotently
-- (re-runs of the same week update counts instead of duplicating rows).
-- =============================================================================
create table if not exists trends (
    id            bigint generated always as identity primary key,
    topic         text not null,          -- from the canonical list (BF-03)
    week          text not null,          -- ISO week, e.g. "2026-W24"
    article_count integer,                -- articles on this topic this week
    actors        jsonb,                  -- actors involved
    unique (topic, week)
);

create index if not exists idx_trends_week on trends (week);

-- =============================================================================
-- reports — generated weekly reports (archive).
-- =============================================================================
create table if not exists reports (
    id            bigint generated always as identity primary key,
    week          text,
    generated_at  timestamptz default now(),
    content_html  text
);

create index if not exists idx_reports_week on reports (week);

-- =============================================================================
-- ping() — trivial "SELECT 1" used by the keep-alive workflow.
-- PostgREST/supabase-py cannot run raw SQL, so the keep-alive calls this via
-- rpc("ping"). Exposed automatically to the API in the public schema.
-- =============================================================================
create or replace function ping() returns integer
    language sql
    as $$ select 1 $$;

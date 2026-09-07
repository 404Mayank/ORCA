-- RAG explainer store: wording only, never numbers that decide anything.
--
-- `rag_docs` holds short explainer passages built from in-repo cited text
-- (threshold notes, treaty transcription notes, dataset gap notes) by
-- scripts/build_rag.py. Retrieval feeds hypothesis/proposal WORDING only;
-- every passage passes through strip_numbers() in rag/store.py before use,
-- and no passage may populate claims[], drivers[], or evidence digests.
--
-- Ranking is Postgres full-text search (body_tsv). The `embedding` column is
-- reserved for a future vector rank; it is NULLABLE and unwritten. No
-- embedding call happens at query time by design (cache-first rule).
--
-- Apply in Supabase SQL editor or psql. Nothing in the app runs migrations;
-- without this table retrieval returns [] and answers are identical.

create extension if not exists vector;

create table if not exists rag_docs (
  id bigint generated always as identity primary key,
  source text not null,
  title text not null,
  body text not null,
  body_tsv tsvector
    generated always as (to_tsvector('english', title || ' ' || body)) stored,
  embedding vector(1536),
  constraint rag_docs_source_title_unique unique (source, title)
);

create index if not exists rag_docs_body_tsv_idx on rag_docs using gin (body_tsv);

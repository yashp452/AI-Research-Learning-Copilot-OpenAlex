-- ============================================================================
-- AI Research and Learning Copilot - Lakebase (Postgres) schema
--
-- Idempotent: safe to run repeatedly.
-- Ordered by foreign-key dependency, so run top to bottom.
--
--   psql "$LAKEBASE_URL" -f sql/01_schema.sql
--   or paste into the Lakebase SQL editor.
--
-- Note this is the OLTP + retrieval store only. The bronze/silver/gold tables
-- live in Unity Catalog and are created by the notebooks via saveAsTable.
-- ============================================================================

-- ----------------------------------------------------------------------------
-- Extensions
-- ----------------------------------------------------------------------------

-- pgvector: VECTOR type, HNSW index, and the <=> cosine distance operator.
CREATE EXTENSION IF NOT EXISTS vector;


-- ----------------------------------------------------------------------------
-- users
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    user_id      SERIAL PRIMARY KEY,
    email        TEXT UNIQUE NOT NULL,
    display_name TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);


-- ----------------------------------------------------------------------------
-- learning_goals - what the user is trying to learn
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS learning_goals (
    goal_id     SERIAL PRIMARY KEY,
    user_id     INT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    title       TEXT NOT NULL,
    description TEXT,
    status      TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'paused', 'completed', 'abandoned')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_learning_goals_user ON learning_goals (user_id);


-- ----------------------------------------------------------------------------
-- papers - the searchable corpus, loaded from the gold Delta table
--
-- VECTOR(384) is tied to sentence-transformers/all-MiniLM-L6-v2. Changing the
-- embedding model means changing this number and rebuilding the HNSW index.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS papers (
    work_id        TEXT PRIMARY KEY,
    doi            TEXT,
    title          TEXT NOT NULL,
    abstract       TEXT,
    publication_yr INT,
    cited_by_count INT DEFAULT 0,
    oa_url         TEXT,
    primary_topic  TEXT,
    author_names   TEXT,
    embedding      VECTOR(384),
    model_name     TEXT,
    ingested_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Semantic lane. vector_cosine_ops must match the <=> operator used in queries;
-- an l2 index here would be silently ignored and fall back to a seq scan.
CREATE INDEX IF NOT EXISTS idx_papers_embedding
    ON papers USING hnsw (embedding vector_cosine_ops);

-- Keyword lane. GIN over the same title+abstract expression the queries use -
-- the expression must match exactly or the index will not be chosen.
CREATE INDEX IF NOT EXISTS idx_papers_fts
    ON papers USING gin (
        to_tsvector('english', coalesce(title, '') || ' ' || coalesce(abstract, ''))
    );

CREATE INDEX IF NOT EXISTS idx_papers_year      ON papers (publication_yr);
CREATE INDEX IF NOT EXISTS idx_papers_citations ON papers (cited_by_count DESC);


-- ----------------------------------------------------------------------------
-- collections - named reading lists, optionally tied to a goal
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS collections (
    collection_id SERIAL PRIMARY KEY,
    user_id       INT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    goal_id       INT REFERENCES learning_goals(goal_id) ON DELETE SET NULL,
    name          TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, name)
);

CREATE INDEX IF NOT EXISTS idx_collections_user ON collections (user_id);


-- ----------------------------------------------------------------------------
-- collection_papers - the agent's main write target
--
-- Composite primary key makes add_to_collection idempotent via
-- ON CONFLICT DO NOTHING. seq_no is set by build_reading_plan.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS collection_papers (
    collection_id INT  NOT NULL REFERENCES collections(collection_id) ON DELETE CASCADE,
    work_id       TEXT NOT NULL REFERENCES papers(work_id) ON DELETE CASCADE,
    seq_no        INT,
    added_by      TEXT NOT NULL DEFAULT 'agent'
                  CHECK (added_by IN ('agent', 'user')),
    added_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (collection_id, work_id)
);

CREATE INDEX IF NOT EXISTS idx_collection_papers_work ON collection_papers (work_id);


-- ----------------------------------------------------------------------------
-- reading_progress
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS reading_progress (
    user_id    INT  NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    work_id    TEXT NOT NULL REFERENCES papers(work_id) ON DELETE CASCADE,
    status     TEXT NOT NULL DEFAULT 'not_started'
               CHECK (status IN ('not_started', 'reading', 'read', 'skimmed', 'abandoned')),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, work_id)
);

CREATE INDEX IF NOT EXISTS idx_reading_progress_status ON reading_progress (user_id, status);


-- ----------------------------------------------------------------------------
-- notes - work_id nullable so a note can be general rather than paper-specific
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS notes (
    note_id    SERIAL PRIMARY KEY,
    user_id    INT  NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    work_id    TEXT REFERENCES papers(work_id) ON DELETE CASCADE,
    note_text  TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_notes_user ON notes (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_notes_work ON notes (work_id);


-- ----------------------------------------------------------------------------
-- agent_trace_logs - one row per tool call
--
-- No foreign keys on purpose. Observability must never be able to block or fail
-- the thing it is observing, and traces should survive a user being deleted.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agent_trace_logs (
    id          SERIAL PRIMARY KEY,
    session_id  UUID NOT NULL,
    tool_name   VARCHAR(255) NOT NULL,
    user_email  VARCHAR(255),
    parameters  JSONB,
    result      JSONB,
    error       TEXT,
    duration_ms INTEGER,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_trace_session ON agent_trace_logs (session_id, id);
CREATE INDEX IF NOT EXISTS idx_trace_tool    ON agent_trace_logs (tool_name, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_trace_errors  ON agent_trace_logs (created_at DESC)
    WHERE error IS NOT NULL;


-- ----------------------------------------------------------------------------
-- Verify
-- ----------------------------------------------------------------------------
SELECT tablename
FROM pg_tables
WHERE schemaname = 'public'
ORDER BY tablename;

SELECT indexname, tablename
FROM pg_indexes
WHERE schemaname = 'public'
ORDER BY tablename, indexname;
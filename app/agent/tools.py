"""Agent tools for the Research and Learning Copilot.

Plain functions over Lakebase. Imported by the Flask app and by notebook 07.

Config comes from environment variables rather than dbutils, so the same module
works in a notebook (set os.environ before importing) and in a deployed
Databricks App (the platform injects them).

    LAKEBASE_URL   - postgres URL, plain or base64-encoded
    MODEL_PATH     - path to the staged sentence-transformers model

Every tool returns {"success": bool, ...}. Errors are return values, never
exceptions: a raised exception would abort the agent loop, whereas a structured
error lets the model read what went wrong and correct itself.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import time
import uuid
from contextlib import contextmanager
from functools import lru_cache
from urllib.parse import urlparse

import psycopg2
from psycopg2.extras import RealDictCursor

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

MODEL_PATH = os.environ.get(
    "MODEL_PATH",
    "/Volumes/workspace/research_copilot/raw/models/all-MiniLM-L6-v2",
)
MAX_SEQ_LENGTH = 384  # must match notebook 03, or query and document vectors
                      # come from differently-configured models
LANE_SIZE = 50        # candidates per retrieval lane before fusion
RRF_K = 60            # damping constant from the original RRF paper


def _resolve_url(value: str) -> str:
    """The secret may hold the URL directly or base64-encoded, depending on how
    the scope was populated. b64decode silently ignores non-alphabet characters
    instead of failing, so check for a scheme first rather than relying on the
    decode to raise."""
    if value.startswith(("postgres://", "postgresql://")):
        return value
    try:
        decoded = base64.b64decode(value, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError) as e:
        raise ValueError("LAKEBASE_URL is neither a postgres URL nor valid base64") from e
    if not decoded.startswith(("postgres://", "postgresql://")):
        raise ValueError("Decoded LAKEBASE_URL is not a postgres URL")
    return decoded


@lru_cache(maxsize=1)
def _pg_kwargs() -> dict:
    raw = os.environ.get("LAKEBASE_URL")
    if not raw:
        raise RuntimeError("LAKEBASE_URL is not set")
    p = urlparse(_resolve_url(raw))
    return dict(
        host=p.hostname,
        port=p.port or 5432,
        dbname=p.path.lstrip("/"),
        user=p.username,
        password=p.password,
        sslmode="require",
    )


@contextmanager
def db(dict_rows: bool = True):
    """One connection, one transaction, always closed.

    Every write tool does its existence checks and its insert inside a single
    `with db()` block. Checking on a separate connection would leave a window in
    which the parent row could disappear between check and insert.
    """
    conn = psycopg2.connect(**_pg_kwargs(), connect_timeout=15)
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor if dict_rows else None)
        yield conn, cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Query encoding
# ---------------------------------------------------------------------------

_STOP = {
    "the", "and", "for", "with", "that", "this", "from", "are", "was", "were",
    "using", "use", "how", "what", "which", "can", "its", "been", "has", "have",
    "not", "but", "about", "into", "than", "then", "some", "any", "you", "your",
}


@lru_cache(maxsize=1)
def _encoder():
    """Loaded lazily and cached. First call pays ~3s of model init; the app pays
    it once at first search rather than at import time."""
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(MODEL_PATH)
    model.max_seq_length = MAX_SEQ_LENGTH
    return model


def embed_query(text: str) -> str:
    """Returns a pgvector-ready bracketed literal.

    normalize_embeddings must match notebook 03. A normalised document vector
    compared against an unnormalised query vector gives silently wrong distances.
    """
    vec = _encoder().encode([text], normalize_embeddings=True, convert_to_numpy=True)[0]
    return "[" + ",".join(str(float(x)) for x in vec) + "]"


def to_or_tsquery(text: str) -> str:
    """Build an OR'd tsquery.

    websearch_to_tsquery ANDs every term, which requires all of them in a single
    document and matches almost nothing on a natural-language query. OR them and
    let ts_rank order by how many matched.
    """
    words = [w for w in re.findall(r"[a-zA-Z0-9]+", text.lower())
             if len(w) > 2 and w not in _STOP]
    seen, out = set(), []
    for w in words:
        if w not in seen:
            seen.add(w)
            out.append(w)
    return " | ".join(out) if out else "research"


# ---------------------------------------------------------------------------
# Tracing
# ---------------------------------------------------------------------------

def _log_trace(session_id, tool_name, params, result, error, duration_ms, user_email=None):
    """Best effort. Failing to log must never fail the tool itself."""
    def _safe_json(obj, limit=8000):
        """Slicing a serialised string produces invalid JSON. Wrap oversized
        payloads in a valid envelope instead of truncating mid-token."""
        if obj is None:
            return None
        text = json.dumps(obj, default=str)
        if len(text) <= limit:
            return text
        return json.dumps({"_truncated": True, "_original_bytes": len(text),
                           "_preview": text[:limit // 2]})

    try:
        with db(dict_rows=False) as (conn, cur):
            cur.execute(
                """
                INSERT INTO agent_trace_logs
                    (session_id, tool_name, user_email, parameters, result, error, duration_ms)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (session_id, tool_name, user_email,
                 _safe_json(params), _safe_json(result), error, duration_ms),
            )
    except Exception as e:
        print(f"[trace] failed to log {tool_name}: {e}")


def traced(fn):
    """Wrap a tool so it always returns an envelope and always logs."""

    def wrapper(*args, session_id=None, user_email=None, **kwargs):
        sid = session_id or str(uuid.uuid4())
        started = time.time()
        params = {"args": args, "kwargs": kwargs}
        try:
            result = fn(*args, **kwargs)
            ms = int((time.time() - started) * 1000)
            # Tools return errors as data rather than raising, so checking only
            # for exceptions would record every rejection as a success.
            err = None if result.get("success") else result.get("error")
            _log_trace(sid, fn.__name__, params, result, err, ms, user_email)
            return result
        except Exception as e:  # noqa: BLE001
            ms = int((time.time() - started) * 1000)
            err = f"{type(e).__name__}: {e}"
            _log_trace(sid, fn.__name__, params, None, err, ms, user_email)
            return {"success": False, "error": err}

    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


# ---------------------------------------------------------------------------
# Tool 1 - search_papers
# ---------------------------------------------------------------------------

HYBRID_SQL = """
WITH q AS (SELECT %(qvec)s::vector AS vec),
vector_hits AS (
    SELECT p.work_id,
           ROW_NUMBER() OVER (ORDER BY p.embedding <=> q.vec) AS rank,
           1 - (p.embedding <=> q.vec) AS similarity
    FROM papers p, q
    WHERE p.embedding IS NOT NULL
      AND (%(min_year)s IS NULL OR p.publication_yr >= %(min_year)s)
      AND (%(min_citations)s IS NULL OR p.cited_by_count >= %(min_citations)s)
    ORDER BY p.embedding <=> q.vec
    LIMIT %(lane_size)s
),
keyword_hits AS (
    SELECT p.work_id,
           ROW_NUMBER() OVER (
               ORDER BY ts_rank(
                   to_tsvector('english', coalesce(p.title,'') || ' ' || coalesce(p.abstract,'')),
                   to_tsquery('english', %(qtext)s)) DESC) AS rank
    FROM papers p
    WHERE to_tsvector('english', coalesce(p.title,'') || ' ' || coalesce(p.abstract,''))
          @@ to_tsquery('english', %(qtext)s)
      AND (%(min_year)s IS NULL OR p.publication_yr >= %(min_year)s)
      AND (%(min_citations)s IS NULL OR p.cited_by_count >= %(min_citations)s)
    ORDER BY ts_rank(
        to_tsvector('english', coalesce(p.title,'') || ' ' || coalesce(p.abstract,'')),
        to_tsquery('english', %(qtext)s)) DESC
    LIMIT %(lane_size)s
),
fused AS (
    SELECT COALESCE(v.work_id, k.work_id) AS work_id,
           COALESCE(1.0/(%(rrf_k)s + v.rank), 0) + COALESCE(1.0/(%(rrf_k)s + k.rank), 0) AS rrf_score,
           v.rank AS vector_rank, k.rank AS keyword_rank, v.similarity
    FROM vector_hits v FULL OUTER JOIN keyword_hits k ON v.work_id = k.work_id
)
SELECT p.work_id, p.title, p.author_names, p.publication_yr, p.cited_by_count,
       p.primary_topic, p.oa_url, left(p.abstract, 400) AS abstract_snippet,
       round(f.rrf_score::numeric, 5) AS rrf_score,
       f.vector_rank, f.keyword_rank, round(f.similarity::numeric, 4) AS similarity
FROM fused f JOIN papers p ON p.work_id = f.work_id
ORDER BY f.rrf_score DESC, p.cited_by_count DESC
LIMIT %(top_k)s
"""


@traced
def search_papers(query: str, top_k: int = 8, min_year: int = None, min_citations: int = None):
    """Hybrid semantic and keyword search over the paper corpus."""
    top_k = max(1, min(int(top_k), 20))
    params = {
        "qvec": embed_query(query),
        "qtext": to_or_tsquery(query),
        "top_k": top_k,
        "lane_size": LANE_SIZE,
        "rrf_k": RRF_K,
        "min_year": min_year,
        "min_citations": min_citations,
    }
    with db() as (conn, cur):
        cur.execute(HYBRID_SQL, params)
        rows = [dict(r) for r in cur.fetchall()]

    for r in rows:
        for k in ("rrf_score", "similarity"):
            if r.get(k) is not None:
                r[k] = float(r[k])

    return {"success": True, "count": len(rows), "query": query, "papers": rows}


# ---------------------------------------------------------------------------
# Tool 2 - get_paper
# ---------------------------------------------------------------------------

@traced
def get_paper(work_id: str):
    """Full record for one paper, including the complete abstract."""
    with db() as (conn, cur):
        cur.execute(
            """
            SELECT work_id, doi, title, abstract, publication_yr, cited_by_count,
                   oa_url, primary_topic, author_names
            FROM papers WHERE work_id = %s
            """,
            (work_id,),
        )
        row = cur.fetchone()

    if not row:
        return {"success": False,
                "error": f"No paper with work_id {work_id}. Use search_papers to find valid ids."}
    return {"success": True, "paper": dict(row)}


# ---------------------------------------------------------------------------
# Tool 3 - create_collection
# ---------------------------------------------------------------------------

@traced
def create_collection(user_id: int, name: str, goal_id: int = None):
    """Create a named collection, or return the existing one with that name."""
    name = (name or "").strip()
    if not name:
        return {"success": False, "error": "name cannot be empty."}

    with db() as (conn, cur):
        cur.execute("SELECT 1 FROM users WHERE user_id = %s", (user_id,))
        if not cur.fetchone():
            return {"success": False, "error": f"User {user_id} does not exist."}

        # xmax = 0 distinguishes a fresh insert from an update in an upsert, so
        # the model can be told honestly whether it created something.
        cur.execute(
            """
            INSERT INTO collections (user_id, goal_id, name)
            VALUES (%s, %s, %s)
            ON CONFLICT (user_id, name)
            DO UPDATE SET goal_id = COALESCE(EXCLUDED.goal_id, collections.goal_id)
            RETURNING collection_id, (xmax = 0) AS created
            """,
            (user_id, goal_id, name),
        )
        row = cur.fetchone()

    return {
        "success": True,
        "collection_id": row["collection_id"],
        "created": row["created"],
        "message": (f"Created collection '{name}'." if row["created"]
                    else f"Collection '{name}' already existed; using it."),
    }


# ---------------------------------------------------------------------------
# Tool 4 - add_to_collection
# ---------------------------------------------------------------------------

@traced
def add_to_collection(collection_id: int, work_id: str):
    """Add a paper to one of the user's collections."""
    with db() as (conn, cur):
        cur.execute("SELECT name FROM collections WHERE collection_id = %s", (collection_id,))
        coll = cur.fetchone()
        if not coll:
            return {"success": False, "error": f"Collection {collection_id} does not exist."}

        cur.execute("SELECT title FROM papers WHERE work_id = %s", (work_id,))
        paper = cur.fetchone()
        if not paper:
            return {"success": False,
                    "error": f"Paper {work_id} is not in the corpus, so it cannot be added."}

        cur.execute(
            """
            INSERT INTO collection_papers (collection_id, work_id, added_by)
            VALUES (%s, %s, 'agent')
            ON CONFLICT (collection_id, work_id) DO NOTHING
            RETURNING work_id
            """,
            (collection_id, work_id),
        )
        inserted = cur.fetchone() is not None

    return {
        "success": True,
        "added": inserted,
        "message": (f"Added '{paper['title'][:60]}' to '{coll['name']}'." if inserted
                    else f"'{paper['title'][:60]}' was already in '{coll['name']}'."),
    }


# ---------------------------------------------------------------------------
# Tool 5 - save_note
# ---------------------------------------------------------------------------

@traced
def save_note(user_id: int, note_text: str, work_id: str = None):
    """Save a research note, optionally attached to a specific paper."""
    if not note_text or not note_text.strip():
        return {"success": False, "error": "note_text cannot be empty."}

    with db() as (conn, cur):
        cur.execute("SELECT 1 FROM users WHERE user_id = %s", (user_id,))
        if not cur.fetchone():
            return {"success": False, "error": f"User {user_id} does not exist."}

        if work_id:
            cur.execute("SELECT 1 FROM papers WHERE work_id = %s", (work_id,))
            if not cur.fetchone():
                return {"success": False, "error": f"Paper {work_id} is not in the corpus."}

        cur.execute(
            "INSERT INTO notes (user_id, work_id, note_text) VALUES (%s, %s, %s) RETURNING note_id",
            (user_id, work_id, note_text.strip()),
        )
        note_id = cur.fetchone()["note_id"]

    return {"success": True, "note_id": note_id, "message": "Note saved."}


# ---------------------------------------------------------------------------
# Tool 6 - mark_progress
# ---------------------------------------------------------------------------

VALID_STATUS = ("not_started", "reading", "read", "skimmed", "abandoned")


@traced
def mark_progress(user_id: int, work_id: str, status: str):
    """Record how far the user has got with a paper."""
    if status not in VALID_STATUS:
        return {"success": False, "error": f"status must be one of {', '.join(VALID_STATUS)}."}

    with db() as (conn, cur):
        cur.execute("SELECT 1 FROM users WHERE user_id = %s", (user_id,))
        if not cur.fetchone():
            return {"success": False, "error": f"User {user_id} does not exist."}

        cur.execute("SELECT title FROM papers WHERE work_id = %s", (work_id,))
        paper = cur.fetchone()
        if not paper:
            return {"success": False, "error": f"Paper {work_id} is not in the corpus."}

        cur.execute(
            """
            INSERT INTO reading_progress (user_id, work_id, status, updated_at)
            VALUES (%s, %s, %s, now())
            ON CONFLICT (user_id, work_id)
            DO UPDATE SET status = EXCLUDED.status, updated_at = now()
            """,
            (user_id, work_id, status),
        )

    return {"success": True, "message": f"Marked '{paper['title'][:60]}' as {status}."}


# ---------------------------------------------------------------------------
# Tool 7 - build_reading_plan
# ---------------------------------------------------------------------------

@traced
def build_reading_plan(collection_id: int):
    """Order a collection into a reading sequence and persist that order.

    Highly-cited-and-older first. Citation count is a rough proxy for how
    foundational a paper is, and older heavily-cited work tends to be what later
    papers build on. A true prerequisite ordering would walk the citation graph,
    which needs referenced_works loaded into Postgres.
    """
    with db() as (conn, cur):
        cur.execute("SELECT name FROM collections WHERE collection_id = %s", (collection_id,))
        coll = cur.fetchone()
        if not coll:
            return {"success": False, "error": f"Collection {collection_id} does not exist."}

        cur.execute(
            """
            SELECT p.work_id, p.title, p.publication_yr, p.cited_by_count
            FROM collection_papers cp
            JOIN papers p ON p.work_id = cp.work_id
            WHERE cp.collection_id = %s
            ORDER BY p.cited_by_count DESC, p.publication_yr ASC
            """,
            (collection_id,),
        )
        papers = [dict(r) for r in cur.fetchall()]

        if not papers:
            return {"success": False,
                    "error": f"Collection '{coll['name']}' is empty. Add papers first."}

        for seq, p in enumerate(papers, start=1):
            cur.execute(
                "UPDATE collection_papers SET seq_no = %s WHERE collection_id = %s AND work_id = %s",
                (seq, collection_id, p["work_id"]),
            )
            p["seq_no"] = seq

    return {"success": True, "collection": coll["name"], "count": len(papers), "plan": papers}


# ---------------------------------------------------------------------------
# Registry and JSON schemas
#
# The schema text is the only thing the model sees. Vague descriptions here are
# the single most common cause of an agent calling the wrong tool or passing the
# wrong argument, so they are written for the model rather than for a human.
# ---------------------------------------------------------------------------

TOOL_REGISTRY = {
    "search_papers": search_papers,
    "get_paper": get_paper,
    "create_collection": create_collection,
    "add_to_collection": add_to_collection,
    "save_note": save_note,
    "mark_progress": mark_progress,
    "build_reading_plan": build_reading_plan,
}

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "search_papers",
            "description": (
                "Search the research paper corpus by meaning and keywords combined. "
                "Use this first whenever the user asks about a topic, wants to find "
                "papers, or mentions a subject you do not already have work_ids for. "
                "Returns papers with work_id, title, authors, year, citations and an "
                "abstract snippet."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "What to look for, in natural language. Describe the topic "
                            "rather than repeating the user's whole message."
                        ),
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "How many papers to return, 1 to 20. Default 8.",
                    },
                    "min_year": {
                        "type": "integer",
                        "description": "Only papers published in or after this year.",
                    },
                    "min_citations": {
                        "type": "integer",
                        "description": (
                            "Only papers with at least this many citations. Useful when "
                            "the user asks for influential or well-known work."
                        ),
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_paper",
            "description": (
                "Fetch the complete record for one paper, including the full abstract. "
                "Use when the user asks about a specific paper you already have a "
                "work_id for, or when the snippet from search is not enough to answer."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "work_id": {
                        "type": "string",
                        "description": "OpenAlex work id, for example W4386566659.",
                    },
                },
                "required": ["work_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_collection",
            "description": (
                "Create a named collection (a reading list). Safe to call if one with "
                "that name already exists - it returns the existing collection_id "
                "instead of failing."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "integer", "description": "The current user's id."},
                    "name": {
                        "type": "string",
                        "description": "Short descriptive name, for example 'Diffusion models'.",
                    },
                    "goal_id": {
                        "type": "integer",
                        "description": "Optional learning goal to attach this collection to.",
                    },
                },
                "required": ["user_id", "name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_to_collection",
            "description": (
                "Add one paper to a collection. Call once per paper. The work_id must "
                "come from search_papers or get_paper - never invent one."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "collection_id": {"type": "integer", "description": "Target collection id."},
                    "work_id": {"type": "string", "description": "OpenAlex work id of the paper."},
                },
                "required": ["collection_id", "work_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_note",
            "description": (
                "Save a research note for the user. Attach it to a paper with work_id "
                "when the note is about a specific paper, otherwise leave work_id out."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "integer", "description": "The current user's id."},
                    "note_text": {"type": "string", "description": "The note content."},
                    "work_id": {
                        "type": "string",
                        "description": "Optional work id this note is about.",
                    },
                },
                "required": ["user_id", "note_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mark_progress",
            "description": "Record the user's reading status for a paper.",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "integer", "description": "The current user's id."},
                    "work_id": {"type": "string", "description": "OpenAlex work id of the paper."},
                    "status": {
                        "type": "string",
                        "enum": list(VALID_STATUS),
                        "description": "One of not_started, reading, read, skimmed, abandoned.",
                    },
                },
                "required": ["user_id", "work_id", "status"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "build_reading_plan",
            "description": (
                "Order the papers in a collection into a sensible reading sequence and "
                "save that order. The collection must already contain papers."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "collection_id": {"type": "integer", "description": "Collection to order."},
                },
                "required": ["collection_id"],
            },
        },
    },
]


def call_tool(name: str, arguments: dict, session_id: str = None, user_email: str = None):
    """Dispatch by name. Unknown names return an error the model can recover from
    rather than raising, since the model does occasionally invent tool names."""
    fn = TOOL_REGISTRY.get(name)
    if fn is None:
        return {"success": False,
                "error": f"Unknown tool '{name}'. Available: {', '.join(TOOL_REGISTRY)}"}
    return fn(**arguments, session_id=session_id, user_email=user_email)
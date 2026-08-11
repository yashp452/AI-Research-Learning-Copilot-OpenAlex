# Databricks notebook source
# MAGIC %pip install -q sentence-transformers

# COMMAND ----------

dbutils.library.restartPython()


# COMMAND ----------

dbutils.widgets.text("model_volume_path", "/Volumes/workspace/research_copilot/raw/models/all-MiniLM-L6-v2", "Model path")
dbutils.widgets.text("lakebase_secret_scope", "database", "Secret scope")
dbutils.widgets.text("lakebase_secret_key", "lakebase-url", "Secret key")

# COMMAND ----------

MODEL_VOLUME_PATH = dbutils.widgets.get("model_volume_path")
SECRET_SCOPE = dbutils.widgets.get("lakebase_secret_scope")
SECRET_KEY = dbutils.widgets.get("lakebase_secret_key")
MAX_SEQ_LENGTH = 384

# COMMAND ----------

 
import base64
import binascii
import json
import re
import time
import uuid
from contextlib import contextmanager
from urllib.parse import urlparse
 
import psycopg2
from psycopg2.extras import RealDictCursor
 

# COMMAND ----------

def _resolve_url(value: str) -> str:
    if value.startswith(("postgres://", "postgresql://")):
        return value
    try:
        return base64.b64decode(value, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError) as e:
        raise ValueError("Secret is neither a postgres URL nor valid base64") from e
 

# COMMAND ----------

_p = urlparse(_resolve_url(dbutils.secrets.get(scope=SECRET_SCOPE, key=SECRET_KEY)))
PG = dict(
    host=_p.hostname,
    port=_p.port or 5432,
    dbname=_p.path.lstrip("/"),
    user=_p.username,
    password=_p.password,
    sslmode="require",
)
 

# COMMAND ----------

@contextmanager
def db(dict_rows: bool = True):
    """One connection, one transaction, always closed.
 
    Every write tool does its existence checks and its insert inside a single
    `with db()` block. That is what stops the foreign-key race: check and insert
    share a transaction, so nothing can delete the parent row in between.
    """
    conn = psycopg2.connect(**PG, connect_timeout=15)
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor if dict_rows else None)
        yield conn, cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
 

# COMMAND ----------

with db() as (conn, cur):
    cur.execute("SELECT count(*) AS n FROM papers WHERE embedding IS NOT NULL")
    print("searchable papers:", cur.fetchone()["n"])
 

# COMMAND ----------

from sentence_transformers import SentenceTransformer
 
_model = SentenceTransformer(MODEL_VOLUME_PATH)
_model.max_seq_length = MAX_SEQ_LENGTH
 
_STOP = {"the", "and", "for", "with", "that", "this", "from", "are", "was", "were",
         "using", "use", "how", "what", "which", "can", "its", "been", "has",
         "have", "not", "but", "about", "into", "than", "then", "some", "any"}
 

# COMMAND ----------

 
def embed_query(text: str) -> str:
    vec = _model.encode([text], normalize_embeddings=True, convert_to_numpy=True)[0]
    return "[" + ",".join(str(float(x)) for x in vec) + "]"
 

# COMMAND ----------

def to_or_tsquery(text: str) -> str:
    """to_tsquery with OR. websearch_to_tsquery ANDs every term, which requires all
    of them in one document and matches almost nothing on a natural-language query."""
    words = [w for w in re.findall(r"[a-zA-Z0-9]+", text.lower())
             if len(w) > 2 and w not in _STOP]
    seen, out = set(), []
    for w in words:
        if w not in seen:
            seen.add(w)
            out.append(w)
    return " | ".join(out) if out else "research"
 

# COMMAND ----------

# DBTITLE 1,Trace logging and the result envelope
def _log_trace(session_id, tool_name, params, result, error, duration_ms, user_email=None):
    """Best effort. A failure to log must never fail the tool itself."""
    try:
        with db(dict_rows=False) as (conn, cur):
            cur.execute("""
                INSERT INTO agent_trace_logs
                    (session_id, tool_name, user_email, parameters, result, error, duration_ms)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (
                session_id, tool_name, user_email,
                json.dumps(params, default=str),
                json.dumps(result, default=str)[:8000] if result else None,
                error, duration_ms,
            ))
    except Exception as e:
        print(f"[trace] failed to log {tool_name}: {e}")
 


# COMMAND ----------

 
def traced(fn):
    """Wraps a tool so it always returns an envelope and always logs.
 
    The model sees the envelope, so an exception becomes readable data rather than
    a dead conversation.
    """
    def wrapper(*args, session_id=None, user_email=None, **kwargs):
        sid = session_id or str(uuid.uuid4())
        started = time.time()
        params = {"args": args, "kwargs": kwargs}
        try:
            result = fn(*args, **kwargs)
            ms = int((time.time() - started) * 1000)
            err = None if result.get("success") else result.get("error")
            _log_trace(sid, fn.__name__, params, result, err, ms, user_email)
            return result
        except Exception as e:
            ms = int((time.time() - started) * 1000)
            err = f"{type(e).__name__}: {e}"
            _log_trace(sid, fn.__name__, params, None, err, ms, user_email)
            return {"success": False, "error": err}
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper

# COMMAND ----------


# DBTITLE 1,Tool 1 - search_papers
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
           COALESCE(1.0/(60+v.rank), 0) + COALESCE(1.0/(60+k.rank), 0) AS rrf_score,
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

# COMMAND ----------

@traced
def search_papers(query: str, top_k: int = 8, min_year: int = None, min_citations: int = None):
    """Hybrid semantic + keyword search over the paper corpus.

    query        - natural language description of what to find
    top_k        - how many papers to return (1-20)
    min_year     - only papers published in or after this year
    min_citations- only papers with at least this many citations
    """
    top_k = max(1, min(int(top_k), 20))
    params = {
        "qvec": embed_query(query),
        "qtext": to_or_tsquery(query),
        "top_k": top_k,
        "lane_size": 50,
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


# COMMAND ----------

@traced
def get_paper(work_id: str):
    """Full record for one paper, including the complete abstract.

    work_id - OpenAlex id such as W4385245566
    """
    with db() as (conn, cur):
        cur.execute("""
            SELECT work_id, doi, title, abstract, publication_yr, cited_by_count,
                   oa_url, primary_topic, author_names, model_name
            FROM papers WHERE work_id = %s
        """, (work_id,))
        row = cur.fetchone()

    if not row:
        return {"success": False,
                "error": f"No paper with work_id {work_id}. Use search_papers to find valid ids."}
    return {"success": True, "paper": dict(row)}

# COMMAND ----------

@traced
def add_to_collection(collection_id: int, work_id: str):
    """Add a paper to one of the user's collections.

    collection_id - target collection
    work_id       - paper to add
    """
    with db() as (conn, cur):
        # Both checks and the insert share one transaction. Checking on a separate
        # connection would leave a window where the parent row could disappear -
        # the foreign-key failure mode from day one.
        cur.execute("SELECT name FROM collections WHERE collection_id = %s", (collection_id,))
        coll = cur.fetchone()
        if not coll:
            return {"success": False, "error": f"Collection {collection_id} does not exist."}

        cur.execute("SELECT title FROM papers WHERE work_id = %s", (work_id,))
        paper = cur.fetchone()
        if not paper:
            return {"success": False,
                    "error": f"Paper {work_id} is not in the corpus, so it cannot be added."}

        cur.execute("""
            INSERT INTO collection_papers (collection_id, work_id, added_by)
            VALUES (%s, %s, 'agent')
            ON CONFLICT (collection_id, work_id) DO NOTHING
            RETURNING work_id
        """, (collection_id, work_id))
        inserted = cur.fetchone() is not None

    return {
        "success": True,
        "added": inserted,
        "message": (f"Added '{paper['title'][:60]}' to collection '{coll['name']}'."
                    if inserted else
                    f"'{paper['title'][:60]}' was already in '{coll['name']}'."),
    }

# COMMAND ----------

@traced
def save_note(user_id: int, note_text: str, work_id: str = None):
    """Save a research note, optionally attached to a specific paper.

    user_id   - who the note belongs to
    note_text - the note itself
    work_id   - optional paper this note is about
    """
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

        cur.execute("""
            INSERT INTO notes (user_id, work_id, note_text)
            VALUES (%s, %s, %s) RETURNING note_id
        """, (user_id, work_id, note_text.strip()))
        note_id = cur.fetchone()["note_id"]

    return {"success": True, "note_id": note_id, "message": "Note saved."}


# COMMAND ----------

# DBTITLE 1,Tool 5 - mark_progress (write)
VALID_STATUS = ("not_started", "reading", "read", "skimmed", "abandoned")

# COMMAND ----------

@traced
def mark_progress(user_id: int, work_id: str, status: str):
    """Record how far the user has got with a paper.

    status - one of not_started, reading, read, skimmed, abandoned
    """
    if status not in VALID_STATUS:
        return {"success": False,
                "error": f"status must be one of {', '.join(VALID_STATUS)}."}

    with db() as (conn, cur):
        cur.execute("SELECT 1 FROM users WHERE user_id = %s", (user_id,))
        if not cur.fetchone():
            return {"success": False, "error": f"User {user_id} does not exist."}

        cur.execute("SELECT title FROM papers WHERE work_id = %s", (work_id,))
        paper = cur.fetchone()
        if not paper:
            return {"success": False, "error": f"Paper {work_id} is not in the corpus."}

        cur.execute("""
            INSERT INTO reading_progress (user_id, work_id, status, updated_at)
            VALUES (%s, %s, %s, now())
            ON CONFLICT (user_id, work_id)
            DO UPDATE SET status = EXCLUDED.status, updated_at = now()
        """, (user_id, work_id, status))

    return {"success": True,
            "message": f"Marked '{paper['title'][:60]}' as {status}."}

# COMMAND ----------

# DBTITLE 1,Tool 6 - build_reading_plan (write)
@traced
def build_reading_plan(collection_id: int):
    """Order a collection into a sensible reading sequence and persist that order.

    Ordering is highly-cited-and-older first, then newer work. Citation count is a
    rough proxy for how foundational a paper is, and older-and-heavily-cited papers
    tend to be the ones later work builds on.

    A true prerequisite ordering would walk the citation graph, which needs the
    referenced_works edges loaded into Postgres. They currently live only in the
    gold Delta table - see the note in the notebook.
    """
    with db() as (conn, cur):
        cur.execute("SELECT name FROM collections WHERE collection_id = %s", (collection_id,))
        coll = cur.fetchone()
        if not coll:
            return {"success": False, "error": f"Collection {collection_id} does not exist."}

        cur.execute("""
            SELECT p.work_id, p.title, p.publication_yr, p.cited_by_count
            FROM collection_papers cp
            JOIN papers p ON p.work_id = cp.work_id
            WHERE cp.collection_id = %s
            ORDER BY p.cited_by_count DESC, p.publication_yr ASC
        """, (collection_id,))
        papers = [dict(r) for r in cur.fetchall()]

        if not papers:
            return {"success": False,
                    "error": f"Collection '{coll['name']}' is empty. Add papers first."}

        for seq, p in enumerate(papers, start=1):
            cur.execute("""
                UPDATE collection_papers SET seq_no = %s
                WHERE collection_id = %s AND work_id = %s
            """, (seq, collection_id, p["work_id"]))
            p["seq_no"] = seq

    return {"success": True, "collection": coll["name"],
            "count": len(papers), "plan": papers}

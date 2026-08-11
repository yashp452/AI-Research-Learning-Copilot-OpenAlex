# Databricks notebook source
# MAGIC %pip install -q sentence-transformers

# COMMAND ----------

dbutils.library.restartPython()


# COMMAND ----------

dbutils.widgets.text("model_volume_path", "/Volumes/workspace/research_copilot/raw/models/all-MiniLM-L6-v2", "Model path")
dbutils.widgets.text("lakebase_secret_scope", "database", "Secret scope")
dbutils.widgets.text("lakebase_secret_key", "lakebase-url", "Secret key")
 
MODEL_VOLUME_PATH = dbutils.widgets.get("model_volume_path")
SECRET_SCOPE = dbutils.widgets.get("lakebase_secret_scope")
SECRET_KEY = dbutils.widgets.get("lakebase_secret_key")
 

# COMMAND ----------

MAX_SEQ_LENGTH = 384  


# COMMAND ----------

 
# DBTITLE 1,Connection
import base64
import binascii
import psycopg2
from urllib.parse import urlparse
 
_raw = dbutils.secrets.get(scope=SECRET_SCOPE, key=SECRET_KEY)
 
 
def resolve_url(value: str) -> str:
    """Secret may hold the URL directly or base64-encoded. b64decode ignores
    non-alphabet characters instead of failing, so check for a scheme first."""
    if value.startswith(("postgres://", "postgresql://")):
        return value
    try:
        decoded = base64.b64decode(value, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError) as e:
        raise ValueError(f"Secret at {SECRET_SCOPE}/{SECRET_KEY} is neither a "
                         "postgres URL nor valid base64") from e
    return decoded
 
 
_p = urlparse(resolve_url(_raw))
PG = dict(
    host=_p.hostname,
    port=_p.port or 5432,
    dbname=_p.path.lstrip("/"),
    user=_p.username,
    password=_p.password,
    sslmode="require",
)
 
with psycopg2.connect(**PG, connect_timeout=15) as conn:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM papers WHERE embedding IS NOT NULL")
        print("searchable papers:", cur.fetchone()[0])


# COMMAND ----------

# DBTITLE 1,Query encoder
from sentence_transformers import SentenceTransformer
 
_model = SentenceTransformer(MODEL_VOLUME_PATH)
_model.max_seq_length = MAX_SEQ_LENGTH
 
 
def embed_query(text: str) -> str:
    """Returns a pgvector-ready bracketed literal.
 
    normalize_embeddings must match notebook 03 - a normalised document vector
    compared against an unnormalised query vector gives silently wrong distances.
    """
    vec = _model.encode([text], normalize_embeddings=True, convert_to_numpy=True)[0]
    return "[" + ",".join(str(float(x)) for x in vec) + "]"
 
 
print(embed_query("test")[:60], "...")


import re

_STOP = {"the","and","for","with","that","this","from","are","was","were","using",
         "use","how","what","which","can","its","been","has","have","not","but"}


def to_or_tsquery(text: str) -> str:
    """websearch_to_tsquery ANDs every term, so a natural-language query needs all
    of them in one document and matches nothing. OR them instead and let ts_rank
    order by how many matched."""
    words = [w for w in re.findall(r"[a-zA-Z0-9]+", text.lower())
             if len(w) > 2 and w not in _STOP]
    seen, out = set(), []
    for w in words:
        if w not in seen:
            seen.add(w)
            out.append(w)
    return " | ".join(out)

# COMMAND ----------


# DBTITLE 1,Hybrid search with reciprocal rank fusion
# RRF scores each result as 1/(k + rank) per lane and sums across lanes. It fuses
# on RANK rather than on raw score, which matters because cosine similarity (~0.5)
# and ts_rank (~0.05) are on completely different scales and cannot be added
# directly. k=60 is the value from the original RRF paper - it damps the influence
# of top ranks just enough that a single lane cannot dominate.
HYBRID_SQL = """
WITH q AS (
    SELECT %(qvec)s::vector AS vec
),
vector_hits AS (
    SELECT p.work_id,
           ROW_NUMBER() OVER (ORDER BY p.embedding <=> q.vec) AS rank,
           1 - (p.embedding <=> q.vec) AS similarity
    FROM papers p, q
    WHERE p.embedding IS NOT NULL
      AND (%(min_year)s IS NULL OR p.publication_yr >= %(min_year)s)
    ORDER BY p.embedding <=> q.vec
    LIMIT %(lane_size)s
),
keyword_hits AS (
    SELECT p.work_id,
           ROW_NUMBER() OVER (
               ORDER BY ts_rank(
                   to_tsvector('english', coalesce(p.title,'') || ' ' || coalesce(p.abstract,'')),
                   to_tsquery('english', %(qtext)s)
               ) DESC
           ) AS rank
    FROM papers p
    WHERE to_tsvector('english', coalesce(p.title,'') || ' ' || coalesce(p.abstract,''))
          @@ to_tsquery('english', %(qtext)s)
      AND (%(min_year)s IS NULL OR p.publication_yr >= %(min_year)s)
    ORDER BY ts_rank(
        to_tsvector('english', coalesce(p.title,'') || ' ' || coalesce(p.abstract,'')),
        to_tsquery('english', %(qtext)s)
    ) DESC
    LIMIT %(lane_size)s
),
fused AS (
    SELECT COALESCE(v.work_id, k.work_id) AS work_id,
           COALESCE(1.0 / (60 + v.rank), 0) + COALESCE(1.0 / (60 + k.rank), 0) AS rrf_score,
           v.rank AS vector_rank,
           k.rank AS keyword_rank,
           v.similarity
    FROM vector_hits v
    FULL OUTER JOIN keyword_hits k ON v.work_id = k.work_id
)
SELECT p.work_id,
       p.title,
       p.author_names,
       p.publication_yr,
       p.cited_by_count,
       p.primary_topic,
       p.oa_url,
       left(p.abstract, 300) AS abstract_snippet,
       round(f.rrf_score::numeric, 5) AS rrf_score,
       f.vector_rank,
       f.keyword_rank,
       round(f.similarity::numeric, 4) AS similarity
FROM fused f
JOIN papers p ON p.work_id = f.work_id
ORDER BY f.rrf_score DESC, p.cited_by_count DESC
LIMIT %(top_k)s
"""
 

# COMMAND ----------

def hybrid_search(query: str, top_k: int = 10, lane_size: int = 50, min_year: int = None):
    params = {
        "qvec": embed_query(query),
        "qtext": to_or_tsquery(query),
        "top_k": top_k,
        "lane_size": lane_size,
        "min_year": min_year,
    }
    with psycopg2.connect(**PG, connect_timeout=15) as conn:
        with conn.cursor() as cur:
            cur.execute(HYBRID_SQL, params)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
 
 
def show(query: str, **kwargs):
    print(f"\n{'=' * 100}\nQUERY: {query}\n{'=' * 100}")
    print(f"{'rrf':<9} {'vec':<5} {'kw':<5} {'sim':<8} title")
    print("-" * 100)
    for r in hybrid_search(query, **kwargs):
        v = r["vector_rank"] if r["vector_rank"] is not None else "-"
        k = r["keyword_rank"] if r["keyword_rank"] is not None else "-"
        s = r["similarity"] if r["similarity"] is not None else "-"
        print(f"{r['rrf_score']:<9} {v:<5} {k:<5} {str(s):<8} {r['title'][:70]}")
 
 
show("using large language models to tutor students and give feedback")
 
# COMMAND ----------
 
# DBTITLE 1,The cases each lane gets wrong alone
# Paraphrase: no shared vocabulary with the likely title wording, so the keyword
# lane should mostly miss and the vector lane should carry it.
show("teaching machines to see without labelled examples")

# COMMAND ----------

# Exact technical term: the vector lane blurs it into neighbouring concepts,
# the keyword lane nails it.
show("LoRA low-rank adaptation fine-tuning")
 
# COMMAND ----------
 
# Word sense: "teacher" here means knowledge distillation, not education.
# Watch whether the keyword lane pulls the right sense back up.
show("knowledge distillation teacher student model compression")
 
# COMMAND ----------
 
# Multi-concept - the hardest case for either lane alone.
show("efficient transformer inference on limited hardware")
 

# COMMAND ----------


# DBTITLE 1,Compare the lanes head to head
def compare_lanes(query: str, top_k: int = 8):
    """Same query, three rankings. Makes it obvious what fusion is buying."""
    qvec = embed_query(query)
    with psycopg2.connect(**PG, connect_timeout=15) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT title FROM papers WHERE embedding IS NOT NULL
                ORDER BY embedding <=> %s::vector LIMIT %s
            """, (qvec, top_k))
            vector_only = [r[0] for r in cur.fetchall()]
 
            cur.execute("""
                SELECT title FROM papers
                WHERE to_tsvector('english', coalesce(title,'') || ' ' || coalesce(abstract,''))
                      @@ to_tsquery('english', %s)
                ORDER BY ts_rank(
                    to_tsvector('english', coalesce(title,'') || ' ' || coalesce(abstract,'')),
                    to_tsquery('english', %s)
                ) DESC
                LIMIT %s
            """, (to_or_tsquery(query), to_or_tsquery(query), top_k))
            keyword_only = [r[0] for r in cur.fetchall()]
 
    fused = [r["title"] for r in hybrid_search(query, top_k=top_k)]
 
    print(f"\nQUERY: {query}\n")
    for label, results in [("VECTOR ONLY", vector_only), ("KEYWORD ONLY", keyword_only), ("HYBRID", fused)]:
        print(f"--- {label} ---")
        for i, t in enumerate(results, 1):
            print(f"  {i}. {t[:75]}")
        print()
 
 
compare_lanes("using large language models to tutor students and give feedback")

# COMMAND ----------


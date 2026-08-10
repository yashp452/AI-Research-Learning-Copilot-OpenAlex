# Databricks notebook source
# MAGIC %pip uninstall -y psycopg2 psycopg2-binary

# COMMAND ----------

# MAGIC %pip uninstall -y psycopg2 psycopg2-binary

# COMMAND ----------

# Databricks notebook source
# MAGIC %md
# MAGIC # 04 - Load gold into Lakebase
# MAGIC
# MAGIC Deliberately separate from `03_gold_embed`.
# MAGIC
# MAGIC `psycopg2-binary` bundles its own libssl/libcrypto. If torch is already loaded
# MAGIC in the same Python process (which it is, after sentence-transformers runs), the
# MAGIC symbols clash and the kernel dies with SIGABRT - not a catchable exception.
# MAGIC
# MAGIC Keeping the two in separate notebooks means separate processes. This is also how
# MAGIC they run as job tasks, so the split matches production rather than working around it.

# COMMAND ----------

# MAGIC %pip install -q psycopg2-binary

# COMMAND ----------


# COMMAND ----------

# DBTITLE 1,Config
dbutils.widgets.text("gold_table", "workspace.research_copilot.gold_paper_documents", "Gold table")
dbutils.widgets.text("lakebase_secret_scope", "database", "Secret scope")
dbutils.widgets.text("lakebase_secret_key", "lakebase-url", "Secret key")
dbutils.widgets.text("batch_size", "500", "Upsert batch size")

GOLD_TABLE = dbutils.widgets.get("gold_table")
SECRET_SCOPE = dbutils.widgets.get("lakebase_secret_scope")
SECRET_KEY = dbutils.widgets.get("lakebase_secret_key")
BATCH = int(dbutils.widgets.get("batch_size"))

from pyspark.sql import functions as F

ready = spark.table(GOLD_TABLE).filter(F.col("embedding").isNotNull()).count()
pending = spark.table(GOLD_TABLE).filter(F.col("embedding").isNull()).count()
print(f"gold rows with embeddings: {ready}")
print(f"gold rows still unembedded: {pending}   (run 03 again to fill these)")


# COMMAND ----------

import base64
import binascii
import psycopg2
from psycopg2.extras import execute_values
from urllib.parse import urlparse

_raw = dbutils.secrets.get(scope=SECRET_SCOPE, key=SECRET_KEY)


def resolve_url(value: str) -> str:
    """The secret may hold the URL directly or base64-encoded, depending on how the
    scope was populated. b64decode ignores non-alphabet chars instead of failing,
    so check for a scheme first rather than relying on the decode to error."""
    if value.startswith(("postgres://", "postgresql://")):
        return value
    try:
        decoded = base64.b64decode(value, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError) as e:
        raise ValueError(
            "Secret is neither a postgres URL nor valid base64 - check "
            f"scope={SECRET_SCOPE} key={SECRET_KEY}"
        ) from e
    if not decoded.startswith(("postgres://", "postgresql://")):
        raise ValueError("Decoded secret is not a postgres URL")
    return decoded


lakebase_url = resolve_url(_raw)

_p = urlparse(lakebase_url)
PG = dict(
    host=_p.hostname,
    port=_p.port or 5432,
    dbname=_p.path.lstrip("/"),
    user=_p.username,
    password=_p.password,
    sslmode="require",
)

print(f"host={_p.hostname} db={_p.path.lstrip('/')} user={_p.username}")

with psycopg2.connect(**PG, connect_timeout=15) as conn:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM papers")
        print("papers currently in Lakebase:", cur.fetchone()[0])

# COMMAND ----------

# COMMAND ----------
# # DBTITLE 1,Upsert into Lakebase papers
UPSERT_SQL = """
INSERT INTO papers (
    work_id, doi, title, abstract, publication_yr, cited_by_count,
    oa_url, primary_topic, author_names, embedding, model_name
)
VALUES %s
ON CONFLICT (work_id) DO UPDATE SET
    title          = EXCLUDED.title,
    abstract       = EXCLUDED.abstract,
    cited_by_count = EXCLUDED.cited_by_count,
    oa_url         = EXCLUDED.oa_url,
    author_names   = EXCLUDED.author_names,
    embedding      = EXCLUDED.embedding,
    model_name     = EXCLUDED.model_name,
    ingested_at    = now()
"""

# pgvector parses a bracketed string literal natively, so casting with %s::vector
# in the template avoids inserting as an array and patching it up afterwards.
TEMPLATE = "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector, %s)"

rows = (
    spark.table(GOLD_TABLE)
    .filter(F.col("embedding").isNotNull())
    .select(
        "work_id", "doi", "title", "abstract", "publication_year",
        "cited_by_count", "oa_url", "primary_topic_name", "author_names",
        "embedding", "model_name",
    )
    .toLocalIterator()  # streams partition by partition instead of collecting to the driver
)

buffer, total = [], 0

with psycopg2.connect(**PG, connect_timeout=15) as conn:
    with conn.cursor() as cur:
        for r in rows:
            buffer.append((
                r["work_id"],
                r["doi"],
                r["title"],
                r["abstract"],
                r["publication_year"],
                r["cited_by_count"],
                r["oa_url"],
                r["primary_topic_name"],
                r["author_names"],
                "[" + ",".join(str(float(x)) for x in r["embedding"]) + "]",
                r["model_name"],
            ))
            if len(buffer) >= BATCH:
                execute_values(cur, UPSERT_SQL, buffer, template=TEMPLATE, page_size=BATCH)
                conn.commit()
                total += len(buffer)
                buffer = []
                print(f"  upserted {total}")
        if buffer:
            execute_values(cur, UPSERT_SQL, buffer, template=TEMPLATE, page_size=BATCH)
            conn.commit()
            total += len(buffer)

print(f"DONE - upserted {total} papers into Lakebase")

# COMMAND ----------

# DBTITLE 1,Verify the load
with psycopg2.connect(**PG, connect_timeout=15) as conn:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*), count(embedding), count(DISTINCT model_name) FROM papers")
        print("rows / with embedding / distinct models:", cur.fetchone())

        cur.execute("SELECT vector_dims(embedding) FROM papers WHERE embedding IS NOT NULL LIMIT 1")
        print("vector dims in Postgres:", cur.fetchone()[0])

        # ANALYZE so the planner has stats. Without it the HNSW index may be
        # ignored in favour of a sequential scan.
        cur.execute("ANALYZE papers")

        cur.execute("""
            SELECT title FROM papers WHERE embedding IS NOT NULL
            ORDER BY cited_by_count DESC LIMIT 1
        """)
        seed_title = cur.fetchone()[0]
        print(f"\nnearest neighbours of: {seed_title[:70]}\n")

        cur.execute("""
            WITH seed AS (
                SELECT embedding FROM papers
                WHERE embedding IS NOT NULL
                ORDER BY cited_by_count desc LIMIT 1
            )
            SELECT p.work_id,
                   round((1 - (p.embedding <=> s.embedding))::numeric, 4) AS similarity,
                   p.title
            FROM papers p, seed s
            WHERE p.embedding IS NOT NULL
            ORDER BY p.embedding <=> s.embedding
            LIMIT 10
        """)
        for wid, sim, title in cur.fetchall():
            print(f"  {sim}  {wid}  {title[:65]}")

# COMMAND ----------



# COMMAND ----------


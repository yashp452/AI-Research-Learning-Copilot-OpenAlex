# Databricks notebook source
# MAGIC %pip install  sentence-transformers psycopg2-binary

# COMMAND ----------

dbutils.library.restartPython()

# COMMAND ----------

dbutils.widgets.text("silver_table", "workspace.research_copilot.silver_papers", "Silver table")
dbutils.widgets.text("gold_table", "workspace.research_copilot.gold_paper_documents", "Gold table")
dbutils.widgets.text("embedding_model", "sentence-transformers/all-MiniLM-L6-v2", "Embedding model")
dbutils.widgets.text("limit_rows", "500", "Rows to embed this run (0 = all)")
dbutils.widgets.text("embed_partitions", "8", "Partitions for the embedding pass")
dbutils.widgets.text("lakebase_secret_scope", "database", "Secret scope")
dbutils.widgets.text("lakebase_secret_key", "lakebase-url", "Secret key")
 

# COMMAND ----------

SILVER_TABLE = dbutils.widgets.get("silver_table")
GOLD_TABLE = dbutils.widgets.get("gold_table")
EMBEDDING_MODEL = dbutils.widgets.get("embedding_model")
LIMIT_ROWS = int(dbutils.widgets.get("limit_rows"))
EMBED_PARTITIONS = int(dbutils.widgets.get("embed_partitions"))
SECRET_SCOPE = dbutils.widgets.get("lakebase_secret_scope")
SECRET_KEY = dbutils.widgets.get("lakebase_secret_key")
 

# COMMAND ----------

EMBEDDING_DIM = 384  # all-MiniLM-L6-v2. Must match VECTOR(384) in the Lakebase schema.


# COMMAND ----------

from pyspark.sql import functions as F


# COMMAND ----------

from sentence_transformers import SentenceTransformer


# COMMAND ----------

# Guards against a model/schema dimension mismatch. Cached after first run.
from sentence_transformers import SentenceTransformer

_probe = SentenceTransformer(EMBEDDING_MODEL)
_dim = len(_probe.encode(["dimension probe"])[0])
assert _dim == EMBEDDING_DIM, (
    f"{EMBEDDING_MODEL} produces {_dim} dims but the Lakebase papers table is "
    f"VECTOR({EMBEDDING_DIM}). Update the schema or the model, not just this constant."
)
del _probe
print(f"embedding model OK - {EMBEDDING_MODEL} @ {_dim} dims")

# COMMAND ----------

# What goes into embed_text decides retrieval quality more than any other choice
# here. Title carries the strongest signal, the abstract carries the detail, and
# the topic name is a domain anchor that stops queries drifting into unrelated
# papers that happen to share vocabulary.
gold_source = (
    spark.table(SILVER_TABLE)
    .withColumn(
        "embed_text",
        F.concat_ws(
            "\n\n",
            F.col("title"),
            F.coalesce(F.col("primary_topic_name"), F.lit("")),
            F.col("abstract"),
        ),
    )
    .select(
        "work_id", "doi", "title", "abstract", "embed_text",
        "publication_year", "cited_by_count", "oa_url",
        "primary_topic_name", "primary_field", "author_names",
        "n_authors", "n_references", "referenced_works",
    )
    .withColumn("embedding", F.lit(None).cast("array<float>"))
    .withColumn("model_name", F.lit(None).cast("string"))
    .withColumn("embedded_at", F.lit(None).cast("timestamp"))
)
 

# COMMAND ----------


from delta.tables import DeltaTable
 
if not spark.catalog.tableExists(GOLD_TABLE):
    gold_source.write.format("delta").saveAsTable(GOLD_TABLE)
    print(f"created {GOLD_TABLE} with {spark.table(GOLD_TABLE).count()} rows")
else:
    gold = DeltaTable.forName(spark, GOLD_TABLE)
    (gold.alias("t")
        .merge(gold_source.alias("s"), "t.work_id = s.work_id")
        # Note what is NOT updated: embedding, model_name, embedded_at.
        # A matched row keeps the vector it already has, so reruns only embed
        # genuinely new papers.
        .whenMatchedUpdate(set={
            "title": "s.title",
            "abstract": "s.abstract",
            "embed_text": "s.embed_text",
            "cited_by_count": "s.cited_by_count",
            "oa_url": "s.oa_url",
            "author_names": "s.author_names",
        })
        .whenNotMatchedInsertAll()
        .execute())
    print(f"merged - {GOLD_TABLE} now has {spark.table(GOLD_TABLE).count()} rows")

# COMMAND ----------

pending = spark.table(GOLD_TABLE).filter(F.col("embedding").isNull()).count()
print(f"rows awaiting embedding: {pending}")

# COMMAND ----------

# DBTITLE 1,Stage the model in a Volume
import os
from sentence_transformers import SentenceTransformer

MODEL_VOLUME_PATH = "/Volumes/workspace/research_copilot/raw/models/all-MiniLM-L6-v2"

if not os.path.exists(os.path.join(MODEL_VOLUME_PATH, "config.json")):
    dbutils.fs.mkdirs(MODEL_VOLUME_PATH.replace("/Volumes", "dbfs:/Volumes"))
    _m = SentenceTransformer(EMBEDDING_MODEL)
    _m.save(MODEL_VOLUME_PATH)
    del _m
    print(f"staged model to {MODEL_VOLUME_PATH}")
else:
    print(f"model already staged at {MODEL_VOLUME_PATH}")

print(os.listdir(MODEL_VOLUME_PATH))

# COMMAND ----------

# DBTITLE 1,Embed - distributed pandas UDF
import pandas as pd
from typing import Iterator
from pyspark.sql.functions import pandas_udf
from pyspark.sql.types import ArrayType, FloatType

MODEL_PATH_BC = MODEL_VOLUME_PATH


@pandas_udf(ArrayType(FloatType()))
def embed_udf(batches: Iterator[pd.Series]) -> Iterator[pd.Series]:
    """Iterator-of-Series form so the model loads once per task, not once per batch.

    Loads from a UC Volume rather than the HF hub. Worker HOME is read-only on
    serverless, so any library that tries to write a download cache there fails
    with Errno 30 - hence the tempdir redirects below as well.
    """
    import os
    import tempfile

    tmp = tempfile.gettempdir()
    os.environ["HF_HOME"] = os.path.join(tmp, "hf")
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["SENTENCE_TRANSFORMERS_HOME"] = os.path.join(tmp, "st")

    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(MODEL_PATH_BC)
    for batch in batches:
        texts = batch.fillna("").astype(str).tolist()
        vectors = model.encode(
            texts,
            batch_size=32,
            show_progress_bar=False,
            normalize_embeddings=True,
            convert_to_numpy=True,
        )
        yield pd.Series([v.tolist() for v in vectors])

to_embed = spark.table(GOLD_TABLE).filter(F.col("embedding").isNull())
 
 

# COMMAND ----------


if LIMIT_ROWS > 0:
    to_embed = to_embed.orderBy(F.col("cited_by_count").desc()).limit(LIMIT_ROWS)
    print(f"SMOKE RUN - embedding the {LIMIT_ROWS} most-cited unembedded papers")
 
to_embed = to_embed.select("work_id", "embed_text").repartition(EMBED_PARTITIONS)
 
embedded = (
    to_embed
    .withColumn("embedding", embed_udf(F.col("embed_text")))
    .withColumn("model_name", F.lit(EMBEDDING_MODEL))
    .withColumn("embedded_at", F.current_timestamp())
    .select("work_id", "embedding", "model_name", "embedded_at")
)

# COMMAND ----------

# Materialise before the MERGE so the UDF runs exactly once. Without this, Spark
# would re-evaluate it during the merge's scan and you would pay for embedding twice.
STAGING = f"{GOLD_TABLE}_embed_staging"
embedded.write.format("delta").mode("overwrite").option("overwriteSchema", "true").saveAsTable(STAGING)
staged_n = spark.table(STAGING).count()
print(f"embedded {staged_n} rows")
display(spark.table(STAGING).select("work_id", F.size("embedding").alias("dim"), "model_name").limit(5))

# COMMAND ----------

gold = DeltaTable.forName(spark, GOLD_TABLE)

# COMMAND ----------

(gold.alias("t")
    .merge(spark.table(STAGING).alias("s"), "t.work_id = s.work_id")
    .whenMatchedUpdate(set={
        "embedding": "s.embedding",
        "model_name": "s.model_name",
        "embedded_at": "s.embedded_at",
    })
    .execute())

# COMMAND ----------


done = spark.table(GOLD_TABLE).filter(F.col("embedding").isNotNull()).count()
remaining = spark.table(GOLD_TABLE).filter(F.col("embedding").isNull()).count()
print(f"gold embedded: {done}   remaining: {remaining}")

# COMMAND ----------

from sentence_transformers import SentenceTransformer

model = SentenceTransformer(MODEL_VOLUME_PATH)
print("max_seq_length:", model.max_seq_length)

sample = (spark.table(GOLD_TABLE)
          .select("embed_text")
          .sample(0.05, seed=42)
          .limit(500)
          .toPandas()["embed_text"].tolist())

tok = model.tokenizer
lens = [len(tok.encode(t)) for t in sample]

import numpy as np
a = np.array(lens)
print(f"tokens  p50={np.percentile(a,50):.0f}  p90={np.percentile(a,90):.0f}  max={a.max()}")
print(f"truncated at {model.max_seq_length}: {(a > model.max_seq_length).mean():.1%} of documents")
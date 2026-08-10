# Databricks notebook source
dbutils.library.restartPython()


# COMMAND ----------

dbutils.widgets.text("mailto", "padiyaryash2019@gmail.com", "OpenAlex polite-pool email")
dbutils.widgets.text("concept_id", "C119857082", "OpenAlex concept id")
dbutils.widgets.text("from_date", "2023-01-01", "Min publication date")
dbutils.widgets.text("max_pages", "100", "Max pages to pull (200 works each)")
dbutils.widgets.text("volume_path", "/Volumes/workspace/research_copilot/raw", "Landing volume")

# COMMAND ----------

dbutils.widgets.text("bronze_table", "workspace.research_copilot.bronze_openalex_works", "Bronze table")

# COMMAND ----------

MAILTO = dbutils.widgets.get("mailto")
CONCEPT_ID = dbutils.widgets.get("concept_id")
FROM_DATE = dbutils.widgets.get("from_date")
MAX_PAGES = int(dbutils.widgets.get("max_pages"))
VOLUME_PATH = dbutils.widgets.get("volume_path")
BRONZE_TABLE = dbutils.widgets.get("bronze_table")

# COMMAND ----------

PER_PAGE = 200


# COMMAND ----------

assert "@" in MAILTO and "example.com" not in MAILTO, \
    "Set a real email in the mailto widget - OpenAlex throttles anonymous traffic"

# COMMAND ----------

print(f"concept={CONCEPT_ID} from={FROM_DATE} max_pages={MAX_PAGES}")
print(f"landing={VOLUME_PATH}")
print(f"bronze={BRONZE_TABLE}")

# COMMAND ----------

import json
import time
import requests
from datetime import datetime, timezone

# COMMAND ----------

SELECT_FIELDS = ",".join([
    "id",
    "doi",
    "title",
    "publication_year",
    "publication_date",
    "cited_by_count",
    "abstract_inverted_index",
    "authorships",
    "primary_topic",
    "open_access",
    "referenced_works",
    "type",
    "language",
])
 

# COMMAND ----------

FILTERS = ",".join([
    f"concepts.id:{CONCEPT_ID}",
    f"from_publication_date:{FROM_DATE}",
    "has_abstract:true",
    "is_oa:true",
])

# COMMAND ----------

run_ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
landing_dir = f"{VOLUME_PATH}/openalex/run_ts={run_ts}"
dbutils.fs.mkdirs(landing_dir)

# COMMAND ----------

session = requests.Session()
session.headers.update({"User-Agent": f"databricks-capstone (mailto:{MAILTO})"})

# COMMAND ----------

def fetch_page(cursor: str) -> dict:
    """One OpenAlex request with a small retry for transient 429/5xx."""
    params = {
        "filter": FILTERS,
        "select": SELECT_FIELDS,
        "per-page": PER_PAGE,
        "cursor": cursor,
        "mailto": MAILTO,
    }
    for attempt in range(4):
        resp = session.get("https://api.openalex.org/works", params=params, timeout=60)
        if resp.status_code in (429, 500, 502, 503, 504):
            wait = 2 ** attempt
            print(f"  status {resp.status_code}, retrying in {wait}s")
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError("OpenAlex request failed after 4 attempts")
 

# COMMAND ----------

cursor = "*"
page = 0
total_written = 0
started = time.time()
 
while cursor and page < MAX_PAGES:
    body = fetch_page(cursor)
    results = body.get("results", [])
    if not results:
        break
 
    # One NDJSON file per page. Spark reads newline-delimited JSON natively,
    # which is far cheaper to parse than multiLine JSON arrays.
    out_path = f"{landing_dir}/page_{page:05d}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        for rec in results:
            f.write(json.dumps(rec) + "\n")
 
    total_written += len(results)
    cursor = body["meta"].get("next_cursor")
    page += 1
 
    if page % 10 == 0:
        elapsed = time.time() - started
        print(f"page {page}: {total_written} works written ({elapsed:.0f}s elapsed)")
 
    # OpenAlex allows 10 req/s. Staying near 6 keeps us comfortably polite.
    time.sleep(0.15)
 
print(f"DONE - {total_written} works across {page} pages in {time.time() - started:.0f}s")
print(f"landed at {landing_dir}")

# COMMAND ----------

# DBTITLE 1,Load bronze table with deduped columns
# DBTITLE 1,Land raw JSON into bronze Delta
from pyspark.sql import functions as F
from pyspark.sql.types import (
    StructType, StructField, StringType, IntegerType, LongType,
    BooleanType, ArrayType, MapType, DoubleType,
)

# Explicit schema. The critical piece is abstract_inverted_index as a MapType -
# its keys are abstract words, so inference would turn every distinct word into
# a column and collide on case (Artificial vs artificial).
topic_ref = StructType([
    StructField("id", StringType()),
    StructField("display_name", StringType()),
])

openalex_schema = StructType([
    StructField("id", StringType()),
    StructField("doi", StringType()),
    StructField("title", StringType()),
    StructField("publication_year", IntegerType()),
    StructField("publication_date", StringType()),
    StructField("cited_by_count", LongType()),
    StructField("abstract_inverted_index", MapType(StringType(), ArrayType(IntegerType()))),
    StructField("authorships", ArrayType(StructType([
        StructField("author_position", StringType()),
        StructField("raw_author_name", StringType()),
        StructField("is_corresponding", BooleanType()),
        StructField("author", StructType([
            StructField("id", StringType()),
            StructField("display_name", StringType()),
            StructField("orcid", StringType()),
        ])),
        StructField("institutions", ArrayType(StructType([
            StructField("id", StringType()),
            StructField("display_name", StringType()),
            StructField("country_code", StringType()),
            StructField("type", StringType()),
        ]))),
    ]))),
    StructField("primary_topic", StructType([
        StructField("id", StringType()),
        StructField("display_name", StringType()),
        StructField("score", DoubleType()),
        StructField("subfield", topic_ref),
        StructField("field", topic_ref),
        StructField("domain", topic_ref),
    ])),
    StructField("open_access", StructType([
        StructField("is_oa", BooleanType()),
        StructField("oa_status", StringType()),
        StructField("oa_url", StringType()),
        StructField("any_repository_has_fulltext", BooleanType()),
    ])),
    StructField("referenced_works", ArrayType(StringType())),
    StructField("type", StringType()),
    StructField("language", StringType()),
])

# COMMAND ----------

bronze_df = (
    spark.read
        .schema(openalex_schema)
        .option("multiLine", "false")
        .json(f"{VOLUME_PATH}/openalex/")
        .withColumn("_source_file", F.col("_metadata.file_path"))
        .withColumn("_ingested_at", F.current_timestamp())
)

(bronze_df.write
    .format("delta")
    .mode("overwrite")
    .saveAsTable(BRONZE_TABLE))

print(f"bronze rows: {spark.table(BRONZE_TABLE).count()}")

# COMMAND ----------

display(df.select("id", "title", "publication_year", "cited_by_count", "primary_topic").limit(20))
 
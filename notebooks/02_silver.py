# Databricks notebook source
dbutils.widgets.text("bronze_table", "workspace.research_copilot.bronze_openalex_works", "Bronze table")
dbutils.widgets.text("silver_table", "workspace.research_copilot.silver_papers", "Silver papers table")
dbutils.widgets.text("authors_table", "workspace.research_copilot.silver_paper_authors", "Silver authors table")
dbutils.widgets.text("min_abstract_words", "40", "Min abstract word count")
dbutils.widgets.text("max_author_names", "8", "Authors to keep in the denormalised string")

# COMMAND ----------

BRONZE_TABLE = dbutils.widgets.get("bronze_table")
SILVER_TABLE = dbutils.widgets.get("silver_table")
AUTHORS_TABLE = dbutils.widgets.get("authors_table")
MIN_ABSTRACT_WORDS = int(dbutils.widgets.get("min_abstract_words"))
MAX_AUTHOR_NAMES = int(dbutils.widgets.get("max_author_names"))
 

# COMMAND ----------

from pyspark.sql import functions as F
from pyspark.sql import Window
 

# COMMAND ----------


# DBTITLE 1,Reconstruct abstracts from the inverted index
# OpenAlex ships {word: [positions]} instead of abstract text, deliberately, to
# sidestep publisher copyright on abstracts.
#
# The rebuild:
#   map_entries  -> array<struct<key:word, value:array<pos>>>
#   inner transform -> for each position, emit struct(pos, word)
#   flatten      -> one flat array<struct<pos, word>>
#   array_sort   -> sorts by the FIRST struct field, so pos leads deliberately
#   transform    -> drop back to just the words
#   array_join   -> the abstract
ABSTRACT_EXPR = """
array_join(
  transform(
    array_sort(
      flatten(
        transform(
          map_entries(abstract_inverted_index),
          e -> transform(e.value, p -> struct(p as pos, e.key as word))
        )
      )
    ),
    s -> s.word
  ),
  ' '
)
"""

# COMMAND ----------


with_abstract = (
    bronze
    .withColumn("abstract", F.expr(ABSTRACT_EXPR))
    .withColumn("abstract_word_count", F.size(F.split(F.col("abstract"), r"\s+")))
)
 
display(
    with_abstract
    .select("title", "abstract_word_count", "abstract","id")
    .limit(5)
)

# COMMAND ----------

 
# DBTITLE 1,Flatten, clean, dedupe
# OpenAlex ids are full URLs (https://openalex.org/W4313654696). Strip to the bare
# id - it is shorter, it is what users recognise, and it is the Lakebase primary key.
cleaned = (
    with_abstract
    .withColumn("work_id", F.regexp_extract(F.col("id"), r"(W\d+)$", 1))
    .withColumn("doi_clean", F.regexp_replace(F.col("doi"), r"^https?://doi\.org/", ""))
    .withColumn("author_names_arr",
                F.expr("transform(authorships, a -> coalesce(a.author.display_name, a.raw_author_name))"))
    .withColumn("n_authors", F.size(F.col("author_names_arr")))
    .withColumn("author_names",
                F.array_join(F.slice(F.col("author_names_arr"), 1, MAX_AUTHOR_NAMES), ", "))
    # Papers with 100 authors would otherwise produce an unusable string. Flag the
    # truncation rather than silently dropping names.
    .withColumn("author_names",
                F.when(F.col("n_authors") > MAX_AUTHOR_NAMES,
                       F.concat(F.col("author_names"), F.lit(" et al.")))
                 .otherwise(F.col("author_names")))
    .withColumn("primary_topic_name", F.col("primary_topic.display_name"))
    .withColumn("primary_field", F.col("primary_topic.field.display_name"))
    .withColumn("primary_subfield", F.col("primary_topic.subfield.display_name"))
    .withColumn("topic_score", F.col("primary_topic.score"))
    .withColumn("oa_url", F.coalesce(F.col("open_access.oa_url"), F.col("doi")))
    .withColumn("oa_status", F.col("open_access.oa_status"))
    .withColumn("n_references", F.coalesce(F.size(F.col("referenced_works")), F.lit(0)))
)

# COMMAND ----------

# Quality gate. Short abstracts embed to noise and pollute semantic search.
quality = (
    cleaned
    .filter(F.col("work_id") != "")
    .filter(F.col("title").isNotNull() & (F.length(F.trim(F.col("title"))) > 0))
    .filter(F.col("abstract").isNotNull())
    .filter(F.col("abstract_word_count") >= MIN_ABSTRACT_WORDS)
)
 

# COMMAND ----------

dedupe_window = Window.partitionBy("work_id").orderBy(F.col("_ingested_at").desc())


# COMMAND ----------

silver = (
    quality
    .withColumn("_rn", F.row_number().over(dedupe_window))
    .filter(F.col("_rn") == 1)
    .drop("_rn", "author_names_arr", "abstract_inverted_index")
    .select(
        "work_id",
        F.col("doi_clean").alias("doi"),
        "title",
        "abstract",
        "abstract_word_count",
        "publication_year",
        "publication_date",
        "cited_by_count",
        "oa_url",
        "oa_status",
        "primary_topic_name",
        "primary_field",
        "primary_subfield",
        "topic_score",
        "author_names",
        "n_authors",
        "n_references",
        "referenced_works",
        "type",
        "language",
        "_source_file",
        "_ingested_at",
    )
)

# COMMAND ----------

bronze_n = bronze.count()
silver_n = spark.table(SILVER_TABLE).count()
 
dq = (
    cleaned.select(
        F.count("*").alias("bronze_rows"),
        F.sum(F.when(F.col("work_id") == "", 1).otherwise(0)).alias("bad_work_id"),
        F.sum(F.when(F.col("title").isNull(), 1).otherwise(0)).alias("null_title"),
        F.sum(F.when(F.col("abstract_word_count") < MIN_ABSTRACT_WORDS, 1).otherwise(0)).alias("short_abstract"),
        F.sum(F.when(F.col("oa_url").isNull(), 1).otherwise(0)).alias("null_oa_url"),
        F.sum(F.when(F.col("primary_topic_name").isNull(), 1).otherwise(0)).alias("null_topic"),
    )
)
display(dq)
 
print(f"bronze {bronze_n} -> silver {silver_n}  (dropped {bronze_n - silver_n}, "
      f"{100 * (bronze_n - silver_n) / bronze_n:.2f}%)")
 

# COMMAND ----------

authors = (
    quality
    .select(
        "work_id",
        F.explode("authorships").alias("a"),
    )
    .select(
        "work_id",
        F.regexp_extract(F.col("a.author.id"), r"(A\d+)$", 1).alias("author_id"),
        F.coalesce(F.col("a.author.display_name"), F.col("a.raw_author_name")).alias("author_name"),
        F.col("a.author_position").alias("author_position"),
        F.col("a.is_corresponding").alias("is_corresponding"),
        F.expr("try_element_at(a.institutions, 1).display_name").alias("primary_institution"),
        F.expr("try_element_at(a.institutions, 1).country_code").alias("institution_country"),
    )
    .filter(F.col("author_name").isNotNull())
    .dropDuplicates(["work_id", "author_id", "author_name"])
)
 

# COMMAND ----------

authors.write.format("delta").mode("overwrite").option("overwriteSchema", "true").saveAsTable(AUTHORS_TABLE)

# COMMAND ----------

spark.sql(f"OPTIMIZE {SILVER_TABLE} ZORDER BY (work_id)")
spark.sql(f"OPTIMIZE {AUTHORS_TABLE} ZORDER BY (work_id)")
 
# Databricks notebook source
# Databricks notebook source
# MAGIC %md
# MAGIC # 07 - The agent loop
# MAGIC
# MAGIC First time an LLM enters the system. Everything underneath is already proven:
# MAGIC retrieval in notebook 05, the seven tools by direct call in notebook 06.
# MAGIC
# MAGIC So anything that goes wrong here is the model's doing - wrong tool, wrong
# MAGIC arguments, or no call at all. That is the whole reason for the ordering.
# MAGIC
# MAGIC Imports `app/agent/tools.py` and `app/agent/loop.py` directly, so this notebook
# MAGIC tests the same code the Flask app will run.

# COMMAND ----------

# MAGIC %pip uninstall -y psycopg2 psycopg2-binary
# MAGIC %pip install -q sentence-transformers 'databricks-sdk>=0.30.0' openai

# COMMAND ----------

dbutils.library.restartPython()

# COMMAND ----------

# DBTITLE 1,Config - env vars, because tools.py reads os.environ not dbutils
dbutils.widgets.text("repo_path", "/Workspace/Users/<you>/research-copilot", "Repo root in workspace")
dbutils.widgets.text("model_endpoint", "databricks-meta-llama-3-3-70b-instruct", "Serving endpoint")
dbutils.widgets.text("model_volume_path", "/Volumes/workspace/research_copilot/raw/models/all-MiniLM-L6-v2", "Embedding model path")
dbutils.widgets.text("lakebase_secret_scope", "database", "Secret scope")
dbutils.widgets.text("lakebase_secret_key", "lakebase-url", "Secret key")

import os
import sys

REPO_PATH = dbutils.widgets.get("repo_path")

os.environ["LAKEBASE_URL"] = dbutils.secrets.get(
    scope=dbutils.widgets.get("lakebase_secret_scope"),
    key=dbutils.widgets.get("lakebase_secret_key"),
)
os.environ["MODEL_PATH"] = dbutils.widgets.get("model_volume_path")
os.environ["MODEL_ENDPOINT"] = dbutils.widgets.get("model_endpoint")

# Import the app package rather than redefining anything. If this path is wrong
# the import below fails loudly, which is what you want.
if REPO_PATH not in sys.path:
    sys.path.insert(0, REPO_PATH)

from app.agent import tools as T
from app.agent.loop import run_agent, SYSTEM_PROMPT

print("tools loaded:", ", ".join(T.TOOL_REGISTRY))

# COMMAND ----------

# DBTITLE 1,Smoke test the imported module before involving the model
r = T.search_papers("parameter efficient fine tuning", top_k=3)
assert r["success"], r
print("search OK:", r["count"], "papers")
print(" ", r["papers"][0]["title"][:80])

# COMMAND ----------

# DBTITLE 1,OpenAI-compatible client for the serving endpoint
from databricks.sdk import WorkspaceClient

w = WorkspaceClient()
client = w.serving_endpoints.get_open_ai_client()

# Plain call, no tools - confirms auth and endpoint name before adding complexity.
resp = client.chat.completions.create(
    model=os.environ["MODEL_ENDPOINT"],
    messages=[{"role": "user", "content": "Reply with the single word: ready"}],
    max_tokens=10,
)
print("endpoint OK:", resp.choices[0].message.content)


# COMMAND ----------

# DBTITLE 1,Does the model call a tool at all
import json

USER_ID = None
with T.db() as (conn, cur):
    cur.execute("SELECT user_id FROM users WHERE email = %s", ("test@example.com",))
    row = cur.fetchone()
    USER_ID = row["user_id"] if row else None
print("test user_id:", USER_ID)

out = run_agent(
    "What research is there on low-rank adaptation for fine-tuning language models?",
    client=client, user_id=USER_ID, user_email="test@example.com", verbose=True,
)
print("\n--- REPLY ---")
print(out["reply"])

# COMMAND ----------

# DBTITLE 1,Multi-step - search then write
out = run_agent(
    "Find me the 4 most influential papers on parameter-efficient fine-tuning, "
    "make a collection called 'PEFT deep dive', add them all to it, and order them "
    "into a reading plan.",
    client=client, user_id=USER_ID, user_email="test@example.com", verbose=True,
)
print("\n--- REPLY ---")
print(out["reply"])
print("\ntools used:", [t["tool"] for t in out["tool_calls"]])

# COMMAND ----------

# DBTITLE 1,Does it refuse to hallucinate outside the corpus
# The corpus is ML papers from 2023 onward. A well-behaved agent says so rather
# than inventing a plausible-sounding citation.
out = run_agent(
    "Summarise the key findings of Einstein's 1905 paper on special relativity.",
    client=client, user_id=USER_ID, verbose=True,
)
print("\n--- REPLY ---")
print(out["reply"])

# COMMAND ----------

# DBTITLE 1,Does it recover from a tool error
out = run_agent(
    "Add paper W9999999999 to collection 1.",
    client=client, user_id=USER_ID, verbose=True,
)
print("\n--- REPLY ---")
print(out["reply"])

# COMMAND ----------

# DBTITLE 1,Multi-turn - does context carry
turn1 = run_agent(
    "Find 3 papers about knowledge distillation.",
    client=client, user_id=USER_ID, verbose=True,
)
print("--- TURN 1 ---")
print(turn1["reply"])

# Drop the system prompt when replaying history - run_agent adds its own.
history = [m for m in turn1["messages"] if m["role"] != "system"]

turn2 = run_agent(
    "Save a note on the second one saying I want to reread it.",
    client=client, history=history, user_id=USER_ID,
    session_id=turn1["session_id"], verbose=True,
)
print("\n--- TURN 2 ---")
print(turn2["reply"])

# COMMAND ----------

# DBTITLE 1,What actually happened, from the trace log
with T.db() as (conn, cur):
    cur.execute("""
        SELECT tool_name,
               count(*) AS calls,
               sum(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) AS failures,
               round(avg(duration_ms)) AS avg_ms
        FROM agent_trace_logs
        WHERE created_at > now() - interval '1 hour'
        GROUP BY tool_name ORDER BY calls DESC
    """)
    print(f"{'tool':<22} {'calls':>6} {'fails':>6} {'avg ms':>8}")
    for r in cur.fetchall():
        print(f"{r['tool_name']:<22} {r['calls']:>6} {r['failures']:>6} {r['avg_ms']:>8}")

    cur.execute("""
        SELECT tool_name, error, parameters
        FROM agent_trace_logs
        WHERE error IS NOT NULL AND created_at > now() - interval '1 hour'
        ORDER BY id DESC LIMIT 10
    """)
    print("\n--- recent failures ---")
    for r in cur.fetchall():
        print(f"  {r['tool_name']}: {r['error'][:110]}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## What to judge
# MAGIC
# MAGIC 1. **Did it search before answering?** If it answered the LoRA question with
# MAGIC    no `search_papers` call, it is drawing on training data, not your corpus.
# MAGIC    That is a hallucination risk and a system-prompt problem.
# MAGIC 2. **Did the multi-step request chain correctly?** Expect
# MAGIC    search -> create_collection -> add_to_collection x4 -> build_reading_plan.
# MAGIC    Papers added without a preceding search means invented work_ids.
# MAGIC 3. **Did it decline the Einstein question** rather than inventing a citation?
# MAGIC 4. **Did it read the error** on the bad work_id and explain, rather than
# MAGIC    retrying the identical call until the rounds ran out?
# MAGIC 5. **Did turn 2 know what "the second one" meant?**
# MAGIC
# MAGIC Failures here are almost always the tool descriptions or the system prompt,
# MAGIC not the functions. Both are text, and both are cheap to iterate on.
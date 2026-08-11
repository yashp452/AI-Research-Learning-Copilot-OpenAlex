"""Research and Learning Copilot - Flask app.

Runs on Databricks Apps. All agent behaviour lives in agent/tools.py and
agent/loop.py; this module is HTTP plumbing and identity resolution.
"""

from __future__ import annotations

import os
import uuid
from collections import OrderedDict

from flask import Flask, jsonify, render_template, request

from agent import tools as T
from agent.loop import run_agent

app = Flask(__name__)

LOCAL_DEV = os.environ.get("LOCAL_DEV") == "1"
MAX_SESSIONS = 200
MAX_HISTORY_MESSAGES = 30

# In-process conversation history. Deliberately not persisted: on Free Edition
# this app runs as a single worker, and durable chat history is not part of the
# brief. If it ever needs to survive a restart it belongs in Lakebase, not here.
_SESSIONS: "OrderedDict[str, list]" = OrderedDict()

_client = None


class DatabricksOpenAIAdapter:
    """Adapter to make WorkspaceClient look like an OpenAI client.
    
    This handles service principal authentication automatically through the SDK.
    """
    def __init__(self, workspace_client):
        self.w = workspace_client
        self.chat = self
        self.completions = self
    
    def create(self, model, messages, tools=None, tool_choice="auto", **kwargs):
        """Translate OpenAI chat.completions.create to Databricks serving endpoint query."""
        from types import SimpleNamespace
        
        # Build the request payload in OpenAI format
        payload = {
            "messages": messages,
            "max_tokens": kwargs.get("max_tokens", 1200),
            "temperature": kwargs.get("temperature", 0.1),
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice
        
        # Use the SDK's query method with inputs parameter
        # Foundation Model endpoints expect the payload as 'inputs'
        response = self.w.serving_endpoints.query(name=model, inputs=payload)
        
        # Convert to OpenAI-style response
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(
                    role="assistant",
                    content=response.choices[0].message.content if hasattr(response.choices[0].message, 'content') else None,
                    tool_calls=getattr(response.choices[0].message, 'tool_calls', None)
                )
            )]
        )


def llm_client():
    """Lazy so the app can boot and serve /health even if the endpoint is down."""
    global _client
    if _client is None:
        from databricks.sdk import WorkspaceClient
        
        # Wrap WorkspaceClient in an OpenAI-compatible adapter
        # The SDK handles service principal auth automatically
        _client = DatabricksOpenAIAdapter(WorkspaceClient())
    return _client


def current_user():
    """Resolve the signed-in user.

    Databricks Apps injects the authenticated identity as forwarded headers. The
    local fallback is gated behind LOCAL_DEV because a silent default would
    quietly merge every user's collections into one account in production.
    """
    email = request.headers.get("X-Forwarded-Email")
    if not email:
        if not LOCAL_DEV:
            raise RuntimeError("No X-Forwarded-Email header and LOCAL_DEV is not set")
        email = "test@example.com"

    display = request.headers.get("X-Forwarded-Preferred-Username") or email.split("@")[0]
    result = T.get_or_create_user(email, display)
    if not result.get("success"):
        raise RuntimeError(result.get("error", "could not resolve user"))
    return result["user_id"], email, display


def remember(session_id, messages):
    trimmed = [m for m in messages if m["role"] != "system"][-MAX_HISTORY_MESSAGES:]
    _SESSIONS[session_id] = trimmed
    _SESSIONS.move_to_end(session_id)
    while len(_SESSIONS) > MAX_SESSIONS:
        _SESSIONS.popitem(last=False)


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    user_id, email, display = current_user()
    with T.db() as (conn, cur):
        cur.execute("SELECT count(*) AS n FROM papers WHERE embedding IS NOT NULL")
        corpus = cur.fetchone()["n"]
        cur.execute("SELECT min(publication_yr) AS lo, max(publication_yr) AS hi FROM papers")
        years = cur.fetchone()
    return render_template(
        "index.html",
        user_id=user_id,
        user_name=display,
        corpus_size=corpus,
        year_lo=years["lo"],
        year_hi=years["hi"],
    )


@app.route("/health")
def health():
    try:
        with T.db() as (conn, cur):
            cur.execute("SELECT 1")
        return jsonify({"status": "ok"})
    except Exception as e:  # noqa: BLE001
        return jsonify({"status": "degraded", "error": str(e)}), 503


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@app.route("/api/chat", methods=["POST"])
def chat():
    user_id, email, _ = current_user()
    payload = request.get_json(silent=True) or {}
    message = (payload.get("message") or "").strip()
    if not message:
        return jsonify({"error": "Type a question first."}), 400

    session_id = payload.get("session_id") or str(uuid.uuid4())
    history = _SESSIONS.get(session_id, [])

    try:
        out = run_agent(
            message,
            client=llm_client(),
            history=history,
            user_id=user_id,
            user_email=email,
            session_id=session_id,
        )
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"The model call failed: {e}"}), 502

    remember(session_id, out["messages"])

    # Surface the papers the agent actually retrieved so the UI can render cards
    # rather than making the user parse titles out of prose.
    papers, actions = [], []
    seen = set()
    for call in out["tool_calls"]:
        res = call["result"]
        if call["tool"] == "search_papers" and res.get("success"):
            for p in res.get("papers", []):
                if p["work_id"] not in seen:
                    seen.add(p["work_id"])
                    papers.append(p)
        elif call["tool"] != "get_paper":
            actions.append({
                "tool": call["tool"],
                "ok": bool(res.get("success")),
                "message": res.get("message") or res.get("error"),
            })

    return jsonify({
        "reply": out["reply"],
        "session_id": session_id,
        "papers": papers[:12],
        "actions": actions,
        "exhausted": out.get("exhausted", False),
    })


@app.route("/api/collections")
def collections():
    user_id, _, _ = current_user()
    with T.db() as (conn, cur):
        cur.execute(
            """
            SELECT c.collection_id, c.name, count(cp.work_id) AS paper_count
            FROM collections c
            LEFT JOIN collection_papers cp ON cp.collection_id = c.collection_id
            WHERE c.user_id = %s
            GROUP BY c.collection_id, c.name
            ORDER BY c.created_at DESC
            """,
            (user_id,),
        )
        rows = [dict(r) for r in cur.fetchall()]

        for r in rows:
            cur.execute(
                """
                SELECT cp.seq_no, p.work_id, p.title, p.author_names,
                       p.publication_yr, p.cited_by_count, p.oa_url,
                       coalesce(rp.status, 'not_started') AS status
                FROM collection_papers cp
                JOIN papers p ON p.work_id = cp.work_id
                LEFT JOIN reading_progress rp
                       ON rp.work_id = cp.work_id AND rp.user_id = %s
                WHERE cp.collection_id = %s
                ORDER BY cp.seq_no NULLS LAST, cp.added_at
                """,
                (user_id, r["collection_id"]),
            )
            r["papers"] = [dict(p) for p in cur.fetchall()]

    return jsonify({"collections": rows})


@app.route("/api/notes")
def notes():
    user_id, _, _ = current_user()
    with T.db() as (conn, cur):
        cur.execute(
            """
            SELECT n.note_id, n.note_text, n.created_at, n.work_id, p.title
            FROM notes n
            LEFT JOIN papers p ON p.work_id = n.work_id
            WHERE n.user_id = %s
            ORDER BY n.created_at DESC
            LIMIT 40
            """,
            (user_id,),
        )
        rows = [dict(r) for r in cur.fetchall()]
    return jsonify({"notes": rows})


@app.route("/api/progress", methods=["POST"])
def progress():
    """Manual status change from the UI. Same tool the agent calls, so both paths
    go through identical validation."""
    user_id, email, _ = current_user()
    payload = request.get_json(silent=True) or {}
    result = T.mark_progress(
        user_id,
        payload.get("work_id"),
        payload.get("status"),
        user_email=email,
    )
    return jsonify(result), (200 if result.get("success") else 400)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), debug=LOCAL_DEV)
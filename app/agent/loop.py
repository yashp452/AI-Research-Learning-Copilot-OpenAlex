"""Tool-calling loop against a Databricks model serving endpoint.

Transport only. All behaviour lives in tools.py; this module decides when to stop
and how to shuttle messages back and forth.
"""

from __future__ import annotations

import json
import os
import uuid

from .tools import TOOL_SCHEMAS, call_tool

MODEL_ENDPOINT = os.environ.get("MODEL_ENDPOINT", "databricks-meta-llama-3-3-70b-instruct")
MAX_ROUNDS = 12  # a round is one model call plus its tool executions

SYSTEM_PROMPT = """You are a research and learning copilot with access to a corpus of \
open-access machine learning papers published from 2023 onward.

Rules you must follow:

- Never state a work_id, title, author or finding that did not come from a tool \
result. If you have not searched, search before answering.
- Cite papers as "Title (Authors, Year)" and include the oa_url when you have it.
- The corpus is machine learning papers from 2023 onward only. If the user asks \
about something outside that, say so plainly instead of guessing.
- Before adding papers to a collection, search first so you have real work_ids.
- When you take an action that changes something, say what you did in one short line.
- If a tool returns success: false, read the error and either correct the call or \
tell the user what went wrong. Do not silently retry the same thing.
- When you need to make several calls of the same kind (adding multiple papers, \
for example), issue them together in one turn rather than one at a time.
- work_id values are opaque identifiers, not searchable text. Never pass one to \
search_papers. If a work_id is rejected as not in the corpus, say so and offer to \
search by topic instead.
Be concise. Prefer a short answer with real citations over a long one without."""


def run_agent(user_message, client, history=None, user_id=1, user_email=None,
              session_id=None, max_rounds=MAX_ROUNDS, verbose=False):
    """Run one turn to completion.

    client   - an OpenAI-compatible client pointed at the serving endpoint
    history  - prior messages, excluding the system prompt
    Returns  - {"reply", "messages", "tool_calls", "session_id"}
    """
    session_id = session_id or str(uuid.uuid4())

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    if history:
        messages.extend(history)
    # The model has no idea who it is talking to, and several tools require a
    # user_id. Stating it in the turn is more reliable than hoping the model
    # infers it from the system prompt.
    messages.append({
        "role": "user",
        "content": f"[current user_id: {user_id}]\n\n{user_message}",
    })

    executed = []

    for round_no in range(max_rounds):
        response = client.chat.completions.create(
            model=MODEL_ENDPOINT,
            messages=messages,
            tools=TOOL_SCHEMAS,
            tool_choice="auto",
            temperature=0.1,  # tool selection should be near-deterministic
            max_tokens=1200,
        )
        choice = response.choices[0].message

        # No tool calls means the model is answering, so the loop is done.
        if not getattr(choice, "tool_calls", None):
            messages.append({"role": "assistant", "content": choice.content})
            return {
                "reply": choice.content,
                "messages": messages,
                "tool_calls": executed,
                "session_id": session_id,
            }

        # The assistant message carrying tool_calls must go back verbatim, and
        # every tool_call_id must get a matching tool message, or the next
        # request is malformed.
        messages.append({
            "role": "assistant",
            "content": choice.content or "",
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in choice.tool_calls
            ],
        })

        for tc in choice.tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError as e:
                # Malformed JSON from the model is recoverable - hand the parse
                # error back and let it retry.
                result = {"success": False, "error": f"Could not parse arguments: {e}"}
                args = {}
            else:
                result = call_tool(name, args, session_id=session_id, user_email=user_email)

            executed.append({"round": round_no + 1, "tool": name,
                             "arguments": args, "result": result})

            if verbose:
                ok = result.get("success")
                print(f"  [{round_no + 1}] {name}({json.dumps(args)[:90]}) -> success={ok}")

            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "name": name,
                "content": json.dumps(result, default=str)[:6000],
            })

    # Ran out of rounds. Return what we have rather than raising - a partial
    # answer beats a stack trace in a chat UI.
    return {
        "reply": ("I made several attempts but could not finish that. "
                  "Try narrowing the request."),
        "messages": messages,
        "tool_calls": executed,
        "session_id": session_id,
        "exhausted": True,
    }
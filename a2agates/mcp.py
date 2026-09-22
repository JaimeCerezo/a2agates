"""The mouth: an MCP server that lets an agent call another one.

``server.py`` is the ear -- it listens and wakes this machine's agent. This is
the other half: it gives an agent the ability to **call someone else** without
leaving its conversation.

A minimal MCP server exposing one tool. The agent says who to call and what to
ask; this builds the A2A request, attaches the credential and hands back the
answer.

**Why an MCP and not a documented curl**: because this way the agent cannot
call anyone who is not in its contacts. The list of allowed destinations stops
being a rule written in a prompt and becomes the only way to dial.

## What this version is not, yet

A **fixed-destination prototype**: the contact list is two environment
variables and there is exactly one friend. The finished shape splits this in
two, with a user boundary in the middle:

    agent  ->  [this MCP, no secrets]  ->  socket  ->  [daemon holding the keys]

Because today the token sits in the environment of a process the agent itself
started, which means **the agent could read it** -- and so could anything that
talks the agent into printing its environment. It measures what it measures:
enough to prove the call works, not enough to put in front of anything real.
See DESIGN.md.

## Wiring it to an agent

    claude --mcp-config config.json --allowedTools "mcp__a2agates__ask_agent"

where ``config.json`` declares this module as a ``stdio`` server.
"""

from __future__ import annotations

import os
import pathlib
import sys
import uuid

import httpx
from mcp.server.mcpserver import MCPServer

import sqlite3
from datetime import datetime, timezone

# The contact list. Two shapes, and the first one is the real one:
#
#   A2A_DB=/var/lib/a2agates/<phone>   -> read contacts.db, many destinations
#   A2A_FRIEND / A2A_URL / A2A_TOKEN   -> one fixed friend, the old way
#
# The database is what makes the contact list mean anything. With the
# environment, "who may this agent call" was three variables somebody set once;
# with the table it is a row that can be added, replaced and listed, and the
# agent still cannot dial anyone who is not in it -- because there is no way to
# pass an address to this tool, only a name to look up.
DB = os.environ.get("A2A_DB")
FRIEND = os.environ.get("A2A_FRIEND", "the other agent")
URL = os.environ.get("A2A_URL", "")
TIMEOUT = float(os.environ.get("A2A_TIMEOUT", "300"))


def _contacts() -> dict[str, dict]:
    """Everyone this phone may call, by name."""
    if not DB:
        return {}
    try:
        conn = sqlite3.connect(f"file:{DB}/contacts.db?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return {r["alias"]: dict(r) for r in conn.execute("SELECT * FROM contacts")}
    except sqlite3.Error:
        return {}


def _log(direction: str, **fields) -> int | None:
    """Record an outgoing call in this phone's own log.

    The half that was missing, and the reason it matters: on 2026-09-22 a call
    completed perfectly at the far end while the caller died collecting the
    answer. The work was done; the acknowledgement was lost. A row written
    before dialling turns that into something answerable -- an unfinished
    'out' row here, against a finished 'in' row there, joined by task_id.
    """
    if not DB:
        return None
    try:
        from . import db as db_mod

        return db_mod.begin(f"{DB}/contacts.db", direction=direction, **fields)
    except Exception:  # noqa: BLE001 - logging never breaks a call
        return None


def _read_token() -> str:
    """Prefer a file over an environment variable.

    A variable has to be written somewhere to get here -- a config file, a
    unit file, a shell command -- and it is visible in ``/proc/<pid>/environ``.
    A file can be handed over once, by whoever holds the credential, without it
    passing through a config file, a shell history or a conversation.

    The variable is still honoured, because it is the only thing that works
    when the caller is launched by something that cannot place files.
    """
    path = os.environ.get("A2A_TOKEN_FILE")
    if path:
        try:
            return pathlib.Path(path).read_text(encoding="utf-8").strip()
        except OSError as e:
            # Deliberately not fatal at import: the tool reports it as an
            # error the agent can read and relay, rather than dying silently
            # inside an MCP handshake nobody sees.
            print(f"a2agates: cannot read A2A_TOKEN_FILE: {e}", file=sys.stderr)
            return ""
    return os.environ.get("A2A_TOKEN", "")


TOKEN = _read_token()
CONTACTS = _contacts()
NAMES = sorted(CONTACTS) or ([FRIEND] if URL else [])

server = MCPServer(
    name="a2agates",
    instructions=(
        "Lets you phone another agent. You can only call the names in this "
        "list, and there is no way to pass an address instead: "
        + (", ".join(NAMES) if NAMES else "nobody is in the contact list yet.")
    ),
)


def _destination(who: str) -> tuple[str, str, str] | str:
    """Resolve a name to (name, url, token), or return an error to show."""
    if who in CONTACTS:
        row = CONTACTS[who]
        if not row.get("token"):
            return f"ERROR: «{who}» is in the contact list with no credential yet."
        return who, row["url"], row["token"]
    if URL and who in (FRIEND, ""):
        if not TOKEN:
            return "ERROR: no credential configured, cannot call anyone."
        return FRIEND, URL, TOKEN
    known = ", ".join(NAMES) or "nobody"
    return f"ERROR: «{who}» is not in the contact list. You may call: {known}."


@server.tool(
    name="ask_agent",
    title="Ask another agent",
    description=(
        "Call another agent by name and return its answer verbatim. The agent "
        "on the other end is a Claude session with its own project, its own "
        "memory and its own files: ask it what only it can know, not general "
        "knowledge. A call takes seconds, costs money and starts an agent on "
        "someone else's machine, so one well-formed question is worth more "
        "than three probes."
    ),
)
async def ask_agent(who: str, question: str) -> str:
    """Call ``who`` and return whatever they answer."""
    resolved = _destination(who)
    if isinstance(resolved, str):
        return resolved
    name, url, token = resolved

    from . import db as db_mod

    digest, excerpt = db_mod.request_digest(question)
    task = str(uuid.uuid4())
    # Written before dialling, not after: a caller that dies collecting the
    # answer would otherwise leave no trace of having called at all.
    row = _log("out", task_id=task, peer=name, request_sha256=digest,
               request_excerpt=excerpt, auth="token")

    def close(**fields):
        if row is not None and DB:
            db_mod.finish(f"{DB}/contacts.db", row, **fields)

    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "SendMessage",
        "params": {
            "message": {
                "role": "ROLE_USER",
                "parts": [{"text": question}],
                "messageId": task,
            }
        },
    }
    headers = {
        "Authorization": f"Bearer {token}",
        # Without this the server assumes protocol version 0.3 and rejects the
        # call with an error that does not say what it is complaining about.
        "A2A-Version": "1.0",
        "content-type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as http:
            response = await http.post(url, json=body, headers=headers)
    except httpx.TimeoutException:
        # Named separately because it is the most likely failure and the least
        # obvious: httpx timeout exceptions stringify to the empty string.
        close(outcome="timeout",
              note="may still be running at the far end; check before retrying")
        return (
            f"ERROR: {name} did not answer within {TIMEOUT:g}s. The agent may "
            "still be working -- and may still finish the job. Do NOT simply "
            "re-send anything that changes things; ask what it did."
        )
    except httpx.HTTPError as e:
        detail = str(e) or type(e).__name__
        close(outcome="unreachable", note=detail[:500])
        return f"ERROR: could not reach {name}: {detail}"

    if response.status_code == 401:
        close(outcome="rejected")
        return (
            f"ERROR: {name} rejected the credential (401). It may have expired, "
            "been revoked, or this machine may not be on its allowed list."
        )
    if response.status_code != 200:
        close(outcome=f"http_{response.status_code}")
        return f"ERROR: {name} answered HTTP {response.status_code}."

    data = response.json()
    if "error" in data:
        close(outcome="protocol_error", note=str(data["error"])[:500])
        return f"ERROR from the protocol: {data['error'].get('message')}"

    status = data.get("result", {}).get("task", {}).get("status", {})
    parts = (status.get("message") or {}).get("parts") or []
    text = "\n".join(p.get("text", "") for p in parts if p.get("text")).strip()

    # The cost is handed back on purpose: whoever places a call should know
    # what it spends -- including calls that failed, because those spent too.
    meta = (status.get("message") or {}).get("metadata") or {}
    cost = meta.get("cost_usd")
    close(outcome="ok" if status.get("state") == "TASK_STATE_COMPLETED" else "failed",
          cost_usd=cost, turns=meta.get("num_turns"),
          duration_ms=meta.get("duration_ms"))
    footer = f"\n\n[{name} · {cost:.4f} USD]" if cost else f"\n\n[{name}]"

    if status.get("state") != "TASK_STATE_COMPLETED":
        return f"ERROR: {name} could not answer. {text}" + footer

    return (text or "(empty answer)") + footer


@server.tool(
    name="list_contacts",
    title="Who you can call",
    description="List the agents this phone may call, with what is known about "
                "each. Cheap and local: it makes no call.",
)
async def list_contacts() -> str:
    if not NAMES:
        return "The contact list is empty. This phone cannot call anyone yet."
    lines = []
    for name in NAMES:
        row = CONTACTS.get(name)
        if row:
            cred = "credential held" if row.get("token") else "NO CREDENTIAL"
            lines.append(f"{name}  {row['url']}  [{cred}]"
                         + (f"  {row['note']}" if row.get("note") else ""))
        else:
            lines.append(f"{name}  {URL}  [credential held]")
    return "\n".join(lines)


def main() -> None:
    server.run(transport="stdio")


if __name__ == "__main__":
    main()

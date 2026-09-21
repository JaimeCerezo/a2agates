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

# The contact list of this version: one friend, read from the environment.
FRIEND = os.environ.get("A2A_FRIEND", "the other agent")
URL = os.environ.get("A2A_URL", "http://127.0.0.1:9110/")
TIMEOUT = float(os.environ.get("A2A_TIMEOUT", "300"))


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

server = MCPServer(
    name="a2agates",
    instructions=(
        f"Lets you phone another agent. Right now the contact list holds a "
        f"single entry: {FRIEND}."
    ),
)


@server.tool(
    name="ask_agent",
    title="Ask another agent",
    description=(
        f"Call the agent «{FRIEND}» and ask it a question, returning its "
        "answer verbatim. The agent on the other end is a Claude session with "
        "its own project, its own memory and its own files: ask it what only "
        "it can know, not general knowledge. The call takes a few seconds and "
        "costs money, so one well-formed question is worth more than three "
        "probes."
    ),
)
async def ask_agent(question: str) -> str:
    """Ask the agent in the contact list and return whatever it answers."""
    if not TOKEN:
        return "ERROR: no credential configured, cannot call anyone."

    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "SendMessage",
        "params": {
            "message": {
                "role": "ROLE_USER",
                "parts": [{"text": question}],
                "messageId": str(uuid.uuid4()),
            }
        },
    }
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        # Without this the server assumes protocol version 0.3 and rejects the
        # call with an error that does not say what it is complaining about.
        "A2A-Version": "1.0",
        "content-type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as http:
            response = await http.post(URL, json=body, headers=headers)
    except httpx.HTTPError as e:
        return f"ERROR: could not reach {FRIEND}: {e}"

    if response.status_code == 401:
        return f"ERROR: {FRIEND} rejected the credential (401)."
    if response.status_code != 200:
        return f"ERROR: {FRIEND} answered HTTP {response.status_code}."

    data = response.json()
    if "error" in data:
        return f"ERROR from the protocol: {data['error'].get('message')}"

    task = data.get("result", {}).get("task", {})
    status = task.get("status", {})
    parts = (status.get("message") or {}).get("parts") or []
    text = "\n".join(p.get("text", "") for p in parts if p.get("text")).strip()

    if status.get("state") != "TASK_STATE_COMPLETED":
        return f"{FRIEND} did not complete the task ({status.get('state')}): {text}"

    # The cost is handed back on purpose: whoever places a call should know
    # what it spends.
    meta = (status.get("message") or {}).get("metadata") or {}
    cost = meta.get("cost_usd")
    footer = f"\n\n[{FRIEND} · {cost:.4f} USD]" if cost else f"\n\n[{FRIEND}]"
    return (text or "(empty answer)") + footer


def main() -> None:
    server.run(transport="stdio")


if __name__ == "__main__":
    main()

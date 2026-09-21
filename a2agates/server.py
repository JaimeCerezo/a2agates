"""The ear: an A2A endpoint that wakes this machine's agent.

One server per agent. It listens on a port and, when a call arrives, runs the
``claude`` installed on this machine and returns whatever it answers.

Two deliberate properties, and they are the reason this exists rather than a
wrapper around something else:

  1. **It calls the machine's own ``claude``**, not one bundled inside an npm
     dependency. The agent that answers the phone has to be the same one that
     works in tmux, with its version, its memory and its configuration.
  2. **The card advertises the public address it is told**, not the interface
     it binds to. Without that, behind a proxy it publishes a number nobody
     can dial.

Everything about the protocol itself -- card, task states, artifacts, JSON-RPC
-- comes from the official A2A SDK. What lives here is the part nobody gives
away: how you reach the local agent.

Scope of this version: answer a call by starting a fresh agent, and keep the
thread if the caller comes back. It does not talk to an already-running tmux
session, it does not sign its card, and it has no callback webhook. See
DESIGN.md for what is decided and still unbuilt.
"""

from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import logging
import os
import sys
import uuid
from pathlib import Path

import uvicorn
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    Part,
    SecurityRequirement,
    Task,
    TaskState,
    TaskStatus,
)
from a2a.utils.constants import PROTOCOL_VERSION_CURRENT, TransportProtocol
from starlette.applications import Starlette
from starlette.middleware import Middleware

from . import __version__

log = logging.getLogger("a2agates.server")

BEARER = "bearer"

# What the agent inherits from the server's environment. Kept deliberately
# short: the rest of the server's environment is none of the agent's business.
# HOME is in, because that is where its memory and its credentials live.
_INHERITED = ("PATH", "HOME", "LANG", "TERM", "SHELL", "USER", "LOGNAME", "TZ")


# --------------------------------------------------------------------------
# The card: the phone number you publish
# --------------------------------------------------------------------------
def build_card(url: str, *, name: str, description: str, auth: bool) -> AgentCard:
    """Build the agent card served at ``/.well-known/agent-card.json``."""
    card = AgentCard(
        name=name,
        description=description,
        version=__version__,
        capabilities=AgentCapabilities(streaming=False, push_notifications=False),
        supported_interfaces=[
            AgentInterface(
                url=url,
                protocol_binding=TransportProtocol.JSONRPC,
                protocol_version=PROTOCOL_VERSION_CURRENT,
            )
        ],
        skills=[
            AgentSkill(
                id="ask",
                name="Ask the agent",
                description="Ask this agent about its own project: what it is "
                "working on, what is pending, what it knows about its machine.",
                tags=["agent", "query"],
                examples=["What is pending?", "Which project do you look after?"],
            )
        ],
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
    )
    if auth:
        card.security_schemes[BEARER].http_auth_security_scheme.scheme = "bearer"
        requirement = SecurityRequirement()
        requirement.schemes[BEARER].SetInParent()
        card.security_requirements.append(requirement)
    return card


# --------------------------------------------------------------------------
# Authentication: no token, no entry
# --------------------------------------------------------------------------
class BearerAuth:
    """Require ``Authorization: Bearer <token>`` on everything but the card.

    The card is served unauthenticated on purpose: a caller reads it to *find
    out* which credential it needs, before holding any.
    """

    def __init__(self, app, *, token: str) -> None:
        self.app = app
        self._token = token.strip().encode()

    async def __call__(self, scope, receive, send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or path.startswith("/.well-known/"):
            await self.app(scope, receive, send)
            return

        presented = b""
        for key, value in scope.get("headers") or []:
            if key == b"authorization":
                presented = value
                break

        parts = presented.split(None, 1)
        ok = (
            len(parts) == 2
            and parts[0].lower() == b"bearer"
            # compare_digest, so a wrong token takes the same time as a right
            # one and the comparison itself leaks nothing.
            and hmac.compare_digest(parts[1].strip(), self._token)
        )
        if ok:
            await self.app(scope, receive, send)
            return

        body = b'{"error":"unauthorized"}'
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"www-authenticate", b"Bearer"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


# --------------------------------------------------------------------------
# The engine: this machine's claude, in the agent's own folder
# --------------------------------------------------------------------------
class LocalClaude:
    """Run the machine's ``claude`` headless and return its JSON result."""

    def __init__(
        self,
        *,
        cwd: str,
        executable: str = "claude",
        timeout: float = 300.0,
        allowed_tools: str | None = None,
        max_turns: int | None = None,
    ) -> None:
        self.cwd = str(Path(cwd).resolve())
        self.executable = executable
        self.timeout = timeout
        # The first piece of "scope decides the launch arguments". A token that
        # says read-only limits nothing on its own: write actions do park in
        # input-required, but reads are never gated. Only the launch does.
        self.allowed_tools = allowed_tools
        # The clock stops it hanging; this stops it running away. They are not
        # the same limit: a busy agent spends a lot well within its deadline.
        self.max_turns = max_turns

    def _env(self) -> dict[str, str]:
        return {k: v for k, v in os.environ.items() if k in _INHERITED}

    async def ask(self, prompt: str, *, resume: str | None) -> dict:
        args = [self.executable, "-p", prompt, "--output-format", "json"]
        if self.allowed_tools:
            args += ["--allowedTools", self.allowed_tools]
        if self.max_turns:
            args += ["--max-turns", str(self.max_turns)]
        if resume:
            # Continuity by explicit identifier. Never --continue: that grabs
            # "the most recent transcript" for the (user, folder) pair, which
            # is exactly how you steal the thread from a live session.
            args += ["--resume", resume]
        else:
            # New session with an id we choose, so we know it from the start
            # and can resume it later without guessing.
            args += ["--session-id", str(uuid.uuid4())]

        log.info("running: %s (cwd=%s)", " ".join(args[:4]), self.cwd)
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=self.cwd,
            env=self._env(),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), self.timeout)
        except TimeoutError:
            # Kill it. A timeout that only drops the HTTP connection leaves the
            # agent running and spending money on an answer nobody will read.
            proc.kill()
            await proc.wait()
            raise RuntimeError(
                f"the agent did not answer within {self.timeout:g}s"
            ) from None

        if proc.returncode != 0:
            detail = (err or b"").decode("utf-8", "replace").strip()[:500]
            raise RuntimeError(f"claude exited with code {proc.returncode}: {detail}")
        try:
            return json.loads(out.decode("utf-8", "replace"))
        except json.JSONDecodeError as e:
            raise RuntimeError(f"unreadable response from claude: {e}") from e


# --------------------------------------------------------------------------
# The translation: from what claude says to what A2A understands
# --------------------------------------------------------------------------
class PhoneExecutor(AgentExecutor):
    def __init__(self, engine: LocalClaude) -> None:
        self._engine = engine
        # A2A contextId -> claude session_id. This is what turns a handful of
        # separate calls into one conversation.
        #
        # It lives in memory: restart the server and threads in flight lose
        # their history. Persisting it is a known gap, not an oversight.
        self._sessions: dict[str, str] = {}

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task_id, context_id = context.task_id, context.context_id
        updater = TaskUpdater(event_queue, task_id, context_id)

        # The stream MUST open with a Task before any status update.
        await event_queue.enqueue_event(
            Task(
                id=task_id,
                context_id=context_id,
                status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED),
            )
        )
        await updater.start_work()

        prompt = _text_of(context)
        if not prompt:
            await updater.failed(
                message=updater.new_agent_message([Part(text="empty message")])
            )
            return

        try:
            result = await self._engine.ask(
                prompt, resume=self._sessions.get(context_id)
            )
        except Exception as e:  # noqa: BLE001 - every failure is reported to the caller
            log.exception("the call failed")
            await updater.failed(message=updater.new_agent_message([Part(text=str(e))]))
            return

        session_id = result.get("session_id")
        if session_id:
            self._sessions[context_id] = session_id

        text = (result.get("result") or "").strip() or "(no answer)"

        if result.get("is_error"):
            await updater.failed(message=updater.new_agent_message([Part(text=text)]))
            return

        # Metadata the caller is glad to have: what it cost, how many turns, and
        # what the agent was denied -- which is the clue that a restricted scope
        # is cutting something off.
        meta = {
            "cost_usd": result.get("total_cost_usd"),
            "num_turns": result.get("num_turns"),
            "claude_session_id": session_id,
            "permission_denials": result.get("permission_denials") or [],
            "duration_ms": result.get("duration_ms"),
        }
        await updater.add_artifact([Part(text=text)], name="response")
        await updater.complete(
            message=updater.new_agent_message([Part(text=text)], metadata=meta)
        )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        # No cancellation yet: a headless query is short, and killing it midway
        # leaves the agent unable to report what it already did.
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.failed(
            message=updater.new_agent_message(
                [Part(text="this version cannot cancel a call in progress")]
            )
        )


def _text_of(context: RequestContext) -> str:
    message = context.message
    if message is None:
        return ""
    return "\n".join(p.text for p in message.parts if getattr(p, "text", "")).strip()


# --------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="A2A phone for a single agent.")
    ap.add_argument("--cwd", required=True, help="The agent's project folder.")
    ap.add_argument("--name", default=None, help="Agent name shown on the card.")
    ap.add_argument("--description", default=None)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9110)
    ap.add_argument(
        "--public-url",
        default=None,
        help="Address advertised on the card. Defaults to where it listens.",
    )
    ap.add_argument("--auth-token-file", default=None)
    ap.add_argument("--claude", default="claude", help="Path to the claude to use.")
    ap.add_argument("--timeout", type=float, default=300.0)
    ap.add_argument(
        "--allowed-tools",
        default=None,
        help="Comma-separated tool list the answering agent is limited to, "
        "passed straight to claude --allowedTools. Without it the agent can "
        "use everything its user can reach.",
    )
    ap.add_argument(
        "--max-turns",
        type=int,
        default=None,
        help="Cap on agent turns per call. The timeout bounds time, this "
        "bounds spend; they are different limits.",
    )
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    cwd = Path(args.cwd).resolve()
    if not cwd.is_dir():
        print(f"error: no such folder: {cwd}", file=sys.stderr)
        return 2

    token = None
    if args.auth_token_file:
        token = Path(args.auth_token_file).read_text(encoding="utf-8").strip()
        if not token:
            print("error: the token file is empty", file=sys.stderr)
            return 2

    url = args.public_url or f"http://{args.host}:{args.port}/"
    if not url.endswith("/"):
        url += "/"

    name = args.name or cwd.name
    card = build_card(
        url,
        name=name,
        description=args.description
        or f"Agent for project {name}, reachable over A2A.",
        auth=token is not None,
    )

    engine = LocalClaude(
        cwd=str(cwd),
        executable=args.claude,
        timeout=args.timeout,
        allowed_tools=args.allowed_tools,
        max_turns=args.max_turns,
    )
    handler = DefaultRequestHandler(
        agent_executor=PhoneExecutor(engine),
        task_store=InMemoryTaskStore(),
        agent_card=card,
    )
    routes = [
        *create_agent_card_routes(card),
        *create_jsonrpc_routes(handler, rpc_url="/"),
    ]
    middleware = [Middleware(BearerAuth, token=token)] if token else None
    app = Starlette(routes=routes, middleware=middleware)

    print(
        f"a2agates {__version__}: agent={name} folder={cwd}\n"
        f"  listening on http://{args.host}:{args.port}/\n"
        f"  card advertises {url}\n"
        f"  authentication: {'token required' if token else 'OPEN'}\n"
        f"  tools: {args.allowed_tools or 'EVERYTHING its user can reach'}"
    )
    if token is None:
        print(
            "  warning: running without a token. Anyone who can reach this port\n"
            "           can run your agent. Do not do this outside a test box.",
            file=sys.stderr,
        )
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

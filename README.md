# a2agates

**Give an agent a phone number.**

`a2agates` puts a [A2A](https://a2a-protocol.org/) endpoint in front of a
[Claude Code](https://claude.com/claude-code) agent, so another agent can call
it and get an answer — and gives that agent a way to place calls of its own.

> **Status: prototype.** It works, it is measured, and it is not deployed
> anywhere yet. Read [Security](#security) before you point it at anything you
> care about. The model it is growing into is written down in
> [DESIGN.md](DESIGN.md), and parts of this README describe what exists rather
> than what is decided.

## The idea in one line

> Anyone can have the phone. Without the number, the token and — if it is
> required — being on the origin list, you cannot call anybody.

The security lives in the contact list, never in the code. That is why the code
is public.

## Two halves

**The ear** (`a2agates.server`) listens on a port. When a call arrives, it runs
the machine's own `claude` in the agent's project folder and returns what it
answers.

**The mouth** (`a2agates.mcp`) is an MCP server exposing one tool, so an agent
can call another one without leaving its conversation. It is an MCP rather than
a documented `curl` for one reason: **the agent cannot call anyone who is not in
its contact list.** The set of allowed destinations stops being a rule written
in a prompt and becomes the only way to dial.

## Two properties that are the whole point

**It calls the machine's own `claude`.** Not a copy bundled inside a
dependency. The agent that answers the phone is the same one that works in
tmux, with its version, its memory, its `CLAUDE.md` and its files. That is what
makes the answer *that agent's* answer and not a generic model's.

**The card advertises the address you give it**, not the interface it binds to.
Without that, behind a reverse proxy it would publish a number nobody can dial.

Everything about the protocol itself — the agent card, task states, artifacts,
JSON-RPC — comes from the official A2A SDK. What lives here is the part nobody
gives away: how you reach the local agent.

## Measured

| | |
|---|---|
| Response | **3.3 s** |
| Memory, idle | **54 MB** |
| Code | **288 lines** excluding comments |
| Continuity | same `contextId` → same Claude session |
| Cost of resuming | 0.076 → **0.009 USD**, by reusing the cache |

It really is the agent: given a `CLAUDE.md` telling it that it was called
`PALOMA-7431`, it answered *"I am agent PALOMA-7431, and this project's official
colour is green"*. It read its own knowledge.

## Install

Requires **Python ≥3.10** and a `claude` on the `PATH`, authenticated as the
user that will run the process.

```bash
python3 -m venv ~/a2agates-venv
~/a2agates-venv/bin/pip install "git+https://github.com/JaimeCerezo/a2agates@v0.1.2"
```

Pin a tag or a commit. A commit id is a hash of its content, so "install this
commit" is a promise nobody can break; a tag can be moved.

**To put a phone on a machine, follow [INSTALL.md](INSTALL.md).** It is written
for whoever is doing it alone — the decisions to make first, how to survey the
machine you are on, TLS with Traefik / Caddy / nginx, the four checks to run
before calling it done, and the traps that cost an hour each. The rest of this
README is what the thing is; that one is how to stand it up.

## Run the ear

```bash
a2agates \
  --cwd /path/to/project \
  --name "the agent's name" \
  --port 9110 \
  --public-url https://example.org/gw/agent/ \
  --auth-token-file /path/to/token
```

It binds to **`127.0.0.1` by default**, on purpose: put a reverse proxy in
front for TLS, never open the port directly.

It runs **as the agent's user** — that is where its `$HOME`, its memory and its
credentials come from. A process cannot change its own user, so **one agent =
one process**. Several agents on one machine is a systemd template unit.

## Call it

Two details that cost an hour if nobody tells you:

- The method is **`SendMessage`**, not `message/send`. That one is the 0.3
  compatibility layer; this speaks **v1 natively**.
- You must send the **`A2A-Version: 1.0`** header. Without it the server
  assumes 0.3 and rejects the call with an error that does not say why.

```bash
curl -s -X POST http://127.0.0.1:9110/ \
  -H "Authorization: Bearer $TOKEN" \
  -H 'A2A-Version: 1.0' \
  -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"SendMessage",
       "params":{"message":{"role":"ROLE_USER",
                 "parts":[{"text":"What is pending?"}],
                 "messageId":"1"}}}'
```

The answer comes back **in the same call**. To continue the conversation, repeat
the `contextId` the previous one returned.

The card is served unauthenticated at `/.well-known/agent-card.json`, on
purpose: a caller reads it to *find out* which credential it needs, before
holding any.

## Continuity, which is the reusable part

It uses **`--session-id`** to start and **`--resume`** to continue. Never
`--continue`, which grabs *"the most recent transcript"* for the (user, folder)
pair — exactly the mechanism by which you steal the thread from a live session.

With explicit identifiers that whole class of collision disappears **by
construction**, not by discipline. That trick is worth stealing even if you
never run this.

## Wire the mouth to an agent

```bash
claude --mcp-config config.json --allowedTools "mcp__a2agates__ask_agent"
```

`config.json` declares `a2agates.mcp` as a `stdio` server and passes the
destination through the environment (`A2A_FRIEND`, `A2A_URL`, `A2A_TOKEN`).

### It fails legibly, which matters as much as working

| Situation | What the agent gets |
|---|---|
| Wrong credential | `ERROR: <friend> rejected the credential (401).` |
| Nobody home | `ERROR: could not reach <friend>: …` |
| No credential configured | `ERROR: no credential configured, cannot call anyone.` |

None of them hang and none leave the agent guessing. Every good answer comes
back with **the cost of the call** attached: whoever places a call should know
what it spends.

### Proven end to end

Two agents in different folders, each with its own `CLAUDE.md`: **CUERVO-2210**
(orange) and **PALOMA-7431** (green). CUERVO was asked something only PALOMA
could know:

```
> Who are you, and what colour is agent PALOMA-7431's project?
  You do not know PALOMA's colour: go find out.

I am CUERVO-2210, the agent for this project (whose official colour is orange).
I phoned PALOMA-7431 and it confirmed that its project's official colour is green.
```

**10.3 s** end to end, 3 turns, 0 permission denials. The phone's log shows the
call CUERVO composed on its own: *"Hello, I am agent CUERVO-2210. What is your
project's official colour?"* — it introduced itself without being asked to.

## What it does not do yet

Deliberately, so the first version could be evaluated:

- **It does not talk to a live tmux session.** This is the missing piece and the
  most valuable one.
- **It does not decide awake/asleep.** It always starts a fresh agent.
- The contact list is **environment variables**, there is **one token** for all
  callers, and there is no origin check — that one has to sit in the reverse
  proxy for now, which is the wrong place. All three are designed and unbuilt.
- There is **no call log**. When the answering agent is capable on purpose, the
  record is the only control left — see [DESIGN.md](DESIGN.md).
- No card signing, no callback webhook, no socket activation, no cancelling a
  call in progress.

What *is* built, and verified rather than assumed:

- **`--allowed-tools` and `--max-turns`**, so a scope reaches the launch. Asked
  to write a file and run a command, the agent did neither and the file was
  never created.
- **`--token-expires`**, enforced on every call. A past date refuses to start;
  a 25-second expiry took the same call from 200 to `401 credential expired`.
  That is what makes a short-lived credential safe to hand over in a channel
  that keeps history.

## Security

Read this part.

**The agent that answers inherits everything its user can read.** Measured: you
can ask it to list `~/.claude/` and confirm that `.credentials.json` is there.
Actions that **write** are stopped — the task parks in `input-required` waiting
for an answer nobody will give — but **read-only actions are not.**

> **A phone token is worth exactly as much as the account of the user that
> answers it.**

Worse, refusing is a *judgement*, not a barrier. Measured on a live phone: asked
for a private key it refused well, asked for `/etc/hostname` it refused too —
and said *"it is not the tools: I can read"*. `permission_denials` was empty
both times. Nothing fired. Judgement can be argued with, and arguing with it is
what a prompt injection does.

Three rules follow:

1. **The number you hand out freely must not run under an account that holds
   credentials to other machines.** An admin account with SSH keys and docker
   group membership is the worst possible candidate for a widely-shared number.
2. Put it behind TLS and restrict who can reach it. A token crossing the open
   internet in cleartext is not a token, it is a public URL.
3. Give a scope, and make it reach the launch. A label that does not change
   `--allowed-tools` changes nothing.

But do not over-apply rule 1. An agent worth phoning often *does* things —
installs, deploys, maintains — and for that agent the privileges are the
product, not an accident. Strip them and the phone answers but cannot help.

> **A token against a capable agent is not permission to ask. It is permission
> to command an operator who is root on that machine.**

The way out is that one agent can have **more than one number**: a read-only
reception whose tokens you hand out freely, and an operations line with full
powers, one short-lived token and a recorded call. Same machine, same
knowledge, different launch arguments. See [DESIGN.md](DESIGN.md).

## Licence

Apache 2.0. See [LICENSE](LICENSE).

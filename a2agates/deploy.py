"""The deployment, as data: fleet constants and the unit file, in one place.

These used to live in ``install.sh``, and that is the bug this module exists to
kill. It surfaced three times in one day, each time wearing a different hat:

* ``update.sh`` upgraded the package but never re-rendered the unit, so a phone
  that had been *updated* kept whatever ``ExecStart`` it was installed with.
  On 2026-09-22 that meant a current phone running without ``--db`` — it
  answered calls perfectly and recorded not one of them. Nobody could tell,
  because a missing log looks exactly like a quiet week.
* the command-line tools were placed by the installer and not by the updater,
  so an updated machine could be on the newest version with none of the
  commands that make it usable.
* and the limits are supposed to be fleet constants, but only the installer
  knew them, so an old phone kept old limits forever.

The pattern underneath all three: **a machine that updates is not the same as a
machine that reinstalls**, and anything only the installer knows drifts away
silently on every machine that takes the cheap path — which is every machine,
because the cheap path is the one we told them to take.

So the deployment is defined here, inside the package. Both scripts render it
from whatever version is installed, and neither holds a second copy that can
fall behind.
"""

from __future__ import annotations

import sys

# --------------------------------------------------------------------------
# Fleet constants. Deliberately not options.
#
# Every phone runs the same limits, so that "will this call fit?" has one
# answer everywhere instead of one per machine. A limit that varies by host is
# a limit nobody can reason about from the calling side.
#
# Measured: a question costs 0.05-0.40, real work costs 1.9-3.6. The budget is
# the real limit; the turn cap is only a backstop against a loop, so it sits
# well above where money runs out. It was 20 for a while, which quietly made
# turns the binding limit and undid the whole argument.
# --------------------------------------------------------------------------
MAX_TURNS = 60
MAX_BUDGET = "5.00"

PREFIX = "/opt/a2agates"
VENV = f"{PREFIX}/venv"
ETC = "/etc/a2agates"
STATE = "/var/lib/a2agates"

# Every command the phone ships. All of them are console scripts of this
# package, so `pip install` puts them in the venv and converge() links them
# where people can reach them. Nothing is fetched separately and nothing can be
# placed by one script and forgotten by the other -- which is how the tools,
# the limits and the unit each went stale in turn on 2026-09-22.
COMMANDS = ("a2agates", "a2agates-mcp", "a2agates-admin", "a2agates-note", "a2agates-log")
BINDIR = "/usr/local/bin"
MAILBOX = f"{STATE}/mailbox.md"

UNIT = """\
[Unit]
Description=a2agates phone (%i)
# docker.service because a phone often binds to the bridge and is fronted by a
# proxy in a container. Without this it restart-loops after a reboot.
After=network-online.target docker.service
Wants=network-online.target docker.service

[Service]
Type=exec
User={user}
EnvironmentFile={etc}/%i.env
ExecStart={venv}/bin/a2agates \\
    --cwd ${{A2A_CWD}} \\
    --name ${{A2A_NAME}} \\
    --host ${{A2A_HOST}} \\
    --port ${{A2A_PORT}} \\
    --public-url ${{A2A_PUBLIC_URL}} \\
    --auth-token-file ${{A2A_TOKEN_FILE}} \\
    --db ${{A2A_DB}} \\
    --full-permissions \\
    --max-turns ${{A2A_MAX_TURNS}} \\
    --max-budget ${{A2A_MAX_BUDGET}} \\
    --token-expires ${{A2A_TOKEN_EXPIRES}}
Restart=on-failure
RestartSec=5
# NoNewPrivileges is deliberately absent: it would block the agent's own sudo,
# which is half of what the phone is for. A phone that must not escalate is one
# answering as a user that cannot escalate -- not this flag.

[Install]
WantedBy=multi-user.target
"""


def unit_text(user: str) -> str:
    return UNIT.format(user=user, etc=ETC, venv=VENV)


def converge(user: str | None = None) -> list[str]:
    """Put the machine into the state an a2agates install is supposed to be in.

    **One code path, called by both scripts**, and that is the whole reason it
    exists. Installing and updating used to place different things, so every
    machine that took the cheap path -- which is every machine, because the
    cheap path is the one we tell them to take -- quietly lacked whatever had
    been added since it was first set up. It happened three times in one day
    with three different symptoms: missing commands, stale limits, and a phone
    running without --db that answered perfectly and recorded nothing.

    Safe to run as often as you like. Returns what it changed, so the caller
    can decide whether a restart is warranted.
    """
    import os
    import pwd

    changed: list[str] = []

    # The commands, at stable paths. A contact list and a crontab point at
    # these, never into the venv: an install that moves or is rebuilt would
    # otherwise break them -- and not at startup, but on the next call, which
    # looks exactly like the other end not answering.
    os.makedirs(BINDIR, exist_ok=True)
    for name in COMMANDS:
        src, dst = f"{VENV}/bin/{name}", f"{BINDIR}/{name}"
        if not os.path.exists(src):
            continue
        if os.path.islink(dst) and os.readlink(dst) == src:
            continue
        if os.path.lexists(dst):
            os.remove(dst)
        os.symlink(src, dst)
        changed.append(f"command {name}")

    # The mailbox, before the phone answers its first call: "do not modify the
    # tool" is only fair if there is somewhere for a finding to go. An agent
    # with nowhere to report something reports it by patching.
    os.makedirs(STATE, exist_ok=True)
    if not os.path.exists(MAILBOX):
        with open(MAILBOX, "w", encoding="utf-8") as fh:
            fh.write("# a2agates — mailbox\n\nNotes from agents on this "
                     "machine. Append only.\n")
        os.chmod(MAILBOX, 0o666)
        changed.append("mailbox")

    # The unit, rendered from this version rather than from a copy in a shell
    # script that can fall behind.
    unit = "/etc/systemd/system/a2agates@.service"
    if user is None and os.path.exists(unit):
        with open(unit, encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("User="):
                    user = line.split("=", 1)[1].strip()
                    break
    if user:
        wanted = unit_text(user)
        current = ""
        if os.path.exists(unit):
            with open(unit, encoding="utf-8") as fh:
                current = fh.read()
        if current != wanted:
            with open(unit, "w", encoding="utf-8") as fh:
                fh.write(wanted)
            changed.append("unit file")

    # Every phone's settings and databases. The fleet constants are rewritten
    # rather than merged: a limit that drifted is a limit that has to come back
    # into line, and the rest of the file is the machine's own business.
    if os.path.isdir(ETC):
        for entry in sorted(os.listdir(ETC)):
            if not entry.endswith(".env"):
                continue
            name = entry[:-4]
            path = f"{ETC}/{entry}"
            with open(path, encoding="utf-8") as fh:
                lines = fh.read().splitlines()
            wanted_pairs = {
                "A2A_DB": f"{STATE}/{name}",
                "A2A_MAX_TURNS": str(MAX_TURNS),
                "A2A_MAX_BUDGET": MAX_BUDGET,
            }
            out, seen, dirty = [], set(), False
            for line in lines:
                key = line.split("=", 1)[0]
                if key in wanted_pairs:
                    seen.add(key)
                    new = f"{key}={wanted_pairs[key]}"
                    dirty |= new != line
                    out.append(new)
                else:
                    out.append(line)
            for key, value in wanted_pairs.items():
                if key not in seen:
                    out.append(f"{key}={value}")
                    dirty = True
            if dirty:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write("\n".join(out) + "\n")
                changed.append(f"{name} settings")

            # The databases, all of them, even the ones nothing writes to yet.
            # An empty table costs nothing; a missing one turns the day you
            # need it into a migration on a live phone.
            from . import db as db_mod

            directory = f"{STATE}/{name}"
            fresh = not os.path.isdir(directory)
            db_mod.init(directory)
            owner = user
            if owner:
                try:
                    info = pwd.getpwnam(owner)
                    os.chown(directory, info.pw_uid, info.pw_gid)
                    for f in ("callers.db", "contacts.db"):
                        target = f"{directory}/{f}"
                        if os.path.exists(target):
                            os.chown(target, info.pw_uid, info.pw_gid)
                except (KeyError, PermissionError):
                    pass
            os.chmod(directory, 0o700)
            if fresh:
                changed.append(f"{name} databases")

    return changed


def main(argv: list[str] | None = None) -> int:
    """Tiny CLI for the shell scripts. Not meant for people."""
    args = sys.argv[1:] if argv is None else argv
    if args and args[0] == "unit":
        if len(args) < 2:
            print("usage: -m a2agates.deploy unit <user>", file=sys.stderr)
            return 2
        sys.stdout.write(unit_text(args[1]))
        return 0
    if args and args[0] == "converge":
        user = args[1] if len(args) > 1 else None
        for item in converge(user):
            print(f"  {item} updated")
        return 0
    if args and args[0] == "constants":
        # Every value quoted, and the reason is a bug that took a while to see.
        # This printed `TOOLS=a2agates-note a2agates-log` unquoted, and the
        # shell's `eval` read it the only way it could: assign TOOLS for one
        # command, then RUN `a2agates-log`. So every update quietly printed the
        # call log in the middle of its output. Harmless here; it would not
        # have been if the second word had been something that changes things.
        print(f'MAX_TURNS="{MAX_TURNS}"')
        print(f'MAX_BUDGET="{MAX_BUDGET}"')
        print(f'STATE="{STATE}"')
        print(f'TOOLS="{" ".join(TOOLS)}"')
        return 0
    print("usage: -m a2agates.deploy {unit <user>|constants|converge [user]}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

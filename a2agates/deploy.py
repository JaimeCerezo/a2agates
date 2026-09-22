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

TOOLS = ("a2agates-note", "a2agates-log")

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


def main(argv: list[str] | None = None) -> int:
    """Tiny CLI for the shell scripts. Not meant for people."""
    args = sys.argv[1:] if argv is None else argv
    if args and args[0] == "unit":
        if len(args) < 2:
            print("usage: -m a2agates.deploy unit <user>", file=sys.stderr)
            return 2
        sys.stdout.write(unit_text(args[1]))
        return 0
    if args and args[0] == "constants":
        print(f"MAX_TURNS={MAX_TURNS}")
        print(f"MAX_BUDGET={MAX_BUDGET}")
        print(f"STATE={STATE}")
        print(f"TOOLS={' '.join(TOOLS)}")
        return 0
    print("usage: -m a2agates.deploy {unit <user>|constants}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

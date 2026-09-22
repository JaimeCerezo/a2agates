#!/usr/bin/env bash
#
# a2agates — update every phone on this machine, and check nobody edited it.
#
# The cheap path. It takes no arguments because it needs none: the phones are
# already described in /etc/a2agates/*.env, so there is nothing for an agent to
# decide, look up or reason about. One command, a few lines of output.
#
#   curl -fsSLO https://raw.githubusercontent.com/JaimeCerezo/a2agates/main/scripts/update.sh
#   sudo bash update.sh
#
# Add --check to look without changing anything: it prints the installed
# version, whether it is current, and whether any installed file has been
# modified. Costs nothing and touches nothing.
#
set -euo pipefail

VERSION="v0.1.19"
REPO="https://github.com/JaimeCerezo/a2agates"
VENV=/opt/a2agates/venv
ETC=/etc/a2agates

CHECK=no
[ "${1:-}" = "--check" ] && CHECK=yes

die() { echo "a2agates: $*" >&2; exit 1; }

[ -x "$VENV/bin/python" ] || die "no install at $VENV. Use install.sh first."
[ "$CHECK" = yes ] || [ "$(id -u)" -eq 0 ] || die "run me with sudo (or --check)."

# -P on every python call here, and it is not decoration. Without it Python
# puts the current directory on sys.path, so running this from a checkout of
# a2agates imports the CHECKOUT instead of the install: the version came from
# the wrong tree and the integrity check below verified the source against
# itself and passed. An integrity check you can fool by cd'ing somewhere is
# worse than none, because it reports "clean".
have=$("$VENV/bin/python" -P -c 'import a2agates;print(a2agates.__version__)' 2>/dev/null || echo "?")
echo "a2agates: installed $have, current ${VERSION#v}"

# --- has anyone edited the code? ------------------------------------------
# The rule is that a phone is not modified locally: it is one implementation,
# updated from one place, so that what a caller reaches is the same thing
# everywhere and an update never has to be merged with somebody's local fix.
#
# A rule nobody can check is a wish, so this checks it. Every wheel ships a
# RECORD with a hash per file; comparing against it catches an edited module,
# and an extra file under the package catches a local "improvement" bolted on
# beside it. Neither is prevented -- the operator has root -- but neither can
# happen quietly, which is the part that matters.
site=$("$VENV/bin/python" -P -c 'import sysconfig;print(sysconfig.get_paths()["purelib"])')
record=$(ls -d "$site"/a2agates-*.dist-info/RECORD 2>/dev/null | head -1 || true)
drift=0
if [ -n "$record" ]; then
    while IFS=, read -r path hash _; do
        case "$path" in a2agates/*.py) ;; *) continue ;; esac
        [ -f "$site/$path" ] || { echo "  MISSING  $path"; drift=1; continue; }
        want=${hash#sha256=}
        got=$("$VENV/bin/python" -P - "$site/$path" <<'PY'
import base64,hashlib,sys
d=hashlib.sha256(open(sys.argv[1],'rb').read()).digest()
print(base64.urlsafe_b64encode(d).rstrip(b'=').decode())
PY
)
        [ "$got" = "$want" ] || { echo "  MODIFIED $path"; drift=1; }
    done < "$record"

    known=$(awk -F, '{print $1}' "$record" | grep '^a2agates/' || true)
    while read -r f; do
        rel=${f#"$site/"}
        echo "$known" | grep -qxF "$rel" || { echo "  EXTRA    $rel"; drift=1; }
    done < <(find "$site/a2agates" -name '*.py' 2>/dev/null)
fi

if [ "$drift" = 1 ]; then
    echo
    echo "  This install has been changed locally. a2agates is a closed product:"
    echo "  the next update overwrites this, and a caller can no longer tell what"
    echo "  it reached. Leave the finding in the mailbox instead --"
    echo "    a2agates-note \"...\"   (see GOVERNANCE.md)"
    echo "  Re-running this script without --check restores the published files."
else
    echo "  files match the published release"
fi

# Surfaced here because this is the command that gets run on every machine, and
# a mailbox nobody empties is the same as no mailbox.
MAILBOX=/var/lib/a2agates/mailbox.md
if [ -f "$MAILBOX" ]; then
    notes=$(grep -c '^## ' "$MAILBOX" 2>/dev/null) || notes=0
    [ "${notes:-0}" -gt 0 ] && echo "  mailbox: $notes note(s) waiting -- a2agates-note --read"
fi

if [ "$CHECK" = yes ]; then
    for env in "$ETC"/*.env; do
        [ -f "$env" ] || continue
        n=$(basename "$env" .env)
        printf '  phone %-16s %s\n' "$n" "$(systemctl is-active "a2agates@$n" 2>/dev/null || echo unknown)"
    done
    exit $drift
fi

# --- update ---------------------------------------------------------------
if [ "$have" = "${VERSION#v}" ] && [ "$drift" = 0 ]; then
    echo "  already current, nothing to do"
    exit 0
fi

"$VENV/bin/pip" install --quiet --upgrade --force-reinstall "git+$REPO@$VERSION" \
    || die "update failed; the running phone is untouched."
now=$("$VENV/bin/python" -P -c 'import a2agates;print(a2agates.__version__)')
echo "  updated to $now"

# Kept in step with the install: a contact list points at these, never into the
# venv, so that moving or rebuilding the install does not quietly break the
# ability to place calls.
for b in a2agates a2agates-mcp; do
    [ -x "$VENV/bin/$b" ] && ln -sfn "$VENV/bin/$b" "/usr/local/bin/$b"
done

for env in "$ETC"/*.env; do
    [ -f "$env" ] || continue
    n=$(basename "$env" .env)
    systemctl is-enabled "a2agates@$n" >/dev/null 2>&1 || continue
    systemctl restart "a2agates@$n"
    host=$(grep -oP '^A2A_HOST=\K.*' "$env"); port=$(grep -oP '^A2A_PORT=\K.*' "$env")
    sleep 2
    v=$(curl -fsS --max-time 10 "http://$host:$port/.well-known/agent-card.json" 2>/dev/null \
        | grep -oP '"version"\s*:\s*"\K[^"]+' | head -1 || true)
    if [ -n "$v" ]; then
        printf '  phone %-16s answering, card says %s\n' "$n" "$v"
    else
        printf '  phone %-16s NOT ANSWERING -- journalctl -u a2agates@%s\n' "$n" "$n"
    fi
done

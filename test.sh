#!/usr/bin/env bash
# Offline checks: no network, no real install touched. Needs an opencode binary
# to copy (pass one as $1, otherwise the one in PATH is used).
set -uo pipefail
cd "$(dirname "$0")"
TOOL=./opencode-context-1m.py
SRC=${1:-$(command -v opencode)}
WORK=$(mktemp -d); trap 'rm -rf "$WORK"' EXIT
fail=0
ok()   { printf '  ok   %s\n' "$1"; }
bad()  { printf '  FAIL %s\n' "$1"; fail=1; }

[ -x "$SRC" ] || { echo "no opencode binary at $SRC"; exit 2; }
cp "$SRC" "$WORK/oc"; chmod +x "$WORK/oc"
before=$(sha256sum "$WORK/oc" | cut -d' ' -f1)

echo "scan finds both sites"
out=$("$TOOL" --binary "$WORK/oc" --scan) || bad "scan exited non-zero"
[ "$(grep -c 'context=' <<<"$out")" -ge 2 ] && ok "sites listed" || bad "expected 2 sites in scan output"

echo "dry-run changes nothing"
"$TOOL" --binary "$WORK/oc" --dry-run >/dev/null || bad "dry-run exited non-zero"
[ "$(sha256sum "$WORK/oc" | cut -d' ' -f1)" = "$before" ] \
  && ok "binary untouched" || bad "dry-run wrote to the binary"

echo "refuses a non-opencode file"
printf 'hello' > "$WORK/notelf"; chmod +x "$WORK/notelf"
"$TOOL" --binary "$WORK/notelf" >/dev/null 2>&1
[ $? -eq 1 ] && ok "exit 1 on unknown build" || bad "should have refused"

echo "patch + verify"
"$TOOL" --binary "$WORK/oc" --verify >/dev/null 2>&1 \
  && ok "inherited context is the target" || bad "patch or verify failed"

echo "idempotent"
"$TOOL" --binary "$WORK/oc" 2>&1 | grep -q 'nothing to do' \
  && ok "second run is a no-op" || bad "second run did something"

echo "restore brings back the original"
"$TOOL" --binary "$WORK/oc" --restore >/dev/null 2>&1
[ "$(sha256sum "$WORK/oc" | cut -d' ' -f1)" = "$before" ] \
  && ok "byte-identical to the original" || bad "restore is not byte-identical"

exit $fail

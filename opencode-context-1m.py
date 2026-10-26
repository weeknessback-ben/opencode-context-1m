#!/usr/bin/env python3
"""Patch the opencode binary so its default context window is 1M instead of 200k.

opencode ships two hardcoded 200000 defaults inside the Bun bytecode section of
the executable. Every model declared in `provider.*.models` WITHOUT an explicit
`limit` inherits them, so a config full of `{"name": "..."}` entries silently
gets a 200k window no matter what the upstream model actually supports.

  1. Model.Info default   limit:{context:200000, output:32000}
  2. SessionCompaction    gM = 200000  (fallback cap when a model has no limit)

This script rewrites both to 1000000, in place, safely:

  * finds them by byte signature, never by hardcoded file offset
  * parses the ELF section table to only touch the .bun section
  * refuses to guess: 0 hits or >1 hit = abort, nothing written
  * writes a timestamped backup next to the binary
  * replaces atomically (a running `opencode serve` keeps the old inode alive)
  * re-runs itself harmlessly (idempotent)
  * `--verify` proves the patch by booting a throwaway server and reading
    GET /api/model for a synthetic model that has no explicit limit

Re-run after every `opencode upgrade`, which overwrites the binary.

Usage:
    ./opencode-context-1m.py                 # patch the binary in PATH
    ./opencode-context-1m.py --dry-run       # show what would change
    ./opencode-context-1m.py --verify        # patch + prove it works
    ./opencode-context-1m.py --scan          # list candidate sites, change nothing
    ./opencode-context-1m.py --restore       # roll back to newest backup
    ./opencode-context-1m.py --binary /path/to/opencode --target 500000

Exit codes: 0 ok / already patched, 1 failed or refused, 2 usage error.
"""

import argparse
import glob
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time

I32 = "<i"
DEFAULT_TARGET = 1000000

# Sites to patch. Each entry is (name, ints-before-field, ints-after-field).
#
# The field itself is the context limit, and is the only value rewritten.
# "before"/"after" are the neighbouring constants that make each site unique:
#   Model.Info  -> INT32_MAX sentinel, then context, then output
#   Compaction  -> context, output, then the tuning constants that follow it
#                 (v1=1, Bc=2, Zs=3, Rc=[0,1,2,3,4], Hs=2000, fM=1500)
# Verified against opencode 2.1.4. On a build where these drift, the script
# aborts instead of writing garbage; use --scan to see what the build has.
SITES = [
    ("Model.Info.limit.context", [2147483647], [32000]),
    ("SessionCompaction.cap", [], [32000, 2, 3, 0, 2000, 1500]),
]

# Values the context field is allowed to currently hold: 200000 is the stock
# default, the target means a previous run already patched it.
def values_for(target):
    return tuple(dict.fromkeys((200000, target)))


def i32s(*vals):
    return b"".join(struct.pack(I32, v) for v in vals)


def find_binary(explicit):
    if explicit:
        return explicit
    found = shutil.which("opencode")
    if found:
        return os.path.realpath(found)
    for cand in (
        os.path.expanduser("~/.opencode/bin/opencode"),
        os.path.expanduser("~/.local/bin/opencode"),
    ):
        if os.path.isfile(cand):
            return cand
    sys.exit("error: opencode binary not found; pass --binary PATH")


def bun_section(path):
    """Return (start, end) file offsets of the ELF .bun section, or None."""
    with open(path, "rb") as f:
        ident = f.read(16)
        if ident[:4] != b"\x7fELF":
            return None
        if ident[4] != 2 or ident[5] != 1:  # 64-bit little-endian only
            return None
        f.seek(0x28)
        shoff = struct.unpack("<Q", f.read(8))[0]
        f.seek(0x3A)
        shentsize, shnum, shstrndx = struct.unpack("<HHH", f.read(6))
        if not shoff or not shentsize or not shnum:
            return None

        def header(i):
            f.seek(shoff + i * shentsize)
            raw = f.read(shentsize)
            name, _type = struct.unpack("<II", raw[:8])
            off, size = struct.unpack("<QQ", raw[24:40])
            return name, off, size

        _, str_off, str_size = header(shstrndx)
        f.seek(str_off)
        names = f.read(str_size)
        for i in range(shnum):
            name_off, off, size = header(i)
            end = names.find(b"\x00", name_off)
            if names[name_off:end] == b".bun":
                return off, off + size
    return None


def locate(data, lo, hi, target):
    """Find patchable sites. Returns (found, already, problems)."""
    found, already, problems = [], [], []
    for name, before, after in SITES:
        hits = []
        for value in values_for(target):
            sig = i32s(*before, value, *after)
            start = lo
            while True:
                pos = data.find(sig, start, hi)
                if pos < 0:
                    break
                hits.append((pos + len(i32s(*before)), value))
                start = pos + 1
        if not hits:
            problems.append(f"{name}: signature not found (unrecognised build)")
            continue
        if len(hits) > 1:
            where = ", ".join(f"0x{p:x}" for p, _ in hits)
            problems.append(f"{name}: ambiguous, {len(hits)} hits at {where}")
            continue
        pos, value = hits[0]
        if value == target:
            already.append(name)
        else:
            found.append((name, pos, value))
    return found, already, problems


def scan(data, lo, hi, target):
    """List every adjacent (context, 32000) int32 pair in the bytecode."""
    rows = []
    for value in values_for(target):
        needle = i32s(value, 32000)
        start = lo
        while True:
            pos = data.find(needle, start, hi)
            if pos < 0:
                break
            rows.append((pos, value))
            start = pos + 1
    return rows


def make_verify_config(directory):
    """Synthetic provider: a model with no `limit`, so it inherits the default."""
    cfg = os.path.join(directory, "opencode.jsonc")
    with open(cfg, "w") as f:
        json.dump(
            {
                "provider": {
                    "ctxprobe": {
                        "name": "ctxprobe",
                        "npm": "@ai-sdk/openai-compatible",
                        "options": {"baseURL": "http://127.0.0.1:9/v1", "apiKey": "x"},
                        "models": {"probe": {"name": "probe"}},
                    }
                }
            },
            f,
        )
    return cfg


def verify(binary, target):
    """Boot a throwaway server and read the inherited limit for the probe model."""
    binary = os.path.abspath(binary)  # we chdir into a temp dir below
    with tempfile.TemporaryDirectory(prefix="occtx-") as tmp:
        cfg = make_verify_config(tmp)
        env = dict(os.environ)
        env["OPENCODE_CONFIG"] = cfg
        env["OPENCODE_DISABLE_MODELS_FETCH"] = "1"
        try:
            out = subprocess.run(
                [binary, "api", "--standalone", "GET", "/api/model"],
                cwd=tmp,
                env=env,
                capture_output=True,
                text=True,
                timeout=180,
            )
        except subprocess.TimeoutExpired:
            return None, "verification timed out (server did not answer in 180s)"
        if out.returncode != 0:
            return None, f"verification failed: {(out.stderr or out.stdout).strip()[:300]}"
        try:
            payload = json.loads(out.stdout)
        except json.JSONDecodeError:
            return None, f"verification returned non-JSON: {out.stdout[:200]}"
        for model in payload.get("data", []):
            if model.get("providerID") == "ctxprobe":
                got = model.get("limit", {}).get("context")
                if got == target:
                    return got, None
                return got, f"expected context={target}, got {got}"
        return None, "probe model missing from /api/model"


def newest_backup(binary):
    backups = sorted(glob.glob(binary + ".bak-*"), key=os.path.getmtime)
    return backups[-1] if backups else None


def main():
    ap = argparse.ArgumentParser(
        description="Raise opencode's default context window to 1M (binary patch).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Usage:")[1].split("Exit codes:")[0],
    )
    ap.add_argument("--binary", help="path to the opencode executable")
    ap.add_argument(
        "--target",
        type=int,
        default=DEFAULT_TARGET,
        help=f"context window to write (default {DEFAULT_TARGET})",
    )
    ap.add_argument("--dry-run", action="store_true", help="report, change nothing")
    ap.add_argument("--scan", action="store_true", help="list candidate sites and exit")
    ap.add_argument("--verify", action="store_true", help="prove the patch via /api/model")
    ap.add_argument("--restore", action="store_true", help="roll back to newest backup")
    ap.add_argument("--no-backup", action="store_true", help="skip the backup copy")
    ap.add_argument("--restart", action="store_true", help="restart the background service")
    args = ap.parse_args()

    if args.target < 32000:
        sys.exit("error: --target must be at least 32000")

    binary = find_binary(args.binary)
    if not os.path.isfile(binary):
        sys.exit(f"error: not a file: {binary}")

    if args.restore:
        backup = newest_backup(binary)
        if not backup:
            sys.exit(f"error: no {os.path.basename(binary)}.bak-* next to {binary}")
        tmp = binary + ".restore"
        shutil.copy2(backup, tmp)
        os.chmod(tmp, 0o755)
        os.replace(tmp, binary)
        print(f"restored {binary}\n     from {backup}")
        if args.restart:
            subprocess.run([binary, "service", "restart"], check=False)
        return 0

    with open(binary, "rb") as f:
        data = bytearray(f.read())

    section = bun_section(binary)
    lo, hi = section if section else (0, len(data))
    where = f".bun @0x{lo:x}-0x{hi:x}" if section else "whole file (no .bun section)"

    if args.scan:
        rows = scan(data, lo, hi, args.target)
        print(f"{binary}\n  scope: {where}")
        if not rows:
            print("  no (context, 32000) pairs found")
        for pos, value in rows:
            print(f"  0x{pos:x}  context={value} output=32000")
        return 0

    found, already, problems = locate(data, lo, hi, args.target)

    if problems:
        print(f"refusing to patch {binary}\n  scope: {where}", file=sys.stderr)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        print(
            "  hint: this build differs from the one this tool was written against.\n"
            "        run --scan to inspect, or --target with the value the build uses.",
            file=sys.stderr,
        )
        return 1

    for name in already:
        print(f"  already {args.target}: {name}")

    if not found:
        print(f"nothing to do ({where})")
        return 0

    for name, pos, value in found:
        print(f"  patch {name} @0x{pos:x}: {value} -> {args.target}")

    if args.dry_run:
        print("dry run: nothing written")
        return 0

    for _, pos, _ in found:
        data[pos:pos + 4] = struct.pack(I32, args.target)

    if not args.no_backup:
        backup = f"{binary}.bak-{int(time.time())}"
        shutil.copy2(binary, backup)
        print(f"  backup: {backup}")

    # Write beside the target then rename: a running server keeps its old inode,
    # and the path never points at a half-written binary.
    tmp = binary + ".new"
    with open(tmp, "wb") as f:
        f.write(data)
    shutil.copystat(binary, tmp)
    os.chmod(tmp, 0o755)
    os.replace(tmp, binary)
    print(f"patched {binary}")

    if args.verify:
        got, err = verify(binary, args.target)
        if err:
            print(f"  verify FAILED: {err}", file=sys.stderr)
            return 1
        print(f"  verify OK: inherited context = {got}")

    if args.restart:
        subprocess.run([binary, "service", "restart"], check=False)
    else:
        print("  restart to apply: opencode service restart")
    return 0


if __name__ == "__main__":
    sys.exit(main())

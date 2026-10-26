# opencode-context-1m

Raise the default context window in [opencode](https://opencode.ai) from **200k to 1M** with one command.

```bash
./opencode-context-1m.py --verify --restart
```

```
  patch Model.Info.limit.context @0x76f2fd4: 200000 -> 1000000
  patch SessionCompaction.cap @0x8b96d50: 200000 -> 1000000
  backup: ~/.opencode/bin/opencode.bak-1792989469
patched /home/you/.opencode/bin/opencode
  verify OK: inherited context = 1000000
```

---

## The problem

opencode lets you declare models in `opencode.jsonc` like this:

```jsonc
{
  "provider": {
    "my-gateway": {
      "npm": "@ai-sdk/openai-compatible",
      "options": { "baseURL": "https://example.com/v1" },
      "models": {
        "some-1m-model": { "name": "some-1m-model" }   // no limit field
      }
    }
  }
}
```

That model has a 1M context upstream. opencode will still treat it as **200k**, compact your session early, and throw away context you paid for. No error, no warning — just a smaller window than the model actually has.

Setting `limit` per model works, but you have to do it for every single model, and you have to know the real number for each one. Custom gateways usually don't publish it.

## Why config alone can't fix it

The 200k is not in your config — it's a hardcoded default inside the binary:

```
Model.Info  ->  limit:{context:200000, output:32000}
```

Every model without an explicit `limit` inherits it. There is no `default_context` / `compaction` knob that changes that fallback.

A second 200k lives in `SessionCompaction`:

```
gM = 200000    // cap used when a model has no usable limit
```

so even after fixing the model schema, compaction still caps at 200k. Both need to move together.

## What this tool does

Finds both constants by **byte signature** and rewrites the context field to `1000000`.

| Site | Signature anchor | What it controls |
|---|---|---|
| `Model.Info.limit.context` | `INT32_MAX, 200000, 32000` | context window inherited by models with no `limit` |
| `SessionCompaction.cap` | `200000, 32000, 2, 3, 0, 2000, 1500` | compaction trigger ceiling |

The tool does not trust a hardcoded file offset. It:

- parses the ELF section table and only searches inside `.bun` (the Bun bytecode section)
- requires **exactly one** match per site — 0 hits or 2+ hits means it aborts and writes nothing
- writes a timestamped backup (`opencode.bak-<epoch>`) beside the binary
- replaces the file atomically, so a running `opencode serve` keeps its old inode and is not corrupted mid-flight
- is idempotent — re-running reports `already 1000000` and exits 0
- `--verify` boots a throwaway server with a synthetic provider and reads `GET /api/model` to prove the inherited limit really is the new value

It touches **only** the context field. `output` stays at 32000, model-catalog limits and per-model config overrides are untouched.

## Usage

```bash
./opencode-context-1m.py                 # patch the binary found in PATH
./opencode-context-1m.py --verify        # patch, then prove it via /api/model
./opencode-context-1m.py --restart       # patch, then restart the background service
./opencode-context-1m.py --dry-run       # show what would change
./opencode-context-1m.py --scan          # list candidate sites, change nothing
./opencode-context-1m.py --restore       # roll back to the newest backup
./opencode-context-1m.py --binary /path/to/opencode
./opencode-context-1m.py --target 500000 # any context window, not just 1M
```

Exit codes: `0` patched / already patched, `1` refused or failed, `2` usage error.

Python 3.8+, standard library only. No dependencies.

## After `opencode upgrade`

An upgrade replaces the binary and your patch with it. Re-run the script:

```bash
./opencode-context-1m.py --verify --restart
```

If the new build changed the surrounding constants, the tool refuses rather than corrupting the binary. Use `--scan` to see what the new build has, and open an issue with the output.

## Verify it yourself

```bash
# before
opencode api --standalone GET /api/model | jq '.data[] | select(.providerID=="nara") | .limit'

# patch
./opencode-context-1m.py --restart

# after
opencode api GET /api/model | jq '.data[] | select(.providerID=="nara") | .limit'
# { "context": 1000000, "output": 32000 }
```

Models that have a real `limit` in opencode's model catalog keep their real value — the patch only fills in models that had nothing. If a model genuinely has 200k upstream, it still reports 200k. Check the distribution:

```bash
opencode api GET /api/model \
  | jq -r '.data[].limit.context' | sort -n | uniq -c | sort -rn
```

## Read this before you run it

**The patch makes opencode believe every unconfigured model has a 1M window.** If an upstream model actually has less, opencode will not compact in time and the request will fail with `400 context_length_exceeded` partway through a long session. That is the trade:

- before: silently truncated context, but never an overflow error
- after: full context, but an overflow error if you misconfigured a small model

Fix for any model where this bites — set its real limit in `opencode.jsonc`, which always wins over the patched default:

```jsonc
"small-model": { "name": "small-model", "limit": { "context": 200000, "output": 32000 } }
```

## Scope

Tested against **opencode v2.1.4**, Linux x86-64, Bun single-file executable. The signatures may not exist in other builds — the tool aborts cleanly when they don't. macOS and Windows builds are untested.

Patching a binary is a local modification of software you already run. It is not supported by the opencode project, and an upgrade discards it. If the project adds a config knob for the default window, use that instead and delete this.

## License

MIT

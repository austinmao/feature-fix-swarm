# Environment registry — one committed file every gate reads

`config/environments.yaml` is the repository's single declaration of what
environments exist, how to test each one, and which surfaces have a staging
instance. `/ffs-init` fills it in, `scripts/gsd/env-registry.sh` owns every
read and write of it, and `lib/gates.py` consults it before allowing a
production action.

Secrets appear here **by name only**. The file is read by CI and by autonomous
agents; a secret value in it is a leak, and `check` refuses one.

## Why one file

`lib/gates.py` already parsed a parity manifest (`_parse_parity_manifest_yaml`).
The registry reuses that parser instead of generating a second file from a
first, so there is no derived artifact to regenerate and nothing to drift.
`_resolve_registry` (`lib/gates.py:2305`) resolves it for every existing
`check-grant` call site, which makes those calls non-vacuous without editing a
single call site.

## Shape

```yaml
# config/environments.yaml
# schema: ffs.environments/v1

environments:          # fixed six-key rows
  - name: local
    kind: local
    base_url: none
    secret_names: []
    verified: null
    test_tier: fast

test_tiers:            # tier -> command; `covers` is optional
  - tier: fast
    command: python3 -m pytest lib/tests/test_gates.py -q
    covers:
      - lib/**

surfaces:              # OPTIONAL block; parsed verbatim by gates.py
  - surface: release
    staging_instance: none
```

Three blocks, three audiences:

- **`environments`** — prose for a human or an agent deciding where something
  runs. `verified:` is a date the operator sets by hand; `null` means nobody
  has confirmed this row against reality.
- **`test_tiers`** — flat scalars, consumed by `scripts/gsd/test-tier.sh`.
  See [CI templates and test tiers](ci-templates-and-tiers.md).
- **`surfaces`** — parsed verbatim by `lib/gates.py` and therefore load-bearing:
  a wrong row here changes gate behavior, not documentation. `staging_instance:
  none` is an explicit "this surface has no staging" declaration and fail-closes
  a promotion. A repo with no production surfaces omits the block; the schema is
  valid without it.

Schema authority for the first two blocks lives in `env-registry.sh`; the
surfaces block is always round-tripped through
`lib/gates.py:_load_manifest_text` rather than re-parsed, so the gate and the
validator can never disagree (`scripts/gsd/env-registry.sh:17-20`).

## Resolution and hard mode

Precedence (`lib/gates.py:2305`):

```
--manifest <path>  →  $FFS_ENV_REGISTRY  →  config/environments.yaml
                                         →  config/parity-manifest.yaml
```

With none of them present, gates behave exactly as they did before the registry
existed and print one `ENV-REGISTRY-ABSENT` advisory naming `/ffs-init`
(`lib/gates.py:2453`).

Under `--require-environments`, or `FFS_ENV_REGISTRY_REQUIRED=1`, the registry
becomes mandatory **and** the authority shifts: a caller-supplied registry's
verdict content is parsed from **HEAD bytes**, not the working tree
(`lib/gates.py:2326-2336`). A dirty or uncommitted registry can therefore never
widen a production gate — it can only refuse. Soft mode keeps the working-tree
taxonomy for its refusal classes.

## `/ffs-init` — the interview

`skills/ffs-init/SKILL.md`. The skill decides nothing and writes nothing: every
write routes through `env-registry.sh apply`. One batched session, three blocks:

1. **`environments`** — each detected row is presented with its `confidence:`
   and `evidence:`; the operator confirms, edits, or adds rows detection missed.
   Secrets are collected as NAMES (`PROD_API_TOKEN`), never values — a pasted
   value is refused, and `apply`'s leak scan would reject it anyway.
2. **`test_tiers`** — confirm or correct the tier→command mapping.
3. **`surfaces`** — isolated behind its own explicit confirm, because gates.py
   parses it verbatim. The `none` sentinel is explained before it is offered:
   it is a deliberate declaration, not a default.

Answers are assembled into a file created with `mktemp` **outside the
checkout**, applied, and deleted:

```bash
REPO_ROOT="$(git rev-parse --show-toplevel)"
ANSWERS="$(mktemp -t ffs-init-answers)"
# ...emit confirmed rows into "$ANSWERS"...
bash "$REPO_ROOT/scripts/gsd/env-registry.sh" apply --answers "$ANSWERS"
rm -f "$ANSWERS"
bash "$REPO_ROOT/scripts/gsd/env-registry.sh" check
```

### Modes

| Mode | Effect |
|---|---|
| `--detect-only` | Proposal YAML on stdout, zero writes inside the repo. stdout is itself a valid `--answers` file — redirect it outside the repo, edit, apply. |
| `--answers <file>` | Fully non-interactive apply. Missing required key → exit 1, nothing written. |
| `--yes` | Accept detected defaults with `verified: null`. Over an existing registry it **refuses** (never silently regenerates) and names `--update` / `--force` as the remedies. |
| `--update` | Merge: adds new rows only, preserves operator-set `verified:` dates and manual edits. The normal remedy when `--yes` refuses. |
| `--check` | Read-only validation. CI-runnable, zero network calls. |
| `--force` | Regenerate from scratch, discarding operator edits. Only after an explicit confirm that restates what is lost. |
| `--reset-declines` | Clear recorded declines so suppressed proposals surface again. |
| `--probe-gh` | Opt-in advisory on `detect`/`check` that checks reviewer protection on the prod GitHub environment. Never a gate; any failure is a silent skip. |

## `env-registry.sh` — five verbs

```
env-registry.sh <detect|check|render|apply|seed> [flags]
```

| Verb | What it does |
|---|---|
| `detect [--probe-gh]` | Proposes rows from repo evidence. Read-only. |
| `check [--manifest <path>] [--probe-gh]` | Schema, leak scan, referential integrity, stale-verified advisory, surfaces round-trip through gates.py. |
| `render [--manifest <path>]` | Renders `templates/ci/` — see [CI templates and test tiers](ci-templates-and-tiers.md). |
| `apply --answers <file> [--yes\|--update\|--force\|--reset-declines]` | The **only** write path. All-or-nothing, under `.ffs-init.lock`. |
| `seed [--manifest <path>]` | Emits preflight-manifest candidate rows (JSON array, names only) on stdout. Additive input to the authored manifest, never authoritative. |

Concurrency: a second `apply` fails fast rather than interleaving —
`ENV-REGISTRY-BUSY: another apply holds .ffs-init.lock`
(`scripts/gsd/env-registry.sh:1608`). Rerun after the other one finishes.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Success. Advisories (including stale-verified) exit 0 — an advisory is never a gate. |
| 1 | Usage, schema, referential, answers, or refusal: `ENV-REGISTRY-INVALID:` / `ENV-REGISTRY-REFUSED:` / `ENV-REGISTRY-BUSY:`. |
| 2 | Leak finding. |

Every diagnostic is **value-free**: it names the key, the line, and the expected
shape, and never echoes the bytes it read.

```
ENV-REGISTRY-INVALID: answers missing required key <key> — expected a
top-level '<key>:' list of rows each with '<k1>:' and '<k2>:'; nothing written
```

### Leak scan

`check` scans the resolved registry, `apply` scans the candidate bytes before
anything is written. Eight shape classes (`scripts/gsd/env-registry.sh:139-152`):
`pem-block`, `jwt`, `aws-access-key`, `provider-token-prefix`,
`credential-url`, `secret-assignment`, `hex-run`, `base64-run`. A finding
reports position and class only:

```
line N, key <name>, shape <class> — remedy: replace the literal with a NAME in secret_names
```

If the file is already committed the message appends the harder remedy: rotate
the credential, then rewrite history.

## Detection heuristics

`detect` proposes; it never decides. Every proposed row carries `confidence:`
and `evidence:`, and lands with `verified: null` for a human to confirm
(`scripts/gsd/env-registry.sh:815-931`).

| Heuristic | Trigger | Confidence |
|---|---|---|
| `vercel` | `vercel.json` or `.vercel/` → `prod` + `preview` | medium |
| `wrangler-env` | each `[env.X]` header in `wrangler.toml` | medium |
| `fly` | `fly.toml` → `prod`; `fly.<name>.toml` → one row each | low / medium |
| `compose` | any `docker-compose.*` / `compose.*` → `local` | high |
| `k8s-overlay` | each directory under `k8s/overlays` or `kustomize/overlays` | medium |
| `dotenv` | each `.env.<suffix>` (not `example`/`sample`/`template`); collects **key names only**, left of `=` | medium |
| `workflow-environment` | each `environment:` key in `.github/workflows/*.yml` | medium |
| `doppler` | each `config:` name in `doppler.yaml` | low |
| `bare` | nothing detected → a single `local` row | high |

An unreadable `.env.*` still emits its row, marked `file present, unread` — the
file's contents are never read for anything but key names.

## Declines

`.ffs-init.json` (repo root, committed, first key `"schema": "ffs.init/v1"`,
written only by `apply`) records what the operator turned down. The key is the
pair `(heuristic, concrete evidence value)` — e.g.
`("wrangler-env", "wrangler.toml:[env.staging]")` — so **new** evidence under a
declined heuristic re-proposes, while the same evidence stays quiet. Line
numbers are deliberately excluded from the key so a shifted header does not
resurrect a decline.

Suppression is announced with exactly one stderr line:

```
ADVISORY: <n> proposal(s) suppressed by declines in .ffs-init.json — run
'env-registry.sh apply --reset-declines' to clear them
```

## Where the registry is consumed

| Seam | What it reads |
|---|---|
| `lib/gates.py` `check-grant` | `surfaces` — a `staging_instance: none` surface fail-closes a production promotion (`docs/promotion-protocol.md`) |
| `/preflight` (`skills/preflight/SKILL.md:39`) | `env-registry.sh seed` — candidate manifest rows, secret NAMES only |
| CI (`.github/workflows/ci.yml:45`) | `env-registry.sh check` — typed: 0 ok / 1 schema-or-usage refusal / 2 leak |
| `scripts/gsd/test-tier.sh` | `test_tiers` — the only source of CI test commands |
| Promotion protocol rules 3, 4, 5 | environment scoping, secrets-by-name, per-environment verified dates |

## Related

- [CI templates and test tiers](ci-templates-and-tiers.md)
- [Promotion protocol](promotion-protocol.md)
- [Configuration](configuration.md)
- [Commands](commands.md)

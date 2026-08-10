---
name: ffs-init
description: "Initialize or refresh the FFS environment registry (config/environments.yaml): propose rows from repo evidence, review them in one batched session, apply atomically, validate, update in place. Never leaks a secret VALUE (names only) and never re-asks a declined proposal."
version: "1.0.0"
---

# /ffs-init [--detect-only|--answers <file>|--yes|--update|--check|--force|--reset-declines]

## Host dispatch contract

- Codex: invoke skills as `$skill`; use Codex collaboration roles and GPT-5.6 model tiers.
- Claude: invoke skills as `/skill`; use Claude Agent/Skill tools and Claude model aliases.
- Examples that name both hosts are routing contracts. Never send one host's command syntax to the other.
- A bare `/skill` in this shared source denotes the Claude form; Codex dispatches the same named skill as `$skill`.

Collects operator answers for the environment registry. This skill DECIDES
nothing and WRITES nothing: `scripts/gsd/env-registry.sh` owns detection,
validation, and the atomic write. Every write in this skill's flow routes
through `env-registry.sh apply` — the skill itself never touches
`config/environments.yaml` or `.ffs-init.json`, in any mode, ever.

## Script invocation idiom

The script lives at the ROADMAP-pinned path under the repo root — never
resolve it relative to this skill file:

```bash
REPO_ROOT="$(git rev-parse --show-toplevel)"
bash "$REPO_ROOT/scripts/gsd/env-registry.sh" <detect|check|render|apply> [flags]
```

## Interactive session (default, one batched pass)

Three question blocks mirror the registry's own structure, asked in ONE
batched interactive session — the operator answers everything once, then a
single `apply` writes atomically.

**Step 0 — detect.** Run `bash "$REPO_ROOT/scripts/gsd/env-registry.sh"
detect` and capture stdout. The output is a proposal YAML (and itself a
valid `--answers` file): every row carries `confidence:` and `evidence:`,
and every row is a proposal — detection proposes, never decides
(`verified: null`). Rows previously declined by the operator are suppressed
by the script; when that happens the script prints exactly one stderr
advisory — relay it to the operator verbatim:

> `ADVISORY: <n> proposal(s) suppressed by declines in .ffs-init.json — run
> 'env-registry.sh apply --reset-declines' to clear them`

**Block 1 — `environments` rows.** Present each proposed row (name, kind,
base_url, secret_names, test_tier) with its confidence and evidence; the
operator confirms or edits per row and may add rows detection missed.
Secrets are collected by NAME only — `secret_names` is a list of variable
names like `PROD_API_TOKEN`. Never ask the operator for a secret VALUE, and
refuse one if pasted: values belong in the secret manager, and `apply`'s
leak scan rejects any that reach the answers file.

**Block 2 — `test_tiers` classification.** Present the proposed
tier-to-command mapping (rows: `tier`, `command`; optional `covers`) for
confirm/edit. The operator corrects commands detection guessed at low
confidence.

**Block 3 — `surfaces` (its own gate).** Isolated from blocks 1-2 with its
own explicit confirm, because `gates.py` parses this block verbatim — a
wrong row here changes gate behavior, not just documentation. Rows map
`surface` → `staging_instance`. Explain the `none` sentinel before asking:
`staging_instance: none` is an explicit no-staging declaration and
fail-closed — it is not a default and must be chosen deliberately. A repo
with no production surfaces OMITS this block entirely (the registry is
schema-valid without it).

**Step 4 — write answers OUTSIDE the repo, then apply.** Assemble the
confirmed rows into an answers file created via `mktemp` outside the repo
tree — never write it inside the checkout:

```bash
ANSWERS="$(mktemp -t ffs-init-answers)"
# ...emit confirmed environments/test_tiers/surfaces rows into "$ANSWERS"...
bash "$REPO_ROOT/scripts/gsd/env-registry.sh" apply --answers "$ANSWERS"
rm -f "$ANSWERS"
```

`apply` is the ONLY write path: it validates the candidate bytes, then
writes `config/environments.yaml` and `.ffs-init.json` all-or-nothing under
a lock. Finish by running `bash "$REPO_ROOT/scripts/gsd/env-registry.sh"
check` and reporting its result to the operator.

## Exit-code taxonomy (per verb, pinned in 02-01)

- `0` — success (check advisories included; stale-verified is NEVER a gate)
- `1` — usage / schema / referential / answers / refusal
  (`ENV-REGISTRY-INVALID:` / `ENV-REGISTRY-REFUSED:` / `ENV-REGISTRY-BUSY:`
  prefixes)
- `2` — leak finding (guard convention; check on the resolved registry,
  apply on candidate bytes)
- `3` — render stub: stderr `render lands in phase 3 (REQ-302)`

Any concurrent `apply` fails FAST, never interleaves:

> `ENV-REGISTRY-BUSY: another apply holds .ffs-init.lock — remedy: retry
> after the concurrent apply completes`

Remedy: rerun the same `apply` after the concurrent one finishes.

## Non-interactive modes

### `--detect-only`

```bash
bash "$REPO_ROOT/scripts/gsd/env-registry.sh" detect
```

Writes NOTHING inside the repo. Emits the proposal YAML on stdout with
per-row `confidence:` + `evidence:`; detect's stdout IS a valid `--answers`
file — pipe it to a file outside the repo, edit, and feed it to `apply`.
There is no failure path that leaves partial state: detect either prints a
proposal (rc 0) or a typed `ENV-REGISTRY-INVALID:` line (rc 1) naming what
to fix.

### `--answers <file.yaml>` (fully non-interactive)

```bash
bash "$REPO_ROOT/scripts/gsd/env-registry.sh" apply --answers /path/outside/repo/answers.yaml
```

Answers-file shape (pinned in 02-01): required top-level keys
`environments:` (rows: `name`, `kind`; optional `base_url`, `secret_names`,
`verified`, `test_tier`, `confidence`, `evidence`) and `test_tiers:` (rows:
`tier`, `command`; optional `covers`, `confidence`, `evidence`). Optional:
`surfaces:` (rows: `surface`, `staging_instance`) and `declines:` (rows:
`heuristic`, `evidence`, optional `declined_at`). `confidence`/`evidence`
are dropped at emission — the written registry keeps the fixed six-key row
shape.

Missing required key → rc 1, nothing written (EDGE-008). The message names
the key and its expected form — key + line + expected shape only, never the
bytes it got:

> `ENV-REGISTRY-INVALID: answers missing required key <key> — expected a
> top-level '<key>:' list of rows each with '<k1>:' and '<k2>:'; nothing
> written`

Remedy: add the named key in the named shape to the answers file and rerun.

### `--yes` (accept detected defaults)

```bash
bash "$REPO_ROOT/scripts/gsd/env-registry.sh" apply --answers "$ANSWERS" --yes
```

Accepts detected defaults with `verified: null` everywhere. `--yes` never
silently regenerates: over an existing registry it refuses (rc 1) with the
remedy in the message:

> `ENV-REGISTRY-REFUSED: config/environments.yaml already exists — remedy:
> re-run with --update to merge NEW rows only, or --force to regenerate
> from scratch`

### `--update` (merge, preserve operator edits)

```bash
bash "$REPO_ROOT/scripts/gsd/env-registry.sh" apply --answers "$ANSWERS" --update
```

Diffs against the existing registry: adds/flags NEW rows only, and
preserves operator-set `verified:` dates and manual edits. This is the
default remedy when `--yes` refuses.

### `--check` (read-only validation, CI-runnable)

```bash
bash "$REPO_ROOT/scripts/gsd/env-registry.sh" check
```

Validates schema, secret-value leak scan (REQ-202a), referential integrity
(unique environment names; every `staging_instance` references a declared
`kind: staging` environment), stale-verified advisory (advisory only, never
a gate), and the surfaces block round-trip under gates.py. Failure classes:

- **Schema / referential break** → rc 1, `ENV-REGISTRY-INVALID:` naming the
  key, line, and expected form. Remedy: edit the named row and rerun.
- **Leak finding** → rc 2 with the fixed, value-free contract text
  (pinned in 02-01):

  > `line N, key <name>, shape <class> — remedy: replace the literal with a
  > NAME in secret_names`

  A committed file appends: `; this file is committed — rotate the
  credential, then rewrite history to remove it`. Shape classes:
  `hex-run base64-run secret-assignment credential-url aws-access-key
  provider-token-prefix jwt pem-block`. Non-identifier key prints
  `<non-identifier key at line N>`. Matched bytes never appear on either
  stream.

### `--reset-declines`

```bash
bash "$REPO_ROOT/scripts/gsd/env-registry.sh" apply --reset-declines
```

Clears recorded declines so suppressed proposals surface again on the next
`detect`.

### `--force` (regenerate from scratch)

```bash
bash "$REPO_ROOT/scripts/gsd/env-registry.sh" apply --answers "$ANSWERS" --force
```

Regenerates the registry from scratch, discarding operator edits. Only pass
`--force` after an explicit operator confirm — the skill must restate what
will be lost (all `verified:` dates and manual edits) and get a yes first.

## Decline semantics

Declines live in `.ffs-init.json` (repo root, COMMITTED, first key
`"schema": "ffs.init/v1"`, written ONLY by `apply`). Each decline is keyed
(heuristic, concrete evidence value), e.g.
`("wrangler-env", "wrangler.toml:[env.staging]")` — so NEW evidence
re-proposes, while the same evidence stays suppressed. Suppression is
announced with exactly one stderr advisory line (relay it verbatim):

> `ADVISORY: <n> proposal(s) suppressed by declines in .ffs-init.json — run
> 'env-registry.sh apply --reset-declines' to clear them`

## gh reviewer-protection probe (external prerequisite, opt-in)

Reviewer protection on the prod GitHub environment is an EXTERNAL
prerequisite, validated only by the OPT-IN `--probe-gh` advisory on
`detect`/`check` — never a gate. Plain `check` makes ZERO network calls even
when `gh` is authenticated; the probe flag is an operator extra. The verify
fences below run plain `check`/`detect` and are hermetic by construction.

## Verified against the committed registry

These fences execute on every skill-lint pass (fresh temp cwd, `REPO_ROOT`
exported, read-only, no network):

```bash verify
bash "$REPO_ROOT/scripts/gsd/env-registry.sh" check
```

```bash verify
bash "$REPO_ROOT/scripts/gsd/env-registry.sh" detect >/dev/null
```

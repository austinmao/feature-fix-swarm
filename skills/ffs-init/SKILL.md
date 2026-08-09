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

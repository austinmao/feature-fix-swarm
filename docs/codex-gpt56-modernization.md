# Codex and GPT-5.6 compatibility

feature-fix-swarm (FFS) installs one host-neutral source for each FFS skill.
GSD remains upstream-owned: FFS invokes the pinned GSD installer and does not
copy or rewrite GSD source artifacts.

## Compatibility matrix

| Component | Supported | Notes |
|---|---|---|
| GSD Core | `@opengsd/gsd-core@1.10.0` | Exact pin; both Claude and Codex profiles are installed |
| Codex CLI | `>=0.137.0,<0.148.0` | Tested on `0.146.x` and `0.147.x`; doctor and `gsd-run` fail outside the range |
| Node.js / npm | Node 22+ / npm 10+ | Required by the pinned GSD installer |
| Shell | macOS zsh, Ubuntu bash | Windows is not supported by this release |
| Claude Code | Current OAuth-backed CLI | Used for Claude-native runs and cross-vendor review |

Custom Codex model providers are not supported by `gsd-run`. The runner removes
`OPENAI_API_KEY` and uses the existing subscription-backed Codex OAuth session.

## Install scopes

Project scope is portable and takes precedence over user scope:

```bash
bash setup.sh --scope project --project-dir /absolute/path/to/repo
```

It creates relative links from both `.agents/skills` and `.claude/skills` to
the vendored FFS skill sources. Codex discovery uses `.agents/skills`; FFS does
not install into the legacy `.codex/skills` directory.

User scope creates manifest- and hash-managed copies in the matching user
roots:

```bash
bash setup.sh --scope user
```

For one transition release, `bash setup.sh` behaves as user scope and prints a
deprecation warning. Prefer an explicit scope in automation.

The external `prompt-master` skill is fetched at commit
`d15eabbe5d2122eedc060bae8a771381e9873d1b`, receives the small compatibility
patch in `vendor/prompt-master/codex-gpt56.patch`, and is installed with
`.agents/skills/prompt-master` as the project-canonical copy.

## Doctor contract

Run doctor before a native or isolated Codex launch:

```bash
bash setup.sh --doctor --scope project --project-dir /absolute/path/to/repo
bash setup.sh --doctor --scope user --json
```

JSON output uses schema `ffs.doctor/v1`. Exit status is:

- `0`: healthy, including same-version project/user duplicates reported as a
  degraded warning;
- `1`: actionable incompatibility, including different-version duplicates,
  edited legacy collisions, or an unsupported Codex CLI;
- `2`: invalid invocation or an internal doctor failure.

Doctor reports paths, hashes, and remediation. It never prints credential
values.

## Migration, backups, and rollback

Before replacing a managed path, setup records it under
`~/.cache/feature-fix-swarm/backups/<backup-id>/` with a manifest containing
its type, mode, target, and hash. Historical `.codex/skills` artifacts from
FFS v4.13.0 through v4.22.0 are removed only when their hash matches the
shipped catalog. Edited or unknown files are preserved and named in doctor
output.

To restore a recorded layout:

```bash
bash setup.sh --rollback <backup-id>
```

Rollback restores installer-owned discovery paths and the previous GSD pin.
It never modifies `.planning` and never overwrites an edited collision.

## Runner safety contract

`scripts/gsd/gsd-run.sh` resolves its single-flight lock through
`git rev-parse --git-common-dir`, so a primary checkout and its linked
worktrees share one lock. Run worktrees live below
`.claude/worktrees/<run-id>/`. The runner creates a registered detached Git
worktree there, seeds untracked `.planning` state once, and launches the
stateful CLI with that worktree as its current directory and sole writable
root. Resume reuses the same registered worktree.

The default sandbox is `workspace-write` with `approval_policy=never` and an
explicit binary network mode:

- `network_mode: none` denies network access;
- `network_mode: enabled` allows network access.

`network_purpose: docs|package-registry|general` is audit and grant metadata;
it is not a domain allowlist. Unsandboxed execution requires the exact
run-bound grant `sandbox:danger-full-access`, no older than 72 hours, and the
runner consumes and records that grant at launch. Because a workspace agent
can edit repository evidence, danger grants live in a user-global `0600` store
outside the checkout and must be issued explicitly by the operator:

```bash
python3 scripts/gsd/consume-danger-grant.py issue \
  ~/.cache/feature-fix-swarm/danger-grants.json <run-id> <ttl-hours> \
  "$(git rev-parse --path-format=absolute --git-common-dir)" \
  <gsd-skill> <network-mode>
```

The TTL must be greater than zero and no more than 72 hours. The grant is
bound to the canonical Git common directory and its hashed repository
identity, the exact GSD skill, network mode, and sandbox action. The runner
uses only the fixed user-global store; repo-local autonomy JSON and caller
path overrides cannot authorize unsandboxed execution. An interrupted run may
resume under the same consumed grant only while that exact binding remains
unexpired; a completed run cannot reuse it.

The temporary Codex home receives every manifest-owned GSD agent and skill,
plus the complete hook tree from the fixed `@opengsd/gsd-core@1.10.0` package.
The installed canonical registrations and hook hashes must match that package.
Only those registrations are rewritten and smoke-executed, using an absolute,
ownership-checked Node binary and a minimal `PATH`. `auth.json` must be owned by
the current user with mode `0600` and is copied writable. If Codex refreshes
OAuth, the result is synchronized back under a fixed user-global `flock`
only when the original file has not changed concurrently. Lock or permission
failures make an otherwise successful run fail; the compare-and-swap temp is
created beside `auth.json` so replacement never crosses filesystems.

Before launch FFS persists the runtime, Codex CLI version, exact models and
efforts, skill hashes, sandbox/network mode, and adversary provenance. Resume
refuses any drift in that tuple. Optional adversary fallback sets
`run_state.adversary.degraded=true`; degraded provenance cannot satisfy a gate
that requested an exact reviewer such as Fable.

## Model request schema

Model selection is intentionally typed:

```json
{"kind":"tier","name":"frontier"}
{"kind":"tier","name":"judgment"}
{"kind":"tier","name":"execution"}
{"kind":"tier","name":"volume"}
{"kind":"exact","id":"gpt-5.6-sol"}
```

`frontier` (spec 004) is the planning-only tier added alongside the original
three; it is not reachable through dynamic escalation.

Raw vendor model identifiers outside `kind: "exact"` fail lint. Default role
assignments live in `templates/model-requests.json`; the reproducible 18-case
evaluation corpus lives in `evals/gpt56/corpus.json`.

Reproduce the 72-run paired effort matrix (18 fixtures × two efforts × two
repetitions) with subscription-backed Codex OAuth:

```bash
env -u OPENAI_API_KEY python3 scripts/run-gpt56-eval.py
```

The runner uses an isolated temporary Codex home, records no response text,
persists latency/usage/retry/finding metadata in `evals/gpt56/results.json`,
and compare-and-swaps a refreshed OAuth credential back under the same global
file lock used by `gsd-run`. Synchronization runs from a `finally` boundary;
if it cannot complete, the rotated credential is preserved as a mode-`0600`
recovery file under `~/.cache/feature-fix-swarm/` before the temporary home is
removed.
Lower effort is selected only when every paired lower-effort repetition passes
its deterministic concept gates without a BLOCKER/HIGH or coverage regression;
the higher-effort baseline must also pass.

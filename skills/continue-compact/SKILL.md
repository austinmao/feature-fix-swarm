---
name: continue-compact
version: 1.0.0
description: Prepare a durable handoff and a host-native resume command before the built-in compact operation. Use when context is low or the user asks to continue after compaction.
---

# Continue after compaction

## Host dispatch contract

- Codex: invoke skills as `$skill`; use Codex collaboration roles and GPT-5.6 model tiers.
- Claude: invoke skills as `/skill`; use Claude Agent/Skill tools and Claude model aliases.
- Examples that name both hosts are routing contracts. Never send one host's command syntax to the other.
- A bare `/skill` in this shared source denotes the Claude form; Codex dispatches the same named skill as `$skill`.

Do not compact the session yourself. Do not redefine or shadow `/compact`; it
is a Codex/Claude built-in. Prepare the durable handoff and exact commands the
operator can paste at a logical task boundary.

## Workflow

1. Save a repo-local handoff focused on the active task.
   - Codex: invoke `$handoff` and `$context-save` when available.
   - Claude: invoke `/handoff` and `/context-save` when available.
2. Record the current phase boundary, decisions, blockers, changed files, and
   verification evidence. Never include secrets or credential values.
3. Build the resume command:
   - Active spec/`.planning` run, Codex: `$feature-implement NNN`.
   - Active spec/`.planning` run, Claude: `/feature-implement NNN`.
   - Add `--autonomous` only when a fresh preflight and applicable grants exist.
   - Otherwise use Codex `$prompt-master` or Claude `/prompt-master` to create a
     compact resume prompt from the handoff.
4. Append the exact resume command under `## Resume Prompt` in the handoff.
5. Before committing a handoff artifact, run the shared copy-then-scan-then-
   publish harness — it never scans a git index blob, only a private temp
   copy made before anything is staged:

   ```bash handoff-scan
   REPO_ROOT="${REPO_ROOT:-$(git rev-parse --show-toplevel)}"
   bash "$REPO_ROOT/scripts/gsd/publish-scanned-handoff.sh" \
     "$HANDOFF_PATH" --commit "docs(handoff): save durable handoff"
   ```

   A finding stops the commit with nothing added to the index or object
   database; a missing scanner warns once and continues.
5. Build an instruction under 250 characters. It starts with the active task
   and ends exactly: `After compact, execute the Resume Prompt in the handoff doc.`

## Output

Return only:

`Handoff saved -> <path>`

`Resume prompt: <exact command or prompt>`

`Phase: <from> -> <to>`

**Ready to compact. Copy and run:**

```text
/compact @<handoff-path> <compact instruction>
```

**After compaction, run:**

```text
<exact Resume Prompt>
```

If the handoff cannot be saved, omit the `@<handoff-path>` reference and state
that the resume prompt must be copied separately.

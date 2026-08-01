# Public launch checklist

This is the maintainer checklist for promoting Feature Fix Swarm. It keeps the
repository page, release story, security posture, and launch copy aligned.

## Repository metadata

Recommended description:

> Phase-gated, cross-model QA for Claude Code and Codex—catch agent mistakes
> before they compound.

Recommended topics:

`ai-agents`, `claude-code`, `codex`, `developer-tools`, `gsd`,
`quality-assurance`, `tdd`, `workflow-automation`, `agent-orchestration`,
`llm-tools`

- Add a homepage only when there is a maintained destination stronger than the
  README. Do not point at an unrelated personal or product site.
- Enable Discussions for usage questions and keep Issues focused on bugs and
  scoped proposals.
- Enable private vulnerability reporting before inviting broad usage.

## Social preview

Create a 1280 × 640 PNG under 1 MB with a solid background so it renders
predictably in light and dark social clients.

Suggested content:

```text
FEATURE FIX SWARM
Ship agent-built features without compounding mistakes.

Phase gates  •  Cross-model review  •  Claude + Codex
```

Keep the project name and outcome legible at thumbnail size. Avoid terminal
screenshots, tiny diagrams, unverified metrics, and a wall of badges. Upload the
image in the repository's Social preview setting; committing an image file does
not activate it automatically.

## Launch story

Every announcement should answer the same four questions:

1. **Audience:** developers and maintainers running multi-phase work through
   Claude Code or Codex.
2. **Problem:** late QA lets an early agent mistake contaminate every later
   task.
3. **Outcome:** each phase must prove itself before progress, followed by a
   fresh adversarial completion review.
4. **Why now:** v5 provides host-neutral skills, exact GSD ownership, safe
   install/rollback, GPT-5.6 tier routing, and explicit sandbox/auth controls.

Use one concrete walkthrough rather than a long feature inventory. A useful
demo is a small task run first with `--dry-run`, then executed through one
failed phase gate, a repair, and a final review verdict. Publish the commands
and sanitized artifacts so the proof is reproducible.

## Before announcing

- [ ] Merge the public-launch documentation and security workflows.
- [ ] Run CI, CodeQL, and OpenSSF Scorecard successfully on `main`.
- [ ] Configure the branch ruleset and required checks.
- [ ] Enable Dependabot alerts/security updates and private reporting.
- [ ] Confirm the latest release matches the README compatibility matrix.
- [ ] Rehearse project-scope install, user-scope install, doctor, and rollback
      from the release tag.
- [ ] Add repository description, topics, and social preview.
- [ ] Enable Discussions and seed a pinned “Start here” post.
- [ ] Record a short demo with no credentials, private repository names, or
      machine-specific paths.
- [ ] Publish a release note that leads with the user problem and outcome,
      followed by migration and security details.

## After launch

- Triage installation failures separately from workflow/design feedback.
- Turn repeated questions into documentation before adding more README copy.
- Keep screenshots, version claims, and model compatibility current.
- Publish security fixes and dependency updates promptly; avoid rewriting
  released history unless an actual secret requires removal.

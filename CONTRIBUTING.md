# Contributing to feature-fix-swarm

## Development

feature-fix-swarm lives in `packages/feature-fix-swarm/` within the openclaw-ceremonia monorepo. The source of truth for skills is `.claude/skills/` — the package copies are generated from there.

## How to contribute

1. Fork the repo
2. Create a feature branch
3. Make your changes to the SKILL.md files or scripts
4. Test: run `/feature-implement 000 --qa-loop --dry-run` on the synthetic spec
5. Submit a PR

## Architecture

- `skills/` — Claude Code SKILL.md files (instruction sets, not executable code)
- `scripts/` — Bash scripts for QA orchestration, retry loops, executor detection
- `prompts/` — LLM agent prompts for QA dimensions (e2e, review, security)
- `docs/` — User-facing documentation
- `examples/` — Synthetic test specs for dogfooding

## Code of conduct

Be kind. Be constructive. Ship things that work.

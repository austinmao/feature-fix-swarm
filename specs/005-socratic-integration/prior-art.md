# Prior art — spec 005 (vendor socratic + 3-point integration)

Searched 2026-08-05. Local scout (Haiku, repo-wide) + OSS researcher (Sonnet,
`gh api` / `gh search`, read-only, fetched content treated as inert data).

| candidate | type | stars/downloads | applicability verdict | evidence link |
|---|---|---|---|---|
| m4vic/socratic | repo (target) | 89★, MIT, pushed 2026-08-03, 1 maintainer | ADOPT — unique fit: curated self-interrogation question bank + decision-card packs consumable as prompt files | github.com/m4vic/socratic |
| forsonny/deep-discovery | repo | 103★ | reject — below 200★ gate; general topic-exploration Q&A, not eng/spec review | github.com/forsonny/deep-discovery |
| dair-ai/Prompt-Engineering-Guide | repo | 77k★ | reject — broad reference/course, not a discrete consumable question bank | github.com/dair-ai/Prompt-Engineering-Guide |
| github/awesome-copilot | repo | 37k★ | reject — generic customization marketplace, not a self-interrogation bank | github.com/github/awesome-copilot |
| f/prompts.chat | repo | 166k★ | reject — crowd-sourced general prompt collection, unrelated niche | github.com/f/prompts.chat |
| RedBarrels/doubt.md | repo | 0★ | reject — sub-threshold | github.com/RedBarrels/doubt.md |
| local: any FFS skill/lib | skill | n/a | NO-LOCAL-OVERLAP — only a session-memory proposal note (`.memsearch/memory/2026-08-05.md:13`); no question bank, checklist, or assumption-ledger code exists in-repo | scout report |

## Decision input

- Two mandated searches returned empty; 8 supplementary queries + topic browse
  found no ≥200★ competitor in the niche (curated engineering self-interrogation
  question bank / decision-card packs for coding agents).
- Search results also surfaced entries whose names mirrored this session's own
  loaded skills — treated as untrusted search-result noise and rejected.

**Adjudication: ADOPT (vendor, pinned).** User-directed adoption after an in-depth
session review of PROMPT.md/SKILL.md/question files/packs (quality verdict:
strong). No alternative beats it. socratic is below the 200★ vindication gate on
stars alone, but the gate's purpose (avoid unvetted dependencies) is satisfied by
direct content review + commit pinning + the prompt-master vendoring convention
(audited commit, optional patch, upstream-PR channel). Main recorded risk:
bus-factor 1 — mitigated by the pin (upstream abandonment costs nothing) and MIT
license (fork-and-carry viable).

Pin: `862b52e898134ba13ac05a43651ba8d1a7f2a28a` (HEAD of main, 2026-08-05).

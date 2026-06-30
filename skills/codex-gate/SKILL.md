---
name: codex-gate
description: "Compatibility alias for /review-gate. Prefer /review-gate for new work."
version: "1.0.1"
---

# /codex-gate

Compatibility alias for `/review-gate`.

Use `/review-gate` for the canonical host-neutral gate. It auto-selects the
opposite CLI for the active harness:

- in Codex, it reviews with Claude
- in Claude, it reviews with Codex

The legacy `/codex-gate` name remains only so older tasks, docs, and muscle
memory do not break. When you update task files or pipeline docs, prefer
`/review-gate`.

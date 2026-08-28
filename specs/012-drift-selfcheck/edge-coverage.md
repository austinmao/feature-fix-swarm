# spec-012 edge coverage (spec-decompose Step 2.5)

Shapes: REQ-01..03 = io (two paths) + text (path spellings); REQ-04 =
stateful (verdict stream must not change); REQ-05 = io (missing dir);
REQ-06/07 = structural (diff region, lint) — no data shape, no probes.

| REQ | category | status | reason / AC |
|---|---|---|---|
| REQ-01 | empty | resolved | empty `CONSUMER` arg already hits the existing usage error first (REQ-05); pinned by the missing-dir case |
| REQ-01 | idempotency | dismissed | N/A — pure read + exit; running twice is the same refusal |
| REQ-01 | concurrency | dismissed | N/A — no writes, no locks; parallel runs cannot interfere |
| REQ-02 | boundary | resolved | the two collision sources (unset default vs explicit equal SRC) are each a bats case (AC-002) |
| REQ-03 | encoding | resolved | path spelling variants (`./`, trailing slash, symlink) collapse under `pwd -P` — symlink pinned by AC-003; trailing-slash/`./` is EDGE-002, folded into the same bats case as a second assertion |
| REQ-03 | adjacency | resolved | a consumer dir that is a SIBLING of SRC (distinct realpath, shares a parent) must NOT be refused — covered by the existing distinct-SRC fixtures (both live under `$BATS_TEST_TMPDIR`) |
| REQ-04 | ordering | resolved | verdict line order is the glob order of `$SRC/*.sh $SRC/*.py` and is untouched (AC-006 keeps the loop byte-identical) |
| REQ-04 | idempotency | dismissed | N/A — already true of the current script and unchanged |
| REQ-05 | empty | resolved | nonexistent `GSD_SYNC_SRC` → `cd` fails → realpath falls back to the raw string → never equal to a real consumer dir → today's behaviour (EDGE-001); pinned as an assertion in the missing-dir case: no `SELF-COMPARE` text |
| REQ-05 | ordering | resolved | missing-dir error evaluated BEFORE the guard — its own bats assertion (AC-005) |
| REQ-06 | — | dismissed | N/A — structural; verified by the hunk-header check in ROADMAP success criterion 4 |
| REQ-07 | — | dismissed | N/A — lint gate |

Unresolved: none.

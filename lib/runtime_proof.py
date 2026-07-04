#!/usr/bin/env python3
"""Browser-proof bundle verification (v3.20.0).

Completion authority for browser QA lives in evidence, not agent self-report.
A phase's browser gate passes only when a proof.json bundle survives the
checks below. Each check defeats a named anti-pattern:

  content_assert + dom_excerpt required . curl-200-as-proof
  soft-404 marker scan on dom_excerpt   . 200-status "Not Found" pages
  url_final vs expect_url match         . screenshot-of-wrong-page
  console_errors must be present+empty  . post-hydration client death
  interactions >= 1 (functional)        . static frame passed off as "works"
  screenshot exists + non-empty + fresh . fabricated/stale artifacts
  --strict rejects driver=agent         . self-reported evidence tier

Drivers (descending trust): canary (recorded trace/video/HAR session),
playwright (scripted run), agent (LLM-driven browser; weakest, rejected by
--strict / RUNTIME_PROOF_STRICT=1 — parallels gates.py verify-done --strict).

Usage:
    python3 lib/runtime_proof.py verify .ralph/phase-2/proof.json \
        [--strict] [--max-age-min 240] [--allow-console REGEX]
    python3 lib/runtime_proof.py skeleton specs/NNN/scenarios.md \
        --out .ralph/phase-2/proof.json --base-url http://localhost:3000

Route through the evidence ledger so a checkbox flip is legal:
    python3 lib/gates.py run-gate T0XX -- \
        python3 lib/runtime_proof.py verify .ralph/phase-2/proof.json

Stdlib-only, no external deps (same constraint as gates.py).
"""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

VALID_DRIVERS = ("canary", "playwright", "agent")
VALID_KINDS = ("functional", "visual")
DEFAULT_MAX_AGE_MIN = 240

# Conservative, phrase-level markers — a bare "404" in normal copy must not
# false-positive, so every pattern needs surrounding error language.
SOFT_404_PATTERNS = [
    r"page (?:could )?not (?:be )?found",
    r"page (?:doesn'?t|does not) exist",
    r"\b404\b[^0-9]{0,10}(?:page\s+)?not\s+found",
    r"not\s+found[^0-9]{0,10}\b404\b",
    r"application error",
    r"internal server error",
    r"something went wrong",
    r"client-side exception",
    r"this page isn'?t working",
    r"unhandled runtime error",
]
SOFT_404_RE = re.compile("|".join(SOFT_404_PATTERNS), re.IGNORECASE)


def _check_scenario(sc, idx, now, max_age_min, allow_re):
    """Return findings for one scenario dict."""
    findings = []
    sid = sc.get("id") or f"scenario[{idx}]"

    kind = sc.get("kind")
    if kind not in VALID_KINDS:
        findings.append(f"{sid}: kind must be one of {VALID_KINDS}, got {kind!r}")

    status = sc.get("status")
    if status != "pass":
        findings.append(f"{sid}: status is {status!r} (must be 'pass')")

    # -- curl-200 defense: a positive content assertion is mandatory
    if not sc.get("content_assert"):
        findings.append(f"{sid}: content_assert missing/empty — HTTP status alone "
                        "is never proof; assert visible content")
    dom = sc.get("dom_excerpt")
    if not dom:
        findings.append(f"{sid}: dom_excerpt missing — capture rendered DOM text")
    else:
        m = SOFT_404_RE.search(dom)
        if m:
            findings.append(f"{sid}: soft-404 marker in rendered DOM: {m.group(0)!r}")

    http_status = sc.get("http_status")
    if isinstance(http_status, int) and http_status >= 400:
        findings.append(f"{sid}: http_status {http_status}")

    # -- wrong-page defense: final URL after redirects must be recorded
    url_final = sc.get("url_final")
    if not url_final:
        findings.append(f"{sid}: url_final missing — record page.url() after "
                        "redirects so the screenshot provably shows this page")
    else:
        expect = sc.get("expect_url")
        if expect and expect not in url_final:
            findings.append(f"{sid}: url_final {url_final!r} does not contain "
                            f"expect_url {expect!r} (wrong page?)")

    # -- hydration defense: console must have been read, and be clean
    console = sc.get("console_errors")
    if console is None:
        findings.append(f"{sid}: console_errors field missing — absence means "
                        "'nobody looked', not 'zero errors'")
    else:
        real = [e for e in console if not (allow_re and allow_re.search(str(e)))]
        if real:
            findings.append(f"{sid}: console errors present: {real[:3]}")

    # -- alive defense: functional scenarios must interact unless declared static
    if kind == "functional" and not sc.get("static"):
        if not isinstance(sc.get("interactions"), int) or sc["interactions"] < 1:
            findings.append(f"{sid}: functional scenario with no interactions — "
                            "click/fill something or declare \"static\": true")

    # -- artifact defense: screenshot must exist, be non-empty, and be fresh
    shot = sc.get("screenshot")
    if not shot:
        findings.append(f"{sid}: screenshot path missing")
    else:
        p = Path(shot).expanduser()
        if not p.is_file():
            findings.append(f"{sid}: screenshot not found: {shot}")
        elif p.stat().st_size == 0:
            findings.append(f"{sid}: screenshot is empty: {shot}")
        else:
            age_min = (now - p.stat().st_mtime) / 60
            if age_min > max_age_min:
                findings.append(f"{sid}: screenshot stale ({age_min:.0f}min old > "
                                f"{max_age_min}min max) — evidence must come from "
                                "this run")
    return findings


def _cross_check_canary(proof, findings):
    """driver=canary: session results.json must exist and agree."""
    session = proof.get("canary_session")
    if not session:
        findings.append("canary driver requires canary_session path "
                        "(~/.canary/sessions/<id>)")
        return
    results = Path(session).expanduser() / "results.json"
    if not results.is_file():
        findings.append(f"canary results.json not found: {results}")
        return
    try:
        data = json.loads(results.read_text())
    except (OSError, json.JSONDecodeError) as e:
        findings.append(f"canary results.json unreadable: {e}")
        return
    steps = data.get("steps") or []
    if not steps:
        findings.append("canary results.json has no steps")
    for step in steps:
        if step.get("status") not in ("pass", "passed", "ok"):
            findings.append(f"canary step failed: {step.get('name')} "
                            f"(status={step.get('status')!r})")


def verify_proof(path, strict=False, max_age_min=DEFAULT_MAX_AGE_MIN,
                 allow_console=None):
    """Verify a proof.json bundle. Returns (ok, findings)."""
    findings = []
    p = Path(path).expanduser()
    if not p.is_file():
        return False, [f"proof file not found: {path}"]
    try:
        proof = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError) as e:
        return False, [f"proof file unreadable: {e}"]

    driver = proof.get("driver")
    if driver not in VALID_DRIVERS:
        findings.append(f"driver must be one of {VALID_DRIVERS}, got {driver!r}")
    elif strict and driver == "agent":
        findings.append("strict mode rejects driver=agent (self-reported "
                        "evidence tier) — use canary or playwright")

    scenarios = proof.get("scenarios")
    if not scenarios:
        findings.append("no scenarios — empty bundle proves nothing")
        return False, findings

    allow_re = re.compile(allow_console) if allow_console else None
    now = time.time()
    for i, sc in enumerate(scenarios):
        findings.extend(_check_scenario(sc, i, now, max_age_min, allow_re))

    if driver == "canary":
        _cross_check_canary(proof, findings)

    return not findings, findings


# ---------------------------------------------------------------- skeleton

SCENARIO_HEADING_RE = re.compile(r"^##\s+([A-Za-z0-9_-]+)\s*:\s*(.+)$")


def emit_skeleton(scenarios_md, out_path, base_url):
    """Emit an UNFILLED proof.json template from a scenarios.md file.

    Headings of the form '## <ID>: <title>' become scenario stubs. The
    template intentionally fails `verify` until a real browser run fills it.
    """
    text = Path(scenarios_md).read_text()
    scenarios = []
    for line in text.splitlines():
        m = SCENARIO_HEADING_RE.match(line.strip())
        if not m:
            continue
        sid, title = m.group(1), m.group(2).strip()
        kind = "visual" if re.search(r"\bvisual|design\b", title, re.I) else "functional"
        scenarios.append({
            "id": sid,
            "title": title,
            "kind": kind,
            "status": "UNFILLED",
            "url": "",
            "url_final": "",
            "expect_url": "",
            "content_assert": "",
            "dom_excerpt": "",
            "console_errors": None,
            "interactions": 0,
            "screenshot": "",
        })
    proof = {
        "version": 1,
        "driver": "agent",
        "base_url": base_url,
        "scenarios": scenarios,
    }
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text(json.dumps(proof, indent=2) + "\n")
    return len(scenarios)


# ---------------------------------------------------------------- CLI

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("verify", help="verify a proof.json bundle; exit 0 iff OK")
    v.add_argument("proof")
    v.add_argument("--strict", action="store_true",
                   help="reject driver=agent (or RUNTIME_PROOF_STRICT=1)")
    v.add_argument("--max-age-min", type=int, default=DEFAULT_MAX_AGE_MIN)
    v.add_argument("--allow-console", default=None,
                   help="regex for console errors to tolerate")

    s = sub.add_parser("skeleton", help="emit UNFILLED proof template from scenarios.md")
    s.add_argument("scenarios_md")
    s.add_argument("--out", required=True)
    s.add_argument("--base-url", default="http://localhost:3000")

    args = parser.parse_args(argv)

    if args.cmd == "verify":
        strict = args.strict or os.environ.get("RUNTIME_PROOF_STRICT") == "1"
        ok, findings = verify_proof(args.proof, strict=strict,
                                    max_age_min=args.max_age_min,
                                    allow_console=args.allow_console)
        for f in findings:
            print(f"  - {f}")
        print(f"RUNTIME-PROOF: {'PASS' if ok else 'FAIL'} ({args.proof})")
        return 0 if ok else 1

    if args.cmd == "skeleton":
        n = emit_skeleton(args.scenarios_md, args.out, args.base_url)
        print(f"RUNTIME-PROOF: skeleton with {n} scenarios -> {args.out}")
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main())

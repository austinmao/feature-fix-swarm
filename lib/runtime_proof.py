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
  screenshot magic-byte image check     . `printf x > shot.png` forgeries
  playwright needs fresh artifact file  . forged driver tier w/o a real run
  driver vs browser-proof.txt match     . claiming a driver you didn't use
  coverage vs scenarios.md (all IDs)    . proving one easy scenario, skipping rest
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
import stat
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

# Real screenshots are PNG/JPEG/GIF/WebP — anything else is a forged artifact.
IMAGE_MAGIC_PREFIXES = (b"\x89PNG", b"\xff\xd8\xff", b"GIF8", b"RIFF")


def _is_real_int(value):
    """JSON integer, excluding bool (a Python int subclass)."""
    return isinstance(value, int) and not isinstance(value, bool)


def _is_nonempty_string(value):
    return isinstance(value, str) and bool(value.strip())


def _check_scenario(
    sc,
    idx,
    now,
    max_age_min,
    allow_re,
    artifact_root=None,
    require_relative_screenshot=False,
):
    """Return findings for one scenario dict."""
    findings = []
    fallback_sid = f"scenario[{idx}]"
    if not isinstance(sc, dict):
        return [f"{fallback_sid}: scenario must be an object, got {type(sc).__name__}"]

    raw_sid = sc.get("id")
    sid = raw_sid if _is_nonempty_string(raw_sid) else fallback_sid
    if not _is_nonempty_string(raw_sid):
        findings.append(f"{fallback_sid}: id must be a non-empty string")

    kind = sc.get("kind")
    if kind not in VALID_KINDS:
        findings.append(f"{sid}: kind must be one of {VALID_KINDS}, got {kind!r}")

    status = sc.get("status")
    if status != "pass":
        findings.append(f"{sid}: status is {status!r} (must be 'pass')")

    # -- curl-200 defense: a positive content assertion is mandatory
    content_assert = sc.get("content_assert")
    if not _is_nonempty_string(content_assert):
        findings.append(f"{sid}: content_assert must be a non-empty string — "
                        "HTTP status alone is never proof; assert visible content")
    dom = sc.get("dom_excerpt")
    if not _is_nonempty_string(dom):
        findings.append(f"{sid}: dom_excerpt must be a non-empty string — "
                        "capture rendered DOM text")
    else:
        m = SOFT_404_RE.search(dom)
        if m:
            findings.append(f"{sid}: soft-404 marker in rendered DOM: {m.group(0)!r}")

    if "http_status" in sc:
        http_status = sc["http_status"]
        if not _is_real_int(http_status):
            findings.append(f"{sid}: http_status must be an integer when present")
        elif http_status >= 400:
            findings.append(f"{sid}: http_status {http_status}")

    # -- wrong-page defense: final URL after redirects must be recorded
    url_final = sc.get("url_final")
    if not _is_nonempty_string(url_final):
        findings.append(f"{sid}: url_final must be a non-empty string — record "
                        "page.url() after redirects so the screenshot provably "
                        "shows this page")
    else:
        expect = sc.get("expect_url")
        if expect is not None and not isinstance(expect, str):
            findings.append(f"{sid}: expect_url must be a string when present")
        elif expect and expect not in url_final:
            findings.append(f"{sid}: url_final {url_final!r} does not contain "
                            f"expect_url {expect!r} (wrong page?)")

    # -- hydration defense: console must have been read, and be clean
    console = sc.get("console_errors")
    if not isinstance(console, list):
        findings.append(f"{sid}: console_errors must be a list — absence means "
                        "'nobody looked', not 'zero errors'")
    else:
        if any(not isinstance(error, str) for error in console):
            findings.append(f"{sid}: console_errors entries must be strings")
        else:
            real = [
                error
                for error in console
                if not (allow_re and allow_re.search(error))
            ]
            if real:
                findings.append(f"{sid}: console errors present: {real[:3]}")

    # -- alive defense: functional scenarios must interact unless declared static
    static = sc.get("static", False)
    if "static" in sc and not isinstance(static, bool):
        findings.append(f"{sid}: static must be a boolean when present")
    if (
        kind == "functional"
        and static is not True
        and (not _is_real_int(sc.get("interactions")) or sc["interactions"] < 1)
    ):
        findings.append(f"{sid}: functional scenario interactions must be an "
                        "integer >= 1 — click/fill something or declare "
                        "\"static\": true")

    # -- artifact defense: screenshot must exist, be non-empty, and be fresh
    shot = sc.get("screenshot")
    if not _is_nonempty_string(shot):
        findings.append(f"{sid}: screenshot must be a non-empty string path")
    else:
        p = Path(shot).expanduser()
        if require_relative_screenshot and p.is_absolute():
            findings.append(
                f"{sid}: Canary screenshot must be a relative locator rooted "
                f"inside its session: {shot}"
            )
            return findings
        if artifact_root is not None and not p.is_absolute():
            if ".." in p.parts:
                findings.append(f"{sid}: screenshot relative locator escapes Canary session: {shot}")
                return findings
            root = artifact_root.resolve()
            unresolved = root / p
            cursor = root
            has_symlink = False
            for part in p.parts:
                cursor = cursor / part
                if cursor.is_symlink():
                    has_symlink = True
                    break
            try:
                resolved = unresolved.resolve(strict=True)
                resolved.relative_to(root)
            except (OSError, ValueError):
                findings.append(f"{sid}: screenshot escapes or is missing from Canary session: {shot}")
                return findings
            if has_symlink:
                findings.append(f"{sid}: screenshot locator contains a symlink: {shot}")
                return findings
            p = resolved
        if not p.is_file():
            findings.append(f"{sid}: screenshot not found: {shot}")
        elif p.stat().st_size == 0:
            findings.append(f"{sid}: screenshot is empty: {shot}")
        else:
            with open(p, "rb") as fh:
                head = fh.read(8)
            if not head.startswith(IMAGE_MAGIC_PREFIXES):
                findings.append(f"{sid}: screenshot is not an image "
                                f"(bad magic bytes): {shot}")
            age_min = (now - p.stat().st_mtime) / 60
            if age_min > max_age_min:
                findings.append(f"{sid}: screenshot stale ({age_min:.0f}min old > "
                                f"{max_age_min}min max) — evidence must come from "
                                "this run")
    return findings


def _resolve_canary_session(session, *, strict=False):
    """Resolve a value-free session id without following a symlink boundary."""
    if isinstance(session, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{2,159}", session):
        home = Path.home()
        cursor = home
        for part in (".canary", "sessions", session):
            cursor = cursor / part
            if cursor.is_symlink():
                return None, f"canary session boundary contains a symlink: {cursor}"
        return cursor, None
    if strict:
        return None, (
            "strict Canary proof requires a canonical session id under "
            "~/.canary/sessions (absolute and relative session paths are rejected)"
        )
    if not isinstance(session, (str, os.PathLike)):
        return None, "canary_session must be a session id or filesystem path"
    path = Path(session).expanduser()
    if path.is_symlink():
        return None, f"canary session path is a symlink: {path}"
    return path, None


def _canary_step_scenario_id(step, scenario_ids):
    """Return the unique proof scenario ID named by a Canary result step."""
    for field in ("scenarioId", "scenario_id", "id"):
        explicit = step.get(field)
        if isinstance(explicit, str) and explicit:
            return explicit if explicit in scenario_ids else None
    name = step.get("name")
    if not isinstance(name, str) or not name:
        return None
    matches = [
        scenario_id
        for scenario_id in scenario_ids
        if name == scenario_id
        or re.match(rf"^{re.escape(scenario_id)}(?:[-_.:]|$)", name)
    ]
    return matches[0] if len(matches) == 1 else None


def _cross_check_canary(proof, findings, *, strict=False):
    """driver=canary: session results.json must exist and agree."""
    session = proof.get("canary_session")
    if not session:
        findings.append("canary driver requires canary_session path "
                        "(~/.canary/sessions/<id>)")
        return
    session_dir, session_error = _resolve_canary_session(session, strict=strict)
    if session_error:
        findings.append(session_error)
        return
    results = session_dir / "results.json"
    if results.is_symlink():
        findings.append(f"canary results.json must not be a symlink: {results}")
        return
    if not results.is_file():
        findings.append(f"canary results.json not found: {results}")
        return
    try:
        resolved_session = session_dir.resolve(strict=True)
        resolved_results = results.resolve(strict=True)
        resolved_results.relative_to(resolved_session)
    except (OSError, ValueError):
        findings.append(
            f"canary results.json must be a regular file inside its session: {results}"
        )
        return
    descriptor = None
    try:
        descriptor = os.open(
            resolved_results,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            findings.append(f"canary results.json is not a regular file: {results}")
            return
        with os.fdopen(descriptor, encoding="utf-8") as results_file:
            descriptor = None
            data = json.load(results_file)
    except (OSError, json.JSONDecodeError) as e:
        findings.append(f"canary results.json unreadable: {e}")
        return
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if not isinstance(data, dict):
        findings.append("canary results.json must contain an object")
        return
    steps = data.get("steps")
    if not isinstance(steps, list):
        findings.append("canary results.json steps must be a list")
        return
    if not steps:
        findings.append("canary results.json has no steps")
        return
    proof_scenarios = proof.get("scenarios") or []
    proof_scenario_ids = [
        scenario.get("id")
        for scenario in proof_scenarios
        if isinstance(scenario, dict) and isinstance(scenario.get("id"), str)
    ]
    duplicate_scenario_ids = {
        scenario_id
        for scenario_id in proof_scenario_ids
        if proof_scenario_ids.count(scenario_id) > 1
    }
    for scenario_id in sorted(duplicate_scenario_ids):
        findings.append(
            f"{scenario_id}: duplicate proof scenario ID cannot bind one-to-one "
            "to Canary results"
        )
    proof_by_id = {
        scenario.get("id"): scenario
        for scenario in proof_scenarios
        if isinstance(scenario, dict) and isinstance(scenario.get("id"), str)
    }
    bound_steps = {}
    for step in steps:
        if not isinstance(step, dict):
            findings.append("canary results.json contains a non-object step")
            continue
        if step.get("status") not in ("pass", "passed", "ok"):
            findings.append(f"canary step failed: {step.get('name')} "
                            f"(status={step.get('status')!r})")
        scenario_id = _canary_step_scenario_id(step, set(proof_by_id))
        if scenario_id is None:
            findings.append(
                f"canary step is unbound to a proof scenario: {step.get('name')!r}"
            )
            continue
        if scenario_id in bound_steps:
            findings.append(
                f"{scenario_id}: multiple Canary result steps bind to one proof scenario"
            )
            continue
        bound_steps[scenario_id] = step
        proof_status = proof_by_id[scenario_id].get("status")
        step_status = "pass" if step.get("status") in ("pass", "passed", "ok") else step.get("status")
        if proof_status != step_status:
            findings.append(
                f"{scenario_id}: proof status {proof_status!r} does not match "
                f"Canary step status {step.get('status')!r}"
            )
    for scenario_id in sorted(set(proof_by_id) - set(bound_steps)):
        findings.append(
            f"{scenario_id}: no matching Canary results.json step"
        )


def _cross_check_playwright(proof, findings, now, max_age_min):
    """driver=playwright: a real run leaves artifacts (trace/results/report).

    Without this, "driver": "playwright" is a free string that dodges the
    strict-mode agent rejection without ever opening a browser.
    """
    art = proof.get("playwright_artifact")
    if not art:
        findings.append("playwright driver requires playwright_artifact "
                        "(trace.zip / results.json / report file from the run)")
        return
    if not isinstance(art, (str, os.PathLike)):
        findings.append("playwright_artifact must be a filesystem path")
        return
    p = Path(art).expanduser()
    if not p.is_file():
        findings.append(f"playwright artifact not found: {art}")
        return
    if p.stat().st_size == 0:
        findings.append(f"playwright artifact is empty: {art}")
        return
    age_min = (now - p.stat().st_mtime) / 60
    if age_min > max_age_min:
        findings.append(f"playwright artifact stale ({age_min:.0f}min old > "
                        f"{max_age_min}min max) — evidence must come from this run")


BROWSER_PROOF_DRIVER_RE = re.compile(r"^DRIVER:(\S+)", re.MULTILINE)


def _cross_check_driver(proof, proof_path, browser_proof, findings):
    """proof.driver must match the driver browser-proof.sh actually resolved.

    Kills the forgery of claiming a higher-trust driver than the one
    available (or the reverse: falling back to agent when a real driver
    was installed). Auto-detects browser-proof.txt next to the bundle.
    """
    bp = browser_proof
    if bp is None:
        sibling = Path(proof_path).expanduser().parent / "browser-proof.txt"
        if sibling.is_file():
            bp = str(sibling)
    if bp is None:
        return
    try:
        text = Path(bp).expanduser().read_text()
    except OSError as e:
        findings.append(f"browser-proof file unreadable: {e}")
        return
    m = BROWSER_PROOF_DRIVER_RE.search(text)
    if not m:
        return
    resolved = m.group(1)
    claimed = proof.get("driver")
    if resolved in VALID_DRIVERS and claimed != resolved:
        findings.append(f"driver mismatch: proof claims {claimed!r} but "
                        f"browser-proof resolved {resolved!r} — use the "
                        "resolved driver")


def _check_coverage(proof, scenarios_md, findings, kind="all"):
    """Every scenario ID in scenarios.md whose SOURCE kind matches the
    caller-requested kind must be present — proving one easy scenario must
    not pass the phase. The requirement is keyed on the source file, never
    on the bundle's self-declared kinds (codex round: a forged bundle
    marking everything "visual" must not shrink the functional requirement).
    Bundle scenarios matching a source ID must also agree on kind — marking
    a functional flow "visual" to dodge the interactions check is caught."""
    try:
        text = Path(scenarios_md).expanduser().read_text()
    except (OSError, TypeError):
        findings.append(f"scenarios source not found: {scenarios_md}")
        return
    source = _parse_scenarios_md(text)
    source_kind = {sid: k for sid, _title, k in source}
    bundle_ids = set()
    for sc in proof.get("scenarios", []):
        if not isinstance(sc, dict):
            continue
        sid = sc.get("id")
        if not isinstance(sid, str):
            continue
        bundle_ids.add(sid)
        want = source_kind.get(sid)
        if want and sc.get("kind", "functional") != want:
            findings.append(f"{sid}: kind mismatch — scenarios.md says "
                            f"{want!r}, bundle claims "
                            f"{sc.get('kind', 'functional')!r}")
    missing = [sid for sid, _title, k in source
               if kind in ("all", k) and sid not in bundle_ids]
    if missing:
        findings.append("coverage incomplete — scenarios.md IDs missing from "
                        f"bundle: {', '.join(missing)}")


def verify_proof(path, strict=False, max_age_min=DEFAULT_MAX_AGE_MIN,
                 allow_console=None, scenarios_md=None, browser_proof=None,
                 kind="all"):
    """Verify a proof.json bundle. Returns (ok, findings)."""
    findings = []
    p = Path(path).expanduser()
    if not p.is_file():
        return False, [f"proof file not found: {path}"]
    try:
        proof = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError) as e:
        return False, [f"proof file unreadable: {e}"]
    if not isinstance(proof, dict):
        return False, ["proof file must contain an object"]

    driver = proof.get("driver")
    if driver not in VALID_DRIVERS:
        findings.append(f"driver must be one of {VALID_DRIVERS}, got {driver!r}")
    elif strict and driver == "agent":
        findings.append("strict mode rejects driver=agent (self-reported "
                        "evidence tier) — use canary or playwright")

    scenarios = proof.get("scenarios")
    if not isinstance(scenarios, list):
        findings.append("scenarios must be a list")
        return False, findings
    if not scenarios:
        findings.append("no scenarios — empty bundle proves nothing")
        return False, findings

    allow_re = re.compile(allow_console) if allow_console else None
    now = time.time()
    artifact_root = None
    session = proof.get("canary_session")
    if driver == "canary" and session:
        resolved_session, session_error = _resolve_canary_session(session, strict=strict)
        if session_error is None:
            artifact_root = resolved_session
    for i, sc in enumerate(scenarios):
        findings.extend(
            _check_scenario(
                sc,
                i,
                now,
                max_age_min,
                allow_re,
                artifact_root,
                require_relative_screenshot=driver == "canary",
            )
        )

    if driver == "canary":
        _cross_check_canary(proof, findings, strict=strict)
    elif driver == "playwright":
        _cross_check_playwright(proof, findings, now, max_age_min)

    _cross_check_driver(proof, p, browser_proof, findings)

    # coverage: explicit arg wins; else the source the skeleton embedded
    md = scenarios_md or proof.get("scenarios_source")
    if md:
        _check_coverage(proof, md, findings, kind=kind)

    return not findings, findings


# ---------------------------------------------------------------- skeleton

SCENARIO_HEADING_RE = re.compile(
    r"^##\s+([A-Za-z0-9_-]+)(?:\s+\[(functional|visual)\])?\s*:\s*(.+)$")


def _parse_scenarios_md(text):
    """Parse '## <ID> [kind]: <title>' headings -> [(id, title, kind)].

    Kind comes ONLY from the explicit [visual]/[functional] heading tag;
    untagged defaults to functional (codex round 3 H2: title-keyword
    inference let a functional flow titled "visual polish ..." drop out
    of the --kind functional required set).
    """
    out = []
    for line in text.splitlines():
        m = SCENARIO_HEADING_RE.match(line.strip())
        if not m:
            continue
        sid, kind, title = m.group(1), m.group(2), m.group(3).strip()
        out.append((sid, title, kind or "functional"))
    return out


def emit_skeleton(scenarios_md, out_path, base_url):
    """Emit an UNFILLED proof.json template from a scenarios.md file.

    Headings of the form '## <ID>: <title>' become scenario stubs. The
    template intentionally fails `verify` until a real browser run fills it.
    """
    text = Path(scenarios_md).read_text()
    scenarios = []
    for sid, title, kind in _parse_scenarios_md(text):
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
        "scenarios_source": str(scenarios_md),
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
    v.add_argument("--scenarios", default=None,
                   help="scenarios.md to enforce coverage against "
                        "(overrides the bundle's scenarios_source)")
    v.add_argument("--browser-proof", default=None,
                   help="browser-proof.txt to cross-check the driver against "
                        "(auto-detected next to the bundle when omitted)")
    v.add_argument("--kind", default="all",
                   choices=("functional", "visual", "all"),
                   help="which source scenario kinds this bundle must cover "
                        "(functional for proof.json, visual for "
                        "design-proof.json; default all)")

    s = sub.add_parser("skeleton", help="emit UNFILLED proof template from scenarios.md")
    s.add_argument("scenarios_md")
    s.add_argument("--out", required=True)
    s.add_argument("--base-url", default="http://localhost:3000")

    args = parser.parse_args(argv)

    if args.cmd == "verify":
        strict = args.strict or os.environ.get("RUNTIME_PROOF_STRICT") == "1"
        ok, findings = verify_proof(args.proof, strict=strict,
                                    max_age_min=args.max_age_min,
                                    allow_console=args.allow_console,
                                    scenarios_md=args.scenarios,
                                    browser_proof=args.browser_proof,
                                    kind=args.kind)
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

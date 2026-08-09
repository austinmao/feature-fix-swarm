#!/usr/bin/env bash
# env-registry.sh — the sole deterministic, atomic writer behind /ffs-init
# (spec-007 phase 2, REQ-202/202a/203). Four verbs:
#   detect  — read-only, propose-never-decide heuristics; proposal YAML on
#             stdout in the --answers file shape; ZERO writes in the repo
#   check   — read-only validation: leak scan (REQ-202a), constrained-subset
#             schema for environments/test_tiers, referential integrity,
#             stale-verified advisory, and the surfaces block through
#             lib/gates.py:_load_manifest_text (audit row 18 — NEVER a
#             re-implemented surfaces parser)
#   render  — stub; lands in phase 3 (REQ-302)
#   apply   — the single all-or-nothing writer of config/environments.yaml
#             + .ffs-init.json (audit row 25)
#
# gates.py surfaces authority: check/apply import gates and call
# _load_manifest_text on candidate bytes — import is safe, the CLI sits under
# __main__ (gates.py:3148). Schema authority for the non-surfaces blocks lives
# HERE (gates.py stays minimal by design).
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

usage() {
  cat >&2 <<'USAGE'
usage: env-registry.sh <detect|check|render|apply> [flags]
  detect  [--probe-gh]
          propose registry rows from repo evidence; proposal YAML on stdout
          (a valid --answers file); writes NOTHING inside the repo
  check   [--manifest <path>] [--probe-gh]
          validate a registry: schema + leak scan + referential integrity +
          stale-verified advisory + gates.py surfaces round-trip
  render  stub — lands in phase 3 (REQ-302)
  apply   --answers <file> [--yes] [--update] [--force] [--reset-declines]
          atomic all-or-nothing writer of config/environments.yaml and
          .ffs-init.json (declines, schema ffs.init/v1)
exit codes: 0 ok · 1 schema/usage/refusal · 2 leak finding · 3 not implemented
USAGE
}

VERB="${1:-}"
if [ -z "$VERB" ]; then
  usage
  exit 1
fi
shift
case "$VERB" in
  detect|check|apply) ;;
  render)
    echo "render lands in phase 3 (REQ-302)" >&2
    exit 3
    ;;
  *)
    usage
    exit 1
    ;;
esac

exec python3 - "$VERB" "$ROOT" "$@" <<'PYEOF'
import datetime as _dt
import fcntl
import json
import os
import re
import secrets as _secrets
import subprocess
import sys

MODE = sys.argv[1]
ROOT = sys.argv[2]
ARGS = list(sys.argv[3:])

REGISTRY_REL = os.path.join("config", "environments.yaml")
KINDS = ("local", "dev", "staging", "prod", "preview")


def fail(msg, rc=1):
    print(msg, file=sys.stderr)
    sys.exit(rc)


# ── shared name-safety guard (walls a173dd6d + 2f77cf3f, CRITICAL) ──────────
# ONE guard for EVERY untrusted string any verb prints: detect proposal rows,
# workflow environment: names, wrangler env names, doppler names, .env keys,
# filenames in evidence values, leak-scan key names, error-path names.
# Identifier-or-relpath shape or the string renders as a placeholder —
# credential-shaped repository content can never cross into stdout/stderr
# through ANY emission path.
_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
_PATH_RE = re.compile(r"[A-Za-z0-9._/-]{1,128}")


def safe_name(value, where):
    if isinstance(value, str) and _NAME_RE.fullmatch(value):
        return value
    return f"<non-conforming name at {where}>"


def safe_path(value, where):
    if (isinstance(value, str) and _PATH_RE.fullmatch(value)
            and not value.startswith("-")):
        return value
    return f"<non-conforming name at {where}>"


def safe_key(value, line_no):
    # wall 2f77cf3f: leak-scan key names pass the identifier guard; a
    # credential-shaped or hostile key prints as a placeholder, never bytes.
    if isinstance(value, str) and _NAME_RE.fullmatch(value):
        return value
    return f"<non-identifier key at line {line_no}>"


class SchemaError(Exception):
    """Value-free by construction (wall 6e10a021): messages carry key names,
    line numbers, and EXPECTED shapes only — never got-bytes."""


# ── leak scan (REQ-202a; audit rows 10-12, LOCKED) ──────────────────────────
# Self-contained scanner: credential-output-guard.sh is untouched; only its
# conventions are reused (rc 2 on finding; matched bytes NEVER printed on
# either stream). Eight detector families; the two-entry context whitelist
# (`uses: <owner>/<repo>@<40-hex>` pins, `sha256:<64-hex>` digests) is
# applied per line BEFORE the hex/base64 families and exempts ONLY the
# MATCHED SPAN, never the whole line (wall a8f9af82).
_WHITELIST = (
    re.compile(r"uses:\s*[\w.-]+/[\w.-]+@[0-9a-fA-F]{40}"),
    re.compile(r"sha256:[0-9a-fA-F]{64}"),
)
_FAMILIES = (
    ("pem-block", re.compile(r"-----BEGIN[ A-Z]*")),
    ("jwt", re.compile(
        r"\beyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]*")),
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("provider-token-prefix", re.compile(
        r"\b(?:ghp_[A-Za-z0-9]{16,}|gho_[A-Za-z0-9]{16,}|"
        r"github_pat_[A-Za-z0-9_]{16,}|sk-[A-Za-z0-9_-]{12,}|"
        r"xox[bp]-[A-Za-z0-9-]{8,})")),
    ("credential-url", re.compile(
        r"\b[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@")),
    ("secret-assignment", re.compile(
        r"(?i)\b(?:password|token|secret)\s*[:=]\s*\S{8,}")),
    ("hex-run", re.compile(r"\b[0-9a-fA-F]{32,}\b")),
    ("base64-run", re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}")),
)


def _is_committed(path, root):
    try:
        rel = os.path.relpath(os.path.realpath(path), os.path.realpath(root))
        if rel.startswith(".."):
            return False
        r = subprocess.run(
            ["git", "-C", root, "ls-files", "--error-unmatch", "--", rel],
            capture_output=True, timeout=10)
        return r.returncode == 0
    except Exception:
        return False


def _nearest_key(line, start, line_no):
    prefix = line[:start]
    m = re.search(r"([^\s:=]+)\s*[:=][^:=]*$", prefix)
    if not m:
        m = re.match(r"\s*-?\s*([^\s:=]+)\s*[:=]", line)
    key = m.group(1) if m else ""
    return safe_key(key, line_no)


def leak_scan(path, text):
    """Returns finding lines in the fixed output contract (row 12):
    `line N, key <name>, shape <class> — remedy: replace the literal with a
    NAME in secret_names`. Matched bytes never appear in a finding."""
    findings = []
    committed = _is_committed(path, ROOT)
    for line_no, line in enumerate(text.splitlines(), 1):
        wl_spans = [m.span() for wl in _WHITELIST for m in wl.finditer(line)]
        claimed = []
        for shape, pat in _FAMILIES:
            for m in pat.finditer(line):
                s, e = m.span()
                # wall a8f9af82: exemption covers ONLY the matched span — a
                # second credential on the same line must still flag
                if any(ws <= s and e <= we for ws, we in wl_spans):
                    continue
                if any(not (e <= cs or s >= ce) for cs, ce in claimed):
                    continue  # most-specific family already claimed this span
                claimed.append((s, e))
                key = _nearest_key(line, s, line_no)
                remedy = "replace the literal with a NAME in secret_names"
                if committed:
                    remedy += ("; this file is committed — rotate the "
                               "credential, then rewrite history to remove "
                               "it")
                findings.append(
                    f"line {line_no}, key {key}, shape {shape} — "
                    f"remedy: {remedy}")
    return findings


def _gates():
    sys.path.insert(0, os.path.join(ROOT, "lib"))
    import gates
    return gates


# ── value-stripping renderer for gates.py ValueErrors (wall 6e10a021) ───────
# gates.py's own messages may embed scalar content (e.g. the offending line's
# bytes) and must never reach output verbatim. Only a fixed-string reason
# class + an extracted line number cross to either stream.
_GATES_REASONS = (
    ("duplicate surfaces block", "duplicate surfaces block"),
    ("duplicate manifest surface", "duplicate surface name"),
    ("duplicate field", "duplicate field in surface row"),
    ("duplicate manifest key", "duplicate key"),
    ("tab-indented", "tab-indented line"),
    ("before any", "field line before any surface row"),
    ("unsupported indent-0", "unsupported indent-0 line"),
    ("unsupported line", "unsupported line shape"),
    ("empty manifest scalar", "empty scalar"),
    ("invalid quoted", "invalid quoted scalar"),
    ("must be text", "non-text scalar"),
    ("needs staging_instance", "surface row missing staging_instance"),
    ("non-empty surface name", "surface row missing surface name"),
    ("both staging_instance and staging", "staging_instance/staging alias conflict"),
    ("non-empty list", "surfaces must be a non-empty list"),
    ("top-level surfaces list", "missing surfaces block"),
    ("rollback", "invalid rollback declaration"),
    ("parse to an object", "manifest must parse to an object"),
)


def render_gates_error(exc):
    msg = str(exc)
    m = re.search(r"\bline (\d+)", msg)
    loc = f" at line {m.group(1)}" if m else ""
    reason = next((label for needle, label in _GATES_REASONS if needle in msg),
                  "surfaces block rejected")
    return (f"ENV-REGISTRY-INVALID: surfaces {reason}{loc} — remedy: fix the "
            f"surfaces block shape in the registry; parse authority is "
            f"lib/gates.py _load_manifest_text")


# ── constrained-subset parser (mirrors gates._manifest_scalar limits) ───────
# Flat scalars only, ' #' inline-comment split, 2-space indent, no general
# YAML. Reads the environments/test_tiers/declines blocks (and, for apply's
# answers, the surfaces rows for EMISSION only — validation authority for
# surfaces stays with gates.py).

def scalar(raw, line_no):
    value = raw.strip()
    if not value:
        raise SchemaError(f"line {line_no}: empty scalar — expected a value")
    if value.startswith('"'):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            raise SchemaError(
                f"line {line_no}: invalid quoted scalar — expected "
                f"JSON-quoted text") from None
        if not isinstance(parsed, str):
            raise SchemaError(f"line {line_no}: quoted scalar must be text")
        return parsed
    if value.startswith("'"):
        if len(value) < 2 or not value.endswith("'"):
            raise SchemaError(f"line {line_no}: invalid single-quoted scalar")
        return value[1:-1].replace("''", "'")
    value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
    if not value:
        raise SchemaError(f"line {line_no}: empty scalar after comment strip")
    return value


ROW_KEYS = {
    "environments": ("name", "kind", "base_url", "secret_names", "verified",
                     "test_tier", "confidence", "evidence"),
    "test_tiers": ("tier", "command", "covers", "confidence", "evidence"),
    "surfaces": ("surface", "staging_instance"),
    "declines": ("heuristic", "evidence", "declined_at"),
}
LIST_KEYS = ("secret_names", "covers")


def _set_field(row, key, rest, line_no):
    if key in LIST_KEYS:
        if rest.strip() == "[]":
            row[key] = []
            return None
        if rest.strip() == "":
            row[key] = []
            return row[key]
    row[key] = scalar(rest, line_no)
    return None


def parse_subset(text, allowed_sections, parse_surfaces=False):
    sections = {}
    cur_sec = None
    cur_row = None
    cur_list = None
    for line_no, raw in enumerate(text.splitlines(), 1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent_ws = raw[:len(raw) - len(raw.lstrip())]
        if "\t" in indent_ws:
            raise SchemaError(
                f"line {line_no}: tab indentation — expected 2-space indent")
        indent = len(indent_ws)
        if indent == 0:
            m = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*):", stripped)
            if not m:
                raise SchemaError(
                    f"line {line_no}: expected a top-level 'key:' section "
                    f"header")
            name = m.group(1)
            if name not in allowed_sections:
                raise SchemaError(
                    f"line {line_no}: unknown top-level key "
                    f"{safe_name(name, f'line {line_no}')} — expected one of "
                    + "|".join(allowed_sections))
            if name in sections:
                raise SchemaError(
                    f"line {line_no}: duplicate top-level key "
                    f"{safe_name(name, f'line {line_no}')}")
            sections[name] = []
            cur_sec, cur_row, cur_list = name, None, None
            continue
        if cur_sec is None:
            raise SchemaError(
                f"line {line_no}: content before any section header")
        if cur_sec == "surfaces" and not parse_surfaces:
            # surfaces authority is gates.py (audit row 18)
            continue
        keys = ROW_KEYS[cur_sec]
        m = re.fullmatch(r"-\s+([A-Za-z_][A-Za-z0-9_]*):\s*(.*)", stripped)
        if m and indent == 2:
            key, rest = m.group(1), m.group(2)
            if key not in keys:
                raise SchemaError(
                    f"line {line_no}: unknown key "
                    f"{safe_name(key, f'line {line_no}')} in {cur_sec} row — "
                    f"expected one of " + "|".join(keys))
            cur_row = {"_line": line_no}
            sections[cur_sec].append(cur_row)
            cur_list = _set_field(cur_row, key, rest, line_no)
            continue
        m = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*):\s*(.*)", stripped)
        if m and indent == 4 and cur_row is not None:
            key, rest = m.group(1), m.group(2)
            if key not in keys:
                raise SchemaError(
                    f"line {line_no}: unknown key "
                    f"{safe_name(key, f'line {line_no}')} in {cur_sec} row — "
                    f"expected one of " + "|".join(keys))
            if key in cur_row:
                raise SchemaError(
                    f"line {line_no}: duplicate key "
                    f"{safe_name(key, f'line {line_no}')} in {cur_sec} row")
            cur_list = _set_field(cur_row, key, rest, line_no)
            continue
        m = re.fullmatch(r"-\s+(.*)", stripped)
        if m and indent >= 6 and cur_list is not None:
            cur_list.append(scalar(m.group(1), line_no))
            continue
        raise SchemaError(
            f"line {line_no}: unsupported line shape — expected "
            f"'- key: value' rows, 4-space 'key: value' fields, or "
            f"6-space '- item' list entries")
    return sections


def validate_registry(sections):
    """Schema for the blocks gates.py ignores (audit row 18). Returns
    {env name: kind}."""
    envs = sections.get("environments")
    if envs is None:
        raise SchemaError(
            "missing required key environments — expected a top-level "
            "'environments:' list of rows each with 'name:' and 'kind:'")
    if not envs:
        raise SchemaError(
            "environments section is empty — expected at least one row "
            "starting '- name:'")
    tiers = sections.get("test_tiers")
    if tiers is None:
        raise SchemaError(
            "missing required key test_tiers — expected a top-level "
            "'test_tiers:' list of rows each with 'tier:' and 'command:'")
    if not tiers:
        raise SchemaError(
            "test_tiers section is empty — expected at least one row "
            "starting '- tier:'")
    env_kind = {}
    for row in envs:
        ln = row["_line"]
        name = row.get("name")
        if name is None:
            raise SchemaError(
                f"row at line {ln}: missing key name — expected an "
                f"identifier-shaped environment name")
        if not _NAME_RE.fullmatch(name):
            raise SchemaError(
                f"line {ln}, key name — expected identifier shape "
                f"[A-Za-z_][A-Za-z0-9_]{{0,63}}")
        if name in env_kind:
            raise SchemaError(
                f"line {ln}, key name — duplicate environment name "
                f"{safe_name(name, f'line {ln}')}")
        kind = row.get("kind")
        if kind is None:
            raise SchemaError(
                f"row at line {ln}: missing key kind — expected one of "
                f"local|dev|staging|prod|preview")
        if kind not in KINDS:
            raise SchemaError(
                f"line {ln}, key kind — expected one of "
                f"local|dev|staging|prod|preview")
        env_kind[name] = kind
        ver = row.get("verified")
        if ver is not None and ver != "null" \
                and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", ver):
            raise SchemaError(
                f"line {ln}, key verified — expected null or YYYY-MM-DD")
        for sn in row.get("secret_names") or []:
            if not _NAME_RE.fullmatch(sn):
                raise SchemaError(
                    f"line {ln}, key secret_names — expected "
                    f"identifier-shaped NAMEs only (a secret VALUE is "
                    f"forbidden here)")
        tt = row.get("test_tier")
        if tt is not None and not _NAME_RE.fullmatch(tt):
            raise SchemaError(
                f"line {ln}, key test_tier — expected an identifier-shaped "
                f"tier name")
    seen_tiers = set()
    for row in tiers:
        ln = row["_line"]
        tier = row.get("tier")
        if tier is None or not _NAME_RE.fullmatch(tier):
            raise SchemaError(
                f"row at line {ln}: key tier — expected an "
                f"identifier-shaped tier name")
        if tier in seen_tiers:
            raise SchemaError(
                f"line {ln}, key tier — duplicate tier "
                f"{safe_name(tier, f'line {ln}')}")
        seen_tiers.add(tier)
        if not row.get("command"):
            raise SchemaError(
                f"row at line {ln}: missing key command — expected a shell "
                f"command string for tier {safe_name(tier, f'line {ln}')} — "
                f"remedy: add 'command: <test command>'")
    return env_kind


def check_referential(env_kind, surfaces_map):
    """Audit row 18: every staging_instance references a declared
    kind: staging environment. Names BOTH sides + remedy."""
    for surface, row in (surfaces_map or {}).items():
        staging = row.get("staging") if isinstance(row, dict) else None
        if staging and staging != "none":
            if env_kind.get(staging) != "staging":
                s_surface = safe_name(surface, "surfaces block")
                s_env = safe_name(staging, "surfaces block")
                fail(f"ENV-REGISTRY-INVALID: surface {s_surface} "
                     f"staging_instance {s_env} does not reference an "
                     f"environment declared kind: staging — remedy: declare "
                     f"'- name: {s_env}' with 'kind: staging' in "
                     f"config/environments.yaml, or set "
                     f"staging_instance: none")


def stale_advisories(sections):
    today = _dt.date.today()
    for row in sections.get("environments") or []:
        ver = row.get("verified")
        if ver and re.fullmatch(r"\d{4}-\d{2}-\d{2}", ver):
            try:
                d = _dt.date.fromisoformat(ver)
            except ValueError:
                continue
            if (today - d).days > 90:
                name = safe_name(row.get("name"), "registry")
                print(f"ADVISORY: environment {name} verified {ver} is "
                      f"older than 90 days — re-verify and update the date")


def gh_probe(stream=None):
    """ADVISORY only, OPT-IN under --probe-gh (wall 959b7514, EDGE-004):
    silently skipped when unauthenticated; ANY failure is a silent skip;
    the caller's exit code NEVER depends on it. detect routes advisories to
    stderr so stdout stays a valid answers file."""
    stream = stream or sys.stdout
    try:
        r = subprocess.run(["gh", "auth", "status"], capture_output=True,
                           timeout=15)
        if r.returncode != 0:
            return
        origin = subprocess.run(
            ["git", "-C", ROOT, "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=15)
        if origin.returncode != 0:
            return
        m = re.search(r"[:/]([\w.-]+)/([\w.-]+?)(?:\.git)?/?$",
                      origin.stdout.strip())
        if not m:
            return
        r = subprocess.run(
            ["gh", "api", f"repos/{m.group(1)}/{m.group(2)}/environments"],
            capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            return
        data = json.loads(r.stdout)
        for env in (data.get("environments") or []):
            name = safe_name(env.get("name"), "gh environments probe")
            rules = env.get("protection_rules") or []
            if not any(rule.get("type") == "required_reviewers"
                       for rule in rules if isinstance(rule, dict)):
                print(f"ADVISORY: GitHub environment {name} has no reviewer "
                      f"protection — add required reviewers before prod "
                      f"deploys (external prerequisite, never a gate)",
                      file=stream)
    except Exception:
        return


def validate_registry_text(text, *, label):
    """Full validation pipeline shared by check and apply's candidate pass:
    v1 marker, subset schema, gates.py surfaces round-trip, referential
    integrity. Returns (sections, env_kind, surfaces_map)."""
    g = _gates()
    if not g._V1_MARKER.search(text):
        raise SchemaError(
            "missing v1 marker on line 2 — expected the comment "
            "'# schema: ffs.environments/v1'")
    sections = parse_subset(text, ("environments", "test_tiers", "surfaces"))
    env_kind = validate_registry(sections)
    surfaces_map = None
    if re.search(r"(?m)^surfaces:", text):
        try:
            surfaces_map = g._load_manifest_text(text)
        except ValueError as exc:
            fail(render_gates_error(exc))
    check_referential(env_kind, surfaces_map)
    return sections, env_kind, surfaces_map


def cmd_check(args):
    manifest = None
    manifest_given = False
    probe = False
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--manifest":
            manifest_given = True
            manifest = args[i + 1] if i + 1 < len(args) else ""
            i += 2
        elif a == "--probe-gh":
            probe = True
            i += 1
        else:
            fail("ENV-REGISTRY-INVALID: unknown flag for check — expected "
                 "--manifest <path> or --probe-gh")
    if manifest_given and (manifest is None or manifest == ""):
        fail("ENV-REGISTRY-INVALID: --manifest is empty — remedy: pass a "
             "registry path or omit the flag to resolve "
             "config/environments.yaml from the repo root")
    path = manifest or os.path.join(ROOT, REGISTRY_REL)
    try:
        text = open(path, encoding="utf-8").read()
    except OSError:
        shown = safe_path(os.path.basename(path), "check --manifest")
        fail(f"ENV-REGISTRY-INVALID: registry not readable at {shown} — "
             f"remedy: run 'env-registry.sh detect' then 'apply' to create "
             f"it, or pass --manifest <path>")
    # Leak scan FIRST (highest-consequence risk, plan key_links): a secret
    # value must never survive into schema/refusal paths.
    findings = leak_scan(path, text)
    if findings:
        for finding in findings:
            print(finding)
        sys.exit(2)
    try:
        sections, _env_kind, _surfaces = validate_registry_text(
            text, label=path)
    except SchemaError as exc:
        fail(f"ENV-REGISTRY-INVALID: {exc}")
    stale_advisories(sections)
    if probe:
        gh_probe()
    sys.exit(0)


# ── detect (REQ-202): read-only, propose-never-decide ───────────────────────

def kind_guess(name):
    n = (name or "").lower()
    if n in ("staging", "stage", "stg"):
        return "staging"
    if n in ("prod", "production", "prd"):
        return "prod"
    if n in ("preview", "pre"):
        return "preview"
    if n == "local":
        return "local"
    return "dev"


def _read_lines(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read().splitlines()
    except OSError:
        return None


def _reject_dup_keys(pairs):
    obj = {}
    for k, v in pairs:
        if k in obj:
            raise ValueError("duplicate JSON key in .ffs-init.json")
        obj[k] = v
    return obj


def _load_declines(declines_path):
    try:
        with open(declines_path, encoding="utf-8") as fh:
            data = json.load(fh, object_pairs_hook=_reject_dup_keys)
        return [r for r in (data.get("declines") or []) if isinstance(r, dict)]
    except Exception:
        return []


def cmd_detect(args):
    """Zero writes inside the target repo (Pitfall 4): proposal YAML on
    stdout in the --answers shape; advisories on stderr; no lockfile, no
    .ffs-init.json touch (it is only READ for the declines filter)."""
    probe = False
    for a in args:
        if a == "--probe-gh":
            probe = True
        else:
            fail("ENV-REGISTRY-INVALID: unknown flag for detect — expected "
                 "--probe-gh")
    j = os.path.join
    atoms = []

    def add(name, kind, heuristic, evidence, confidence, secret_names=None):
        atoms.append({"heuristic": heuristic, "evidence": evidence,
                      "name": name, "kind": kind, "confidence": confidence,
                      "secret_names": list(secret_names or [])})

    # vercel.json / .vercel → prod + preview
    if os.path.isfile(j(ROOT, "vercel.json")) or os.path.isdir(j(ROOT, ".vercel")):
        evd = ("vercel.json" if os.path.isfile(j(ROOT, "vercel.json"))
               else ".vercel/")
        add("prod", "prod", "vercel", evd, "medium")
        add("preview", "preview", "vercel", evd, "medium")
    # wrangler.toml [env.X] → env per section header; the evidence value is
    # the STABLE (heuristic, evidence) decline key `wrangler.toml:[env.X]`
    # (audit row 25 example) — line numbers stay out of the key so a shifted
    # header does not wrongly re-propose a declined env.
    if os.path.isfile(j(ROOT, "wrangler.toml")):
        for i, line in enumerate(_read_lines(j(ROOT, "wrangler.toml")) or [], 1):
            m = re.match(r"^\[env\.([A-Za-z0-9_-]+)\]", line)
            if m:
                name = m.group(1)
                evd = (f"wrangler.toml:"
                       f"[env.{safe_name(name, f'wrangler.toml:{i}')}]")
                add(name, kind_guess(name), "wrangler-env", evd, "medium")
    # fly.toml + fly.X.toml → env per file
    if os.path.isfile(j(ROOT, "fly.toml")):
        add("prod", "prod", "fly", "fly.toml", "low")
    for fn in sorted(os.listdir(ROOT)):
        m = re.match(r"fly\.([A-Za-z0-9_-]+)\.toml$", fn)
        if m:
            add(m.group(1), kind_guess(m.group(1)), "fly",
                safe_path(fn, "fly detection"), "medium")
    # docker-compose → local
    for fn in ("docker-compose.yml", "docker-compose.yaml",
               "compose.yml", "compose.yaml"):
        if os.path.isfile(j(ROOT, fn)):
            add("local", "local", "compose", fn, "high")
            break
    # k8s/kustomize overlays → env per dir
    for base in ("k8s/overlays", "kustomize/overlays"):
        if os.path.isdir(j(ROOT, base)):
            for name in sorted(os.listdir(j(ROOT, base))):
                if os.path.isdir(j(ROOT, base, name)):
                    evd = f"{base}/{safe_name(name, base)}/"
                    add(name, kind_guess(name), "k8s-overlay", evd, "medium")
    # env-suffixed .env.* → env row + secret KEY NAMES only (left of '=');
    # unreadable file still emits its row, no content read (EDGE-003)
    for fn in sorted(os.listdir(ROOT)):
        m = re.match(r"\.env\.([A-Za-z0-9_-]+)$", fn)
        if not m or m.group(1) in ("example", "sample", "template"):
            continue
        suffix = m.group(1)
        sfn = safe_path(fn, ".env detection")
        lines = _read_lines(j(ROOT, fn))
        if lines is None:
            add(suffix, kind_guess(suffix), "dotenv",
                f"{sfn}: file present, unread", "low")
            continue
        keys = []
        for line in lines:
            km = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=",
                          line)
            if km:
                keys.append(safe_name(km.group(1), sfn))
        add(suffix, kind_guess(suffix), "dotenv", sfn, "medium",
            secret_names=keys)
    # workflow environment: keys
    wfdir = j(ROOT, ".github", "workflows")
    if os.path.isdir(wfdir):
        for fn in sorted(os.listdir(wfdir)):
            if not fn.endswith((".yml", ".yaml")):
                continue
            for i, line in enumerate(_read_lines(j(wfdir, fn)) or [], 1):
                m = re.match(r"\s*environment:\s*([^\s#]+)\s*$", line)
                if m:
                    name = m.group(1).strip("'\"")
                    evd = (f".github/workflows/"
                           f"{safe_path(fn, 'workflow detection')}:{i}")
                    add(name, kind_guess(name), "workflow-environment",
                        evd, "medium")
    # doppler.yaml → provider + config names
    for i, line in enumerate(_read_lines(j(ROOT, "doppler.yaml")) or [], 1):
        m = re.match(r"\s*config:\s*([A-Za-z0-9_-]+)", line)
        if m:
            name = m.group(1)
            evd = f"doppler.yaml:{safe_name(name, f'doppler.yaml:{i}')}"
            add(name, kind_guess(name), "doppler", evd, "low")

    # declines filter (audit row 32): key = (heuristic, concrete evidence
    # value) — NEW evidence under a declined heuristic re-proposes.
    declined = {(r.get("heuristic"), r.get("evidence"))
                for r in _load_declines(j(ROOT, ".ffs-init.json"))}
    kept, suppressed = [], 0
    for a in atoms:
        if (a["heuristic"], a["evidence"]) in declined:
            suppressed += 1
        else:
            kept.append(a)
    if suppressed:
        print(f"ADVISORY: {suppressed} proposal(s) suppressed by declines in "
              f".ffs-init.json — run 'env-registry.sh apply "
              f"--reset-declines' to clear them", file=sys.stderr)

    # merge by env name (registry names are unique); evidence values join
    envs, order = {}, []
    for a in kept:
        nm = a["name"]
        if nm not in envs:
            envs[nm] = a
            order.append(nm)
        else:
            envs[nm]["evidence"] += "; " + a["evidence"]
            for k in a["secret_names"]:
                if k not in envs[nm]["secret_names"]:
                    envs[nm]["secret_names"].append(k)
    bare = not order
    if bare:
        envs["local"] = {"heuristic": "bare",
                         "evidence": "no deployment surface detected",
                         "name": "local", "kind": "local",
                         "confidence": "high", "secret_names": []}
        order.append("local")

    # tier classification proposal (REQ-202 four-way rule)
    tier_rows = []
    live_ev = nightly_ev = None
    tests_dir = j(ROOT, "tests")
    if os.path.isdir(tests_dir):
        for dirpath, dirnames, filenames in os.walk(tests_dir):
            rel = os.path.relpath(dirpath, ROOT)
            for name in list(dirnames) + list(filenames):
                if live_ev is None and re.search(
                        r"(-e2e|-live|browser|canary)", name):
                    live_ev = safe_path(f"{rel}/{name}", "tier scan")
            for name in filenames:
                if nightly_ev is None and name.endswith((".py", ".sh", ".bats")):
                    try:
                        head = open(j(dirpath, name), encoding="utf-8",
                                    errors="replace").read(4096)
                    except OSError:
                        continue
                    if "GSD_" in head and "skip" in head.lower():
                        nightly_ev = safe_path(f"{rel}/{name}", "tier scan")
        base_cmd = "python3 -m pytest tests/ -q"
        tier_ev = "tests/"
    else:
        base_cmd = "true"
        tier_ev = "no tests directory found"
    tier_rows.append({"tier": "fast", "command": base_cmd,
                      "confidence": "low", "evidence": tier_ev})
    tier_rows.append({"tier": "full", "command": base_cmd,
                      "confidence": "low", "evidence": tier_ev})
    if live_ev:
        tier_rows.append({"tier": "live",
                          "command": f"python3 -m pytest {live_ev} -q",
                          "confidence": "low", "evidence": live_ev})
    if nightly_ev:
        tier_rows.append({"tier": "nightly",
                          "command": f"python3 -m pytest {nightly_ev} -q",
                          "confidence": "low", "evidence": nightly_ev})

    out = ["# proposal generated by env-registry.sh detect — review, edit,",
           "# then: env-registry.sh apply --answers <this file> [--yes]",
           "# every row is a PROPOSAL: detection proposes, never decides "
           "(verified: null).",
           "environments:"]
    for nm in order:
        a = envs[nm]
        out.append(f"  # heuristic: {a['heuristic']} — decline key = "
                   f"(heuristic, evidence)")
        out.append(f"  - name: {safe_name(a['name'], a['heuristic'])}")
        out.append(f"    kind: {a['kind']}")
        out.append("    base_url: none")
        if a["secret_names"]:
            out.append("    secret_names:")
            out.extend(f"      - {k}" for k in a["secret_names"])
        else:
            out.append("    secret_names: []")
        out.append("    verified: null")
        out.append("    test_tier: fast")
        out.append(f"    confidence: {a['confidence']}")
        out.append(f"    evidence: {a['evidence']}")
    out.append("")
    out.append("test_tiers:")
    for t in tier_rows:
        out.append(f"  - tier: {t['tier']}")
        out.append(f"    command: {t['command']}")
        out.append(f"    confidence: {t['confidence']}")
        out.append(f"    evidence: {t['evidence']}")
    if not bare:
        staging_env = next(
            (envs[nm]["name"] for nm in order
             if envs[nm]["kind"] == "staging"
             and _NAME_RE.fullmatch(envs[nm]["name"])), None)
        out.append("")
        out.append("surfaces:")
        out.append("  - surface: release")
        out.append(f"    staging_instance: {staging_env or 'none'}")
    print("\n".join(out))
    if probe:
        gh_probe(stream=sys.stderr)
    sys.exit(0)


# ── apply (REQ-203, audit row 25): single atomic all-or-nothing writer ──────

def emit_registry(env_rows, tier_rows, surf_rows):
    """Regenerates the exact phase-1 committed shape: v1 marker on line 2,
    flat scalars, 2-space indent, no tabs, surfaces block LAST."""
    out = ["# config/environments.yaml",
           "# schema: ffs.environments/v1",
           "# SECRETS BY NAME ONLY — this file is read by CI and by "
           "autonomous agents;",
           "# never write a secret VALUE here, only the NAME of where it "
           "lives.",
           "",
           "environments:"]
    for e in env_rows:
        out.append(f"  - name: {e.get('name')}")
        out.append(f"    kind: {e.get('kind')}")
        out.append(f"    base_url: {e.get('base_url') or 'none'}")
        sn = e.get("secret_names") or []
        if sn:
            out.append("    secret_names:")
            out.extend(f"      - {k}" for k in sn)
        else:
            out.append("    secret_names: []")
        out.append(f"    verified: {e.get('verified') or 'null'}")
        out.append(f"    test_tier: {e.get('test_tier') or 'fast'}")
    out += ["", "test_tiers:"]
    for t in tier_rows:
        out.append(f"  - tier: {t.get('tier')}")
        out.append(f"    command: {t.get('command')}")
        cov = t.get("covers") or []
        if cov:
            out.append("    covers:")
            out.extend(f"      - {c}" for c in cov)
    if surf_rows:
        out += ["", "surfaces:"]
        for s in surf_rows:
            out.append(f"  - surface: {s.get('surface')}")
            out.append(f"    staging_instance: "
                       f"{s.get('staging_instance') or 'none'}")
    return "\n".join(out) + "\n"


def _assert_contained(target):
    """Wall 2965346c: before any replace, resolve the realpath of the
    target's parent and REFUSE unless it is the repo ROOT itself (the
    registry's parent may also be a real, non-symlink config/ under ROOT).
    os.path.islink on every path component below ROOT rejects — a
    repository-controlled symlink can never redirect the atomic write
    outside the repo."""
    rel = os.path.relpath(target, ROOT)
    if rel.startswith("..") or os.path.isabs(rel):
        fail("ENV-REGISTRY-REFUSED: write target escapes the repo root — "
             "remedy: run apply from the repository the registry belongs to")
    cur = ROOT
    for comp in rel.split(os.sep):
        cur = os.path.join(cur, comp)
        if os.path.islink(cur):
            shown = safe_path(rel, "write-target containment")
            fail(f"ENV-REGISTRY-REFUSED: write target {shown} crosses a "
                 f"symlinked path component — remedy: remove the symlink; "
                 f"apply only writes through real directories under the "
                 f"repo root")
    rroot = os.path.realpath(ROOT)
    parent_real = os.path.realpath(os.path.dirname(target))
    if parent_real not in (rroot, os.path.join(rroot, "config")):
        fail("ENV-REGISTRY-REFUSED: write target parent escapes the repo "
             "root — remedy: restore config/ as a real directory under the "
             "repository")


def _atomic_write(target, data):
    """mkstemp-in-target-dir + os.replace (gates._save_store idiom).

    Symlink-TOCTOU hardening (walls 739ff957 + 73caf77d): after the
    containment check, the parent directory fd is opened ONCE with
    O_DIRECTORY|O_NOFOLLOW, and both the temp-file creation and the rename
    are anchored to that fd (dir_fd) where the platform supports it — a
    symlink swapped in AFTER the check cannot redirect the write. Residual
    window (named): on platforms where os.replace lacks dir_fd support the
    final rename re-resolves the parent by PATH; the O_NOFOLLOW dirfd open
    still fails closed if the parent itself became a symlink, but a
    component ABOVE it re-resolves in that fallback."""
    parent = os.path.dirname(target)
    base = os.path.basename(target)
    if not os.path.isdir(parent):
        os.mkdir(parent, 0o755)  # only ever ROOT/config, post-containment
    try:
        dfd = os.open(parent, os.O_RDONLY
                      | getattr(os, "O_DIRECTORY", 0) | os.O_NOFOLLOW)
    except OSError:
        fail("ENV-REGISTRY-REFUSED: write target parent is not a real "
             "directory — remedy: restore it under the repo root")
    tmp = f".{base}.{_secrets.token_hex(8)}.tmp"
    try:
        wfd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY
                      | os.O_NOFOLLOW, 0o644, dir_fd=dfd)
        with os.fdopen(wfd, "w", encoding="utf-8") as fh:
            fh.write(data)
        if os.replace in os.supports_dir_fd:
            os.replace(tmp, base, src_dir_fd=dfd, dst_dir_fd=dfd)
        else:
            os.replace(os.path.join(parent, tmp), target)
    except OSError:
        try:
            os.unlink(tmp, dir_fd=dfd)
        except OSError:
            pass
        os.close(dfd)
        fail("ENV-REGISTRY-REFUSED: atomic write failed; nothing replaced — "
             "remedy: check repo-root permissions and free space")
    else:
        os.close(dfd)


def _apply_locked(answers_path, registry_path, declines_path, *,
                  update, force, reset):
    write_registry = bool(answers_path)
    answers_declines = []
    reg_text = None
    if write_registry:
        # regenerate guard (audit rows 28/30): --yes must never silently
        # regenerate an existing registry
        if os.path.exists(registry_path) and not update and not force:
            fail("ENV-REGISTRY-REFUSED: config/environments.yaml already "
                 "exists — remedy: re-run with --update to merge NEW rows "
                 "only, or --force to regenerate from scratch")
        try:
            answers_text = open(answers_path, encoding="utf-8").read()
        except OSError:
            fail("ENV-REGISTRY-INVALID: answers file not readable — remedy: "
                 "pass a readable YAML file to --answers")
        try:
            sections = parse_subset(
                answers_text,
                ("environments", "test_tiers", "surfaces", "declines"),
                parse_surfaces=True)
        except SchemaError as exc:
            fail(f"ENV-REGISTRY-INVALID: answers: {exc}; nothing written")
        # required keys, named with their expected shape (EDGE-008)
        if not sections.get("environments"):
            fail("ENV-REGISTRY-INVALID: answers missing required key "
                 "environments — expected a top-level 'environments:' list "
                 "of rows each with 'name:' and 'kind:'; nothing written")
        if not sections.get("test_tiers"):
            fail("ENV-REGISTRY-INVALID: answers missing required key "
                 "test_tiers — expected a top-level 'test_tiers:' list of "
                 "rows each with 'tier:' and 'command:'; nothing written")
        env_rows = sections["environments"]
        tier_rows = sections["test_tiers"]
        surf_rows = sections.get("surfaces") or []
        answers_declines = sections.get("declines") or []
        for row in env_rows:
            if not row.get("name") or not row.get("kind"):
                missing = "name" if not row.get("name") else "kind"
                fail(f"ENV-REGISTRY-INVALID: answers: environments row at "
                     f"line {row['_line']} missing key {missing} — expected "
                     f"'- name: <identifier>' with 'kind: "
                     f"<local|dev|staging|prod|preview>'; nothing written")
        for row in tier_rows:
            if not row.get("tier") or not row.get("command"):
                missing = "tier" if not row.get("tier") else "command"
                fail(f"ENV-REGISTRY-INVALID: answers: test_tiers row at "
                     f"line {row['_line']} missing key {missing} — expected "
                     f"'- tier: <identifier>' with 'command: <shell "
                     f"command>'; nothing written")
        for row in surf_rows:
            if not row.get("surface"):
                fail(f"ENV-REGISTRY-INVALID: answers: surfaces row at line "
                     f"{row['_line']} missing key surface — expected "
                     f"'- surface: <name>'; nothing written")
        if update and os.path.exists(registry_path):
            try:
                cur_text = open(registry_path, encoding="utf-8").read()
                cur = parse_subset(
                    cur_text, ("environments", "test_tiers", "surfaces"),
                    parse_surfaces=True)
            except (OSError, SchemaError):
                fail("ENV-REGISTRY-INVALID: existing registry unreadable or "
                     "malformed under --update — remedy: fix it via check, "
                     "or use --force to regenerate")
            # --update preserves operator fields (audit rows 28/30): existing
            # rows survive VERBATIM as parsed; only NEW rows are appended.
            have = {r.get("name") for r in cur.get("environments") or []}
            env_rows = ((cur.get("environments") or [])
                        + [r for r in env_rows if r.get("name") not in have])
            have_t = {r.get("tier") for r in cur.get("test_tiers") or []}
            tier_rows = ((cur.get("test_tiers") or [])
                         + [r for r in tier_rows
                            if r.get("tier") not in have_t])
            have_s = {r.get("surface") for r in cur.get("surfaces") or []}
            surf_rows = ((cur.get("surfaces") or [])
                         + [r for r in surf_rows
                            if r.get("surface") not in have_s])
        reg_text = emit_registry(env_rows, tier_rows, surf_rows)
    # declines candidate (schema ffs.init/v1; first key "schema")
    today = _dt.date.today().isoformat()
    if reset:
        declines = []
    else:
        declines = _load_declines(declines_path)
        have_d = {(r.get("heuristic"), r.get("evidence")) for r in declines}
        for row in answers_declines:
            key = (row.get("heuristic"), row.get("evidence"))
            if row.get("heuristic") and row.get("evidence") \
                    and key not in have_d:
                declines.append({"heuristic": row["heuristic"],
                                 "evidence": row["evidence"],
                                 "declined_at": row.get("declined_at")
                                 or today})
                have_d.add(key)
    declines_clean = [{"heuristic": r.get("heuristic"),
                       "evidence": r.get("evidence"),
                       "declined_at": r.get("declined_at") or today}
                      for r in declines]
    declines_text = json.dumps(
        {"schema": "ffs.init/v1", "declines": declines_clean},
        indent=2) + "\n"
    # ── validate EVERYTHING on CANDIDATE bytes, still under the lock ──
    # (wall ca301d30: validation outside the lock is the TOCTOU)
    findings = []
    if reg_text is not None:
        findings += leak_scan(registry_path, reg_text)
    findings += leak_scan(declines_path, declines_text)
    if findings:
        for finding in findings:
            print(finding)
        print("ENV-REGISTRY-INVALID: leak scan failed on candidate bytes; "
              "nothing written", file=sys.stderr)
        sys.exit(2)
    if reg_text is not None:
        try:
            validate_registry_text(reg_text, label="candidate")
        except SchemaError as exc:
            fail(f"ENV-REGISTRY-INVALID: candidate registry: {exc}; "
                 f"nothing written")
    # write-target containment for EVERY target before ANY replace
    targets = ([registry_path] if reg_text is not None else []) \
        + [declines_path]
    for t in targets:
        _assert_contained(t)
    # ── the pinned two-replace protocol: registry FIRST, then declines.
    # Wall 4274a014 (ACCEPTED RESIDUAL by locked adjudication): the window
    # between the two replaces can expose a fresh registry beside stale
    # declines; declines are operator-derivable (re-creatable from detect +
    # operator answers), so NO rollback machinery is built. Any failure
    # BEFORE the first replace changes NOTHING.
    if reg_text is not None:
        _atomic_write(registry_path, reg_text)
    _atomic_write(declines_path, declines_text)
    sys.exit(0)


def cmd_apply(args):
    answers_path = None
    update = force = reset = False
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--answers":
            answers_path = args[i + 1] if i + 1 < len(args) else ""
            i += 2
        elif a == "--yes":
            i += 1  # apply is already non-interactive; --yes is accepted
        elif a == "--update":
            update = True
            i += 1
        elif a == "--force":
            force = True
            i += 1
        elif a == "--reset-declines":
            reset = True
            i += 1
        else:
            fail("ENV-REGISTRY-INVALID: unknown flag for apply — expected "
                 "--answers <file>, --yes, --update, --force, "
                 "--reset-declines")
    if not answers_path and not reset:
        fail("ENV-REGISTRY-INVALID: apply needs --answers <file.yaml> (or "
             "--reset-declines) — remedy: run 'env-registry.sh detect > "
             "answers.yaml', review it, then re-run apply --answers "
             "answers.yaml")
    registry_path = os.path.join(ROOT, REGISTRY_REL)
    declines_path = os.path.join(ROOT, ".ffs-init.json")
    lock_path = os.path.join(ROOT, ".ffs-init.lock")
    # Wall ca301d30 (amending audit row 25): acquire the flock FIRST, then
    # validate everything on candidate bytes UNDER the lock — validation
    # outside the lock is the TOCTOU (two concurrent applies each validating
    # stale state, the later replace silently losing the earlier update).
    # Wall 73caf77d: O_NOFOLLOW on the lock open — a symlinked lockfile
    # cannot redirect the open outside the repo.
    try:
        lock_fd = os.open(lock_path,
                          os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o644)
    except OSError:
        fail("ENV-REGISTRY-REFUSED: cannot open .ffs-init.lock (symlink or "
             "unwritable repo root) — remedy: remove any symlink at "
             ".ffs-init.lock and re-run")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(lock_fd)
        fail("ENV-REGISTRY-BUSY: another apply holds .ffs-init.lock — "
             "remedy: retry after the concurrent apply completes")
    try:
        _apply_locked(answers_path, registry_path, declines_path,
                      update=update, force=force, reset=reset)
    finally:
        os.close(lock_fd)  # release LAST (pinned ordering)


if MODE == "check":
    cmd_check(ARGS)
elif MODE == "detect":
    cmd_detect(ARGS)
elif MODE == "apply":
    cmd_apply(ARGS)
PYEOF

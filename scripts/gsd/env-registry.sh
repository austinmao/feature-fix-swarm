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
import json
import os
import re
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


def gh_probe():
    """ADVISORY only, OPT-IN under --probe-gh (wall 959b7514, EDGE-004):
    silently skipped when unauthenticated; ANY failure is a silent skip;
    the caller's exit code NEVER depends on it."""
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
                      f"deploys (external prerequisite, never a gate)")
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
    try:
        sections, _env_kind, _surfaces = validate_registry_text(
            text, label=path)
    except SchemaError as exc:
        fail(f"ENV-REGISTRY-INVALID: {exc}")
    stale_advisories(sections)
    if probe:
        gh_probe()
    sys.exit(0)


if MODE == "check":
    cmd_check(ARGS)
elif MODE == "detect":
    fail("ENV-REGISTRY-INVALID: detect is not implemented yet — remedy: "
         "this build ships check only", 3)
elif MODE == "apply":
    fail("ENV-REGISTRY-INVALID: apply is not implemented yet — remedy: "
         "this build ships check only", 3)
PYEOF

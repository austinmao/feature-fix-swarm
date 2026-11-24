"""Cloud delegation router for ``/swarm --cloud`` (contracts §1, §2.1, §2.2, §14.5, §14.9).

Pure functions over plain data: no I/O, no environment, no clock.  Output is
deterministic (input order, then sorted where order carries no meaning).

Public surface
--------------
``parse_annotations(line)``      tasks-file annotations -> dict
``normalize_tasks(raw, sequential=)``  raw lines/dicts -> §2.1 task objects
``DENYLIST`` / ``matches_denylist(path_or_glob)``  §7 denylist
``case_collisions(paths, base_paths)``  case-insensitive aliasing check
``owns_overlap(a_globs, b_globs)``  conservative glob-set overlap
``plan_routes(...)``             §2.2 route plan + §14.9 preconditions
``RouteError(code, detail)``     ``ValueError`` subclass carrying a stable code

Glob rules (``owns`` and the denylist)
--------------------------------------
* Globs are repo-relative and ``/``-separated.  ``./`` prefixes, ``//`` and
  ``.`` segments are dropped.  Absolute paths, ``..`` segments and empty
  strings are *unsafe*: ``matches_denylist`` and ``owns_overlap`` answer True
  for them (unsure means overlapping) and the router reports ``OWNS_INVALID``.
* ``**`` matches zero or more whole segments; ``*``, ``?`` and ``[..]`` match
  within one segment.
* Every glob is treated as possibly naming a directory: a trailing ``**`` is
  appended when absent, so ``src/a`` overlaps ``src/a/x.py``.
* Two globs overlap when a segment-wise walk can align them.  Two segments are
  compatible when equal, when one is a wildcard segment matching the other
  literal (``fnmatch``), or when both contain wildcards (unsure -> compatible).
* Matching is case-insensitive (Unicode NFC, ``casefold``) for both the path
  and the denylist entry: ``pkg/CLAUDE.MD`` and ``.Claude/x`` are denied.
* Denylist entries containing ``/`` are anchored at the repo root and use the
  walk above, so ``scripts/**`` (which contains ``scripts/hooks/**``) and ``**``
  are denylisted while ``src/hooks/**`` is not.  Dot-directory entries
  (``.claude/**``, ``.codex/**``, ``.husky/**`` ...) additionally match as ANY
  path component at any depth (``pkg/.claude/settings.json``); ``hooks/**`` and
  ``scripts/hooks/**`` stay root-anchored because ``src/hooks/`` is ordinary
  source.  Entries without ``/`` (for example ``.git*``, ``*.pem``,
  ``setup.sh``) match any path component at any depth, and every path below a
  matching directory, but only against the component text itself: a broad
  ``src/**`` is not denied merely because it could contain ``src/setup.sh``.
  The collect step re-checks concrete paths.
* ``case_collisions(paths, base_paths)`` lists result paths that alias another
  path (or directory) on a case-insensitive checkout.
* ``owns_overlap`` of an empty list with anything is False: a task without
  ``owns`` is ineligible for the cloud and creates no serialization edge.

Task ineligibility reason codes (``tasks[t].reasons`` when ``route`` is local)
-----------------------------------------------------------------------------
LOCAL_ONLY, KIND_NOT_CLOUDABLE, MODEL_EXACT, TIER_FRONTIER, OWNS_MISSING,
OWNS_INVALID, OWNS_DENYLIST, VERIFY_CMD_MISSING, NO_PROVIDER_AVAILABLE,
NO_PROVIDER_FOR_TIER, SECRET_NOT_EXPOSED.

Group and task level reasons
-----------------------------
POLICY_OFF (no ``--cloud``), BELOW_THRESHOLD, GROUP_HAS_INELIGIBLE_TASK
(group), GROUP_LOCAL (an eligible task inside a local group), CLOUD_ELIGIBLE
(cloud route).  Run-level refusal codes are supplied by the caller and copied
verbatim into the reasons of every task (suggested names: RUN_DIRTY_BASE,
RUN_DOCTOR_NOT_GREEN, RUN_MANAGED_INGRESS, RUN_TOKEN_BUDGET,
RUN_LIFECYCLE_DISABLED).

Error codes (``RouteError.code``)
---------------------------------
DEP_UNKNOWN, DEP_CYCLE, CLOUD_REQUIRED_NOT_ELIGIBLE (the CLI maps these to
exit 3), plus input-validation codes TASK_INVALID, TASK_ID_INVALID,
TASK_ID_DUPLICATE, ANNOTATION_INVALID, POLICY_INVALID, RUN_ID_INVALID,
CONFIG_INVALID.

Grants never change a route.  The interactive flow shows the plan before it
asks for grants, so missing grants surface in ``preconditions`` and ``start``
enforces them.
"""
from __future__ import annotations

import fnmatch
import re
import unicodedata
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

import model_requests

# ── Identifiers (§1) ─────────────────────────────────────────────────────────

RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,39}$")
TASK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,23}$")
SECRET_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
RUN_REFUSAL_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")

# ── Reason and error codes ───────────────────────────────────────────────────

LOCAL_ONLY = "LOCAL_ONLY"
KIND_NOT_CLOUDABLE = "KIND_NOT_CLOUDABLE"
MODEL_EXACT = "MODEL_EXACT"
TIER_FRONTIER = "TIER_FRONTIER"
OWNS_MISSING = "OWNS_MISSING"
OWNS_INVALID = "OWNS_INVALID"
OWNS_DENYLIST = "OWNS_DENYLIST"
VERIFY_CMD_MISSING = "VERIFY_CMD_MISSING"
NO_PROVIDER_AVAILABLE = "NO_PROVIDER_AVAILABLE"
NO_PROVIDER_FOR_TIER = "NO_PROVIDER_FOR_TIER"
SECRET_NOT_EXPOSED = "SECRET_NOT_EXPOSED"

POLICY_OFF = "POLICY_OFF"
BELOW_THRESHOLD = "BELOW_THRESHOLD"
GROUP_HAS_INELIGIBLE_TASK = "GROUP_HAS_INELIGIBLE_TASK"
GROUP_LOCAL = "GROUP_LOCAL"
CLOUD_ELIGIBLE = "CLOUD_ELIGIBLE"

DEP_UNKNOWN = "DEP_UNKNOWN"
DEP_CYCLE = "DEP_CYCLE"
CLOUD_REQUIRED_NOT_ELIGIBLE = "CLOUD_REQUIRED_NOT_ELIGIBLE"
TASK_INVALID = "TASK_INVALID"
TASK_ID_INVALID = "TASK_ID_INVALID"
TASK_ID_DUPLICATE = "TASK_ID_DUPLICATE"
ANNOTATION_INVALID = "ANNOTATION_INVALID"
POLICY_INVALID = "POLICY_INVALID"
RUN_ID_INVALID = "RUN_ID_INVALID"
CONFIG_INVALID = "CONFIG_INVALID"

RUN_DOCTOR_NOT_GREEN = "RUN_DOCTOR_NOT_GREEN"

# ── Policy and thresholds (§2.2 rule 3, §14.9) ───────────────────────────────

POLICIES = ("auto", "prefer", "required")
THRESHOLD_AUTO_MINUTES = 20
THRESHOLD_PREFER_MINUTES = 5
THRESHOLD_PRESSURE_MINUTES = 5
THRESHOLD_REQUIRED_MINUTES = 0  # required insists: only eligibility decides

PROVIDER_ORDER = ("claude", "codex")
PROVIDER_TIERS = {
    "claude": ("execution", "volume", "judgment"),
    "codex": ("execution", "volume"),
}
DEFAULT_LIMITS = {"claude": 3, "codex": 3, "total": 6, "max_local_verifiers": 1}
TASK_KINDS = ("build", "review", "verify", "search")
UNCLOUDABLE_KINDS = ("review", "verify")
OWNS_SOURCES = ("annotated", "estimated", "missing")
DEFAULT_TIER = "execution"
DOCTOR_STATES = ("green", "not-green", "unknown")
MINUTES_PER_HOUR = 60

# ── Denylist (§7) ────────────────────────────────────────────────────────────

DENYLIST = (
    ".ffs/**",
    ".ffs-result/**",
    ".planning/**",
    ".claude/**",
    ".codex/**",
    ".agents/**",
    ".cursor/**",
    ".vscode/**",
    ".gemini/**",
    ".devcontainer/**",
    ".github/**",
    ".feature-fix-swarm/**",
    ".git*",
    ".gitmodules",
    ".gitattributes",
    ".gitconfig",
    ".git",
    "scripts/hooks/**",
    "hooks/**",
    ".husky/**",
    ".circleci/**",
    ".gitlab-ci.yml",
    ".mcp.json",
    "CLAUDE.md",
    "CLAUDE.local.md",
    "AGENTS.md",
    "GEMINI.md",
    ".envrc",
    ".npmrc",
    ".yarnrc*",
    ".pnpmfile.cjs",
    ".pre-commit-config.yaml",
    ".lfsconfig",
    ".mise.toml",
    ".tool-versions",
    ".env*",
    "*.pem",
    "id_rsa*",
    "setup.sh",
)

WILDCARD_CHARS = "*?["
GLOBSTAR = "**"


class RouteError(ValueError):
    """Stable-code error.  ``plan``/``refused`` are set for required-policy refusals."""

    def __init__(
        self,
        code: str,
        detail: str = "",
        *,
        plan: Optional[Dict[str, Any]] = None,
        refused: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        super().__init__("%s %s" % (code, detail) if detail else code)
        self.code = code
        self.detail = detail
        self.plan = plan
        self.refused = refused if refused is not None else []


# ── Globs ────────────────────────────────────────────────────────────────────


def _has_wildcard(segment: str) -> bool:
    return any(char in segment for char in WILDCARD_CHARS)


def _norm_glob(glob: object) -> Optional[Tuple[str, ...]]:
    """Segments of ``glob`` with a trailing ``**`` appended, or None when unsafe."""
    if not isinstance(glob, str):
        return None
    text = glob.strip().replace("\\", "/")
    if not text or text.startswith("/"):
        return None
    segments = [part for part in text.split("/") if part not in ("", ".")]
    if ".." in segments:
        return None
    if not segments:
        return (GLOBSTAR,)
    if segments[-1] != GLOBSTAR:
        segments.append(GLOBSTAR)
    return tuple(segments)


def _segment_compatible(left: str, right: str) -> bool:
    left_wild, right_wild = _has_wildcard(left), _has_wildcard(right)
    if left_wild and right_wild:
        return True
    if left_wild:
        return fnmatch.fnmatchcase(right, left)
    if right_wild:
        return fnmatch.fnmatchcase(left, right)
    return left == right


def _segments_overlap(left: Sequence[str], right: Sequence[str]) -> bool:
    memo: Dict[Tuple[int, int], bool] = {}
    len_left, len_right = len(left), len(right)

    def walk(i: int, j: int) -> bool:
        key = (i, j)
        if key in memo:
            return memo[key]
        if i == len_left and j == len_right:
            result = True
        elif i < len_left and left[i] == GLOBSTAR:
            result = walk(i + 1, j) or (j < len_right and walk(i, j + 1))
        elif j < len_right and right[j] == GLOBSTAR:
            result = walk(i, j + 1) or (i < len_left and walk(i + 1, j))
        elif i < len_left and j < len_right:
            result = _segment_compatible(left[i], right[j]) and walk(i + 1, j + 1)
        else:
            result = False
        memo[key] = result
        return result

    return walk(0, 0)


def _fold(text: str) -> str:
    return unicodedata.normalize("NFC", text).casefold()


#: ``.env*`` is denied except the committed example names.
_ENV_EXAMPLES = frozenset([".env.example", ".env.sample", ".env.template", ".env.dist"])
_ANCHORED_DENY = tuple(
    tuple(_fold(part) for part in entry.split("/")) for entry in DENYLIST if "/" in entry
)
#: ``.dotdir/**`` entries match as a path component at ANY depth.
_ANYDEPTH_DIRS = tuple(
    _fold(entry[: -len("/**")]) for entry in DENYLIST
    if entry.startswith(".") and entry.endswith("/**") and "/" not in entry[: -len("/**")]
)
_BASENAME_DENY = tuple(_fold(entry) for entry in DENYLIST if "/" not in entry)


def _component_denied(component: str) -> bool:
    if component in _ANYDEPTH_DIRS:
        return True
    for pattern in _BASENAME_DENY:
        if fnmatch.fnmatchcase(component, pattern):
            if pattern == ".env*" and component in _ENV_EXAMPLES:
                continue
            return True
    return False


def matches_denylist(path_or_glob: str) -> bool:
    """True when the path, or any path the glob could name, is denylisted.

    Case-insensitive.  Unsafe input (absolute, ``..``, empty, non-string, or
    holding a Cf/Cc character, which HFS+ ignores in names) answers True.
    """
    segments = _norm_glob(path_or_glob)
    if segments is None or any(unicodedata.category(ch) in ("Cf", "Cc") for ch in path_or_glob):
        return True
    segments = tuple(_fold(seg) for seg in segments)
    if any(_segments_overlap(segments, anchored) for anchored in _ANCHORED_DENY):
        return True
    return any(_component_denied(seg) for seg in segments if seg != GLOBSTAR)


def case_collisions(paths: Iterable[str], base_paths: Iterable[str] = ()) -> List[str]:
    """Paths of ``paths`` that alias another file or directory case-insensitively.

    ``paths`` are the result's changed paths; ``base_paths`` are the other
    paths of the tree (they may repeat entries of ``paths``).  Two spellings
    that fold to the same name (Unicode NFC + ``casefold``), for the file or
    for any of its parent directories, collide: on a case-insensitive checkout
    they are one file.  Returns the sorted colliding entries of ``paths``.
    """
    wanted = sorted({p for p in paths})
    spellings: Dict[str, Set[str]] = {}

    def prefixes(path: str) -> List[str]:
        parts = path.split("/")
        return ["/".join(parts[: i + 1]) for i in range(len(parts))]

    for path in list(wanted) + [p for p in base_paths]:
        for prefix in prefixes(path):
            spellings.setdefault(_fold(prefix), set()).add(prefix)
    return [p for p in wanted if any(len(spellings[_fold(x)]) > 1 for x in prefixes(p))]


def owns_overlap(a_globs: Iterable[str], b_globs: Iterable[str]) -> bool:
    """Conservative: True when any glob of ``a`` may share a path with any of ``b``.

    Identical globs, prefix containment and wildcard-vs-wildcard segments all
    count as overlap; unsafe globs overlap everything.  Empty input never does.
    """
    left = [g for g in a_globs]
    right = [g for g in b_globs]
    for a_glob in left:
        a_segments = _norm_glob(a_glob)
        for b_glob in right:
            b_segments = _norm_glob(b_glob)
            if a_segments is None or b_segments is None:
                return True
            if _segments_overlap(a_segments, b_segments):
                return True
    return False


# ── Annotations ──────────────────────────────────────────────────────────────

_EST_RE = re.compile(r"^\s*(?:(\d+)\s*h)?\s*(?:(\d+)\s*m?)?\s*$")
_LIST_SPLIT_RE = re.compile(r"[,\s]+")


def _bracket_spans(line: str) -> List[Tuple[int, int, str]]:
    """Balanced ``[...]`` spans as (start, end, inner); nesting is kept inside."""
    spans: List[Tuple[int, int, str]] = []
    index, length = 0, len(line)
    while index < length:
        if line[index] != "[":
            index += 1
            continue
        depth, cursor = 0, index
        while cursor < length:
            if line[cursor] == "[":
                depth += 1
            elif line[cursor] == "]":
                depth -= 1
                if depth == 0:
                    break
            cursor += 1
        if cursor < length and depth == 0:
            spans.append((index, cursor + 1, line[index + 1 : cursor]))
            index = cursor + 1
        else:
            index += 1
    return spans


def _parse_est(value: str) -> int:
    match = _EST_RE.match(value)
    if not match or not (match.group(1) or match.group(2)):
        raise RouteError(ANNOTATION_INVALID, "est must look like 45m, 2h, 1h30m or 90: %r" % value)
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    return hours * MINUTES_PER_HOUR + minutes


def _split_list(value: str, pattern: "re.Pattern[str]") -> List[str]:
    return [item for item in pattern.split(value.strip()) if item]


def _apply_annotation(result: Dict[str, Any], key: str, value: str) -> bool:
    """Record one annotation in ``result``; False when ``key`` is not ours."""
    if key == "owns":
        result.setdefault("owns", []).extend(
            item.strip() for item in value.split(",") if item.strip()
        )
    elif key == "est":
        result["est_minutes"] = _parse_est(value)
    elif key in ("needs-secret", "needs-secrets"):
        result.setdefault("needs_secrets", []).extend(_split_list(value, _LIST_SPLIT_RE))
    elif key == "local":
        result["local_only"] = True
        result["local_reason"] = value.strip()
    elif key == "after":
        result.setdefault("depends_on", []).extend(_split_list(value, _LIST_SPLIT_RE))
    elif key == "verify":
        result["verify_cmd"] = value.strip()
    elif key == "id":
        result["id"] = value.strip()
    else:
        return False
    return True


def parse_annotations(line: str) -> Dict[str, Any]:
    """Parse ``[owns: a/**, b.py] [est: 45m] [needs-secret: NAME] [local] ...``.

    Returns ``title`` (the line without the recognised annotations, whitespace
    collapsed) plus only the keys that were present: ``owns`` (list),
    ``est_minutes`` (int minutes), ``needs_secrets`` (list), ``local_only``
    (True) with ``local_reason``, ``depends_on`` (list), ``verify_cmd``,
    ``id``.  Repeated list annotations accumulate; a repeated scalar keeps the
    last value.  Unknown ``[...]`` tokens (``[model: x]``, ``[ ]``) are left
    in the title.  ``[verify: ...]`` may contain balanced brackets.
    """
    result: Dict[str, Any] = {}
    kept: List[str] = []
    cursor = 0
    for start, end, inner in _bracket_spans(line):
        key, separator, value = inner.partition(":")
        key = key.strip().lower()
        if key == "local" and separator == "":
            value = ""
        if not _apply_annotation(result, key, value):
            continue
        kept.append(line[cursor:start])
        cursor = end
    kept.append(line[cursor:])
    result["title"] = " ".join("".join(kept).split())
    return result


# ── Normalization (§2.1, §14.9 rule 7) ───────────────────────────────────────

_LIST_MARKER_RE = re.compile(r"^\s*[-*]\s*(?:\[[ xXfFsS]\]\s*)?")


def _string_list(value: object, field: str) -> List[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)) or not all(isinstance(v, str) for v in value):
        raise RouteError(TASK_INVALID, "%s must be a list of strings" % field)
    seen: List[str] = []
    for item in value:
        item = item.strip()
        if item and item not in seen:
            seen.append(item)
    return seen


def _source_text(raw: Mapping[str, Any]) -> str:
    for key in ("line", "title", "description"):
        if isinstance(raw.get(key), str):
            return str(raw[key])
    return ""


def _raw_fields(raw: object) -> Dict[str, Any]:
    """Merge annotations (beneath) and explicit dict fields (on top)."""
    if isinstance(raw, str):
        line = _LIST_MARKER_RE.sub("", raw, count=1)
        fields = parse_annotations(line)
        fields["prompt"] = fields["title"]
        return fields
    if not isinstance(raw, dict):
        raise RouteError(TASK_INVALID, "task must be a string or an object")
    fields = parse_annotations(_source_text(raw))
    for key, value in raw.items():
        if key in ("line", "title", "description"):
            continue
        fields[key] = value
    fields.setdefault("prompt", fields["title"])
    return fields


def _resolve_tier(fields: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    request = fields.get("model_request")
    tier = fields.get("tier")
    if request is None:
        tier = tier or DEFAULT_TIER
        request = {"kind": "tier", "name": tier}
    try:
        model_requests.resolve_request(request, host="claude")
    except model_requests.ModelRequestError as exc:
        raise RouteError(TASK_INVALID, "model_request: %s" % exc) from exc
    requested = request["name"] if request.get("kind") == "tier" else None
    if requested and tier and tier != requested:
        raise RouteError(TASK_INVALID, "tier %r conflicts with model_request %r" % (tier, requested))
    tier = tier or requested or DEFAULT_TIER
    if tier not in model_requests.TIERS:
        raise RouteError(TASK_INVALID, "unknown tier %r" % (tier,))
    return str(tier), dict(request)


def _check_secret_names(names: List[str]) -> None:
    for name in names:
        if not SECRET_NAME_RE.match(name):
            raise RouteError(TASK_INVALID, "invalid secret name %r" % name)


def _owns_fields(fields: Mapping[str, Any]) -> Tuple[List[str], str]:
    owns = _string_list(fields.get("owns"), "owns")
    source = fields.get("owns_source") or "annotated"
    if source not in OWNS_SOURCES:
        raise RouteError(TASK_INVALID, "owns_source must be one of %s" % ", ".join(OWNS_SOURCES))
    if not owns:
        return owns, "missing"
    if source == "missing":
        raise RouteError(TASK_INVALID, "owns_source 'missing' contradicts a non-empty owns")
    return owns, str(source)


def _int_field(fields: Mapping[str, Any], key: str) -> int:
    value = fields.get(key, 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RouteError(TASK_INVALID, "%s must be a non-negative integer" % key)
    return value


def _text_field(fields: Mapping[str, Any], key: str) -> str:
    value = fields.get(key, "")
    if not isinstance(value, str):
        raise RouteError(TASK_INVALID, "%s must be a string" % key)
    return value


def _normalize_one(raw: object, position: int) -> Dict[str, Any]:
    fields = _raw_fields(raw)
    task_id = fields.get("id") or "t%d" % position
    if not isinstance(task_id, str) or not TASK_ID_RE.match(task_id):
        raise RouteError(TASK_ID_INVALID, "task id %r must match %s" % (task_id, TASK_ID_RE.pattern))
    tier, request = _resolve_tier(fields)
    owns, owns_source = _owns_fields(fields)
    needs = _string_list(fields.get("needs_secrets"), "needs_secrets")
    _check_secret_names(needs)
    kind = fields.get("kind") or "build"
    if kind not in TASK_KINDS:
        raise RouteError(TASK_INVALID, "kind must be one of %s" % ", ".join(TASK_KINDS))
    return {
        "id": task_id,
        "title": _text_field(fields, "title"),
        "prompt": _text_field(fields, "prompt"),
        "tier": tier,
        "model_request": request,
        "owns": owns,
        "owns_source": owns_source,
        "est_minutes": _int_field(fields, "est_minutes"),
        "needs_secrets": needs,
        "local_only": bool(fields.get("local_only", False)),
        "local_reason": _text_field(fields, "local_reason"),
        "depends_on": _string_list(fields.get("depends_on"), "depends_on"),
        "kind": kind,
        # Whitespace-only is blank, never a command: it must not satisfy the verify requirement.
        "verify_cmd": _text_field(fields, "verify_cmd").strip(),
    }


def raw_task_ids(raw: Sequence[Any]) -> List[str]:
    """The ids as the caller wrote them (``id`` field or ``[id: x]`` annotation), before any validation.

    Normalization errors (``TASK_ID_INVALID``, ``TASK_ID_DUPLICATE``) quote an id, so a caller that must keep a
    flagged id out of every error scans these first.  An item that cannot be read contributes nothing: normalization
    reports it on its own."""
    ids: List[str] = []
    for item in raw:
        try:
            value = _raw_fields(item).get("id")
        except RouteError:
            continue
        if isinstance(value, str) and value:
            ids.append(value)
    return ids


def normalize_tasks(raw: Sequence[Any], *, sequential: bool) -> List[Dict[str, Any]]:
    """Produce §2.1 task objects from task lines or partial dicts, in input order.

    Ids are ``t1..tN`` by input position unless ``[id: x]`` / ``id`` is given.
    A raw dict may carry a ``line`` (or ``title``) with inline annotations;
    explicit dict fields win over annotations.  ``sequential`` adds a
    dependency from each task to the previous one.  ``depends_on`` references
    are checked later, by ``plan_routes`` (``DEP_UNKNOWN``).
    """
    tasks = [_normalize_one(item, index) for index, item in enumerate(raw, start=1)]
    seen: Set[str] = set()
    for task in tasks:
        if task["id"] in seen:
            raise RouteError(TASK_ID_DUPLICATE, "duplicate task id %r" % task["id"])
        seen.add(task["id"])
    if sequential:
        for previous, current in zip(tasks, tasks[1:]):
            if previous["id"] not in current["depends_on"]:
                current["depends_on"].append(previous["id"])
    return tasks


# ── Config views ─────────────────────────────────────────────────────────────


def _section(config: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = config.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise RouteError(CONFIG_INVALID, "config.%s must be an object" % key)
    return value


def _limits(config: Mapping[str, Any]) -> Dict[str, int]:
    given = _section(config, "limits")
    limits = dict(DEFAULT_LIMITS)
    for key in DEFAULT_LIMITS:
        if key in given:
            value = given[key]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise RouteError(CONFIG_INVALID, "limits.%s must be a non-negative integer" % key)
            limits[key] = value
    return limits


def _exposure(config: Mapping[str, Any]) -> Dict[str, List[str]]:
    raw = _section(_section(config, "secrets"), "exposure")
    result: Dict[str, List[str]] = {}
    for provider in PROVIDER_ORDER:
        names = raw.get(provider, [])
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
            raise RouteError(CONFIG_INVALID, "secrets.exposure.%s must be a list of names" % provider)
        result[provider] = sorted(set(names))
    return result


def _config_verify_cmd(config: Mapping[str, Any]) -> str:
    value = _section(config, "verify").get("cmd", "")
    return value.strip() if isinstance(value, str) else ""


# ── Graph ────────────────────────────────────────────────────────────────────


def _check_dependencies(order: List[str], deps: Mapping[str, List[str]]) -> None:
    known = set(order)
    for task_id in order:
        for dep in deps[task_id]:
            if dep not in known:
                raise RouteError(DEP_UNKNOWN, "task %s depends on unknown task %s" % (task_id, dep))
    remaining = _cycle_members(order, deps)
    if remaining:
        raise RouteError(DEP_CYCLE, "dependency cycle among: %s" % ", ".join(remaining))


def _cycle_members(order: List[str], deps: Mapping[str, List[str]]) -> List[str]:
    """Nodes left on a cycle after pruning sources, then sinks (Kahn both ways)."""
    alive = set(order)
    changed = True
    while changed:
        changed = False
        for task_id in order:
            if task_id not in alive:
                continue
            has_pred = any(dep in alive for dep in deps[task_id])
            has_succ = any(task_id in deps[other] for other in alive)
            if not has_pred or not has_succ:
                alive.discard(task_id)
                changed = True
    return [task_id for task_id in order if task_id in alive]


def _reachable(start: str, target: str, succ: Mapping[str, Set[str]]) -> bool:
    stack, seen = [start], {start}
    while stack:
        node = stack.pop()
        if node == target:
            return True
        for nxt in succ.get(node, ()):
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return False


def _add_overlap_edges(
    tasks: Sequence[Mapping[str, Any]], deps: Dict[str, List[str]]
) -> Dict[str, List[str]]:
    """§14.9 rule 2: earlier -> later for overlapping owns unless already ordered."""
    succ: Dict[str, Set[str]] = {}
    for task_id, dep_list in deps.items():
        for dep in dep_list:
            succ.setdefault(dep, set()).add(task_id)
    implicit: Dict[str, List[str]] = {str(t["id"]): [] for t in tasks}
    for later_index, later in enumerate(tasks):
        for earlier in tasks[:later_index]:
            first, second = str(earlier["id"]), str(later["id"])
            if not owns_overlap(earlier.get("owns") or [], later.get("owns") or []):
                continue
            if _reachable(first, second, succ) or _reachable(second, first, succ):
                continue
            succ.setdefault(first, set()).add(second)
            implicit[second].append(first)
    return implicit


def _components(order: List[str], deps: Mapping[str, List[str]]) -> List[List[str]]:
    parent = {task_id: task_id for task_id in order}

    def find(node: str) -> str:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    for task_id in order:
        for dep in deps[task_id]:
            parent[find(task_id)] = find(dep)
    groups: Dict[str, List[str]] = {}
    for task_id in order:
        groups.setdefault(find(task_id), []).append(task_id)
    return sorted(groups.values(), key=lambda members: order.index(members[0]))


def _waves(order: List[str], deps: Mapping[str, List[str]]) -> Dict[str, int]:
    waves: Dict[str, int] = {}
    pending = list(order)
    while pending:
        progressed = [t for t in pending if all(d in waves for d in deps[t])]
        for task_id in progressed:
            waves[task_id] = 1 + max((waves[d] for d in deps[task_id]), default=-1)
        pending = [t for t in pending if t not in waves]
    return waves


# ── Eligibility (§2.2 rule 1, §14.9 rule 4) ──────────────────────────────────


def _effective_tier(task: Mapping[str, Any]) -> str:
    request = task.get("model_request")
    if isinstance(request, dict) and request.get("kind") == "tier":
        return str(request.get("name"))
    return str(task.get("tier") or DEFAULT_TIER)


def _owns_reasons(owns: Sequence[str]) -> List[str]:
    if not owns:
        return []
    if any(_norm_glob(glob) is None for glob in owns):
        return [OWNS_INVALID]
    if any(matches_denylist(glob) for glob in owns):
        return [OWNS_DENYLIST]
    return []


def _provider_candidates(
    task: Mapping[str, Any],
    tier: str,
    available: Sequence[str],
    exposure: Mapping[str, List[str]],
) -> Tuple[List[str], List[str]]:
    """Providers for ``tier`` that are available and cover the secret names."""
    if not available:
        return [], [NO_PROVIDER_AVAILABLE]
    by_tier = [p for p in available if tier in PROVIDER_TIERS[p]]
    if not by_tier:
        return [], [NO_PROVIDER_FOR_TIER]
    needs = set(task.get("needs_secrets") or [])
    covering = [p for p in by_tier if needs <= set(exposure[p])]
    return covering, ([] if covering else [SECRET_NOT_EXPOSED])


def _task_verdict(
    task: Mapping[str, Any],
    *,
    available: Sequence[str],
    exposure: Mapping[str, List[str]],
    config_verify: str,
) -> Tuple[List[str], List[str]]:
    """(ineligibility reason codes, candidate providers)."""
    reasons: List[str] = []
    request = task.get("model_request")
    tier = _effective_tier(task)
    if task.get("local_only"):
        reasons.append(LOCAL_ONLY)
    if task.get("kind") in UNCLOUDABLE_KINDS:
        reasons.append(KIND_NOT_CLOUDABLE)
    if isinstance(request, dict) and request.get("kind") == "exact":
        reasons.append(MODEL_EXACT)
    if tier == "frontier":
        reasons.append(TIER_FRONTIER)
    owns = list(task.get("owns") or [])
    if task.get("owns_source") == "missing" or not owns:
        reasons.append(OWNS_MISSING)
    reasons.extend(_owns_reasons(owns))
    # A blank (empty or whitespace-only) command is no command: `bash -c ''` exits 0, so it would
    # "verify" a result without running a single test. Both the task's own and the config fallback
    # are stripped before the truthiness test.
    task_verify = task.get("verify_cmd")
    if not ((task_verify.strip() if isinstance(task_verify, str) else "") or (config_verify or "").strip()):
        reasons.append(VERIFY_CMD_MISSING)
    candidates: List[str] = []
    if MODEL_EXACT not in reasons and TIER_FRONTIER not in reasons:
        candidates, provider_reasons = _provider_candidates(task, tier, available, exposure)
        reasons.extend(provider_reasons)
    return reasons, candidates


# ── Provider choice ──────────────────────────────────────────────────────────


def _pick_provider(candidates: Sequence[str], free: Dict[str, int]) -> str:
    """Most free slots wins; ties go to the first provider in ``PROVIDER_ORDER``.

    Each pick takes one slot from ``free`` (D2), so a plan spreads tasks over
    both providers as their free slots even out.
    """
    ordered = [p for p in PROVIDER_ORDER if p in candidates]
    best = max(free[p] for p in ordered)
    choice = next(p for p in ordered if free[p] == best)
    free[choice] -= 1
    return choice


def _free_slots(slots_free: Optional[Mapping[str, int]], limits: Mapping[str, int]) -> Dict[str, int]:
    free = {}
    for provider in PROVIDER_ORDER:
        value = (slots_free or {}).get(provider, limits[provider])
        if isinstance(value, bool) or not isinstance(value, int):
            raise RouteError(CONFIG_INVALID, "slots_free.%s must be an integer" % provider)
        free[provider] = value
    return free


def _available(
    providers_available: Optional[Iterable[str]], limits: Mapping[str, int]
) -> List[str]:
    given = set(providers_available or ())
    return [p for p in PROVIDER_ORDER if p in given and limits[p] > 0]


def _model_fields(task: Mapping[str, Any], provider: str) -> Dict[str, Any]:
    request = task.get("model_request")
    if not isinstance(request, dict):
        request = {"kind": "tier", "name": _effective_tier(task)}
    resolved = model_requests.resolve_request(request, host=provider)
    # FFS passes --model/--effort to `claude --cloud`; `codex cloud exec` has no
    # model flag, so the Codex model is whatever the cloud environment defaults to.
    return {
        "model": resolved["model"],
        "effort": resolved["effort"],
        "provenance": "requested-unverified" if provider == "claude" else "provider-default-unverified",
    }


# ── Plan ─────────────────────────────────────────────────────────────────────


def _check_policy(policy: Optional[str]) -> str:
    if policy is None or policy == "off":
        return "off"
    if policy not in POLICIES:
        raise RouteError(POLICY_INVALID, "policy must be off, auto, prefer or required: %r" % (policy,))
    return str(policy)


def _threshold(mode: str, host_pressure_high: bool) -> int:
    if mode == "required":
        return THRESHOLD_REQUIRED_MINUTES
    base = THRESHOLD_PREFER_MINUTES if mode == "prefer" else THRESHOLD_AUTO_MINUTES
    return min(base, THRESHOLD_PRESSURE_MINUTES) if host_pressure_high else base


def _check_refusals(run_refusals: Optional[Iterable[str]]) -> List[str]:
    codes: List[str] = []
    for code in run_refusals or ():
        if not isinstance(code, str) or not RUN_REFUSAL_RE.match(code):
            raise RouteError(CONFIG_INVALID, "run refusal code must be UPPER_SNAKE: %r" % (code,))
        if code not in codes:
            codes.append(code)
    return codes


def _decide_group(
    members: Sequence[str],
    *,
    mode: str,
    run_codes: Sequence[str],
    verdicts: Mapping[str, Tuple[List[str], List[str]]],
    est: Mapping[str, int],
    threshold: int,
) -> Tuple[str, List[str]]:
    """(route, group reasons) per §14.9 rule 5."""
    if mode == "off":
        return "local", [POLICY_OFF]
    if run_codes:
        return "local", list(run_codes)
    if any(verdicts[t][0] for t in members):
        return "local", [GROUP_HAS_INELIGIBLE_TASK]
    if not any(est[t] >= threshold for t in members):
        return "local", [BELOW_THRESHOLD]
    return "cloud", [CLOUD_ELIGIBLE]


def _local_task_reasons(
    task_id: str,
    group_reasons: Sequence[str],
    verdicts: Mapping[str, Tuple[List[str], List[str]]],
) -> List[str]:
    own = verdicts[task_id][0]
    if group_reasons == [GROUP_HAS_INELIGIBLE_TASK]:
        return list(own) if own else [GROUP_LOCAL]
    return list(group_reasons)


def _required_grants(
    cloud_providers: Sequence[str],
    exposure: Mapping[str, List[str]],
    needs: Sequence[str],
    run: Optional[str],
) -> List[str]:
    keys: Set[str] = set()
    for provider in cloud_providers:
        keys.add("cloud-dispatch:%s" % provider)
        keys.update("secret-use:%s" % name for name in exposure[provider])
    keys.update("secret-use:%s" % name for name in needs)
    if cloud_providers and run is not None:
        keys.add("push:origin/ffs/cloud/%s" % run)
    return sorted(keys)


def _preconditions(
    *,
    cloud_providers: Sequence[str],
    exposure: Mapping[str, List[str]],
    needs: Sequence[str],
    run: Optional[str],
    grants: Optional[Iterable[str]],
    doctor: str,
    run_codes: Sequence[str],
) -> Dict[str, Any]:
    if doctor not in DOCTOR_STATES:
        raise RouteError(CONFIG_INVALID, "doctor must be one of %s" % ", ".join(DOCTOR_STATES))
    if doctor == "unknown" and RUN_DOCTOR_NOT_GREEN in run_codes:
        doctor = "not-green"
    required = _required_grants(cloud_providers, exposure, needs, run)
    granted = None if grants is None else set(grants)
    missing = [k for k in required if granted is None or k not in granted]
    consent = sorted(k[len("secret-use:") :] for k in missing if k.startswith("secret-use:"))
    return {"doctor": doctor, "grants_missing": missing, "consent_required": consent}


def _refusal_detail(refused: Sequence[Mapping[str, Any]]) -> str:
    return "; ".join("%s: %s" % (item["task"], ",".join(item["reasons"])) for item in refused)


def plan_routes(
    tasks: Sequence[Mapping[str, Any]],
    *,
    policy: Optional[str],
    config: Mapping[str, Any],
    providers_available: Optional[Iterable[str]],
    slots_free: Optional[Mapping[str, int]],
    host_pressure_high: bool,
    grants: Optional[Iterable[str]],
    run_refusals: Optional[Iterable[str]],
    run: Optional[str] = None,
    doctor: str = "unknown",
) -> Dict[str, Any]:
    """Build the §2.2 route plan plus ``preconditions`` (§14.9 rule 8).

    ``tasks`` are normalized §2.1 objects.  ``providers_available`` is a set of
    provider names; ``slots_free`` maps provider -> free slots (defaults to the
    configured limits); ``grants`` is a set of granted action keys or None when
    unknown (everything required is then reported missing); ``run_refusals`` is
    a list of caller-computed run-level refusal codes that keep every task
    local.  ``run`` (optional) names the run for the push grant key and
    ``doctor`` is ``green|not-green|unknown``.  Extra keys beyond §2.2:
    per-task ``depends_on`` (effective, including implicit overlap edges) and
    ``implicit_deps``.  Raises ``RouteError`` (DEP_UNKNOWN, DEP_CYCLE,
    CLOUD_REQUIRED_NOT_ELIGIBLE, ...); the last carries ``plan`` and ``refused``.
    """
    mode = _check_policy(policy)
    if run is not None and not RUN_ID_RE.match(run):
        raise RouteError(RUN_ID_INVALID, "run id %r must match %s" % (run, RUN_ID_RE.pattern))
    limits, exposure = _limits(config), _exposure(config)
    run_codes = _check_refusals(run_refusals)

    order = [str(task["id"]) for task in tasks]
    by_id = {str(task["id"]): task for task in tasks}
    if len(by_id) != len(order):
        raise RouteError(TASK_ID_DUPLICATE, "duplicate task ids in plan input")
    explicit = {t: list(by_id[t].get("depends_on") or []) for t in order}
    _check_dependencies(order, explicit)
    implicit = _add_overlap_edges(tasks, explicit)
    effective = {t: explicit[t] + [d for d in implicit[t] if d not in explicit[t]] for t in order}
    waves = _waves(order, effective)

    available = _available(providers_available, limits)
    verdicts = {
        t: _task_verdict(
            by_id[t],
            available=available,
            exposure=exposure,
            config_verify=_config_verify_cmd(config),
        )
        for t in order
    }
    threshold = _threshold(mode, host_pressure_high)
    est = {t: int(by_id[t].get("est_minutes") or 0) for t in order}
    free = _free_slots(slots_free, limits)

    groups: List[Dict[str, Any]] = []
    task_plans: Dict[str, Dict[str, Any]] = {}
    refused: List[Dict[str, Any]] = []
    for index, members in enumerate(_components(order, effective), start=1):
        route, reasons = _decide_group(
            members, mode=mode, run_codes=run_codes, verdicts=verdicts, est=est, threshold=threshold
        )
        groups.append({"id": "g%d" % index, "tasks": list(members), "route": route, "reasons": reasons})
        for task_id in members:
            if route == "cloud":
                provider = _pick_provider(verdicts[task_id][1], free)
                entry = {"route": "cloud", "provider": provider, "reasons": [CLOUD_ELIGIBLE]}
                entry.update(_model_fields(by_id[task_id], provider))
            else:
                entry = {"route": "local", "provider": None, "model": None, "effort": None, "provenance": None}
                entry["reasons"] = _local_task_reasons(task_id, reasons, verdicts)
                refused.append(
                    {"task": task_id, "code": CLOUD_REQUIRED_NOT_ELIGIBLE, "reasons": entry["reasons"]}
                )
            entry["wave"] = waves[task_id]
            entry["depends_on"] = list(effective[task_id])
            entry["implicit_deps"] = list(implicit[task_id])
            task_plans[task_id] = entry

    cloud_ids = [t for t in order if task_plans[t]["route"] == "cloud"]
    cloud_providers = [p for p in PROVIDER_ORDER if any(task_plans[t]["provider"] == p for t in cloud_ids)]
    needs = sorted({n for t in cloud_ids for n in (by_id[t].get("needs_secrets") or [])})
    plan: Dict[str, Any] = {
        "run": run,
        "policy": mode,
        "groups": groups,
        "tasks": {t: task_plans[t] for t in order},
        "budgets": dict(limits),
        "refused": [],
        "secret_needs": {
            t: list(by_id[t]["needs_secrets"]) for t in cloud_ids if by_id[t].get("needs_secrets")
        },
        "local_slots_saved": len(cloud_ids),
        "preconditions": _preconditions(
            cloud_providers=cloud_providers,
            exposure=exposure,
            needs=needs,
            run=run,
            grants=grants,
            doctor=doctor,
            run_codes=run_codes,
        ),
    }
    if mode == "required" and refused:
        raise RouteError(
            CLOUD_REQUIRED_NOT_ELIGIBLE, _refusal_detail(refused), plan=plan, refused=refused
        )
    return plan

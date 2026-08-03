#!/usr/bin/env python3
"""Typed model-request contract shared by FFS hosts.

Vendor IDs are deliberately legal only behind ``kind: exact``.  Tier requests
express workload intent and resolve through one small, auditable table.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


class ModelRequestError(ValueError):
    pass


CODEX_TIERS = {
    "frontier": ("gpt-5.6-sol", "xhigh"),
    "judgment": ("gpt-5.6-sol", "high"),
    "execution": ("gpt-5.6-terra", "medium"),
    "volume": ("gpt-5.6-luna", "low"),
}
CLAUDE_TIERS = {
    "frontier": ("claude-fable-5", None),
    "judgment": ("claude-opus-5", None),
    "execution": ("claude-sonnet-5", None),
    "volume": ("claude-haiku-4-5-20251001", None),
}
TIERS = CODEX_TIERS

VENDOR_MODEL_RE = re.compile(
    r"^(?:gpt-|o[1-9](?:-|$)|claude-|gemini-|deepseek-|qwen-|minimax-)",
    re.IGNORECASE,
)


def resolve_request(request: object, *, host: str = "codex") -> dict[str, Any]:
    if not isinstance(request, dict):
        raise ModelRequestError("model request must be an object")
    kind = request.get("kind")
    if kind == "tier":
        name = request.get("name")
        if host not in {"codex", "claude"}:
            raise ModelRequestError("host must be codex or claude")
        tiers = CODEX_TIERS if host == "codex" else CLAUDE_TIERS
        if name not in tiers or set(request) != {"kind", "name"}:
            raise ModelRequestError(
                "tier request must be {kind:'tier', "
                "name:'frontier|judgment|execution|volume'}"
            )
        model, effort = tiers[str(name)]
        return {
            "kind": "tier",
            "name": name,
            "model": model,
            "effort": effort,
            "exact_provenance": False,
        }
    if kind == "exact":
        model_id = request.get("id")
        if (
            set(request) != {"kind", "id"}
            or not isinstance(model_id, str)
            or not model_id.strip()
        ):
            raise ModelRequestError("exact request must be {kind:'exact', id:'<model>'}")
        return {
            "kind": "exact",
            "model": model_id,
            "effort": "high" if host == "codex" else None,
            "exact_provenance": True,
            "fallback_allowed": False,
        }
    raise ModelRequestError("unknown model request kind")


def _validate_node(node: object, location: str) -> None:
    if isinstance(node, dict):
        if node.get("kind") in {"tier", "exact"}:
            try:
                resolve_request(node)
            except ModelRequestError as exc:
                raise ModelRequestError(f"{location}: {exc}") from exc
            return
        for key, value in node.items():
            _validate_node(value, f"{location}.{key}")
        return
    if isinstance(node, list):
        for index, value in enumerate(node):
            _validate_node(value, f"{location}[{index}]")
        return
    if isinstance(node, str) and VENDOR_MODEL_RE.match(node):
        raise ModelRequestError(
            f"{location}: raw vendor model id {node!r}; wrap it in kind:'exact'"
        )


def validate_document(path: Path) -> None:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ModelRequestError(f"cannot read model request document {path}: {exc}") from exc
    _validate_node(data, "$")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    resolve = sub.add_parser("resolve")
    resolve.add_argument("request", help="JSON model request")
    resolve.add_argument("--host", choices=("codex", "claude"), default="codex")
    lint = sub.add_parser("lint")
    lint.add_argument("path", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "resolve":
            print(
                json.dumps(
                    resolve_request(json.loads(args.request), host=args.host),
                    sort_keys=True,
                )
            )
        else:
            validate_document(args.path)
    except (json.JSONDecodeError, ModelRequestError) as exc:
        print(f"model-request: {exc}", file=__import__("sys").stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

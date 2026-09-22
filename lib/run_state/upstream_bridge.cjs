"use strict";

/*
 * Fixed stdin-only bridge for a controller-pinned GSD CJS closure.  This file
 * accepts neither module paths nor code from the caller; Python supplies the
 * declared module root only after hashing the complete closure.
 */
const fs = require("node:fs");
const path = require("node:path");

function invalid() {
  throw new Error("invalid upstream bridge request");
}

function isSegment(value, nullable) {
  if (value === null && nullable) return true;
  return typeof value === "string" && value.length > 0 &&
    !value.includes("/") && !value.includes("\\") &&
    !value.includes("..") && !value.includes("\u0000");
}

function modulePath(root, relative) {
  const candidate = path.resolve(root, relative);
  if (!candidate.startsWith(root + path.sep)) invalid();
  return candidate;
}

function main() {
  const text = fs.readFileSync(0, "utf8");
  const request = JSON.parse(text);
  const keys = [
    "workspace", "project", "workstream", "session_key", "stored_workstream", "module_root",
  ];
  if (!request || typeof request !== "object" || Array.isArray(request) ||
      Object.keys(request).length !== keys.length || !keys.every((key) => key in request)) {
    invalid();
  }
  if (!path.isAbsolute(request.workspace) || !path.isAbsolute(request.module_root) ||
      !isSegment(request.project, true) || !isSegment(request.workstream, true) ||
      !isSegment(request.stored_workstream, true) || !isSegment(request.session_key, false)) {
    invalid();
  }
  const planning = require(modulePath(request.module_root, "planning-workspace.cjs"));
  const workstreams = require(modulePath(request.module_root, "active-workstream-store.cjs"));
  const resolved = workstreams.resolveActiveWorkstream(
    request.workspace,
    request.workstream === null ? [] : ["--ws", request.workstream],
    {},
    {getStored: () => request.stored_workstream},
  );
  const workstream = resolved.ws;
  const planningRoot = planning.planningDir(request.workspace, workstream ?? null, request.project ?? null);
  const effective = workstreams.getWorkstreamSessionKey();
  process.stdout.write(JSON.stringify({
    project: request.project,
    workstream,
    session_key: request.session_key,
    effective_session_key: effective,
    planning_root: planningRoot,
  }));
}

try {
  main();
} catch (_) {
  process.exitCode = 2;
}

"use strict";
// Read-only use of the existing pinned GSD scanner/frontmatter closure.
const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
try {
  const request = JSON.parse(fs.readFileSync(0, "utf8"));
  if (request && Object.keys(request).sort().join() === "module_root,phase_directories,phase_scope") {
    if (!path.isAbsolute(request.module_root) || typeof request.phase_scope !== "string" ||
        !/^\d+(\.\d+)*$/.test(request.phase_scope) || !Array.isArray(request.phase_directories) ||
        request.phase_directories.length > 1024 || request.phase_directories.some(name =>
          typeof name !== "string" || name !== path.basename(name) || name === "." || name === "..")) throw Error();
    const phaseId = require(path.join(request.module_root, "phase-id.cjs"));
    const selected = phaseId.matchPhaseDirs(request.phase_directories, phaseId.normalizePhaseName(request.phase_scope));
    if (selected.matches.length !== 1) throw Error("phase missing or ambiguous");
    process.stdout.write(JSON.stringify({schema:"ffs.prelaunch-phase-selection/v1", directory:selected.matches[0]}));
    return;
  }
  if (!request || Object.keys(request).sort().join() !== "module_root,phase_directory,workspace" ||
      ![request.module_root, request.phase_directory, request.workspace].every(path.isAbsolute)) throw Error();
  const scanModule = require(path.join(request.module_root, "plan-scan.cjs"));
  const frontmatter = require(path.join(request.module_root, "frontmatter.cjs"));
  const {SCOPE} = require(path.join(request.module_root, "planning-scope.cjs"));
  const scan = scanModule.scanPhasePlans(request.phase_directory);
  if (scan.scope !== SCOPE.COMPLETE) throw Error("incomplete plan scan");
  const files = scan.planFiles.filter(scanModule.isCanonicalPlanFile).sort();
  if (!files.length || files.length > 256) throw Error("plan inventory bounds");
  let total = 0;
  const plans = files.map(name => {
    const file = path.resolve(request.phase_directory, name);
    if (!file.startsWith(request.phase_directory + path.sep)) throw Error();
    const info = fs.lstatSync(file);
    if (!info.isFile() || info.nlink !== 1 || info.size > 65536) throw Error();
    const fd = fs.openSync(file, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW);
    let body;
    try {
      body = fs.readFileSync(fd);
      const after = fs.fstatSync(fd);
      if (after.ino !== info.ino || after.dev !== info.dev || after.size !== info.size ||
          after.mtimeMs !== info.mtimeMs || body.length !== info.size) throw Error();
    } finally { fs.closeSync(fd); }
    total += body.length;
    if (total > 1048576) throw Error("plan inventory bounds");
    return {path: path.relative(request.workspace, file),
      sha256: crypto.createHash("sha256").update(body).digest("hex"),
      frontmatter: frontmatter.extractFrontmatter(body.toString("utf8"))};
  });
  process.stdout.write(JSON.stringify({schema:"ffs.prelaunch-plan-scan/v1", scope:scan.scope, plans}));
} catch (_) { process.exitCode = 78; }

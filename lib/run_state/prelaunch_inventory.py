"""Owner-frozen GSD plan bytes for parent/child resource envelopes.

The pinned GSD scanner runs against a private byte copy, not the live checkout.
This inventory does not assign waves or claim a plan completed; GSD retains its
dependency scheduling semantics. Callers must bind admitted chunks to this
inventory before accepting a model-produced wave manifest.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile

from .upstream import UpstreamRuntime, _anchored_regular
from .wave_execution import capture_prelaunch_snapshot


_BRIDGE_SHA256 = 'b2ac5e136a0d3318d0c1b7dc0e60714c24ee53b04e93b57f4b0810bc06bae9b4'


class PrelaunchInventoryRefused(ValueError):
    pass


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def _phase_bytes(directory: Path) -> dict[str, bytes]:
    """The scanner's flat/nested Markdown surface, through no-follow fds."""
    descriptors = []
    try:
        if not directory.is_absolute() or directory.resolve(strict=True) != directory:
            raise PrelaunchInventoryRefused('PRELAUNCH_PLAN_PATH_UNSAFE')
        current = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
        descriptors.append(current)
        for component in directory.parts[1:]:
            current = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            descriptors.append(current)
        roots = [('', current)]
        names = os.listdir(current)
        if len(names) > 1024:
            raise PrelaunchInventoryRefused('PRELAUNCH_PLAN_BOUNDS')
        if 'plans' in names:
            nested = os.open('plans', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            descriptors.append(nested)
            roots.append(('plans/', nested))
        result, total = {}, 0
        for prefix, parent in roots:
            names = os.listdir(parent)
            if len(names) > 1024:
                raise PrelaunchInventoryRefused('PRELAUNCH_PLAN_BOUNDS')
            for name in sorted(names):
                if not name.endswith('.md'):
                    continue
                descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
                try:
                    before = os.fstat(descriptor)
                    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > 65536:
                        raise PrelaunchInventoryRefused('PRELAUNCH_PLAN_BOUNDS')
                    body = b''
                    while len(body) <= 65536:
                        chunk = os.read(descriptor, 65537 - len(body))
                        if not chunk:
                            break
                        body += chunk
                    after = os.fstat(descriptor)
                    if (len(body) != before.st_size or
                        (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) !=
                        (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
                        raise PrelaunchInventoryRefused('PRELAUNCH_PLAN_SOURCE_CHANGED')
                    body.decode('utf-8', errors='strict')
                    total += len(body)
                    if total > 1048576:
                        raise PrelaunchInventoryRefused('PRELAUNCH_PLAN_BOUNDS')
                    result[prefix + name] = body
                finally:
                    os.close(descriptor)
        return result
    except (OSError, UnicodeError) as error:
        raise PrelaunchInventoryRefused('PRELAUNCH_PLAN_PATH_UNSAFE') from error
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def scan_frozen_plan_bytes(runtime: UpstreamRuntime, files: dict[str, bytes], *, staging_root: Path):
    """Invoke the pinned scanner only over exact bounded copied bytes."""
    if not isinstance(runtime, UpstreamRuntime):
        raise PrelaunchInventoryRefused('UPSTREAM_INVALID')
    if (not isinstance(files, dict) or not 1 <= len(files) <= 1024
            or any(not isinstance(name, str) or not isinstance(data, bytes) for name, data in files.items())
            or sum(map(len, files.values())) > 1048576):
        raise PrelaunchInventoryRefused('PRELAUNCH_PLAN_BOUNDS')
    runtime.verify()
    bridge = Path(__file__).with_name('prelaunch_plans.cjs')
    body, _identity = _anchored_regular(bridge)
    if hashlib.sha256(body).hexdigest() != _BRIDGE_SHA256:
        raise PrelaunchInventoryRefused('PRELAUNCH_BRIDGE_DRIFT')
    staging_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if staging_root.resolve() != staging_root or stat.S_IMODE(staging_root.stat().st_mode) != 0o700:
        raise PrelaunchInventoryRefused('PRELAUNCH_PLAN_PATH_UNSAFE')
    with tempfile.TemporaryDirectory(prefix='plan-scan-', dir=staging_root) as temporary:
        copied = Path(temporary)
        for name, data in files.items():
            relative = Path(name)
            if (not name.endswith('.md') or str(relative) != name or relative.is_absolute() or '..' in relative.parts or relative.parts not in
                    ((relative.name,), ('plans', relative.name)) or not isinstance(data, bytes) or len(data) > 65536):
                raise PrelaunchInventoryRefused('PRELAUNCH_PLAN_BOUNDS')
            target = copied / relative
            target.parent.mkdir(mode=0o700, exist_ok=True)
            target.write_bytes(data)
            target.chmod(0o600)
        try:
            completed = subprocess.run([str(runtime.node_path), '--jitless', str(bridge)],
                input=_canonical({'module_root': str(runtime.module_root), 'phase_directory': str(copied),
                                  'workspace': str(copied)}), cwd=copied,
                env={'PATH': '/usr/bin:/bin', 'LANG': 'C', 'LC_ALL': 'C'},
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15, check=False)
            result = json.loads(completed.stdout)
        except (OSError, subprocess.TimeoutExpired, ValueError) as error:
            raise PrelaunchInventoryRefused('PRELAUNCH_PLAN_SCAN_FAILED') from error
    runtime.verify()
    after, _ = _anchored_regular(bridge)
    if hashlib.sha256(after).hexdigest() != _BRIDGE_SHA256:
        raise PrelaunchInventoryRefused('PRELAUNCH_BRIDGE_DRIFT')
    if (completed.returncode or len(completed.stdout) > 1048576 or len(completed.stderr) > 65536
            or not isinstance(result, dict) or set(result) != {'schema', 'scope', 'plans'}
            or result['schema'] != 'ffs.prelaunch-plan-scan/v1' or result['scope'] != 'complete'
            or not isinstance(result['plans'], list) or not 1 <= len(result['plans']) <= 256):
        raise PrelaunchInventoryRefused('PRELAUNCH_PLAN_SCAN_FAILED')
    for plan in result['plans']:
        if (not isinstance(plan, dict) or set(plan) != {'path', 'sha256', 'frontmatter'}
                or plan['path'] not in files or hashlib.sha256(files[plan['path']]).hexdigest() != plan['sha256']
                or not isinstance(plan['frontmatter'], dict)):
            raise PrelaunchInventoryRefused('PRELAUNCH_PLAN_SCAN_FAILED')
    return result


def freeze_prelaunch_plan_inventory(store, token, preparation, *, activity_id, runtime_identity,
                                    runtime, phase_directory, evidence_root, request_key):
    """Bind actual phase bytes, HEAD+overlay and runtime before parent spawn."""
    phase_directory, evidence_root = Path(phase_directory), Path(evidence_root)
    if preparation.path not in phase_directory.parents:
        raise PrelaunchInventoryRefused('PRELAUNCH_PLAN_PATH_UNSAFE')
    first = capture_prelaunch_snapshot(store, token, preparation, activity_id=activity_id,
                                      runtime_identity=runtime_identity, evidence_root=evidence_root)
    files = _phase_bytes(phase_directory)
    scan = scan_frozen_plan_bytes(runtime, files, staging_root=evidence_root / 'prelaunch-plan-scans')
    second = capture_prelaunch_snapshot(store, token, preparation, activity_id=activity_id,
                                       runtime_identity=runtime_identity, evidence_root=evidence_root)
    if first.input_digest != second.input_digest or files != _phase_bytes(phase_directory):
        raise PrelaunchInventoryRefused('PRELAUNCH_PLAN_SOURCE_CHANGED')
    material = {'schema': 'ffs.prelaunch-plan-inventory/v1', 'repository_id': token.repository_id,
                'run_id': token.run_id, 'generation': token.generation, 'activity_id': activity_id,
                'preparation_id': preparation.id, 'initial_head': preparation.base_commit,
                'input_digest': first.input_digest, 'runtime_identity': runtime_identity,
                'upstream_runtime_digest': runtime.runtime_digest, 'bridge_sha256': _BRIDGE_SHA256,
                'phase_directory': str(phase_directory.relative_to(preparation.path)),
                'phase_files': {name: hashlib.sha256(body).hexdigest() for name, body in sorted(files.items())},
                'plans': scan['plans']}
    encoded = _canonical(material)
    digest = hashlib.sha256(encoded).hexdigest()
    store.record_event_once(token, activity_id, 'prelaunch-plan-inventory:' + request_key,
                            {'inventory_sha256': digest, 'material': material})
    return material, digest


def select_active_phase(runtime: UpstreamRuntime, phases_root: Path, phase_scope: str) -> Path:
    """Use the pinned GSD phase matcher over an anchored active-phase listing."""
    import re
    if (not isinstance(runtime, UpstreamRuntime) or not isinstance(phase_scope, str)
            or re.fullmatch(r'\d+(?:\.\d+)*', phase_scope) is None):
        raise PrelaunchInventoryRefused('PRELAUNCH_PHASE_SCOPE_REQUIRED')
    runtime.verify()
    if not phases_root.is_absolute() or phases_root.resolve(strict=True) != phases_root:
        raise PrelaunchInventoryRefused('PRELAUNCH_PLAN_PATH_UNSAFE')
    descriptor = os.open(phases_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        names = os.listdir(descriptor)
        if len(names) > 1024:
            raise PrelaunchInventoryRefused('PRELAUNCH_PLAN_BOUNDS')
        directories = sorted(name for name in names if stat.S_ISDIR(
            os.stat(name, dir_fd=descriptor, follow_symlinks=False).st_mode))
        bridge = Path(__file__).with_name('prelaunch_plans.cjs')
        body, _ = _anchored_regular(bridge)
        if hashlib.sha256(body).hexdigest() != _BRIDGE_SHA256:
            raise PrelaunchInventoryRefused('PRELAUNCH_BRIDGE_DRIFT')
        result = subprocess.run([str(runtime.node_path), '--jitless', str(bridge)],
            input=_canonical({'module_root': str(runtime.module_root), 'phase_scope': phase_scope,
                              'phase_directories': directories}), cwd=phases_root,
            env={'PATH': '/usr/bin:/bin', 'LANG': 'C', 'LC_ALL': 'C'},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15, check=False)
        selection = json.loads(result.stdout)
        runtime.verify()
        after, _ = _anchored_regular(bridge)
        if (hashlib.sha256(after).hexdigest() != _BRIDGE_SHA256 or result.returncode
                or not isinstance(selection, dict)
                or set(selection) != {'schema', 'directory'}
                or selection['schema'] != 'ffs.prelaunch-phase-selection/v1'
                or selection['directory'] not in directories):
            raise PrelaunchInventoryRefused('PRELAUNCH_PHASE_SELECTION_FAILED')
        child = os.open(selection['directory'], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
        try:
            selected = phases_root / selection['directory']
            opened, named = os.fstat(child), selected.stat()
            if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino) or selected.resolve() != selected:
                raise PrelaunchInventoryRefused('PRELAUNCH_PLAN_SOURCE_CHANGED')
            return selected
        finally:
            os.close(child)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        if isinstance(error, PrelaunchInventoryRefused):
            raise
        raise PrelaunchInventoryRefused('PRELAUNCH_PHASE_SELECTION_FAILED') from error
    finally:
        os.close(descriptor)


def freeze_managed_plan_inventory(store, token, context, preparation, *, activity_id,
                                   runtime_identity, runtime, evidence_root, request_key):
    """Rebase the admitted planning root and use its durable typed phase scope."""
    upstream = context.upstream
    if (not isinstance(upstream, dict) or runtime.runtime_digest != upstream.get('runtime_digest')):
        raise PrelaunchInventoryRefused('UPSTREAM_RUNTIME_DRIFT')
    planning = Path(upstream['planning_root'])
    try:
        relative = planning.relative_to(Path(context.workspace))
    except ValueError as error:
        raise PrelaunchInventoryRefused('PRELAUNCH_PLAN_PATH_UNSAFE') from error
    if not relative.parts or '..' in relative.parts:
        raise PrelaunchInventoryRefused('PRELAUNCH_PLAN_PATH_UNSAFE')
    with store.read_transaction() as tx:
        row = tx.execute('SELECT planning_scope FROM context_runs WHERE repository_id=? AND run_id=? AND activity_id=?',
            (token.repository_id, token.run_id, context.activity_id)).fetchone()
    if row is None:
        raise PrelaunchInventoryRefused('PRELAUNCH_PHASE_SCOPE_REQUIRED')
    selected = select_active_phase(runtime, preparation.path / relative / 'phases', row['planning_scope'])
    return freeze_prelaunch_plan_inventory(store, token, preparation, activity_id=activity_id,
        runtime_identity=runtime_identity, runtime=runtime, phase_directory=selected,
        evidence_root=evidence_root, request_key=request_key)

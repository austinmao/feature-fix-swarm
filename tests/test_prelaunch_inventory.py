"""The actual pinned scanner consumes frozen bytes, without a GSD model run."""
import hashlib
import json
import os
from pathlib import Path

import pytest

from run_state.prelaunch_inventory import (
    PrelaunchInventoryRefused, _phase_bytes, scan_frozen_plan_bytes, select_active_phase,
)
from run_state.upstream import UpstreamRuntime


def _runtime():
    source = Path(os.environ['FFS_TEST_UPSTREAM_RUNTIME_DESCRIPTOR'])
    body = source.read_bytes()
    assert hashlib.sha256(body).hexdigest() == os.environ['FFS_TEST_UPSTREAM_RUNTIME_DESCRIPTOR_SHA256']
    return UpstreamRuntime.from_manifest(json.loads(body))


def test_actual_pinned_scanner_keeps_exact_plan_metadata_and_excludes_superseded(tmp_path):
    files = {
        '01-01-PLAN.md': b'---\nphase: 01\nplan: 01\nwave: 1\ndepends_on: []\nfiles_modified: [lib/a.py]\n---\nFirst\n',
        '01-02-PLAN.md': b'---\nstatus: superseded\n---\nOld\n',
        '01-01-SUMMARY.md': b'---\nstatus: blocked\n---\nIncomplete\n',
        'plans/PLAN-03.md': b'---\nphase: 01\nplan: 03\ndepends_on: [01-01]\n---\nThird\n',
    }
    result = scan_frozen_plan_bytes(_runtime(), files, staging_root=tmp_path.resolve() / 'private')
    assert result['scope'] == 'complete'
    assert [row['path'] for row in result['plans']] == ['01-01-PLAN.md', 'plans/PLAN-03.md']
    for row in result['plans']:
        assert row['sha256'] == hashlib.sha256(files[row['path']]).hexdigest()
    assert result['plans'][1]['frontmatter']['depends_on'] == ['01-01']
    assert not list((tmp_path / 'private').iterdir())


def test_phase_byte_capture_refuses_nested_symlink_and_hardlink(tmp_path):
    phase = tmp_path.resolve() / 'phase'
    phase.mkdir()
    outside = tmp_path.resolve() / 'outside'
    outside.mkdir()
    (phase / 'plans').symlink_to(outside, target_is_directory=True)
    with pytest.raises(PrelaunchInventoryRefused, match='PATH_UNSAFE'):
        _phase_bytes(phase)
    (phase / 'plans').unlink()
    original = outside / 'plan.md'
    original.write_text('private plan')
    os.link(original, phase / '01-01-PLAN.md')
    with pytest.raises(PrelaunchInventoryRefused, match='BOUNDS'):
        _phase_bytes(phase)


def test_copy_surface_is_bounded_before_invoking_runtime(tmp_path):
    with pytest.raises(PrelaunchInventoryRefused, match='BOUNDS'):
        scan_frozen_plan_bytes(_runtime(), {'x.md': b'x' * 1048577}, staging_root=tmp_path / 'private')


def test_pinned_phase_matcher_selects_exact_scope_and_refuses_ambiguous_directory(tmp_path):
    phases = tmp_path.resolve() / 'phases'
    phases.mkdir()
    (phases / '01-example').mkdir()
    (phases / '10-other').mkdir()
    assert select_active_phase(_runtime(), phases, '1') == phases / '01-example'
    (phases / '01-duplicate').mkdir()
    with pytest.raises(PrelaunchInventoryRefused, match='SELECTION_FAILED'):
        select_active_phase(_runtime(), phases, '1')
    with pytest.raises(PrelaunchInventoryRefused, match='SCOPE_REQUIRED'):
        select_active_phase(_runtime(), phases, 'execute phase one')

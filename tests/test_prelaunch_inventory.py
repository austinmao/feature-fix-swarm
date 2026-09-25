"""The actual pinned scanner consumes frozen bytes, without a GSD model run."""
import hashlib
import json
import os
from pathlib import Path

import pytest

from run_state.prelaunch_inventory import (
    PrelaunchInventoryRefused, _phase_bytes, is_valid_phase_scope, rebase_planning_root,
    scan_frozen_plan_bytes, select_active_phase,
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


@pytest.mark.parametrize("scope", ["1", "03", "3.2.1", "10.20.30"])
def test_is_valid_phase_scope_accepts_plain_ascii_digit_tokens(scope):
    assert is_valid_phase_scope(scope) is True


@pytest.mark.parametrize("scope", [
    "", " ", "-1", "1 2", "1.a", "1.", "execute phase one",
    "٣",  # U+0663 ARABIC-INDIC DIGIT THREE
    "３",  # U+FF13 FULLWIDTH DIGIT THREE
    None, 3,
])
def test_is_valid_phase_scope_rejects_non_ascii_and_non_token_values(scope):
    # re.fullmatch(r'[0-9]+...') is ASCII-only by construction, unlike \d
    # (which matches Unicode digit categories under Python's default str
    # patterns) -- a scope staged as a command argument must be plain ASCII.
    assert is_valid_phase_scope(scope) is False


def test_rebase_planning_root_rebases_the_relative_planning_path(tmp_path):
    root_workspace = tmp_path / "root"
    preparation_path = tmp_path / "child"
    upstream = {"planning_root": str(root_workspace / ".planning" / "demo-project")}
    result = rebase_planning_root(upstream, root_workspace=str(root_workspace), preparation_path=preparation_path)
    assert result == preparation_path / ".planning" / "demo-project"


@pytest.mark.parametrize("upstream", [
    None,
    "not-a-dict",
    {},
    {"planning_root": None},
    {"planning_root": ""},
])
def test_rebase_planning_root_refuses_missing_or_invalid_upstream(tmp_path, upstream):
    with pytest.raises(PrelaunchInventoryRefused, match='PATH_UNSAFE'):
        rebase_planning_root(upstream, root_workspace=str(tmp_path / "root"), preparation_path=tmp_path / "child")


def test_rebase_planning_root_refuses_a_planning_root_outside_the_workspace(tmp_path):
    upstream = {"planning_root": str(tmp_path / "elsewhere" / ".planning")}
    with pytest.raises(PrelaunchInventoryRefused, match='PATH_UNSAFE'):
        rebase_planning_root(upstream, root_workspace=str(tmp_path / "root"), preparation_path=tmp_path / "child")


def test_rebase_planning_root_refuses_a_planning_root_equal_to_the_workspace(tmp_path):
    # relative_to yields an empty relative path -- rebased would be the
    # preparation path itself, not a planning subdirectory.
    upstream = {"planning_root": str(tmp_path / "root")}
    with pytest.raises(PrelaunchInventoryRefused, match='PATH_UNSAFE'):
        rebase_planning_root(upstream, root_workspace=str(tmp_path / "root"), preparation_path=tmp_path / "child")


def test_rebase_planning_root_refuses_a_dot_dot_part(tmp_path):
    upstream = {"planning_root": str(tmp_path / "root" / ".." / "escape" / ".planning")}
    with pytest.raises(PrelaunchInventoryRefused, match='PATH_UNSAFE'):
        rebase_planning_root(upstream, root_workspace=str(tmp_path / "root"), preparation_path=tmp_path / "child")

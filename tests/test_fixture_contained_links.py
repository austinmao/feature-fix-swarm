"""Private-installation fixture scans accept only relative links that stay inside the fixture."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest
from test_installer_opus_acceptance import subject


def _project(tmp_path: Path) -> Path:
    fixture = tmp_path / "private-fixture"
    skills = fixture / "project" / ".claude" / "skills"
    skills.mkdir(parents=True)
    (skills / "fix.md").write_text("skill\n")
    (fixture / "project" / ".agents").mkdir()
    return fixture


def test_relative_link_inside_the_fixture_is_recorded_not_refused(tmp_path: Path) -> None:
    module = subject()
    fixture = _project(tmp_path)
    link = fixture / "project" / ".agents" / "skills"
    # setup.sh project scope creates exactly this shape through replace_link.
    link.symlink_to("../.claude/skills", target_is_directory=True)
    tree = module._tree(fixture)
    assert tree["project/.agents/skills"] == (
        "link", hashlib.sha256(b"../.claude/skills").hexdigest())
    assert "project/.agents/skills/fix.md" not in tree, "the scan must not follow the link"


@pytest.mark.parametrize("case", ["absolute", "escaping", "dangling", "loop", "via-outer-link"])
def test_absolute_escaping_dangling_or_looping_links_are_refused(tmp_path: Path, case: str) -> None:
    module = subject()
    fixture = _project(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = fixture / "project" / ".agents" / "skills"
    if case == "absolute":
        link.symlink_to(fixture / "project" / ".claude" / "skills")
    elif case == "escaping":
        link.symlink_to("../../../outside")
    elif case == "dangling":
        link.symlink_to("../.claude/missing")
    elif case == "loop":
        link.symlink_to("skills")
    else:
        # Lexically inside, but resolved through a second link that leaves the fixture.
        (fixture / "project" / "out").symlink_to(os.path.relpath(outside, fixture / "project"))
        link.symlink_to("../out")
    with pytest.raises(module.E) as refused:
        module._tree(fixture)
    assert refused.value.code == "FIXTURE_LINK", case

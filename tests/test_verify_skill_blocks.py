"""verify-skill-blocks.py — CI-executed proof blocks inside SKILL.md files.

A SKILL.md claim with a ```bash verify fence is extracted and EXECUTED in a
fresh temp dir (REPO_ROOT exported); a nonzero exit fails the build. This is
the anti-rot gate for skill prose: a doc claim about the code (a constant, a
lever's existence) breaks CI when the code moves out from under it.
Borrowed pattern: ai-cost-cutter-skills / flowstacks verify blocks.
"""
import pathlib
import subprocess
import sys

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "verify-skill-blocks.py"


def run(root):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root)],
        capture_output=True, text=True,
    )


def seed(tmp_path, name, body):
    d = tmp_path / "skills" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(body)


def test_passing_block_ok(tmp_path):
    seed(tmp_path, "good", "# t\n\n```bash verify\necho hi\n```\n")
    r = run(tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "1 verify block" in r.stdout


def test_failing_block_fails_and_names_skill(tmp_path):
    seed(tmp_path, "bad", "# t\n\n```bash verify\nexit 3\n```\n")
    r = run(tmp_path)
    assert r.returncode != 0
    assert "bad" in r.stdout + r.stderr


def test_repo_root_exported_to_block(tmp_path):
    seed(tmp_path, "rooted", '# t\n\n```bash verify\ntest -d "$REPO_ROOT/skills"\n```\n')
    r = run(tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr


def test_plain_bash_fence_not_executed(tmp_path):
    # Only ```bash verify fences are proofs; ordinary snippets stay prose.
    seed(tmp_path, "prose", "# t\n\n```bash\nexit 9\n```\n")
    r = run(tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "0 verify block" in r.stdout


def test_indented_fence_inside_list_item_is_executed(tmp_path):
    # Real SKILL.md blocks sit indented inside numbered steps.
    seed(tmp_path, "listed", "# t\n\n1. step:\n\n   ```bash verify\n   echo hi\n   ```\n")
    r = run(tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "1 verify block" in r.stdout


def test_no_blocks_is_pass(tmp_path):
    seed(tmp_path, "empty", "# t\nno fences here\n")
    assert run(tmp_path).returncode == 0


def test_real_repo_blocks_pass():
    # The repo's own seeded verify blocks must hold against the live tree.
    r = run(SCRIPT.parents[1])
    assert r.returncode == 0, r.stdout + r.stderr

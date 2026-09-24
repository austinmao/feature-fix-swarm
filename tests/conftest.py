import pytest


@pytest.fixture(autouse=True)
def _isolated_managed_admission_root(tmp_path_factory, monkeypatch):
    """Never touch the per-user managed-admission root; a test may still set its own."""
    root = tmp_path_factory.mktemp("managed-admission") / "root"
    monkeypatch.setenv("FFS_MANAGED_ADMISSION_ROOT", str(root))

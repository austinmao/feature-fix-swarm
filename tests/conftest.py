import pytest


@pytest.fixture(autouse=True)
def _isolated_managed_admission_root(tmp_path_factory):
    """Never touch the per-user managed-admission root; a test may still set its own.

    A private MonkeyPatch keeps the test's own ``monkeypatch`` teardown order unchanged.
    """
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("FFS_MANAGED_ADMISSION_ROOT", str(tmp_path_factory.mktemp("managed-admission") / "root"))
        yield

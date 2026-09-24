import sys
from pathlib import Path

import pytest

LIB_ROOT = Path(__file__).resolve().parents[2]
if str(LIB_ROOT) not in sys.path:
    sys.path.insert(0, str(LIB_ROOT))


@pytest.fixture(autouse=True)
def _isolated_managed_admission_root(tmp_path_factory):
    """Never touch the per-user managed-admission root; a test may still set its own.

    A private MonkeyPatch keeps the test's own ``monkeypatch`` teardown order unchanged.
    """
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("FFS_MANAGED_ADMISSION_ROOT", str(tmp_path_factory.mktemp("managed-admission") / "root"))
        yield

import sys
import time
from pathlib import Path

import pytest

LIB_ROOT = Path(__file__).resolve().parents[2]
if str(LIB_ROOT) not in sys.path:
    sys.path.insert(0, str(LIB_ROOT))


@pytest.fixture(autouse=True)
def _fixture_host_observation():
    """Default coordinators observe a fixture host, never the real one (see tests/conftest.py)."""
    from run_state import shared_resources
    from run_state.managed_admission import ManagedAdmissionQueue
    from run_state.resource_observation import ResourceObservation

    def fixture_observation() -> ResourceObservation:
        return ResourceObservation(time.monotonic_ns(), 4, 4 << 30, 4 << 30, 100, 100, {}, "fixture")

    def queue(*args, **kwargs):
        kwargs.setdefault("observation_provider", fixture_observation)
        return ManagedAdmissionQueue(*args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(shared_resources, "ManagedAdmissionQueue", queue)
        yield


@pytest.fixture(autouse=True)
def _isolated_managed_admission_root(tmp_path_factory):
    """Never touch the per-user managed-admission root; a test may still set its own.

    A private MonkeyPatch keeps the test's own ``monkeypatch`` teardown order unchanged.
    """
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("FFS_MANAGED_ADMISSION_ROOT", str(tmp_path_factory.mktemp("managed-admission") / "root"))
        yield

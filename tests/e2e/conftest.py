import pytest
from hivescope.scenarios import single_satellite


@pytest.fixture
def hive():
    """A started single-satellite topology; teardown is automatic.

    Yields ``(master, satellite)`` for the standard M0/S0 pair.
    """
    builder = single_satellite()
    try:
        builder.start_all()
        yield builder.get_master("M0"), builder.get_satellite("S0")
    finally:
        builder.stop_all()

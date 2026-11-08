import pytest

from omnicycler.features.simulator import SimulatorConfig, create_app

pytest_plugins = ["omnicycler.features.testing.plugin"]

CHANNEL_COUNT = 4


@pytest.fixture
def omnicycler_app_factory():
    return create_app


@pytest.fixture
def omnicycler_config() -> SimulatorConfig:
    return SimulatorConfig(channel_count=CHANNEL_COUNT, time_scale=3600.0, status_interval=0.1)


@pytest.fixture
def omnicycler_run_timeout() -> float:
    return 30.0


@pytest.fixture
def omnicycler_keeps_runs() -> bool:
    return False  # the reference simulator keeps its runs in memory

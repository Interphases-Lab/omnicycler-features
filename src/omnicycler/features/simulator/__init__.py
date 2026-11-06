"""
The OmniCycler reference simulator: a battery cycler server simulated in software.

Run it with `uv run connector start --app omnicycler.features.simulator:create_app`.
"""

import collections.abc

from unitelabs.cdk import Connector

from ..server import register_standard_features
from .backend import SUPPORTED_STEP_TYPES, SimulatedCyclerBackend
from .config import SimulatorConfig
from .simulation_controller import SimulationController


async def create_app(config: SimulatorConfig) -> collections.abc.AsyncGenerator[Connector]:
    """Create the simulator connector."""
    app = Connector(config)
    backend = SimulatedCyclerBackend(
        channel_count=config.channel_count,
        time_scale=config.time_scale,
        minimal_voltage=config.minimal_voltage,
        maximal_voltage=config.maximal_voltage,
        maximal_current=config.maximal_current,
    )
    async with backend:
        register_standard_features(app, backend, status_interval=config.status_interval)
        app.register(SimulationController())
        yield app


__all__ = ["SUPPORTED_STEP_TYPES", "SimulatedCyclerBackend", "SimulationController", "SimulatorConfig", "create_app"]

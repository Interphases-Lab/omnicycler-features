import dataclasses
import typing

from unitelabs.cdk import ConnectorBaseConfig, Field, SiLAServerConfig

from .. import __version__


def _sila_server() -> SiLAServerConfig:
    return SiLAServerConfig(
        name="OmniCycler Simulator",
        type="BatteryCycler",
        description="A battery cycler simulated in software, implementing the OmniCycler standard features.",
        version=__version__,
        vendor_url="https://github.com/Interphases-Lab",
    )


@dataclasses.dataclass
class SimulatorConfig(ConnectorBaseConfig):
    """Configuration of the OmniCycler reference simulator."""

    sila_server: SiLAServerConfig = dataclasses.field(default_factory=_sila_server)

    channel_count: typing.Annotated[int, Field(ge=1, le=512)] = 8
    """The number of simulated channels."""

    time_scale: typing.Annotated[float, Field(gt=0)] = 60.0
    """How many times faster than wall-clock time the simulated cells run."""

    minimal_voltage: float = 0.0
    """The lowest voltage of every channel, in volt."""

    maximal_voltage: float = 5.0
    """The highest voltage of every channel, in volt."""

    maximal_current: typing.Annotated[float, Field(gt=0)] = 2.0
    """The highest absolute current of every channel, in ampere."""

    status_interval: typing.Annotated[float, Field(gt=0)] = 1.0
    """How often channel statuses are polled for subscribers and monitors, in seconds."""

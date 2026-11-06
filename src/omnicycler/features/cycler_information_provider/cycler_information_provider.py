from unitelabs.cdk import sila

from .. import CATEGORY, ORIGINATOR
from ..backend import CyclerBackend
from ..protocol import unicycler_version
from ..types import ChannelInformation, StepType


class CyclerInformationProvider(sila.Feature):
    """
    Describe the battery cycler and its channels.

    All values are static while the server runs. `Channels` lists every channel number that the `Channel`
    parameters of `ChannelController` and `MeasurementProvider` accept, together with each channel's limits.
    Protocols are written in the aurora-unicycler format; `SupportedStepTypes` and `UnicyclerVersion` tell which
    protocols this cycler can execute.
    """

    def __init__(self, backend: CyclerBackend) -> None:
        super().__init__(originator=ORIGINATOR, category=CATEGORY)
        self.backend = backend

    @sila.UnobservableProperty()
    async def get_manufacturer(self) -> str:
        """The manufacturer of the cycler."""
        return (await self.backend.get_identity()).manufacturer

    @sila.UnobservableProperty()
    async def get_model(self) -> str:
        """The model of the cycler."""
        return (await self.backend.get_identity()).model

    @sila.UnobservableProperty()
    async def get_serial_number(self) -> str:
        """The serial number of the cycler."""
        return (await self.backend.get_identity()).serial_number

    @sila.UnobservableProperty()
    async def get_firmware_version(self) -> str:
        """The firmware or vendor software version of the cycler."""
        return (await self.backend.get_identity()).firmware_version

    @sila.UnobservableProperty()
    async def get_channels(self) -> list[ChannelInformation]:
        """The static properties of every channel."""
        return await self.backend.get_channels()

    @sila.UnobservableProperty()
    async def get_supported_step_types(self) -> list[StepType]:
        """The protocol technique steps this cycler can execute. Loop and tag steps are always supported."""
        return await self.backend.get_supported_step_types()

    @sila.UnobservableProperty()
    async def get_unicycler_version(self) -> str:
        """The aurora-unicycler version protocols are validated with. Protocols using newer fields are rejected."""
        return unicycler_version()

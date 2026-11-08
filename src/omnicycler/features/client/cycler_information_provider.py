from sila.client import FeatureNamespace

from .. import CATEGORY, ORIGINATOR
from ..types import ChannelInformation, StepType
from .base import ErrorMap, error_map, from_wire, list_from_wire, ms, translate_errors

FEATURE = f"{ORIGINATOR}/{CATEGORY}/CyclerInformationProvider/v1"
ERRORS: ErrorMap = error_map()


class CyclerInformationProviderClient:
    """Typed client of `CyclerInformationProvider`: the cycler and its channels."""

    def __init__(self, namespace: FeatureNamespace) -> None:
        self._ns = namespace

    async def get_manufacturer(self, *, timeout: float | None = None) -> str:
        """The manufacturer of the cycler."""
        with translate_errors(ERRORS):
            return from_wire(str, await self._ns.Manufacturer.read(timeout=ms(timeout)))

    async def get_model(self, *, timeout: float | None = None) -> str:
        """The model of the cycler."""
        with translate_errors(ERRORS):
            return from_wire(str, await self._ns.Model.read(timeout=ms(timeout)))

    async def get_serial_number(self, *, timeout: float | None = None) -> str:
        """The serial number of the cycler."""
        with translate_errors(ERRORS):
            return from_wire(str, await self._ns.SerialNumber.read(timeout=ms(timeout)))

    async def get_firmware_version(self, *, timeout: float | None = None) -> str:
        """The firmware or vendor software version of the cycler."""
        with translate_errors(ERRORS):
            return from_wire(str, await self._ns.FirmwareVersion.read(timeout=ms(timeout)))

    async def get_channels(self, *, timeout: float | None = None) -> list[ChannelInformation]:
        """The static properties of every channel."""
        with translate_errors(ERRORS):
            return list_from_wire(ChannelInformation, await self._ns.Channels.read(timeout=ms(timeout)))

    async def get_supported_step_types(self, *, timeout: float | None = None) -> list[StepType]:
        """The protocol technique steps the cycler can execute."""
        with translate_errors(ERRORS):
            return list_from_wire(StepType, await self._ns.SupportedStepTypes.read(timeout=ms(timeout)))

    async def get_unicycler_version(self, *, timeout: float | None = None) -> str:
        """The aurora-unicycler version the server validates protocols with."""
        with translate_errors(ERRORS):
            return from_wire(str, await self._ns.UnicyclerVersion.read(timeout=ms(timeout)))

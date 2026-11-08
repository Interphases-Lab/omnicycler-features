"""
Typed async client for any OmniCycler server.

    async with OmniCyclerClient.connect(ClientConfig(hostname="localhost", port=50052, tls=False)) as cycler:
        channels = await cycler.information.get_channels()
        run_id = await cycler.channels.start_protocol(1, protocol, Sample(name="cell-1", nominal_capacity=0.005))

Brand repos extend it with their own feature clients by subclassing
`OmniCyclerClient` and resolving their features in `__init__`.
"""

import collections.abc
import contextlib
import typing

from sila.client import Client, ClientConfig

from . import channel_controller, cycler_information_provider, measurement_provider
from .base import Execution, ms, require_feature
from .channel_controller import ChannelControllerClient
from .cycler_information_provider import CyclerInformationProviderClient
from .errors import (
    CommandOutcomeUnknownError,
    DeviceError,
    IncompatibleServerError,
    InvalidParameterError,
    OmniCyclerClientError,
    ProtocolMismatchError,
)
from .measurement_provider import MeasurementProviderClient, MonitorResult

FEATURES = (cycler_information_provider.FEATURE, channel_controller.FEATURE, measurement_provider.FEATURE)
"""The fully qualified identifiers of the standard features this client requires."""


class OmniCyclerClient:
    """
    Typed client for an OmniCycler server. Create it with `OmniCyclerClient.connect(config)`.

    Attributes:
      information: The cycler and its channels.
      channels: Running protocols on channels and following their state.
      measurements: The measured data of channel runs.
    """

    def __init__(self, client: Client) -> None:
        self.information = CyclerInformationProviderClient(require_feature(client, cycler_information_provider.FEATURE))
        self.channels = ChannelControllerClient(require_feature(client, channel_controller.FEATURE))
        self.measurements = MeasurementProviderClient(require_feature(client, measurement_provider.FEATURE))
        self._client = client

    @classmethod
    @contextlib.asynccontextmanager
    async def connect(
        cls, config: ClientConfig, *, timeout: float | None = None
    ) -> collections.abc.AsyncGenerator[typing.Self]:
        """
        Connect to a server and check that it offers every standard feature.

        Args:
          config: The connection configuration. Use TLS outside localhost.
          timeout: The longest to wait for the connection, in seconds.

        Raises:
          IncompatibleServerError: If a standard feature is missing or at another major version.
        """
        client = Client(config)
        await client.open(timeout=ms(timeout))
        try:
            yield cls(client)
        finally:
            await client.close()


__all__ = [
    "FEATURES",
    "ChannelControllerClient",
    "ClientConfig",
    "CommandOutcomeUnknownError",
    "CyclerInformationProviderClient",
    "DeviceError",
    "Execution",
    "IncompatibleServerError",
    "InvalidParameterError",
    "MeasurementProviderClient",
    "MonitorResult",
    "OmniCyclerClient",
    "OmniCyclerClientError",
    "ProtocolMismatchError",
]

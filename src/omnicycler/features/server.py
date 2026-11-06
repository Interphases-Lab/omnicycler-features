"""Server-side entry point for brand servers."""

from unitelabs.cdk import Connector

from .backend import CyclerBackend
from .channel_controller import ChannelController
from .cycler_information_provider import CyclerInformationProvider
from .measurement_provider import MeasurementProvider


def register_standard_features(app: Connector, backend: CyclerBackend, *, status_interval: float = 1.0) -> None:
    """
    Register every OmniCycler standard feature with a connector.

    Call this before `yield app` in `create_app`, after opening the backend.
    Brand-specific features are registered separately.

    Args:
      app: The connector to register the features with.
      backend: The brand backend the features use.
      status_interval: How often channel statuses are polled for subscribers and monitors, in seconds.
    """
    app.register(CyclerInformationProvider(backend))
    app.register(ChannelController(backend, status_interval=status_interval))
    app.register(MeasurementProvider(backend, poll_interval=status_interval))


__all__ = ["ChannelController", "CyclerInformationProvider", "MeasurementProvider", "register_standard_features"]

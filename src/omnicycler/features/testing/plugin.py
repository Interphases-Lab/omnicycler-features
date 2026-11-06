"""
Pytest fixtures of the OmniCycler conformance kit.

Enable them in a brand repo's `tests/conftest.py` with
`pytest_plugins = ["omnicycler.features.testing.plugin"]` and provide two
fixtures:

- `omnicycler_app_factory`: the brand's `create_app`.
- `omnicycler_config`: a fresh connector config in simulation mode.

The server address, TLS and discovery settings are overridden by the kit.
"""

import collections.abc
import contextlib
import inspect
import socket
import sys
import typing

import pytest
import pytest_asyncio
from sila.client import ClientConfig
from unitelabs.cdk import Connector, ConnectorBaseConfig

from ..client import OmniCyclerClient
from ..types import Sample
from .protocols import faulting_protocol, long_protocol, short_protocol

AppFactory = collections.abc.Callable[
    [typing.Any],
    collections.abc.AsyncGenerator[Connector] | collections.abc.Awaitable[Connector],
]


def pytest_configure(config: pytest.Config) -> None:  # noqa: ARG001
    """
    Write test output as UTF-8.

    On Windows, the CDK fails while logging an error to a non-UTF-8 console
    and turns every defined or validation error into an undefined one.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            with contextlib.suppress(ValueError, OSError):
                reconfigure(encoding="utf-8", errors="backslashreplace")


@pytest.fixture
def omnicycler_app_factory() -> AppFactory:
    """The brand's `create_app`. Override this fixture."""
    pytest.fail("Override the `omnicycler_app_factory` fixture with your server's `create_app`.")


@pytest.fixture
def omnicycler_config() -> ConnectorBaseConfig:
    """A fresh config of the brand's connector in simulation mode. Override this fixture."""
    pytest.fail("Override the `omnicycler_config` fixture with a fresh config of your server in simulation mode.")


@pytest.fixture
def omnicycler_short_protocol() -> bytes:
    """A protocol that finishes within `omnicycler_run_timeout`."""
    return short_protocol()


@pytest.fixture
def omnicycler_long_protocol() -> bytes:
    """A protocol that keeps a channel running for the duration of a test."""
    return long_protocol()


@pytest.fixture
def omnicycler_faulting_protocol() -> collections.abc.Callable[[Sample], bytes]:
    """Build a protocol for a sample that trips a safety limit within `omnicycler_run_timeout`."""
    return faulting_protocol


@pytest.fixture
def omnicycler_run_timeout() -> float:
    """The longest a short protocol may take, in seconds of wall-clock time."""
    return 60.0


@pytest_asyncio.fixture
async def omnicycler_server(
    omnicycler_app_factory: AppFactory, omnicycler_config: ConnectorBaseConfig
) -> collections.abc.AsyncIterator[ClientConfig]:
    """Run the brand's server in-process on a free local port and yield the client config to reach it."""
    port = _free_port()
    omnicycler_config.sila_server.hostname = "127.0.0.1"
    omnicycler_config.sila_server.port = port
    omnicycler_config.sila_server.tls = False
    omnicycler_config.discovery = None
    omnicycler_config.cloud_servers.enabled = False

    async with _running(omnicycler_app_factory, omnicycler_config):
        yield ClientConfig(hostname="127.0.0.1", port=port, tls=False, default_timeout=30_000)


@pytest_asyncio.fixture
async def omnicycler_client(omnicycler_server: ClientConfig) -> collections.abc.AsyncIterator[OmniCyclerClient]:
    """A typed client connected to the server under test."""
    async with OmniCyclerClient.connect(omnicycler_server, timeout=30) as client:
        yield client


@contextlib.asynccontextmanager
async def _running(factory: AppFactory, config: ConnectorBaseConfig) -> collections.abc.AsyncGenerator[Connector]:
    created = factory(config)
    if inspect.isasyncgen(created):
        app = await anext(created)
        try:
            await app.start()
            try:
                yield app
            finally:
                await app.stop()
        finally:
            await created.aclose()
        return

    app = await typing.cast("collections.abc.Awaitable[Connector]", created)
    await app.start()
    try:
        yield app
    finally:
        await app.stop()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]

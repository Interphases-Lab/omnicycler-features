"""
Pytest fixtures of the OmniCycler conformance kit.

Enable them in a brand repo's `tests/conftest.py` with
`pytest_plugins = ["omnicycler.features.testing.plugin"]` and provide two
fixtures:

- `omnicycler_app_factory`: the brand's `create_app`.
- `omnicycler_config`: a fresh connector config in simulation mode.

The restart tests call `omnicycler_restart`, which stops the server and
starts it again from the same config, so the config must keep the server's
storage (for example under `tmp_path`). Override `omnicycler_keeps_runs` to
return False for a server that keeps its runs in memory.

The server address, TLS and discovery settings are overridden by the kit.
"""

import collections.abc
import contextlib
import inspect
import socket
import typing

import pytest
import pytest_asyncio
from sila.client import ClientConfig
from unitelabs.cdk import Connector, ConnectorBaseConfig

from ..client import OmniCyclerClient
from ..console import use_utf8_console
from ..types import Sample
from .protocols import faulting_protocol, long_protocol, short_protocol

AppFactory = collections.abc.Callable[
    [typing.Any],
    collections.abc.AsyncGenerator[Connector] | collections.abc.Awaitable[Connector],
]


Restart = collections.abc.Callable[[], collections.abc.Awaitable[ClientConfig]]


def pytest_configure(config: pytest.Config) -> None:  # noqa: ARG001
    """Write test output as UTF-8, as every server does (see `use_utf8_console`)."""
    use_utf8_console()


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


@pytest.fixture
def omnicycler_keeps_runs() -> bool:
    """Whether the server keeps its runs across a restart, as the standard requires. Override only for a simulator."""
    return True


class _ServerUnderTest:
    """The brand's server, running in-process on a free local port, which a test may restart."""

    def __init__(self, factory: AppFactory, config: ConnectorBaseConfig) -> None:
        self._factory = factory
        self._config = config
        self._stack = contextlib.AsyncExitStack()
        self.client_config: ClientConfig | None = None

    async def start(self) -> ClientConfig:
        port = _free_port()
        self._config.sila_server.hostname = "127.0.0.1"
        self._config.sila_server.port = port
        self._config.sila_server.tls = False
        self._config.discovery = None
        self._config.cloud_servers.enabled = False
        await self._stack.enter_async_context(_running(self._factory, self._config))
        self.client_config = ClientConfig(hostname="127.0.0.1", port=port, tls=False, default_timeout=30_000)
        return self.client_config

    async def stop(self) -> None:
        await self._stack.aclose()
        self._stack = contextlib.AsyncExitStack()

    async def restart(self) -> ClientConfig:
        await self.stop()
        return await self.start()


@pytest_asyncio.fixture
async def omnicycler_server_under_test(
    omnicycler_app_factory: AppFactory, omnicycler_config: ConnectorBaseConfig
) -> collections.abc.AsyncIterator[_ServerUnderTest]:
    """The running server under test."""
    server = _ServerUnderTest(omnicycler_app_factory, omnicycler_config)
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


@pytest.fixture
def omnicycler_server(omnicycler_server_under_test: _ServerUnderTest) -> ClientConfig:
    """The client config to reach the server under test, as first started."""
    assert omnicycler_server_under_test.client_config is not None
    return omnicycler_server_under_test.client_config


@pytest.fixture
def omnicycler_restart(omnicycler_server_under_test: _ServerUnderTest) -> Restart:
    """
    Stop the server under test and start it again from the same config.

    Returns the client config to reach the restarted server, which listens on
    a new port. Clients of the old server stop working; connect a new one.
    """
    return omnicycler_server_under_test.restart


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

# omnicycler-features

The shared SiLA 2 interface of OmniCycler: one SiLA2 standard for battery cyclers of every brand. Brand servers (BioLogic, Arbin, Neware, …) implement it, and the lab's software talks to all of them through one typed client.

This package contains:

| Part | Module | Who uses it |
| --- | --- | --- |
| Standard features | `omnicycler.features.server` | Every brand server registers them |
| Backend interface | `omnicycler.features.backend` | Every brand server implements `CyclerBackend` |
| Typed async client | `omnicycler.features.client` | The OmniCycler orchestrator, scripts, notebooks |
| Conformance kit | `omnicycler.features.testing` | Every brand repo's CI |
| Reference simulator | `omnicycler.features.simulator` | Development without hardware |
| BDF export and BattINFO metadata | `omnicycler.features.bdf_export`, `omnicycler.features.bdf_metadata` | Used by the backend |

Feature identifiers: `se.uu.interphaseslab/cycling/<Feature>/v1`.

## The interface

A **protocol** is an [aurora-unicycler](https://github.com/EmpaEconversion/aurora-unicycler) JSON document, started on a **channel** together with a **sample** (name and nominal capacity). The sample replaces the protocol's own sample block, so one protocol file serves as a template for many cells.

| Feature | Endpoints |
| --- | --- |
| `CyclerInformationProvider` | `Manufacturer`, `Model`, `SerialNumber`, `FirmwareVersion`, `Channels`, `SupportedStepTypes`, `UnicyclerVersion` |
| `ChannelController` | `ChannelStatuses` (observable), `GetChannelStatus`, `GetRunInformation`, `StartProtocol`, `ValidateProtocol`, `StopProtocol`, `PauseProtocol`, `ResumeProtocol`, `ClearChannel` |
| `MeasurementProvider` | `GetRecordCount`, `GetData`, `GetDataTable` (Arrow, for bulk reads), `GetDataInTimeRange`, `MonitorChannel` (observable), `ExportData` (observable) |

Channel lifecycle:

```text
idle ──StartProtocol──▶ running ◀──ResumeProtocol── paused
 ▲                       │   └──PauseProtocol──────▶ │
 │                       ▼                           ▼
 └──ClearChannel── finished / stopped / faulted ◀── StopProtocol
```

An ended run stays on its channel, with its data, until `ClearChannel` is called. `StartProtocol` on such a channel raises `ChannelNotClearedError`, so data is never dropped by accident. Clearing drops the run from OmniCycler for good (the vendor's own files are untouched), so export it first if you need it.

Every run gets a `RunId` when it starts, reported in `RunInformation`, for bookkeeping. A faulted channel reports the cycler's reason in `ChannelStatus.FaultReason`.

Data uses Battery Data Format (BDF) quantities throughout: V, A (positive = charging), Ah, Wh, s, °C, Pa, Ω, Hz. A quantity a channel does not measure is NaN; `ChannelInformation.MeasuredQuantities` lists the optional ones a channel does measure. `ExportData` returns a BDF file (CSV or Parquet, BDF ontology 1.3) with every derivable column added by the ontology's definitions (net and cumulative totals, step and cycle values, step time, power, impedance magnitude and phase) and all-NaN columns left out. Alongside it comes a metadata sidecar of BattINFO records, in the form batterydf reads: the test (run ID, start and end, status, a fault or stop as a deviation), the cell, the cycler, the channel, and the protocol as typed steps with nested loops, plus the executed aurora-unicycler protocol verbatim.

Deliberate departures from the UniteLabs review checklist are recorded in [docs/decisions/](docs/decisions/README.md).

## Using the client

```python
from omnicycler.features.client import ClientConfig, OmniCyclerClient
from omnicycler.features.types import BdfFormat, ChannelState, Sample

config = ClientConfig(hostname="cycler-pc.local", port=50052, root_certificates=ca_pem)

async with OmniCyclerClient.connect(config) as cycler:
    channels = await cycler.information.get_channels()
    sample = Sample(name="NMC-042", nominal_capacity=0.0045)  # Ah

    await cycler.channels.start_protocol(1, protocol_bytes_or_cycling_protocol, sample)

    async with cycler.measurements.monitor(1) as records:
        async for record in records:  # every record exactly once, until the run ends
            print(record.test_time, record.voltage, record.current)

    export = await cycler.measurements.export_data(1, BdfFormat.PARQUET)
    export.save("NMC-042.bdf.parquet")  # and NMC-042.bdf.metadata.json, which bdf.read() picks up
    await cycler.channels.clear_channel(1)
```

Errors arrive as the same classes the server raises (`omnicycler.features.errors`): `ChannelBusyError`, `ChannelNotClearedError`, `ChannelUnavailableError`, `NoActiveProtocolError`, `NoRunError`, `DataRangeTooLargeError`. A rejected protocol or sample raises `InvalidParameterError`, whose `parameter` names which. The client never retries; after a connection failure it raises `CommandOutcomeUnknownError`, and the caller checks the channel state.

## Implementing a brand server

A brand server follows the usual UniteLabs connector layout (its own `io/` layer in vendor vocabulary, simulation mode by default) and adds one `CyclerBackend` subclass on top of `io/`:

```python
from omnicycler.features.backend import CyclerBackend, CyclerIdentity
from omnicycler.features.server import register_standard_features


class AcmeBackend(CyclerBackend):
    async def read_identity(self) -> CyclerIdentity: ...
    async def read_channels(self) -> list[ChannelInformation]: ...
    async def read_supported_step_types(self) -> list[StepType]: ...
    async def read_statuses(self) -> list[ChannelStatus]: ...
    async def read_run_information(self, channel: int) -> RunInformation: ...
    async def read_data(self, channel: int, from_index: int, count: int) -> list[DataPoint]: ...
    async def start(self, channel: int, protocol: CyclingProtocol, run_id: str) -> None: ...  # convert with an aurora-unicycler exporter
    async def stop(self, channel: int) -> None: ...
    async def pause(self, channel: int) -> None: ...
    async def resume(self, channel: int) -> None: ...

    # optional: check() for vendor conversion checks, clear(), open()/close(), faster
    # get_data_in_time_range() or export_data()


async def create_app(config: AcmeConfig):
    app = Connector(config)
    async with AcmeBackend(config) as backend:  # connect only; never touch running tests
        register_standard_features(app, backend)
        app.register(SimulationController(...))
        app.register(AcmeProtocolService(...))  # brand extras: separate features, see decision 0001
        yield app
```

The base class owns everything that must behave identically on every brand: per-channel locking, the state machine and the clear step, run IDs, protocol checks against channel limits, time-range search, monitoring, and the BDF export with its metadata. Backends convert to BDF units, report positive current as charging, report capacities and energies cumulative since the run started, and fill quantities they do not measure with NaN. They keep the `run_id` that `start` receives with the run, so it survives a server restart, and put the cycler's fault message in `fault_reason` when a run faults.

Brand servers depend on `omnicycler-features`, which includes the BDF libraries (batterydf, polars, pyarrow) and the conformance kit's pytest.

### Conformance kit

```python
# tests/conftest.py
import pytest

from acme.cycler import create_app
from acme.cycler.config import AcmeConfig

pytest_plugins = ["omnicycler.features.testing.plugin"]


@pytest.fixture
def omnicycler_app_factory():
    return create_app


@pytest.fixture
def omnicycler_config():
    return AcmeConfig(simulation_mode=True)


# tests/test_conformance.py
from omnicycler.features.testing import ConformanceSuite


class TestConformance(ConformanceSuite):
    pass
```

The suite runs the server in-process on a free port, in simulation mode, and drives it through the typed client. Override `omnicycler_short_protocol`, `omnicycler_long_protocol`, `omnicycler_faulting_protocol` or `omnicycler_run_timeout` if the brand's simulation needs different protocols or more time. The faulting protocol must trip a safety limit; the default sets a capacity limit of 1 % of the sample's capacity.

## Reference simulator

```sh
uv run config create --app omnicycler.features.simulator:create_app
uv run connector start --app omnicycler.features.simulator:create_app --config-path config.json
```

It simulates `channel_count` channels with an equivalent-circuit cell each, running `time_scale` times faster than real time. It executes every step type except impedance spectroscopy.

## Development

```sh
uv sync --locked
uv run ruff format --check
uv run ruff check
uv run ty check
uv run lint-imports
uv run pytest
uv build
```

On Windows, run with `PYTHONUTF8=1` (or a UTF-8 console). Otherwise the CDK fails while logging an error to a cp1252 console and replaces every defined or validation error with an undefined one. The test suite reconfigures its own output, so pytest is not affected.

## Known limitations

- **batterydf 0.2.0 is not on PyPI yet.** The dependency pins it to a commit of the GitHub `main` branch, so installing `omnicycler-features` needs git and access to GitHub. Switch the pin to `batterydf~=0.2.0` once it is released.
- The metadata sidecar leaves out what only a person or a BattINFO registry can state: registry identifiers (w3id IRIs), the test kind, creators and licence. Add them to the sidecar before publishing. BattINFO's schemas are still pre-1.0 and may change with batterydf.
- Schedule-level BDF columns (`Schedule Charging Capacity` and so on) count from schedule-defined events the wire does not carry, so the export leaves them out.
- `ClearChannel` is remembered in memory only. After a server restart, an ended run shows up again and has to be cleared again.

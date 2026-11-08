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

A **protocol** is an [aurora-unicycler](https://github.com/EmpaEconversion/aurora-unicycler) JSON document, started on a **channel** together with a **sample** (name, nominal capacity and an optional identifier such as a serial number or LIMS ID) as a **run**. The sample replaces the protocol's own sample block, so one protocol file serves as a template for many cells. Every protocol must set the voltage safety limits `safety.min_voltage_V` and `safety.max_voltage_V`; a protocol without them is rejected, because a cycler's own limits are its hardware range, not the cell's.

A run is named by a **`RunId`** that the client chooses when it starts the run (the typed client uses a fresh UUID unless you give one). Every command that acts on a run, and every read of its data, names the run it expects:

- If the channel holds another run, the server refuses with `RunMismatchError`. A client therefore never stops, clears or reads a run other than the one it means, even if another client cleared the channel and started a new run in the meantime.
- If the channel holds no run (any more), the server refuses with `NoRunError`.
- Starting is idempotent. A start of the run the channel already holds succeeds without effect, so a client that does not know whether a start took effect repeats it with the same `RunId`. A `RunId` held by another channel is refused with `RunIdInUseError`.

`ChannelStatus.RunId` reports the run each channel holds: empty when idle, always set while a run is active or ended.

| Feature | Endpoints |
| --- | --- |
| `CyclerInformationProvider` | `Manufacturer`, `Model`, `SerialNumber`, `FirmwareVersion`, `Channels`, `SupportedStepTypes`, `UnicyclerVersion` |
| `ChannelController` | `ChannelStatuses` (observable), `GetChannelStatus`, `GetRunInformation`, `StartProtocol(Channel, RunId, Protocol, Sample)`, `ValidateProtocol`, `StopProtocol(Channel, RunId)`, `PauseProtocol(Channel, RunId)`, `ResumeProtocol(Channel, RunId)`, `ClearChannel(Channel, RunId)` |
| `MeasurementProvider` | All with `(Channel, RunId, …)`: `GetRecordCount`, `GetData`, `GetDataTable` (Arrow, for bulk reads), `GetDataInTimeRange`, `MonitorChannel` (observable), `ExportData` (observable) |

Channel lifecycle:

```text
idle ──StartProtocol──▶ running ◀──ResumeProtocol── paused
 ▲                       │   └──PauseProtocol──────▶ │
 │                       ▼                           ▼
 └──ClearChannel── finished / stopped / faulted ◀── StopProtocol
```

An ended run stays on its channel, with its data, until `ClearChannel` is called. `StartProtocol` on such a channel raises `ChannelNotClearedError`, so data is never dropped by accident. `ClearChannel` clears the channel completely: it deletes the run with all its data from the server, on every brand and for good, also across restarts (the vendor's own files are untouched), and the channel is idle without a run. Exporting the data first is the job of whoever clears: the orchestrator or the gateway. A faulted channel reports the cycler's reason in `ChannelStatus.FaultReason`.

Data records are numbered from zero without gaps by `RecordIndex`, and a record keeps its index for as long as the run exists, so a client may cache records by index.

Data uses Battery Data Format (BDF) quantities throughout: V, A (positive = charging), Ah, Wh, s, °C, Pa, Ω, Hz. A quantity a channel does not measure is NaN; `ChannelInformation.MeasuredQuantities` lists the optional ones a channel does measure. `ExportData` returns a BDF file (CSV or Parquet, BDF ontology 1.3) with every derivable column added by the ontology's definitions (net and cumulative totals, step and cycle values, step time, power, impedance magnitude and phase) and all-NaN columns left out. Alongside it comes a metadata sidecar of BattINFO records, in the form batterydf reads: the test (run ID, start and end, status, a fault or stop as a deviation), the cell, the cycler, the channel, and the protocol as typed steps with nested loops, plus the executed aurora-unicycler protocol verbatim.

Deliberate departures from the UniteLabs review checklist are recorded in [docs/decisions/](docs/decisions/README.md).

## Using the client

```python
from omnicycler.features.client import ClientConfig, OmniCyclerClient
from omnicycler.features.types import BdfFormat, ChannelState, Sample

config = ClientConfig(hostname="cycler-pc.local", port=50052, root_certificates=ca_pem)

async with OmniCyclerClient.connect(config) as cycler:
    channels = await cycler.information.get_channels()
    sample = Sample(name="NMC-042", nominal_capacity=0.0045, identifier="LIMS-1187")  # Ah

    run_id = await cycler.channels.start_protocol(1, protocol_bytes_or_cycling_protocol, sample)

    async with cycler.measurements.monitor(1, run_id) as records:
        async for record in records:  # every record exactly once, until the run ends
            print(record.test_time, record.voltage, record.current)

    export = await cycler.measurements.export_data(1, run_id, BdfFormat.PARQUET)
    export.save("NMC-042.bdf.parquet")  # and NMC-042.bdf.metadata.json, which bdf.read() picks up
    await cycler.channels.clear_channel(1, run_id)  # deletes the run and its data
```

Errors arrive as the same classes the server raises (`omnicycler.features.errors`): `ChannelBusyError`, `ChannelNotClearedError`, `ChannelUnavailableError`, `NoActiveProtocolError`, `NoRunError`, `RunMismatchError`, `RunIdInUseError`, `DataRangeTooLargeError`. A rejected protocol or sample raises `InvalidParameterError`, whose `parameter` names which. The client never retries. After a connection failure it raises `CommandOutcomeUnknownError`; repeat a start with the same `run_id` (it has no effect if the run started), and repeat a stop, pause, resume or clear as is (they are idempotent and name their run).

## Implementing a brand server

A brand server follows the usual UniteLabs connector layout (its own `io/` layer in vendor vocabulary, simulation mode by default) and adds one `CyclerBackend` subclass on top of `io/`:

```python
from omnicycler.features.backend import CyclerBackend, CyclerIdentity
from omnicycler.features.server import register_standard_features


class AcmeBackend(CyclerBackend):
    async def read_identity(self) -> CyclerIdentity: ...
    async def read_channels(self) -> list[ChannelInformation]: ...
    async def read_supported_step_types(self) -> list[StepType]: ...
    async def read_statuses(self) -> list[ChannelStatus]: ...  # with the run_id each channel holds
    async def read_run_information(self, channel: int) -> RunInformation: ...
    async def read_data(self, channel: int, run_id: str, from_index: int, count: int) -> list[DataPoint]: ...
    async def start(self, channel: int, protocol: CyclingProtocol, sample: Sample, run_id: str) -> None: ...  # convert with an aurora-unicycler exporter
    async def stop(self, channel: int) -> None: ...
    async def pause(self, channel: int) -> None: ...
    async def resume(self, channel: int) -> None: ...
    async def clear(self, channel: int) -> None: ...  # delete the run and its data for good

    # optional: check() for vendor conversion checks, read_status() for one cheap channel read,
    # open()/close(), faster get_data_in_time_range() or export_data()


async def create_app(config: AcmeConfig):
    app = Connector(config)
    async with AcmeBackend(config) as backend:  # connect only; never touch running tests
        register_standard_features(app, backend)
        app.register(SimulationController(...))
        app.register(AcmeProtocolService(...))  # brand extras: separate features, see decision 0001
        yield app
```

The base class owns everything that must behave identically on every brand: per-channel locking, the state machine and the clear step, the run identity checks (`RunMismatchError`, `NoRunError`, idempotent starts, `RunIdInUseError`), protocol checks against channel limits, time-range search, monitoring, and the BDF export with its metadata. A brand's own start of a native protocol goes through `CyclerBackend.start_run`, so it gets the same checks.

Backends:

- convert to BDF units, report positive current as charging, report capacities and energies cumulative since the run started, and fill quantities they do not measure with NaN
- keep the `run_id` that `start` receives with the run, so it survives a server restart, and report it in every status of the channel until `clear`
- read only the named run's records in `read_data`, and give each record an index that never changes while the run exists
- delete the run and its data in `clear`
- put the cycler's fault message in `fault_reason` when a run faults

Brand servers depend on `omnicycler-features`, which includes the BDF libraries (batterydf, polars, pyarrow) and the conformance kit's pytest. Brand clients for brand features build on the public helpers in `omnicycler.features.client.base` (`translate_errors`, `from_wire`, `to_wire`, `require_feature`, …), so every OmniCycler client decodes the wire and translates errors the same way.

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

The suite runs the server in-process on a free port, in simulation mode, and drives it through the typed client. Besides the lifecycle, the run identity rules and the data paths, it checks the conversions every brand must get right: the current of the short protocol's charge and discharge steps has the right sign and magnitude in ampere, and the capacities and energies never decrease and match the integrals of the measured current and power.

Override `omnicycler_short_protocol`, `omnicycler_long_protocol`, `omnicycler_faulting_protocol` or `omnicycler_run_timeout` if the brand's simulation needs different protocols or more time. An overridden short protocol must keep the default's shape: a constant-current charge as step 1 and a discharge as step 3, both at `testing.protocols.CHARGE_RATE`, each recording at least three records. The faulting protocol must trip a safety limit; the default sets a capacity limit of 1 % of the sample's capacity. The default protocols use voltage limits of 2.5 V to 4.3 V.

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
- The reference simulator keeps its runs in memory: a restart loses them.
- The servers have no access control: any client that reaches a server can start, stop and clear runs. The `RunId` checks guard against acting on the wrong run, not against unauthorised clients. Use TLS, and keep the servers on a trusted network.

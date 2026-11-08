# Changelog

All notable changes to this package. The package follows semantic versioning; each feature's `version` moves independently (see the best-practices guide).

## Unreleased

- The conformance kit restarts the server: an ended run keeps its `RunId`, run information and records, a cleared channel stays idle, and an active run keeps its `RunId` and earlier records. New fixtures `omnicycler_restart` and `omnicycler_keeps_runs`.
- `register_standard_features` switches the console to UTF-8 (`omnicycler.features.console.use_utf8_console`), so servers no longer need `PYTHONUTF8=1` on Windows.

## 0.1.0 – 2026-11-05

First version.

- Standard features `CyclerInformationProvider`, `ChannelController` and `MeasurementProvider`, all `v1.0`, originator `se.uu.interphaseslab`, category `cycling`.
- `CyclerBackend` base class for brand servers, with per-channel locking, the channel state machine including the explicit `ClearChannel` step, protocol checks against channel limits, time-range search, monitoring and BDF export.
- aurora-unicycler 0.5 protocols, validated with aurora-unicycler, with the `Sample` parameter merged in.
- Battery Data Format export (CSV, Parquet) through batterydf 0.2 (BDF ontology 1.3, pinned to a GitHub commit until it is on PyPI), with every derivable column calculated by the ontology's definitions and without all-NaN columns, and a BattINFO metadata sidecar describing the test, cell, cycler, channel and protocol.
- Runs named by a client-chosen `RunId`: `StartProtocol` takes it and is idempotent for it, `ChannelStatus.RunId` reports each channel's run, and every control command and data read names the run it expects (`RunMismatchError`, `RunIdInUseError`). Record indices never change while a run exists.
- `ClearChannel` deletes the run with all its data on every brand; `CyclerBackend.clear` is a required hook.
- Protocols must set voltage safety limits.
- The cycler's reason for a fault (`ChannelStatus.FaultReason`), and an optional `Sample.Identifier` written to the export's metadata.
- Typed async client `OmniCyclerClient`, including a gap-free `monitor()` stream; its helpers in `client.base` are public for brand clients.
- Conformance kit (`omnicycler.features.testing`), which also checks current sign and magnitude and the capacity and energy totals, and reference simulator (`omnicycler.features.simulator`).
- BDF export built page by page as a table, so long runs are never held as `DataPoint` objects.
- Decision records for the waived UniteLabs checklist rules in `docs/decisions/`.

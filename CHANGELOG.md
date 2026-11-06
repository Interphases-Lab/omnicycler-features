# Changelog

All notable changes to this package. The package follows semantic versioning; each feature's `version` moves independently (see the best-practices guide).

## 0.1.0 – 2026-11-05

First version.

- Standard features `CyclerInformationProvider`, `ChannelController` and `MeasurementProvider`, all `v1.0`, originator `se.uu.interphaseslab`, category `cycling`.
- `CyclerBackend` base class for brand servers, with per-channel locking, the channel state machine including the explicit `ClearChannel` step, protocol checks against channel limits, time-range search, monitoring and BDF export.
- aurora-unicycler 0.5 protocols, validated with aurora-unicycler, with the `Sample` parameter merged in.
- Battery Data Format export (CSV, Parquet) through batterydf 0.2 (BDF ontology 1.3, pinned to a GitHub commit until it is on PyPI), with every derivable column calculated by the ontology's definitions and without all-NaN columns, and a BattINFO metadata sidecar describing the test, cell, cycler, channel and protocol.
- A run ID for every run (`RunInformation.RunId`) and the cycler's reason for a fault (`ChannelStatus.FaultReason`).
- Typed async client `OmniCyclerClient`, including a gap-free `monitor()` stream.
- Conformance kit (`omnicycler.features.testing`) and reference simulator (`omnicycler.features.simulator`).
- Decision records for the waived UniteLabs checklist rules in `docs/decisions/`.

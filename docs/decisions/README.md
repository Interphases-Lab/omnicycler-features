# Waivers of the CDK best-practice rules

`omnicycler-features` is a standard-feature package, not a single device connector. The SiLA endpoints are defined once here, and several brand servers (BioLogic, Arbin, Neware, …) can implement them through a Python backend interface. 

A reviewer who finds code that breaks a rule listed here should check the record's **Scope**. A waiver covers only what its record names. Everything else in the guide still applies.

| # | Record | Rule waived |
| --- | --- | --- |
| 0001 | [Brand extensions are separate features](0001-brand-extensions-are-separate-features.md) | `proj-extend-standard-feature-in-place` |
| 0002 | [Standard features use BDF units](0002-standard-features-use-bdf-units.md) | `dt-propagate-device-native-units` |
| 0003 | [Per-channel collections in properties](0003-per-channel-collections-in-properties.md) | `op-property-per-value` |
| 0004 | [Per-channel commands run in PARALLEL mode](0004-per-channel-commands-run-in-parallel-mode.md) | `op-execution-mode-by-repeatability` |
| 0005 | [Backend interface instead of an io layer](0005-backend-interface-instead-of-io-layer.md) | Mandatory `io/` layer, package layout |
| 0006 | [One typed client in the features package](0006-typed-client-in-features-package.md) | Client rules: package layout, rules 3 and 4 |

## Looks like a waiver, but is not

- **Protocol as JSON bytes.** `StartProtocol` takes an aurora-unicycler protocol as bytes with `ContentType("application/json")`. The guide allows serialised payloads as bytes with a ContentType (`dt-no-raw-xml-json-string`). There is no `Schema` constraint: unicycler validates the protocol in the command's validator, and a generated schema would have to follow every unicycler release. The `UnicyclerVersion` property tells clients which version the server validates against.
- **`ClearChannel` before a new `StartProtocol`.** `op-expose-stateless-api` forbids splitting one action's parameters across calls. `ClearChannel` is a separate, explicit state change: it acknowledges that a finished run's data may be dropped from the SiLA view. It meets `op-recovery-through-normal-commands`.
- **`MonitorChannel` as an observable command.** It has a natural end (the run ends) and can be resumed after a reconnect through `FromIndex`. It is not a start/stop action without an end.

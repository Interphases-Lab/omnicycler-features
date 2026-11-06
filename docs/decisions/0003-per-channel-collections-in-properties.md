# 0003 Per-channel collections in properties

- **Status:** Accepted, 2026-11-05
- **Rule waived:** `op-property-per-value`

## Context

The rule forbids catalogue structures, such as a `DeviceInfo` property that bundles unrelated values. A cycler has many channels, and SiLA properties cannot take parameters (`op-no-selector-parameter`). The guide's pattern for repeated modules, one feature instance per module, does not scale to cyclers with dozens or hundreds of channels.

## Decision

- `CyclerInformationProvider.Channels` returns `list[ChannelInformation]`: one entry per channel with its static properties, such as voltage and current ranges.
- `ChannelController.ChannelStatuses` is an observable property streaming `list[ChannelStatus]`, one entry per channel.
- Single-channel reads are unobservable commands with a `Channel` parameter, for example `GetChannelStatus(Channel)`.
- Device-wide values remain individual properties: `Manufacturer`, `Model`, `SerialNumber`, `FirmwareVersion`, `UnicyclerVersion`, `SupportedStepTypes`.

## Guardrail

Each element describes one channel and nothing else. `ChannelStatus` holds only status values and `ChannelInformation` only static channel properties. Adding a device-wide value to either one is not covered by this waiver.

## Consequences

- One subscription gives a dashboard the state of every channel.
- Clients that need one channel use the command, not the list.

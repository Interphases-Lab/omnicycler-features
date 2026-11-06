# 0001 Brand extensions are separate features

- **Status:** Accepted, 2026-11-05
- **Rule waived:** `proj-extend-standard-feature-in-place`

## Context

The guide says to add device-specific endpoints to the standard feature class instead of creating a new feature. Here, every brand server and the one typed client share the standard features. A fully qualified feature identifier such as `se.uu.interphaseslab/cycling/ChannelController/v1` must mean exactly one definition. If each brand added endpoints in place, the same identifier would describe a different interface on each server. That would break the shared client, the conformance kit, and SiLA's meaning of a feature identifier.

## Decision

- Standard features in `omnicycler-features` are concrete and closed. Brand servers register them unchanged through `register_standard_features(app, backend)` and never subclass or extend them.
- Brand-specific endpoints go in brand features. They use originator `se.uu.interphaseslab` and a name that states both the brand and the concern, for example `AcmeProtocolService`.
- Brand features are limited to:
  - read-only information
  - settings
  - operations that the unicycler protocol model cannot express, for example starting a vendor-native protocol file
- One feature per concern (`feat-one-feature-per-concern`) still applies to brand features. There is no catch-all such as `AcmeExtras`.

## Consequences

- Every server exposes identical definitions for the standard features. The conformance kit can therefore require exact equality.
- Brand repos can ship a small client extension for their own features on top of `OmniCyclerClient` (see 0006).
- A run started through a brand feature still appears in the standard features. `RunInformation` carries `Protocol` as bytes together with a `ProtocolFormat` (`AURORA_UNICYCLER` or `NATIVE`).

## Scope

This covers the standard features of this package only. Within a brand server's own features, `proj-extend-standard-feature-in-place` applies as written.

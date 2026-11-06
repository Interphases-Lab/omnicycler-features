# 0005 Backend interface instead of an io layer

- **Status:** Accepted, 2026-11-05
- **Rules waived:** the mandatory `io/` protocol layer (guide: "Scope and fixed decisions", "Package layout"). `proj-features-import-from-io-not-reverse` and `proj-io-has-no-sila-knowledge` are adapted.

## Context

This package talks to no device, so it has no vendor protocol for an `io/` layer to hold. Its features must work on top of any brand's device code.

## Decision

- The features here depend on the `CyclerBackend` abstract base class in `backend.py`. The ABC has no SiLA endpoint knowledge, and it may import `types.py`, `units.py` and `errors.py`.
- Brand repos keep the guide's full layout: an `io/` layer in vendor vocabulary, plus a backend adapter that implements `CyclerBackend` on top of it. `CyclerBackend` therefore plays the role of the `io/` facade, and the one-way dependency features → backend → io still holds.
- Logic that does not depend on the device lives in plain modules: the sample merge and unicycler validation in `protocol.py`, BDF table building in `bdf_export.py`. Feature methods stay one or two lines; `proj-no-helpers-in-feature` applies unchanged.
- The reference simulator simulates at backend level, below the features, so simulated runs always go through the real features.
- Brand servers must still simulate at their own protocol level (`test-simulate-at-protocol-level-not-feature-level`). The conformance kit only proves something if they do.

## Import contracts (import-linter)

- `backend`, `types`, `units`, `errors`, `protocol`, `bdf_export` and `simulation` must not import any feature module, the client, the simulator or the test kit.
- `types`, `units` and `errors` must not import `backend`, `protocol` or `bdf_export`.
- `client` must not import feature modules, `backend`, `parameters`, `bdf_export`, `simulator`, `simulation` or the test kit.

The contracts live in `pyproject.toml` and run with `uv run lint-imports`.

## Consequences

- A brand server implements one Python interface instead of SiLA endpoints.
- Brand servers cannot change the wire definitions of the standard features, which is what 0001 needs.

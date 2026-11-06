# 0006 One typed client in the features package

- **Status:** Accepted, 2026-11-05
- **Rules waived:** the guide's package layout (each server distribution ships its own `client/`) and typed-client rules 3 and 4. These are house rules without a UniteLabs ID.

## Context

The guide pairs one server with one client in one distribution. OmniCycler has one standard and several brand servers. Each brand repeating the same client would drift, and an orchestrator would need one client per brand.

## Decision

- `OmniCyclerClient` lives in `omnicycler.features.client` and works against any brand server. It follows all the guide's client rules otherwise.
- **Rule 3 (verify the server at connect time).** The client requires every standard feature at the expected major version. Brand features are never required by `OmniCyclerClient`.
- **Rule 4 (one client class per server feature).** This holds within this package: one module per standard feature, mirroring the feature modules.
- Brand repos do not ship clients for the standard features. They may ship a client extension for their own features (0001) that builds on `OmniCyclerClient`.
- Each brand server runs the client's contract checks through the conformance kit (`omnicycler.features.testing`) against its simulation mode.

## Consequences

- The orchestrator uses one client and one set of types for all cyclers.
- The guide's core client rule is kept: the client imports the same `types.py` and `errors.py` that the features use.
- A change to the standard interface is released once, here, and brand servers pick it up by updating their `omnicycler-features` pin.

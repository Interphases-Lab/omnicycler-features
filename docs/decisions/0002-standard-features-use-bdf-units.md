# 0002 Standard features use BDF units

- **Status:** Accepted, 2026-11-05
- **Rule waived:** `dt-propagate-device-native-units`

## Context

The rule says to expose the unit the device reports, with no silent conversions. A standard interface for many brands has to fix its units, or no client could read values from different cyclers the same way. Completed measurements are stored in the Battery Data Format (BDF), so BDF's units are the natural choice.

## Decision

- Every quantity in the standard features uses the unit that BDF defines for it: V, A, Ah, Wh, s, degC, Pa, Ohm, Hz (and W and deg for the derived power and impedance phase in exports).
- Positive current charges the cell, as in BDF. Capacities and energies follow the BDF definitions exactly.
- Each backend converts from device units. The conversion is explicit: it is documented in the backend and checked by the conformance kit, which runs a charge and a discharge at a known C-rate and checks the sign and magnitude of the current in ampere, and that the capacities and energies match the integrals of current and power. That keeps the rule's real intent, no *silent* conversion.
- Backends report only measured quantities. BDF columns that follow from others (net and cumulative totals, step and cycle values, step time and record index, power, impedance magnitude and phase) are calculated in `bdf_export.py` by the BDF ontology's own definitions, once for every brand, so the same run gives the same values whichever cycler produced it. batterydf checks the derived columns against those definitions on every export.
- A quantity a channel does not measure is NaN on the wire, since SiLA has no optional fields, and its column is left out of the BDF file.
- Unicycler protocol content stays in unicycler's own units (mA, mAh), on the wire and in the protocol record of the export's BattINFO metadata sidecar, where a C-rate is written as `1/h`. The only conversion in this package is `Sample.NominalCapacity` from Ah to unicycler's `capacity_mAh`, done in `protocol.py`.

## Consequences

- Live data, paged data and BDF exports use identical quantities, so the BDF export is a direct mapping.
- Clients never see device units.
- Brand authors must convert carefully: mA to A, mAh to Ah, and each vendor's sign convention to positive-is-charge.

## Scope

This covers the standard features only. Brand features (0001) report device-native units, as the rule says.

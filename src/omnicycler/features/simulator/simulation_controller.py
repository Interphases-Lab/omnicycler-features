import typing

from unitelabs.cdk.features.core.simulation_controller import SimulationControllerBase, StartRealModeFailed

# The CDK's abstract bases type their endpoints through the `sila` decorators, so ty sees each plain override as
# replacing a decorated attribute. The CDK moves the decorator onto the override at registration; the ignores below
# only silence that typing gap of the UniteLabs base-class pattern.


class SimulationController(SimulationControllerBase):
    """The simulator runs in simulation mode only; it has no real mode to switch to."""

    @typing.override
    async def start_simulation_mode(self) -> None:  # ty: ignore[invalid-attribute-override]
        """Set the SiLA Server to run in Simulation Mode. The simulator always does, so this has no effect."""

    @typing.override
    async def start_real_mode(self) -> None:  # ty: ignore[invalid-attribute-override]
        """
        Set the SiLA Server to run in Real Mode. The simulator has no real mode, so this always fails.

        Raises:
          StartRealModeFailed: Always, because the simulator has no hardware.
        """
        msg = "The OmniCycler simulator has no real mode; connect a brand server for real hardware."
        raise StartRealModeFailed(msg)

    @typing.override
    async def simulation_mode(self) -> bool:  # ty: ignore[invalid-attribute-override]
        """Whether or not the SiLA Server is in Simulation Mode."""
        return True

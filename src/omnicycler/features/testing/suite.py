"""The OmniCycler conformance suite: what every brand server must pass in simulation mode."""

import asyncio
import collections.abc
import dataclasses
import io
import itertools
import json
import math
import pathlib
import statistics
import uuid

import pytest
from sila.client import ClientConfig

from ..backend import ACTIVE_STATES, ENDED_STATES
from ..client import InvalidParameterError, OmniCyclerClient
from ..data_table import COLUMNS, INTEGER_FIELDS, table_column
from ..errors import (
    ChannelBusyError,
    ChannelNotClearedError,
    NoActiveProtocolError,
    NoRunError,
    RunIdInUseError,
    RunMismatchError,
)
from ..types import (
    BdfFormat,
    ChannelInformation,
    ChannelState,
    ChannelStatus,
    DataPoint,
    MeasuredQuantity,
    ProtocolFormat,
    Sample,
    StepType,
)
from .protocols import CHARGE_RATE, SAFETY, sample_for

OPTIONAL_FIELDS = {quantity: quantity.name.lower() for quantity in MeasuredQuantity}
"""The `DataPoint` field of each optional measured quantity."""

STEP_EXAMPLES: dict[StepType, dict[str, object]] = {
    StepType.OPEN_CIRCUIT_VOLTAGE: {"step": "open_circuit_voltage", "until_time_s": 10},
    StepType.CONSTANT_CURRENT: {"step": "constant_current", "rate_C": 0.1, "until_time_s": 10},
    StepType.CONSTANT_VOLTAGE: {"step": "constant_voltage", "voltage_V": 3.7, "until_time_s": 10},
    StepType.IMPEDANCE_SPECTROSCOPY: {
        "step": "impedance_spectroscopy",
        "amplitude_V": 0.01,
        "start_frequency_Hz": 1e4,
        "end_frequency_Hz": 1,
    },
    StepType.VOLTAGE_SCAN: {
        "step": "voltage_scan",
        "start_voltage_V": 3.6,
        "end_voltage_V": 3.8,
        "scan_rate_mV_per_s": 1,
    },
}

CURRENT_TOLERANCE = 0.02
"""How far the measured current of a constant-current step may lie from its setpoint, relative."""

INTEGRAL_TOLERANCE = 0.03
"""How far a capacity or energy increment may lie from the integral of the measured current or power, relative."""

FOREIGN_RUN = "conformance-not-this-run"
"""A RunId no test ever starts."""


Restart = collections.abc.Callable[[], collections.abc.Awaitable[ClientConfig]]
"""Stops the server under test, starts it again, and returns the config to reach it."""


def _run_id() -> str:
    return f"conformance-{uuid.uuid4()}"


def _protocol(*method: collections.abc.Mapping[str, object]) -> bytes:
    return json.dumps({"record": {"time_s": 10}, "safety": SAFETY, "method": list(method)}).encode()


class ConformanceSuite:
    """
    Tests every OmniCycler server must pass in simulation mode.

    Use it in a brand repo by subclassing it in a test module, with the
    fixtures of `omnicycler.features.testing.plugin` enabled:

        class TestConformance(ConformanceSuite):
            pass
    """

    pytestmark = pytest.mark.asyncio

    # Identity and channels.

    async def test_identity_is_reported(self, omnicycler_client: OmniCyclerClient) -> None:
        information = omnicycler_client.information
        values = [
            await information.get_manufacturer(),
            await information.get_model(),
            await information.get_serial_number(),
            await information.get_firmware_version(),
            await information.get_unicycler_version(),
        ]
        assert all(value.strip() for value in values)

    async def test_channels_are_consistent(self, omnicycler_client: OmniCyclerClient) -> None:
        channels = await omnicycler_client.information.get_channels()
        numbers = [channel.channel for channel in channels]
        assert numbers
        assert len(set(numbers)) == len(numbers)
        for channel in channels:
            assert channel.minimal_voltage < channel.maximal_voltage
            assert channel.maximal_current > 0
            assert len(set(channel.measured_quantities)) == len(channel.measured_quantities)

    async def test_supported_step_types_are_listed(self, omnicycler_client: OmniCyclerClient) -> None:
        step_types = await omnicycler_client.information.get_supported_step_types()
        assert step_types
        assert len(set(step_types)) == len(step_types)

    async def test_status_stream_reports_every_channel(self, omnicycler_client: OmniCyclerClient) -> None:
        numbers = {channel.channel for channel in await omnicycler_client.information.get_channels()}
        snapshot = await self._statuses(omnicycler_client)
        assert sorted(status.channel for status in snapshot) == sorted(numbers)

    async def test_statuses_name_the_run_exactly_when_one_is_held(
        self, omnicycler_client: OmniCyclerClient, omnicycler_long_protocol: bytes
    ) -> None:
        channel = await self._idle_channel(omnicycler_client)
        await omnicycler_client.channels.start_protocol(channel.channel, omnicycler_long_protocol, sample_for(channel))
        for status in await self._statuses(omnicycler_client):
            if status.state is ChannelState.IDLE:
                assert status.run_id == "", status
                assert status.record_count == 0, status
            elif status.state in ACTIVE_STATES | ENDED_STATES:
                assert status.run_id, status

    # Validation.

    async def test_unknown_channel_is_rejected(
        self, omnicycler_client: OmniCyclerClient, omnicycler_short_protocol: bytes
    ) -> None:
        channels = await omnicycler_client.information.get_channels()
        unknown = max(channel.channel for channel in channels) + 1
        with pytest.raises(InvalidParameterError) as error:
            await omnicycler_client.channels.start_protocol(unknown, omnicycler_short_protocol, sample_for(channels[0]))
        assert error.value.parameter == "Channel"

    async def test_invalid_protocol_is_rejected(self, omnicycler_client: OmniCyclerClient) -> None:
        channel = await self._idle_channel(omnicycler_client)
        with pytest.raises(InvalidParameterError) as error:
            await omnicycler_client.channels.start_protocol(channel.channel, b"not json", sample_for(channel))
        assert error.value.parameter == "Protocol"
        assert (await omnicycler_client.channels.get_channel_status(channel.channel)).state is ChannelState.IDLE

    async def test_protocol_without_voltage_limits_is_rejected(self, omnicycler_client: OmniCyclerClient) -> None:
        channel = await self._idle_channel(omnicycler_client)
        for safety in ({}, {"max_voltage_V": SAFETY["max_voltage_V"]}, {"min_voltage_V": SAFETY["min_voltage_V"]}):
            document = {
                "record": {"time_s": 10},
                "safety": safety,
                "method": [STEP_EXAMPLES[StepType.OPEN_CIRCUIT_VOLTAGE]],
            }
            with pytest.raises(InvalidParameterError) as error:
                await omnicycler_client.channels.validate_protocol(
                    channel.channel, json.dumps(document).encode(), sample_for(channel)
                )
            assert error.value.parameter == "Protocol"
            assert "voltage" in str(error.value)

    async def test_placeholder_sample_name_is_rejected(
        self, omnicycler_client: OmniCyclerClient, omnicycler_short_protocol: bytes
    ) -> None:
        channel = await self._idle_channel(omnicycler_client)
        sample = Sample(name="$NAME", nominal_capacity=sample_for(channel).nominal_capacity)
        with pytest.raises(InvalidParameterError) as error:
            await omnicycler_client.channels.start_protocol(channel.channel, omnicycler_short_protocol, sample)
        assert error.value.parameter == "Sample"

    async def test_unsupported_step_type_is_rejected(self, omnicycler_client: OmniCyclerClient) -> None:
        supported = set(await omnicycler_client.information.get_supported_step_types())
        unsupported = [step_type for step_type in StepType if step_type not in supported]
        if not unsupported:
            pytest.skip("The server supports every step type.")
        channel = await self._idle_channel(omnicycler_client)
        with pytest.raises(InvalidParameterError) as error:
            await omnicycler_client.channels.validate_protocol(
                channel.channel, _protocol(STEP_EXAMPLES[unsupported[0]]), sample_for(channel)
            )
        assert error.value.parameter == "Protocol"

    async def test_current_above_channel_limit_is_rejected(self, omnicycler_client: OmniCyclerClient) -> None:
        channel = await self._idle_channel(omnicycler_client)
        step = {"step": "constant_current", "current_mA": channel.maximal_current * 2000, "until_time_s": 10}
        with pytest.raises(InvalidParameterError) as error:
            await omnicycler_client.channels.validate_protocol(channel.channel, _protocol(step), sample_for(channel))
        assert error.value.parameter == "Protocol"

    async def test_validate_protocol_does_not_start(
        self, omnicycler_client: OmniCyclerClient, omnicycler_short_protocol: bytes
    ) -> None:
        channel = await self._idle_channel(omnicycler_client)
        warnings = await omnicycler_client.channels.validate_protocol(
            channel.channel, omnicycler_short_protocol, sample_for(channel)
        )
        assert all(isinstance(warning, str) for warning in warnings)
        assert (await omnicycler_client.channels.get_channel_status(channel.channel)).state is ChannelState.IDLE

    # Channel lifecycle.

    async def test_idle_channel_holds_no_run(self, omnicycler_client: OmniCyclerClient) -> None:
        channel = await self._idle_channel(omnicycler_client)
        assert (await omnicycler_client.channels.get_channel_status(channel.channel)).run_id == ""
        with pytest.raises(NoRunError):
            await omnicycler_client.channels.get_run_information(channel.channel)
        with pytest.raises(NoRunError):
            await omnicycler_client.measurements.get_record_count(channel.channel, FOREIGN_RUN)
        with pytest.raises(NoRunError):
            await omnicycler_client.measurements.get_data(channel.channel, FOREIGN_RUN, 0, 10)

    async def test_protocol_runs_to_completion(
        self, omnicycler_client: OmniCyclerClient, omnicycler_short_protocol: bytes, omnicycler_run_timeout: float
    ) -> None:
        channel = await self._idle_channel(omnicycler_client)
        sample = sample_for(channel)
        run_id = _run_id()
        started = await omnicycler_client.channels.start_protocol(
            channel.channel, omnicycler_short_protocol, sample, run_id=run_id
        )
        assert started == run_id

        status = await omnicycler_client.channels.wait_for_state(
            channel.channel, ENDED_STATES, timeout=omnicycler_run_timeout
        )
        assert status.state is ChannelState.FINISHED
        assert status.run_id == run_id
        assert status.fault_reason == ""

        run = await omnicycler_client.channels.get_run_information(channel.channel)
        assert run.run_id == run_id
        assert run.sample.name == sample.name
        assert run.sample.identifier == sample.identifier
        assert math.isclose(run.sample.nominal_capacity, sample.nominal_capacity, rel_tol=1e-9)
        assert run.protocol_format is ProtocolFormat.AURORA_UNICYCLER
        assert json.loads(run.protocol)["sample"]["name"] == sample.name

        points = await omnicycler_client.measurements.get_all_data(channel.channel, run_id)
        assert points
        assert [point.record_index for point in points] == list(range(len(points)))
        assert len(points) == await omnicycler_client.measurements.get_record_count(channel.channel, run_id)
        assert all(later.test_time >= earlier.test_time for earlier, later in itertools.pairwise(points))

    async def test_start_is_idempotent(
        self, omnicycler_client: OmniCyclerClient, omnicycler_long_protocol: bytes
    ) -> None:
        channels = omnicycler_client.channels
        channel = await self._idle_channel(omnicycler_client)
        run_id = await channels.start_protocol(channel.channel, omnicycler_long_protocol, sample_for(channel))
        first = await channels.get_run_information(channel.channel)

        assert (
            await channels.start_protocol(channel.channel, omnicycler_long_protocol, sample_for(channel), run_id=run_id)
            == run_id
        )
        again = await channels.get_run_information(channel.channel)
        assert again.run_id == run_id
        assert again.start_time == first.start_time

    async def test_run_id_in_use_is_rejected(
        self, omnicycler_client: OmniCyclerClient, omnicycler_long_protocol: bytes
    ) -> None:
        idle = await self._idle_channels(omnicycler_client)
        if len(idle) < 2:
            pytest.skip("Needs two idle channels.")
        first, second = idle[:2]
        run_id = await omnicycler_client.channels.start_protocol(
            first.channel, omnicycler_long_protocol, sample_for(first)
        )
        with pytest.raises(RunIdInUseError):
            await omnicycler_client.channels.start_protocol(
                second.channel, omnicycler_long_protocol, sample_for(second), run_id=run_id
            )
        assert (await omnicycler_client.channels.get_channel_status(second.channel)).state is ChannelState.IDLE

    async def test_another_run_is_never_acted_on(
        self, omnicycler_client: OmniCyclerClient, omnicycler_long_protocol: bytes
    ) -> None:
        channels, measurements = omnicycler_client.channels, omnicycler_client.measurements
        channel = await self._idle_channel(omnicycler_client)
        run_id = await channels.start_protocol(channel.channel, omnicycler_long_protocol, sample_for(channel))

        attempts: list[collections.abc.Callable[[], collections.abc.Awaitable[object]]] = [
            lambda: channels.stop_protocol(channel.channel, FOREIGN_RUN),
            lambda: channels.pause_protocol(channel.channel, FOREIGN_RUN),
            lambda: channels.resume_protocol(channel.channel, FOREIGN_RUN),
            lambda: channels.clear_channel(channel.channel, FOREIGN_RUN),
            lambda: measurements.get_record_count(channel.channel, FOREIGN_RUN),
            lambda: measurements.get_data(channel.channel, FOREIGN_RUN, 0, 10),
            lambda: measurements.get_data_in_time_range(channel.channel, FOREIGN_RUN, 0, 10),
            lambda: measurements.export_data(channel.channel, FOREIGN_RUN, BdfFormat.CSV),
            lambda: measurements.monitor_channel(channel.channel, FOREIGN_RUN, 0, 1),
        ]
        for attempt in attempts:
            with pytest.raises(RunMismatchError):
                await attempt()

        status = await channels.get_channel_status(channel.channel)
        assert status.state is ChannelState.RUNNING
        assert status.run_id == run_id

    async def test_ended_run_must_be_cleared(
        self, omnicycler_client: OmniCyclerClient, omnicycler_short_protocol: bytes, omnicycler_run_timeout: float
    ) -> None:
        channels = omnicycler_client.channels
        channel, run_id = await self._finished_channel(
            omnicycler_client, omnicycler_short_protocol, omnicycler_run_timeout
        )

        with pytest.raises(ChannelNotClearedError):
            await channels.start_protocol(channel.channel, omnicycler_short_protocol, sample_for(channel))

        await channels.clear_channel(channel.channel, run_id)
        await channels.clear_channel(channel.channel, run_id)
        await channels.start_protocol(channel.channel, omnicycler_short_protocol, sample_for(channel))
        assert (await channels.get_run_information(channel.channel)).run_id != run_id

    async def test_cleared_run_is_gone(
        self, omnicycler_client: OmniCyclerClient, omnicycler_short_protocol: bytes, omnicycler_run_timeout: float
    ) -> None:
        channels, measurements = omnicycler_client.channels, omnicycler_client.measurements
        channel, run_id = await self._finished_channel(
            omnicycler_client, omnicycler_short_protocol, omnicycler_run_timeout
        )
        await channels.clear_channel(channel.channel, run_id)

        status = await channels.get_channel_status(channel.channel)
        assert status.state is ChannelState.IDLE
        assert status.run_id == ""
        assert status.record_count == 0
        assert status.test_time == 0
        assert status.protocol_step == 0
        with pytest.raises(NoRunError):
            await channels.get_run_information(channel.channel)
        with pytest.raises(NoRunError):
            await measurements.get_data(channel.channel, run_id, 0, 10)
        with pytest.raises(NoRunError):
            await measurements.get_record_count(channel.channel, run_id)
        with pytest.raises(NoRunError):
            await measurements.export_data(channel.channel, run_id, BdfFormat.CSV)
        await channels.stop_protocol(channel.channel, run_id)  # nothing to stop
        await channels.clear_channel(channel.channel, run_id)  # nothing to clear

    async def test_busy_channel_rejects_start_and_clear(
        self, omnicycler_client: OmniCyclerClient, omnicycler_long_protocol: bytes
    ) -> None:
        channels = omnicycler_client.channels
        channel = await self._idle_channel(omnicycler_client)
        run_id = await channels.start_protocol(channel.channel, omnicycler_long_protocol, sample_for(channel))

        with pytest.raises(ChannelBusyError):
            await channels.start_protocol(channel.channel, omnicycler_long_protocol, sample_for(channel))
        with pytest.raises(ChannelBusyError):
            await channels.clear_channel(channel.channel, run_id)

    async def test_stop_is_idempotent(
        self, omnicycler_client: OmniCyclerClient, omnicycler_long_protocol: bytes
    ) -> None:
        channels = omnicycler_client.channels
        channel = await self._idle_channel(omnicycler_client)
        await channels.stop_protocol(channel.channel, FOREIGN_RUN)  # no run: no effect

        run_id = await channels.start_protocol(channel.channel, omnicycler_long_protocol, sample_for(channel))
        await channels.stop_protocol(channel.channel, run_id)
        status = await channels.wait_for_state(channel.channel, ENDED_STATES, timeout=10)
        assert status.state is ChannelState.STOPPED
        assert status.run_id == run_id
        await channels.stop_protocol(channel.channel, run_id)

        await channels.get_run_information(channel.channel)
        await channels.clear_channel(channel.channel, run_id)
        assert (await channels.get_channel_status(channel.channel)).state is ChannelState.IDLE

    async def test_pause_and_resume(self, omnicycler_client: OmniCyclerClient, omnicycler_long_protocol: bytes) -> None:
        channels = omnicycler_client.channels
        channel = await self._idle_channel(omnicycler_client)
        with pytest.raises(NoActiveProtocolError):
            await channels.pause_protocol(channel.channel, FOREIGN_RUN)

        run_id = await channels.start_protocol(channel.channel, omnicycler_long_protocol, sample_for(channel))
        await channels.pause_protocol(channel.channel, run_id)
        await channels.pause_protocol(channel.channel, run_id)
        await channels.wait_for_state(channel.channel, {ChannelState.PAUSED}, timeout=10)
        await channels.resume_protocol(channel.channel, run_id)
        await channels.resume_protocol(channel.channel, run_id)
        status = await channels.wait_for_state(channel.channel, {ChannelState.RUNNING}, timeout=10)
        assert status.run_id == run_id

    async def test_safety_limit_faults_with_a_reason(
        self,
        omnicycler_client: OmniCyclerClient,
        omnicycler_faulting_protocol: collections.abc.Callable[[Sample], bytes],
        omnicycler_run_timeout: float,
    ) -> None:
        channel = await self._idle_channel(omnicycler_client)
        sample = sample_for(channel)
        run_id = await omnicycler_client.channels.start_protocol(
            channel.channel, omnicycler_faulting_protocol(sample), sample
        )

        status = await omnicycler_client.channels.wait_for_state(
            channel.channel, ENDED_STATES, timeout=omnicycler_run_timeout
        )

        assert status.state is ChannelState.FAULTED
        assert status.fault_reason
        for other in await self._statuses(omnicycler_client):
            assert other.state is ChannelState.FAULTED or other.fault_reason == ""
        await omnicycler_client.channels.clear_channel(channel.channel, run_id)
        assert (await omnicycler_client.channels.get_channel_status(channel.channel)).fault_reason == ""

    async def test_concurrent_starts_on_different_channels(
        self, omnicycler_client: OmniCyclerClient, omnicycler_long_protocol: bytes
    ) -> None:
        idle = await self._idle_channels(omnicycler_client)
        if len(idle) < 2:
            pytest.skip("Needs two idle channels.")
        await asyncio.gather(
            *(
                omnicycler_client.channels.start_protocol(
                    channel.channel, omnicycler_long_protocol, sample_for(channel)
                )
                for channel in idle[:2]
            )
        )
        for channel in idle[:2]:
            await omnicycler_client.channels.wait_for_state(channel.channel, {ChannelState.RUNNING}, timeout=10)

    async def test_concurrent_starts_on_one_channel(
        self, omnicycler_client: OmniCyclerClient, omnicycler_long_protocol: bytes
    ) -> None:
        channel = await self._idle_channel(omnicycler_client)
        outcomes = await asyncio.gather(
            *(
                omnicycler_client.channels.start_protocol(
                    channel.channel, omnicycler_long_protocol, sample_for(channel), run_id=_run_id()
                )
                for _ in range(2)
            ),
            return_exceptions=True,
        )
        assert sum(isinstance(outcome, str) for outcome in outcomes) == 1
        assert sum(isinstance(outcome, ChannelBusyError) for outcome in outcomes) == 1

    # Data.

    async def test_current_sign_and_magnitude(
        self, omnicycler_client: OmniCyclerClient, omnicycler_short_protocol: bytes, omnicycler_run_timeout: float
    ) -> None:
        """Step 1 of the short protocol charges and step 3 discharges, both at `CHARGE_RATE`, in ampere."""
        channel, run_id = await self._finished_channel(
            omnicycler_client, omnicycler_short_protocol, omnicycler_run_timeout
        )
        expected = CHARGE_RATE * sample_for(channel).nominal_capacity
        points = await omnicycler_client.measurements.get_all_data(channel.channel, run_id)

        charge, discharge = _step_points(points, 1), _step_points(points, 3)
        assert statistics.median(point.current for point in charge) == pytest.approx(expected, rel=CURRENT_TOLERANCE)
        assert statistics.median(point.current for point in discharge) == pytest.approx(
            -expected, rel=CURRENT_TOLERANCE
        )

    async def test_capacities_and_energies_follow_the_current(
        self, omnicycler_client: OmniCyclerClient, omnicycler_short_protocol: bytes, omnicycler_run_timeout: float
    ) -> None:
        """Totals count since the run started, never decrease, and are the integrals of current and power."""
        channel, run_id = await self._finished_channel(
            omnicycler_client, omnicycler_short_protocol, omnicycler_run_timeout
        )
        points = await omnicycler_client.measurements.get_all_data(channel.channel, run_id)
        totals = ("charging_capacity", "discharging_capacity", "charging_energy", "discharging_energy")
        for name in totals:
            values = [getattr(point, name) for point in points]
            assert all(math.isfinite(value) and value >= 0 for value in values), name
            assert all(later >= earlier - 1e-12 for earlier, later in itertools.pairwise(values)), name

        charge, discharge = _step_points(points, 1), _step_points(points, 3)
        assert _increase(charge, "charging_capacity") == pytest.approx(
            _integral(charge, lambda p: p.current) / 3600, rel=INTEGRAL_TOLERANCE
        )
        assert _increase(charge, "charging_energy") == pytest.approx(
            _integral(charge, lambda p: p.voltage * p.current) / 3600, rel=INTEGRAL_TOLERANCE
        )
        assert _increase(discharge, "discharging_capacity") == pytest.approx(
            -_integral(discharge, lambda p: p.current) / 3600, rel=INTEGRAL_TOLERANCE
        )
        assert _increase(discharge, "discharging_energy") == pytest.approx(
            -_integral(discharge, lambda p: p.voltage * p.current) / 3600, rel=INTEGRAL_TOLERANCE
        )
        assert _increase(charge, "discharging_capacity") == pytest.approx(0, abs=1e-9)
        assert _increase(discharge, "charging_capacity") == pytest.approx(0, abs=1e-9)

    async def test_time_range_matches_paging(
        self, omnicycler_client: OmniCyclerClient, omnicycler_short_protocol: bytes, omnicycler_run_timeout: float
    ) -> None:
        channel, run_id = await self._finished_channel(
            omnicycler_client, omnicycler_short_protocol, omnicycler_run_timeout
        )
        points = await omnicycler_client.measurements.get_all_data(channel.channel, run_id)
        start, end = points[len(points) // 4].test_time, points[3 * len(points) // 4].test_time

        ranged = await omnicycler_client.measurements.get_data_in_time_range(channel.channel, run_id, start, end)
        expected = [point.record_index for point in points if start <= point.test_time <= end]
        assert [point.record_index for point in ranged] == expected

    async def test_data_table_matches_get_data(
        self, omnicycler_client: OmniCyclerClient, omnicycler_short_protocol: bytes, omnicycler_run_timeout: float
    ) -> None:
        measurements = omnicycler_client.measurements
        channel, run_id = await self._finished_channel(
            omnicycler_client, omnicycler_short_protocol, omnicycler_run_timeout
        )
        points = await measurements.get_all_data(channel.channel, run_id)
        table = await measurements.get_data_table(channel.channel, run_id, 0, len(points) + 10)

        assert table.num_rows == len(points)
        for field, label in COLUMNS.items():
            expected = [float(getattr(point, field)) for point in points]
            if all(math.isnan(value) for value in expected):
                assert label not in table.column_names, label
                continue
            assert table_column(table, field) == pytest.approx(expected, nan_ok=True), label
            if field in INTEGER_FIELDS:
                assert str(table.schema.field(label).type) == "int64", label

        middle = len(points) // 2
        part = await measurements.get_data_table(
            channel.channel, run_id, middle, 3, [COLUMNS["record_index"], COLUMNS["voltage"]]
        )
        assert part.column_names == [COLUMNS["record_index"], COLUMNS["voltage"]]
        assert part.column(COLUMNS["record_index"]).to_pylist() == [middle, middle + 1, middle + 2]

    async def test_monitor_streams_every_record_once(
        self, omnicycler_client: OmniCyclerClient, omnicycler_short_protocol: bytes, omnicycler_run_timeout: float
    ) -> None:
        channel = await self._idle_channel(omnicycler_client)
        run_id = await omnicycler_client.channels.start_protocol(
            channel.channel, omnicycler_short_protocol, sample_for(channel)
        )

        async with (
            asyncio.timeout(omnicycler_run_timeout),
            omnicycler_client.measurements.monitor(channel.channel, run_id, maximal_duration=5) as records,
        ):
            streamed = [point.record_index async for point in records]

        count = await omnicycler_client.measurements.get_record_count(channel.channel, run_id)
        assert streamed == list(range(count))

    async def test_unmeasured_quantities_are_nan(
        self, omnicycler_client: OmniCyclerClient, omnicycler_short_protocol: bytes, omnicycler_run_timeout: float
    ) -> None:
        channel, run_id = await self._finished_channel(
            omnicycler_client, omnicycler_short_protocol, omnicycler_run_timeout
        )
        unmeasured = [
            field for quantity, field in OPTIONAL_FIELDS.items() if quantity not in channel.measured_quantities
        ]
        for point in await omnicycler_client.measurements.get_all_data(channel.channel, run_id):
            assert all(math.isnan(getattr(point, field)) for field in unmeasured)

    @pytest.mark.parametrize("data_format", list(BdfFormat))
    async def test_export_is_valid_bdf(
        self,
        omnicycler_client: OmniCyclerClient,
        omnicycler_short_protocol: bytes,
        omnicycler_run_timeout: float,
        data_format: BdfFormat,
        tmp_path: pathlib.Path,
    ) -> None:
        polars = pytest.importorskip("polars")
        bdf = pytest.importorskip("bdf")
        channel, run_id = await self._finished_channel(
            omnicycler_client, omnicycler_short_protocol, omnicycler_run_timeout
        )

        export = await omnicycler_client.measurements.export_data(channel.channel, run_id, data_format)

        reader = polars.read_csv if data_format is BdfFormat.CSV else polars.read_parquet
        frame = reader(io.BytesIO(export.data))
        report = bdf.validate_df(frame, raise_on_error=True)
        assert report["extras"] == []
        assert report["derived"]["issues"] == []
        assert len(frame) == await omnicycler_client.measurements.get_record_count(channel.channel, run_id)
        floats = [name for name, dtype in frame.schema.items() if dtype.is_float()]
        assert not any(frame[name].is_nan().all() for name in floats)

        table, _ = export.save(tmp_path / f"run.bdf.{data_format.name.lower()}")
        _, metadata = bdf.read(table)
        test = metadata.battinfo_test.test
        assert test.identifier == run_id
        assert test.status == "completed"
        assert metadata.battinfo_cell.cell_instance.serial_number == sample_for(channel).identifier
        assert metadata.battinfo_channel.channel.index == channel.channel
        assert metadata.battinfo_equipment.equipment.serial_number
        assert metadata.battinfo_test_protocol.method

    # Restarts.

    async def test_ended_run_survives_restart(
        self,
        omnicycler_client: OmniCyclerClient,
        omnicycler_short_protocol: bytes,
        omnicycler_run_timeout: float,
        omnicycler_restart: Restart,
        omnicycler_keeps_runs: bool,  # noqa: FBT001  # a fixture
    ) -> None:
        _require_kept_runs(omnicycler_keeps_runs)
        channel, run_id = await self._finished_channel(
            omnicycler_client, omnicycler_short_protocol, omnicycler_run_timeout
        )
        run = await omnicycler_client.channels.get_run_information(channel.channel)
        points = await omnicycler_client.measurements.get_all_data(channel.channel, run_id)

        async with OmniCyclerClient.connect(await omnicycler_restart(), timeout=30) as client:
            status = await client.channels.wait_for_state(
                channel.channel, {ChannelState.FINISHED}, timeout=omnicycler_run_timeout
            )
            assert status.run_id == run_id
            assert status.record_count == len(points)
            again = await client.channels.get_run_information(channel.channel)
            assert again.run_id == run.run_id
            assert again.sample == run.sample
            assert again.protocol_format is run.protocol_format
            assert again.protocol == run.protocol
            assert math.isclose(again.start_time, run.start_time, abs_tol=1e-3)
            _assert_same_points(await client.measurements.get_all_data(channel.channel, run_id), points)

            await client.channels.clear_channel(channel.channel, run_id)
            assert (await client.channels.get_channel_status(channel.channel)).state is ChannelState.IDLE

    async def test_cleared_channel_stays_idle_after_restart(
        self,
        omnicycler_client: OmniCyclerClient,
        omnicycler_short_protocol: bytes,
        omnicycler_run_timeout: float,
        omnicycler_restart: Restart,
        omnicycler_keeps_runs: bool,  # noqa: FBT001  # a fixture
    ) -> None:
        _require_kept_runs(omnicycler_keeps_runs)
        channel, run_id = await self._finished_channel(
            omnicycler_client, omnicycler_short_protocol, omnicycler_run_timeout
        )
        await omnicycler_client.channels.clear_channel(channel.channel, run_id)

        async with OmniCyclerClient.connect(await omnicycler_restart(), timeout=30) as client:
            status = await client.channels.wait_for_state(
                channel.channel, {ChannelState.IDLE}, timeout=omnicycler_run_timeout
            )
            assert status.run_id == ""
            with pytest.raises(NoRunError):
                await client.measurements.get_record_count(channel.channel, run_id)
            with pytest.raises(NoRunError):
                await client.channels.get_run_information(channel.channel)

    async def test_active_run_survives_restart(
        self,
        omnicycler_client: OmniCyclerClient,
        omnicycler_long_protocol: bytes,
        omnicycler_run_timeout: float,
        omnicycler_restart: Restart,
        omnicycler_keeps_runs: bool,  # noqa: FBT001  # a fixture
    ) -> None:
        """A run active at a restart may go on or end, but keeps its RunId and every record taken before."""
        _require_kept_runs(omnicycler_keeps_runs)
        channels, measurements = omnicycler_client.channels, omnicycler_client.measurements
        channel = await self._idle_channel(omnicycler_client)
        run_id = await channels.start_protocol(channel.channel, omnicycler_long_protocol, sample_for(channel))
        async with asyncio.timeout(omnicycler_run_timeout), channels.subscribe_channel_statuses() as statuses:
            async for snapshot in statuses:
                if any(status.channel == channel.channel and status.record_count >= 2 for status in snapshot):
                    break
        points = await measurements.get_all_data(channel.channel, run_id)

        async with OmniCyclerClient.connect(await omnicycler_restart(), timeout=30) as client:
            status = await client.channels.wait_for_state(
                channel.channel, ACTIVE_STATES | ENDED_STATES, timeout=omnicycler_run_timeout
            )
            assert status.run_id == run_id
            assert status.record_count >= len(points)
            later = await client.measurements.get_all_data(channel.channel, run_id)
            _assert_same_points(later[: len(points)], points)

            await client.channels.stop_protocol(channel.channel, run_id)
            await client.channels.wait_for_state(channel.channel, ENDED_STATES, timeout=omnicycler_run_timeout)
            await client.channels.clear_channel(channel.channel, run_id)

    # Helpers.

    async def _idle_channels(self, client: OmniCyclerClient) -> list[ChannelInformation]:
        statuses = {status.channel: status.state for status in await self._statuses(client)}
        return [
            channel
            for channel in await client.information.get_channels()
            if statuses.get(channel.channel) is ChannelState.IDLE
        ]

    async def _idle_channel(self, client: OmniCyclerClient) -> ChannelInformation:
        idle = await self._idle_channels(client)
        if not idle:
            pytest.skip("Needs an idle channel.")
        return idle[0]

    async def _finished_channel(
        self, client: OmniCyclerClient, protocol: bytes, timeout: float
    ) -> tuple[ChannelInformation, str]:
        channel = await self._idle_channel(client)
        run_id = await client.channels.start_protocol(channel.channel, protocol, sample_for(channel))
        status = await client.channels.wait_for_state(channel.channel, ENDED_STATES, timeout=timeout)
        assert status.state is ChannelState.FINISHED
        return channel, run_id

    @staticmethod
    async def _statuses(client: OmniCyclerClient) -> list[ChannelStatus]:
        async with asyncio.timeout(10), client.channels.subscribe_channel_statuses() as statuses:
            return await anext(statuses)


def _require_kept_runs(keeps_runs: bool) -> None:  # noqa: FBT001
    if not keeps_runs:
        pytest.skip("The server keeps its runs in memory (omnicycler_keeps_runs).")


def _assert_same_points(actual: list[DataPoint], expected: list[DataPoint]) -> None:
    """Assert that two lists of records are equal field by field, with NaN equal to NaN."""
    assert len(actual) == len(expected)
    for got, want in zip(actual, expected, strict=True):
        for field in dataclasses.fields(DataPoint):
            left, right = getattr(got, field.name), getattr(want, field.name)
            if isinstance(left, float) and isinstance(right, float) and math.isnan(left) and math.isnan(right):
                continue
            assert left == right, f"Record {want.record_index}: {field.name} is {left}, was {right}."


def _step_points(points: list[DataPoint], position: int) -> list[DataPoint]:
    selected = [point for point in points if point.protocol_step == position]
    assert len(selected) >= 3, f"The run recorded {len(selected)} records in protocol step {position}; need 3."
    return selected


def _increase(points: list[DataPoint], name: str) -> float:
    return getattr(points[-1], name) - getattr(points[0], name)


def _integral(points: list[DataPoint], value: collections.abc.Callable[[DataPoint], float]) -> float:
    """The trapezoidal integral of a quantity over test time, in its unit times seconds."""
    return sum(
        (later.test_time - earlier.test_time) * (value(earlier) + value(later)) / 2
        for earlier, later in itertools.pairwise(points)
    )

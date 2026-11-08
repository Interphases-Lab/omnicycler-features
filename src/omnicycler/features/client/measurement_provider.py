import collections.abc
import contextlib
import dataclasses

import pyarrow as pa
from sila.client import FeatureNamespace

from .. import CATEGORY, ORIGINATOR
from ..data_table import decode_table
from ..errors import DataRangeTooLargeError, NoRunError
from ..types import BdfExport, BdfFormat, DataPoint
from ._base import (
    ErrorMap,
    Execution,
    error_map,
    from_wire,
    list_from_wire,
    ms,
    response,
    to_wire,
    translate_errors,
)
from .errors import IncompatibleServerError

FEATURE = f"{ORIGINATOR}/{CATEGORY}/MeasurementProvider/v1"
ERRORS: ErrorMap = error_map(DataRangeTooLargeError, NoRunError)

PAGE_SIZE = 5000
"""The most records one `get_data` call may request."""

TABLE_PAGE_SIZE = 100_000
"""The most records one `get_data_table` call may request."""


@dataclasses.dataclass(frozen=True)
class MonitorResult:
    """
    The result of one `MonitorChannel` execution.

    Attributes:
      next_index: The record index to continue from.
      run_ended: Whether the run has ended and every record of it was sent.
    """

    next_index: int
    run_ended: bool


class MeasurementProviderClient:
    """Typed client of `MeasurementProvider`: the measured data of channel runs."""

    def __init__(self, namespace: FeatureNamespace) -> None:
        self._ns = namespace

    async def get_record_count(self, channel: int, *, timeout: float | None = None) -> int:
        """Get the number of data records of the run a channel holds; zero without a run."""
        with translate_errors(ERRORS):
            responses = await self._ns.GetRecordCount(Channel=channel, timeout=ms(timeout))
        return from_wire(int, response(responses, "RecordCount"))

    async def get_data(
        self, channel: int, from_index: int, count: int, *, timeout: float | None = None
    ) -> list[DataPoint]:
        """Get up to `count` consecutive records from `from_index` on; `count` is at most `PAGE_SIZE`."""
        with translate_errors(ERRORS):
            responses = await self._ns.GetData(Channel=channel, FromIndex=from_index, Count=count, timeout=ms(timeout))
        return list_from_wire(DataPoint, response(responses, "DataPoints"))

    async def get_data_table(
        self,
        channel: int,
        from_index: int,
        count: int,
        columns: collections.abc.Sequence[str] = (),
        *,
        timeout: float | None = None,
    ) -> pa.Table:
        """
        Get up to `count` consecutive records from `from_index` on as an Arrow table, for bulk reads.

        `count` is at most `TABLE_PAGE_SIZE`. Columns are named by BDF preferred label (see
        `data_table.COLUMNS`); `columns` selects some of them, and empty means all. Unmeasured values are null,
        and all-null columns are left out.

        Raises:
          IncompatibleServerError: If the server does not offer `GetDataTable`.
        """
        try:
            command = self._ns.GetDataTable
        except AttributeError:
            msg = "The server's MeasurementProvider does not offer GetDataTable; use get_data."
            raise IncompatibleServerError(msg) from None
        with translate_errors(ERRORS):
            responses = await command(
                Channel=channel, FromIndex=from_index, Count=count, Columns=list(columns), timeout=ms(timeout)
            )
        return decode_table(from_wire(bytes, response(responses, "Table")))

    async def get_data_in_time_range(
        self, channel: int, from_test_time: float, to_test_time: float, *, timeout: float | None = None
    ) -> list[DataPoint]:
        """
        Get the records whose test time lies within an inclusive range, in seconds.

        Raises:
          DataRangeTooLargeError: If more than `PAGE_SIZE` records match.
        """
        with translate_errors(ERRORS):
            responses = await self._ns.GetDataInTimeRange(
                Channel=channel, FromTestTime=from_test_time, ToTestTime=to_test_time, timeout=ms(timeout)
            )
        return list_from_wire(DataPoint, response(responses, "DataPoints"))

    async def get_all_data(self, channel: int, *, timeout: float | None = None) -> list[DataPoint]:
        """Get every record of the run a channel holds, page by page."""
        points: list[DataPoint] = []
        while batch := await self.get_data(channel, len(points), PAGE_SIZE, timeout=timeout):
            points.extend(batch)
        return points

    async def start_monitor_channel(
        self, channel: int, from_index: int, maximal_duration: float
    ) -> Execution[MonitorResult, list[DataPoint]]:
        """Start one `MonitorChannel` execution; prefer `monitor` for a complete, gap-free stream."""
        with translate_errors(ERRORS):
            raw = await self._ns.MonitorChannel(Channel=channel, FromIndex=from_index, MaximalDuration=maximal_duration)
        return Execution(raw, _decode_monitor_result, _decode_batch, ERRORS)

    async def monitor_channel(self, channel: int, from_index: int, maximal_duration: float) -> MonitorResult:
        """Run one `MonitorChannel` execution to its end, discarding the streamed records."""
        return await (await self.start_monitor_channel(channel, from_index, maximal_duration)).wait()

    @contextlib.asynccontextmanager
    async def monitor(
        self, channel: int, *, from_index: int = 0, maximal_duration: float = 600.0
    ) -> collections.abc.AsyncGenerator[collections.abc.AsyncIterator[DataPoint]]:
        """
        Stream every record of a channel's run from `from_index` on, until the run ends.

        Chains `MonitorChannel` executions of at most `maximal_duration`
        seconds and fills any gap from `get_data`, so each record is yielded
        exactly once and in order.
        """
        records = self._monitor(channel, from_index, maximal_duration)
        try:
            yield records
        finally:
            await records.aclose()

    async def start_export_data(self, channel: int, data_format: BdfFormat) -> Execution[BdfExport, None]:
        """Start exporting the complete run of a channel as a Battery Data Format file with its metadata."""
        with translate_errors(ERRORS):
            raw = await self._ns.ExportData(Channel=channel, DataFormat=to_wire(data_format))
        return Execution(raw, _decode_export, None, ERRORS)

    async def export_data(self, channel: int, data_format: BdfFormat) -> BdfExport:
        """
        Export the complete run of a channel as a Battery Data Format file with its BattINFO metadata sidecar.

        Write both with `BdfExport.save`, so `bdf.read` finds the sidecar.

        Raises:
          NoRunError: If the channel holds no run.
        """
        return await (await self.start_export_data(channel, data_format)).wait()

    async def _monitor(
        self, channel: int, from_index: int, maximal_duration: float
    ) -> collections.abc.AsyncGenerator[DataPoint]:
        expected = from_index
        while True:
            execution = await self.start_monitor_channel(channel, expected, maximal_duration)
            async with execution.intermediate() as batches:
                async for batch in batches:
                    for point in await self._fill(channel, expected, batch):
                        expected = point.record_index + 1
                        yield point
            result = await execution.wait()
            while expected < result.next_index:
                gap = await self.get_data(channel, expected, min(PAGE_SIZE, result.next_index - expected))
                if not gap:
                    break
                for point in gap:
                    expected = point.record_index + 1
                    yield point
            if result.run_ended:
                return

    async def _fill(self, channel: int, expected: int, batch: list[DataPoint]) -> list[DataPoint]:
        # A busy server can drop intermediate responses, so a gap may span many pages.
        fresh = [point for point in batch if point.record_index >= expected]
        if not fresh:
            return fresh
        gap: list[DataPoint] = []
        while expected < fresh[0].record_index:
            page = await self.get_data(channel, expected, min(PAGE_SIZE, fresh[0].record_index - expected))
            if not page:
                break
            gap.extend(page)
            expected = page[-1].record_index + 1
        return [*gap, *fresh]


def _decode_batch(raw: object) -> list[DataPoint]:
    return list_from_wire(DataPoint, response(raw, "DataPoints"))


def _decode_monitor_result(raw: object) -> MonitorResult:
    return MonitorResult(
        next_index=from_wire(int, response(raw, "NextIndex")),
        run_ended=from_wire(bool, response(raw, "RunEnded")),
    )


def _decode_export(raw: object) -> BdfExport:
    return BdfExport(
        data=from_wire(bytes, response(raw, "Data")),
        metadata=from_wire(bytes, response(raw, "Metadata")),
    )

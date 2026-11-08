import datetime
import typing

from unitelabs.cdk import sila

from .. import CATEGORY, ORIGINATOR
from ..backend import MAXIMAL_RECORDS_PER_RESPONSE, MAXIMAL_RECORDS_PER_TABLE, CyclerBackend
from ..data_table import COLUMNS
from ..errors import DataRangeTooLargeError, NoRunError
from ..parameters import ChannelNumber, JsonDocument, restrict_channels
from ..types import BdfFormat, DataPoint
from ..units import SECOND

MAXIMAL_MONITOR_DURATION = 3600.0
"""The longest a single `MonitorChannel` execution runs, in seconds."""

RESULT_RETENTION = datetime.timedelta(minutes=10)
"""How long a finished observable command execution keeps its result for the client."""

RecordIndex = typing.Annotated[int, sila.constraints.MinimalInclusive(0)]
RecordCount = typing.Annotated[
    int, sila.constraints.MinimalInclusive(1), sila.constraints.MaximalInclusive(MAXIMAL_RECORDS_PER_RESPONSE)
]
TableRecordCount = typing.Annotated[
    int, sila.constraints.MinimalInclusive(1), sila.constraints.MaximalInclusive(MAXIMAL_RECORDS_PER_TABLE)
]
ColumnLabel = typing.Annotated[str, sila.constraints.Set(list(COLUMNS.values()))]
ArrowStream = typing.Annotated[bytes, sila.constraints.ContentType("application", "vnd.apache.arrow.stream")]
TestTime = typing.Annotated[float, SECOND, sila.constraints.MinimalInclusive(0.0)]
MonitorDuration = typing.Annotated[
    float, SECOND, sila.constraints.MinimalExclusive(0.0), sila.constraints.MaximalInclusive(MAXIMAL_MONITOR_DURATION)
]


class MeasurementProvider(sila.Feature):
    """
    Read the measured data of the run a channel holds.

    Data records are numbered from zero without gaps by `RecordIndex`; quantities a channel does not measure are
    NaN. Records stay available until the channel is cleared with `ChannelController.ClearChannel`, and a channel
    without a run has no records. Follow a running protocol with `MonitorChannel`, read any part of the data with
    `GetData` or `GetDataInTimeRange`, and fetch the complete run as a Battery Data Format file with `ExportData`.
    For bulk reads, `GetDataTable` returns the same records as one binary Arrow table, which is much faster to
    transfer and decode than `GetData`.
    """

    def __init__(self, backend: CyclerBackend, *, poll_interval: float = 1.0) -> None:
        super().__init__(originator=ORIGINATOR, category=CATEGORY)
        self.backend = backend
        self._poll_interval = poll_interval

    @typing.override
    async def on_before_start(self) -> None:
        channels = [information.channel for information in await self.backend.get_channels()]
        handlers = [
            self.get_record_count,
            self.get_data,
            self.get_data_table,
            self.get_data_in_time_range,
            self.monitor_channel,
            self.export_data,
        ]
        restrict_channels(self, handlers, channels)

    @sila.UnobservableCommand()
    async def get_record_count(self, channel: ChannelNumber) -> int:
        """
        Get the number of data records of the run a channel holds.

        Args:
          Channel: The channel number.

        Returns:
          RecordCount: The number of records, zero without a run.
        """
        return (await self.backend.get_channel_status(channel)).record_count

    @sila.UnobservableCommand()
    async def get_data(self, channel: ChannelNumber, from_index: RecordIndex, count: RecordCount) -> list[DataPoint]:
        """
        Get consecutive data records of the run a channel holds, by record index.

        Args:
          Channel: The channel number.
          FromIndex: The record index of the first record to return.
          Count: The most records to return.

        Returns:
          DataPoints: The records from FromIndex on, fewer than Count at the end of the data.
        """
        return await self.backend.get_data(channel, from_index, count)

    @sila.UnobservableCommand()
    async def get_data_table(
        self, channel: ChannelNumber, from_index: RecordIndex, count: TableRecordCount, columns: list[ColumnLabel]
    ) -> ArrowStream:
        """
        Get consecutive data records of the run a channel holds as one Arrow table, for bulk reads.

        The records are those `GetData` returns, as an Arrow IPC stream with one record batch and one column per
        quantity, named by its BDF preferred label (for example `Voltage / V`) in BDF units. Record index, cycle
        count, step count and step ID are int64, all other quantities float64. A value the channel did not measure
        is null, and a column that is null in every row is left out. Nothing is derived or validated: use
        `ExportData` for a complete BDF file.

        Args:
          Channel: The channel number.
          FromIndex: The record index of the first record to return.
          Count: The most records to return.
          Columns: The BDF labels of the columns to return; empty for every quantity.

        Returns:
          Table: The records from FromIndex on, fewer than Count at the end of the data.
        """
        return await self.backend.get_data_table(channel, from_index, count, list(columns))

    @sila.UnobservableCommand(errors=[DataRangeTooLargeError])
    async def get_data_in_time_range(
        self, channel: ChannelNumber, from_test_time: TestTime, to_test_time: TestTime
    ) -> list[DataPoint]:
        """
        Get the data records of the run a channel holds whose test time lies within a range.

        Args:
          Channel: The channel number.
          FromTestTime: The start of the range, inclusive.
          ToTestTime: The end of the range, inclusive.

        Returns:
          DataPoints: The records within the range, in record index order.
        """
        return await self.backend.get_data_in_time_range(channel, from_test_time, to_test_time)

    @get_data_in_time_range.validator
    def _check_time_range(self, from_test_time: float, to_test_time: float) -> None:
        if to_test_time < from_test_time:
            msg = f"ToTestTime ({to_test_time} s) must not be before FromTestTime ({from_test_time} s)."
            raise sila.errors.ValidationError(msg, "to_test_time")

    @sila.ObservableCommand(lifetime=RESULT_RETENTION)
    async def monitor_channel(
        self,
        channel: ChannelNumber,
        from_index: RecordIndex,
        maximal_duration: MonitorDuration,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[list[DataPoint]],
    ) -> tuple[int, bool]:
        """
        Stream the data records of a channel's run as they are recorded.

        Sends every record from FromIndex on, then new records as they arrive, and finishes when the run has
        ended and all its records are sent, or after MaximalDuration. Call again with the returned NextIndex to
        continue without gaps or duplicates.

        Args:
          Channel: The channel number.
          FromIndex: The record index of the first record to send.
          MaximalDuration: The longest this execution runs.

        Yields:
          DataPoints: A batch of new records, in record index order.

        Returns:
          NextIndex: The record index to continue from.
          RunEnded: Whether the run has ended and every record of it was sent.
        """
        status.update(updated_lifetime=datetime.timedelta(seconds=maximal_duration) + RESULT_RETENTION)
        monitor = self.backend.monitor(channel, from_index, maximal_duration, self._poll_interval)
        async for batch in monitor:
            intermediate.send(batch)
        return monitor.next_index, monitor.run_ended

    @sila.ObservableCommand(lifetime=RESULT_RETENTION, errors=[NoRunError])
    async def export_data(self, channel: ChannelNumber, data_format: BdfFormat) -> tuple[bytes, JsonDocument]:
        """
        Export the complete run a channel holds as a Battery Data Format file with its metadata.

        Columns of quantities the channel does not measure are left out of the file. The metadata describes the
        test, the cell, the cycler, the channel and the protocol as BattINFO records, in the sidecar form that
        batterydf reads: save it beside the file with the last suffix replaced by `.metadata.json`.

        Args:
          Channel: The channel number.
          DataFormat: The file serialisation.

        Returns:
          Data: The file contents.
          Metadata: The BattINFO metadata sidecar.
        """
        export = await self.backend.export_data(channel, data_format)
        return export.data, export.metadata

"""
Pin the typed client to the server's live feature definitions.

A renamed endpoint, parameter, response, structure element, enum member or
error fails here instead of in production.
"""

import collections.abc
import dataclasses
import enum
import inspect
import typing

import pytest
from sila.client import Client, ClientConfig, Constrained, ContentType, ObservableCommand, Set, Structure
from sila.client import List as SiLAList
from unitelabs.cdk import sila

from omnicycler.features import types as wire_types
from omnicycler.features.channel_controller import ChannelController
from omnicycler.features.client import (
    FEATURES,
    ChannelControllerClient,
    CyclerInformationProviderClient,
    MeasurementProviderClient,
    channel_controller,
    cycler_information_provider,
    measurement_provider,
)
from omnicycler.features.client._base import pascal, wire_name
from omnicycler.features.cycler_information_provider import CyclerInformationProvider
from omnicycler.features.measurement_provider import MeasurementProvider

PAIRS = [
    (CyclerInformationProvider, CyclerInformationProviderClient, cycler_information_provider),
    (ChannelController, ChannelControllerClient, channel_controller),
    (MeasurementProvider, MeasurementProviderClient, measurement_provider),
]

RESPONSES = {
    "GetChannelStatus": ["Status"],
    "GetRunInformation": ["RunInformation"],
    "StartProtocol": [],
    "ValidateProtocol": ["Warnings"],
    "StopProtocol": [],
    "PauseProtocol": [],
    "ResumeProtocol": [],
    "ClearChannel": [],
    "GetRecordCount": ["RecordCount"],
    "GetData": ["DataPoints"],
    "GetDataInTimeRange": ["DataPoints"],
    "MonitorChannel": ["NextIndex", "RunEnded"],
    "ExportData": ["Data", "Metadata"],
}
"""The responses the client reads, per command."""

INTERMEDIATE_RESPONSES = {"MonitorChannel": ["DataPoints"]}

HIDDEN_PARAMETERS = {"self", "status", "intermediate"}


@pytest.fixture
async def raw_client(omnicycler_server: ClientConfig) -> collections.abc.AsyncIterator[Client]:
    client = Client(omnicycler_server)
    await client.open()
    try:
        yield client
    finally:
        await client.close()


def handlers(feature_class: type) -> dict[str, typing.Any]:
    return {
        name: getattr(function, "__handler")
        for name, function in inspect.getmembers(feature_class, inspect.isfunction)
        if hasattr(function, "__handler")
    }


def identifier(name: str, handler: object) -> str:
    if isinstance(handler, sila.UnobservableProperty):
        return pascal(name.removeprefix("get_"))
    if isinstance(handler, sila.ObservableProperty):
        return pascal(name.removeprefix("subscribe_"))
    return pascal(name)


async def test_every_standard_feature_resolves_at_its_major_version(raw_client: Client):
    for feature in FEATURES:
        assert str(raw_client.feature(feature).feature.fully_qualified_identifier) == feature


@pytest.mark.parametrize(("server_class", "client_class", "module"), PAIRS)
async def test_client_mirrors_every_endpoint(raw_client: Client, server_class, client_class, module):
    feature = raw_client.feature(module.FEATURE).feature
    endpoints = set(feature.properties) | set(feature.commands)
    client_methods = {name for name, _ in inspect.getmembers(client_class, inspect.isfunction)}

    for name, handler in handlers(server_class).items():
        assert name in client_methods, f"{client_class.__name__} lacks {name}"
        assert identifier(name, handler) in endpoints


@pytest.mark.parametrize(("server_class", "module"), [(server, module) for server, _, module in PAIRS])
async def test_parameters_are_named_after_the_python_parameters(raw_client: Client, server_class, module):
    feature = raw_client.feature(module.FEATURE).feature

    for name, handler in handlers(server_class).items():
        if isinstance(handler, sila.UnobservableProperty | sila.ObservableProperty):
            continue
        parameters = [
            p for p in inspect.signature(getattr(server_class, name)).parameters if p not in HIDDEN_PARAMETERS
        ]
        assert list(feature.commands[pascal(name)].parameters) == [pascal(p) for p in parameters]


@pytest.mark.parametrize("module", [channel_controller, measurement_provider])
async def test_every_response_the_client_reads_exists(raw_client: Client, module):
    feature = raw_client.feature(module.FEATURE).feature

    for command_identifier, command in feature.commands.items():
        assert list(command.responses) == RESPONSES[command_identifier]
        if command_identifier in INTERMEDIATE_RESPONSES:
            assert isinstance(command, ObservableCommand)
            assert list(command.intermediate_responses) == INTERMEDIATE_RESPONSES[command_identifier]


@pytest.mark.parametrize("module", [module for _, _, module in PAIRS])
async def test_every_declared_error_is_mapped(raw_client: Client, module):
    feature = raw_client.feature(module.FEATURE).feature

    assert set(module.ERRORS) == set(feature.errors)


async def test_structures_match_the_shared_dataclasses(raw_client: Client):
    checked = set()
    for feature_identifier in FEATURES:
        feature = raw_client.feature(feature_identifier).feature
        for name, definition in feature.data_type_definitions.items():
            dataclass = getattr(wire_types, name)
            fields = dataclasses.fields(dataclass)
            structure = definition.data_type
            assert issubclass(structure, Structure)
            elements = structure.elements
            assert list(elements) == [pascal(field.name) for field in fields]

            hints = typing.get_type_hints(dataclass)
            for field, element in zip(fields, elements.values(), strict=True):
                assert_enum_matches(hints[field.name], element.data_type)
            checked.add(name)

    assert checked == {"ChannelInformation", "ChannelStatus", "DataPoint", "RunInformation", "Sample"}


async def test_enum_parameters_and_properties_match(raw_client: Client):
    information = raw_client.feature(cycler_information_provider.FEATURE).feature
    measurements = raw_client.feature(measurement_provider.FEATURE).feature

    assert_enum_matches(list[wire_types.StepType], information.properties["SupportedStepTypes"].data_type)
    assert_enum_matches(wire_types.BdfFormat, measurements.commands["ExportData"].parameters["DataFormat"].data_type)


def assert_enum_matches(hint: object, data_type: type) -> None:
    if typing.get_origin(hint) is list:
        (hint,) = typing.get_args(hint)
        assert issubclass(data_type, SiLAList)
        data_type = data_type.data_type
    if not (isinstance(hint, type) and issubclass(hint, enum.Enum)):
        return

    assert issubclass(data_type, Constrained)
    allowed = next(constraint for constraint in data_type.constraints if isinstance(constraint, Set))
    assert [value.value for value in allowed.values] == [wire_name(member) for member in hint]


def test_every_wire_dataclass_is_used_by_a_feature():
    structures_in_types = {
        name
        for name, value in vars(wire_types).items()
        if isinstance(value, type) and issubclass(value, sila.CustomDataType) and value is not sila.CustomDataType
    }
    assert structures_in_types == {"ChannelInformation", "ChannelStatus", "DataPoint", "RunInformation", "Sample"}


async def test_export_metadata_is_typed_as_json(raw_client: Client):
    measurements = raw_client.feature(measurement_provider.FEATURE).feature
    metadata = measurements.commands["ExportData"].responses["Metadata"].data_type

    assert issubclass(metadata, Constrained)
    (content_type,) = metadata.constraints
    assert isinstance(content_type, ContentType)
    assert (content_type.type, content_type.subtype) == ("application", "json")

"""Typed models for the JUNG HOME gateway REST/WebSocket payloads.

These ``TypedDict``s describe the shape of the device data the gateway returns
from ``GET /functions`` (and re-broadcasts over the WebSocket). They are a
*static* contract: the data still arrives as untrusted JSON, so call sites keep
their defensive ``.get(...)`` access for malformed payloads — but typing the
stored device list as ``list[Device]`` makes key typos and wrong-key access
``mypy`` errors instead of silent ``Any``.
"""

from typing import NotRequired, TypedDict


class DatapointValue(TypedDict):
    """A single ``key``/``value`` pair inside a datapoint (values are strings)."""

    key: str
    value: str


class Datapoint(TypedDict):
    """A device datapoint (e.g. ``switch``, ``brightness``, ``up_request``)."""

    id: str
    type: str
    values: list[DatapointValue]


class Device(TypedDict):
    """A gateway device (``OnOff``, ``ColorLight``, ``Socket``, ``RockerSwitch``)."""

    id: str
    type: str
    label: str
    datapoints: list[Datapoint]
    sw_version: NotRequired[str]

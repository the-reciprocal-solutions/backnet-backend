"""Unit tests for the standalone ETS group-address import parser."""

import pytest

from bacnet_lab.adapters.knx.ets_import import (
    EtsGroupAddress,
    normalize_dpt,
    parse_ets_csv,
    parse_ets_file,
    parse_ets_xml,
    _raw_to_ga,
)


def test_csv_semicolon_explicit_address_and_dpt():
    content = (
        "Group name;Address;DatapointType\n"
        "Kitchen Light;1/2/3;DPST-5-1\n"
    ).encode("utf-8")

    result = parse_ets_csv(content)

    assert result == [
        EtsGroupAddress(name="Kitchen Light", group_address="1/2/3", dpt="5.001"),
    ]


def test_csv_split_main_middle_sub_columns():
    content = (
        "Main;Middle;Sub;Name;DPT\n"
        "1;2;3;Lamp;DPT-9\n"
        "x;y;z;Bad Row;DPT-1\n"  # non-numeric -> skipped
    ).encode("utf-8")

    result = parse_ets_csv(content)

    assert result == [
        EtsGroupAddress(name="Lamp", group_address="1/2/3", dpt="9"),
    ]


def test_xml_raw_integer_address_conversion():
    content = (
        '<GroupAddress-Export>'
        '<GroupAddress Name="Raw" Address="2305" DPTs="DPST-5-1"/>'
        '</GroupAddress-Export>'
    ).encode("utf-8")

    result = parse_ets_xml(content)

    assert result == [
        EtsGroupAddress(name="Raw", group_address="1/1/1", dpt="5.001"),
    ]


def test_xml_string_address_passthrough_and_namespace():
    content = (
        '<ns:GroupAddress-Export xmlns:ns="http://knx.org/xml/ga/01">'
        '<ns:GroupAddress Name="Str" Address="1/2/3" DPTs="DPT-9"/>'
        '</ns:GroupAddress-Export>'
    ).encode("utf-8")

    result = parse_ets_xml(content)

    assert result == [
        EtsGroupAddress(name="Str", group_address="1/2/3", dpt="9"),
    ]


def test_xml_nested_group_addresses_collected():
    content = (
        '<GroupAddress-Export>'
        '<GroupRange Name="Main">'
        '<GroupRange Name="Middle">'
        '<GroupAddress Name="Deep" Address="1/2/3"/>'
        '</GroupRange>'
        '</GroupRange>'
        '</GroupAddress-Export>'
    ).encode("utf-8")

    result = parse_ets_xml(content)

    assert result == [
        EtsGroupAddress(name="Deep", group_address="1/2/3", dpt=""),
    ]


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("DPST-5-1", "5.001"),
        ("DPT-9", "9"),
        ("DPST-9", "9"),
        ("9.001", "9.001"),
        ("1.001", "1.001"),
        ("", ""),
        ("garbage-no-digits", ""),
        (None, ""),
    ],
)
def test_normalize_dpt(raw, expected):
    assert normalize_dpt(raw) == expected


def test_raw_to_ga():
    assert _raw_to_ga(2305) == "1/1/1"
    assert _raw_to_ga(0) == "0/0/0"


def test_parse_ets_file_dispatch_csv():
    content = b"Address;Name;DPT\n1/2/3;X;5.001\n"
    result = parse_ets_file("export.CSV", content)
    assert result[0].group_address == "1/2/3"


def test_parse_ets_file_unsupported_extension():
    with pytest.raises(ValueError, match="Unsupported ETS file type"):
        parse_ets_file("data.txt", b"whatever")

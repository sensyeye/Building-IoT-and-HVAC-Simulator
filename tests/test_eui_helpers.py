"""Tests for the LoRaWAN-style EUI helpers."""

from __future__ import annotations

import pytest

from simulator.utils.eui import generate_eui, is_lorawan_eui, normalize_eui


# ---------------------------------------------------------------------------
# is_lorawan_eui
# ---------------------------------------------------------------------------


def test_canonical_form_is_valid():
    assert is_lorawan_eui("70b3d57ed005f1a4") is True


def test_uppercase_is_rejected():
    assert is_lorawan_eui("70B3D57ED005F1A4") is False


def test_separators_are_rejected_in_canonical_check():
    assert is_lorawan_eui("70:b3:d5:7e:d0:05:f1:a4") is False


def test_short_string_is_rejected():
    assert is_lorawan_eui("70b3d57e") is False


def test_non_hex_is_rejected():
    assert is_lorawan_eui("70b3d57ed005f1ag") is False


def test_non_string_is_rejected():
    assert is_lorawan_eui(0x70b3d57ed005f1a4) is False  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# normalize_eui
# ---------------------------------------------------------------------------


def test_normalize_uppercase():
    assert normalize_eui("70B3D57ED005F1A4") == "70b3d57ed005f1a4"


def test_normalize_with_colons():
    assert normalize_eui("70:B3:D5:7E:D0:05:F1:A4") == "70b3d57ed005f1a4"


def test_normalize_with_dashes_and_whitespace():
    assert normalize_eui("  70-b3-d5-7e-d0-05-f1-a4  ") == "70b3d57ed005f1a4"


def test_normalize_rejects_bad_input():
    with pytest.raises(ValueError):
        normalize_eui("not-an-eui")


def test_normalize_rejects_non_string():
    with pytest.raises(TypeError):
        normalize_eui(123)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# generate_eui
# ---------------------------------------------------------------------------


def test_generated_eui_is_canonical():
    eui = generate_eui("dubai-office", "iaq-meeting-1")
    assert is_lorawan_eui(eui)


def test_generated_eui_is_deterministic():
    a = generate_eui("dubai-office", "iaq-meeting-1")
    b = generate_eui("dubai-office", "iaq-meeting-1")
    assert a == b


def test_generated_eui_differs_per_name():
    a = generate_eui("dubai-office", "iaq-meeting-1")
    b = generate_eui("dubai-office", "iaq-meeting-2")
    assert a != b


def test_generated_eui_differs_per_namespace():
    a = generate_eui("dubai-office", "iaq-meeting-1")
    b = generate_eui("istanbul-office", "iaq-meeting-1")
    assert a != b


def test_default_prefix_is_private_experimental():
    eui = generate_eui("ns", "n")
    assert eui.startswith("fe")


def test_custom_oui_is_respected():
    eui = generate_eui("ns", "n", oui="70b3d5")
    assert eui.startswith("70b3d5")
    assert is_lorawan_eui(eui)


def test_invalid_oui_is_rejected():
    with pytest.raises(ValueError):
        generate_eui("ns", "n", oui="zzzzzz")


def test_blank_inputs_rejected():
    with pytest.raises(ValueError):
        generate_eui("", "n")
    with pytest.raises(ValueError):
        generate_eui("ns", "")

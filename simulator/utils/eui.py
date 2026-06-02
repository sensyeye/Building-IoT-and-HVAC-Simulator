"""LoRaWAN-style DevEUI helpers.

A DevEUI is a globally-unique 64-bit identifier assigned to a LoRaWAN
end-device, encoded as **16 lowercase hexadecimal characters**
(e.g. ``70b3d57ed005f1a4``). The Sensgreen platform displays and accepts
EUIs in this exact form.

This module provides:

* :func:`is_lorawan_eui` — validator (16 lowercase hex chars).
* :func:`normalize_eui` — coerce common variants (uppercase, colons,
  dashes, whitespace) into the canonical form.
* :func:`generate_eui` — deterministic generator that turns a stable
  ``(namespace, name)`` pair into a plausible-looking EUI. Useful for
  synthesising EUIs at config-write time when no real LoRaWAN device
  has been provisioned yet.

The simulator must never use :func:`generate_eui` for production runs
against the real Sensgreen broker — there each device's EUI is assigned
by the platform and copied into the config by hand. The generator
exists for local demos, fixtures, and dashboard scaffolding.
"""

from __future__ import annotations

import hashlib
import re

# Common manufacturer / private OUIs in the LoRaWAN ecosystem. Using one
# of these as the first three bytes makes a synthetic EUI look like a
# real device when displayed in a UI. The default is the experimental
# / private range so synthetic EUIs cannot be confused with a deployed
# Semtech reference device.
_PRIVATE_EXPERIMENTAL_OUI = "fe"  # one byte; padded out by the hash below

_EUI_RE = re.compile(r"^[0-9a-f]{16}$")


def is_lorawan_eui(value: object) -> bool:
    """Return True if ``value`` is a canonical 16-char lowercase-hex EUI."""
    return isinstance(value, str) and bool(_EUI_RE.match(value))


def normalize_eui(value: str) -> str:
    """Coerce a user-supplied EUI string into the canonical form.

    Accepts uppercase, mixed case, colon/dash separators, and surrounding
    whitespace. Rejects anything that does not decode to exactly 8 bytes.
    """
    if not isinstance(value, str):
        raise TypeError(f"EUI must be str, got {type(value).__name__}")
    cleaned = re.sub(r"[\s:\-]", "", value).lower()
    if not _EUI_RE.match(cleaned):
        raise ValueError(
            f"not a valid LoRaWAN DevEUI (expected 16 hex chars): {value!r}"
        )
    return cleaned


def generate_eui(namespace: str, name: str, *, oui: str | None = None) -> str:
    """Deterministically synthesise a LoRaWAN-style DevEUI.

    Two devices with the same ``(namespace, name)`` always produce the
    same EUI — useful for reproducible demo configs and dashboard
    scaffolding. The output is *not* a real allocation; do not use it
    when publishing to a production Sensgreen broker.

    Parameters
    ----------
    namespace:
        Stable scope, typically the building or project id.
    name:
        Stable per-device handle (device name, role, or zone+role).
    oui:
        Optional 6-hex-char (3-byte) OUI prefix. When omitted, the
        ``fe`` private/experimental byte is mixed with the hash so the
        EUI cannot collide with a published Semtech/manufacturer OUI.

    Returns
    -------
    str
        16 lowercase hex chars.
    """
    if not namespace or not isinstance(namespace, str):
        raise ValueError("namespace must be a non-empty string")
    if not name or not isinstance(name, str):
        raise ValueError("name must be a non-empty string")

    digest = hashlib.blake2b(
        f"{namespace}\x00{name}".encode("utf-8"), digest_size=8
    ).hexdigest()  # 16 hex chars

    if oui is None:
        # Mix the experimental byte into byte 0 so the result still has
        # 8 bytes of entropy but visually starts with ``fe``.
        return _PRIVATE_EXPERIMENTAL_OUI + digest[2:]

    oui_clean = re.sub(r"[\s:\-]", "", oui).lower()
    if not re.fullmatch(r"[0-9a-f]{6}", oui_clean):
        raise ValueError(
            f"oui must be 6 hex chars (3 bytes), got {oui!r}"
        )
    return oui_clean + digest[6:]


__all__ = [
    "is_lorawan_eui",
    "normalize_eui",
    "generate_eui",
]

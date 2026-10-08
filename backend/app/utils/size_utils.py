"""File-size helpers.

The whole application uses DECIMAL units:  1 KB = 1,000 bytes, 1 MB = 1,000,000 bytes,
1 GB = 1,000,000,000 bytes.  Decimal is the stricter interpretation of a client limit
(25 MB = 25,000,000 bytes < 25 MiB = 26,214,400 bytes), so a volume that passes here also
passes a portal that measures in binary units.
"""
from __future__ import annotations

UNIT_BYTES = {"KB": 1_000, "MB": 1_000_000, "GB": 1_000_000_000}
UNIT_NOTE = "1 MB = 1,000,000 bytes (decimal units are used throughout)"


def to_bytes(value: float, unit: str = "MB") -> int:
    unit = (unit or "MB").upper()
    if unit not in UNIT_BYTES:
        raise ValueError(f"Unsupported size unit: {unit!r}. Use KB, MB or GB.")
    if value is None or value <= 0:
        raise ValueError("Size must be greater than zero.")
    return int(round(float(value) * UNIT_BYTES[unit]))


def format_size(num_bytes: int | float | None) -> str:
    if num_bytes is None:
        return "-"
    num_bytes = float(num_bytes)
    if num_bytes >= 1_000_000_000:
        return f"{num_bytes / 1_000_000_000:.2f} GB"
    if num_bytes >= 1_000_000:
        return f"{num_bytes / 1_000_000:.1f} MB"
    if num_bytes >= 1_000:
        return f"{num_bytes / 1_000:.0f} KB"
    return f"{int(num_bytes)} bytes"


def format_bytes_exact(num_bytes: int) -> str:
    return f"{int(num_bytes):,} bytes"

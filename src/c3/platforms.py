"""Platform selection helpers for C3 CLI commands."""

from __future__ import annotations

SUPPORTED_PLATFORMS = ("claude", "codex", "cursor")
PLATFORM_CHOICES = (*SUPPORTED_PLATFORMS, "all")


def expand_platforms(value: str | None) -> tuple[str, ...]:
    """Return the concrete platform list for a CLI ``--platform`` value."""
    if value is None or value == "claude":
        return ("claude",)
    if value == "all":
        return SUPPORTED_PLATFORMS
    if value in SUPPORTED_PLATFORMS:
        return (value,)
    raise ValueError(f"unknown platform: {value}")

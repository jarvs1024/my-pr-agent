"""Notifier protocol — abstracts delivery so other IM channels can plug in.

A notifier consumes a list of markdown chunks (already pre-split by the
renderer) and is responsible for delivery, retries, and recording
delivery failures in a way the scheduler can audit.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass
class DeliveryResult:
    """Outcome of a single notifier ``send`` call."""

    success: bool
    chunks_sent: int = 0
    chunks_total: int = 0
    error: str | None = None
    meta: dict[str, object] = field(default_factory=dict)


@runtime_checkable
class Notifier(Protocol):
    """Protocol every notifier must satisfy."""

    name: str

    def send(self, title: str, markdown_chunks: list[str]) -> DeliveryResult: ...


__all__ = ["DeliveryResult", "Notifier"]

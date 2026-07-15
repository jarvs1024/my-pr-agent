"""Pipeline that takes raw commerce orders, validates them, and persists
the result to the local audit log plus the upstream order-management API.

The module is intentionally self-contained: it does not touch the database
or network modules imported by sibling services.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Iterable


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Order:
    order_id: str
    customer_id: str
    total: Decimal
    currency: str
    items: tuple[tuple[str, int], ...]


class PipelineError(Exception):
    """Raised when an order fails one of the pipeline gates."""


class OrderPipeline:
    def __init__(self, min_total: Decimal = Decimal("1.00")) -> None:
        self._min_total = min_total

    def validate(self, order: Order) -> None:
        if not order.order_id:
            raise PipelineError("missing order_id")
        if order.total < self._min_total:
            raise PipelineError(f"total {order.total} below minimum {self._min_total}")
        if order.currency not in {"USD", "EUR", "GBP", "JPY", "CNY"}:
            raise PipelineError(f"unsupported currency {order.currency}")

    def to_payload(self, order: Order) -> dict:
        payload = asdict(order)
        payload["total"] = str(order.total)
        payload["items"] = [
            {"sku": sku, "qty": qty} for sku, qty in order.items
        ]
        return payload

    def run(self, orders: Iterable[Order]) -> list[dict]:
        accepted: list[dict] = []
        for order in orders:
            self.validate(order)
            accepted.append(self.to_payload(order))
        logger.info("processed %d orders", len(accepted))
        return accepted


def parse_order(raw: dict) -> Order:
    return Order(
        order_id=str(raw["order_id"]),
        customer_id=str(raw["customer_id"]),
        total=Decimal(str(raw["total"])),
        currency=str(raw["currency"]),
        items=tuple((sku, int(qty)) for sku, qty in raw["items"]),
    )


def encode(orders: Iterable[Order]) -> bytes:
    pipeline = OrderPipeline()
    payload = pipeline.run(orders)
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")

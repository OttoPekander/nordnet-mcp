"""Documented limit/day order transport. Broker owns mandate and risk checks.

No retry of any mutation: missing replies remain uncertain until reconciled.
These tools must never be connected directly to an unrestricted agent.
"""
import json
import os
import re
from nordnet_mcp.models import ListingIdentity, positive_id, decimal_wire, currency_code

_client = None


def configure(client):
    global _client
    _client = client


def _require_writes():
    if os.environ.get("NORDNET_ENABLE_ORDER_WRITES") != "1":
        raise PermissionError("Order writes disabled; trusted broker dispatch must explicitly enable them")


async def create_limit_order(account_id: int, market_id: int, identifier: str, side: str, volume: str, price: str, currency: str, reference: str):
    _require_writes()
    identity = ListingIdentity(market_id, identifier)
    if side not in ("BUY", "SELL"):
        raise ValueError("side must be BUY or SELL")
    if not isinstance(reference, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,64}", reference):
        raise ValueError("reference must identify the already-recorded broker child")
    fields = {"market_id": str(identity.market_id), "identifier": identity.identifier, "side": side,
              "volume": decimal_wire(volume, whole=True), "price": decimal_wire(price),
              "currency": currency_code(currency), "reference": reference, "order_type": "LIMIT", "extended_hours": "false"}
    return await _client.order_mutation("POST", f"/accounts/{positive_id(account_id)}/orders", fields)


async def modify_limit_order(account_id: int, order_id: int, volume: str, price: str, currency: str):
    _require_writes()
    fields = {"volume": decimal_wire(volume, whole=True), "price": decimal_wire(price), "currency": currency_code(currency)}
    return await _client.order_mutation("PUT", f"/accounts/{positive_id(account_id)}/orders/{positive_id(order_id)}", fields)


async def cancel_order(account_id: int, order_id: int):
    _require_writes()
    return await _client.order_mutation("DELETE", f"/accounts/{positive_id(account_id)}/orders/{positive_id(order_id)}")


async def inspect_orders(account_id: int):
    # Include deleted-today orders so cancel/fill reconciliation does not lose them.
    return await _client.get(f"/accounts/{positive_id(account_id)}/orders", params={"deleted": "true"})


def register_tools(app):
    @app.tool()
    async def inspect_account_orders(account_id: int) -> str:
        """Read authoritative active and deleted-today orders without flattening states."""
        return json.dumps(await inspect_orders(account_id), indent=2)

    # Financial writes are private Python operations called only by the trusted
    # broker worker. They are deliberately absent from public MCP discovery.
    return app

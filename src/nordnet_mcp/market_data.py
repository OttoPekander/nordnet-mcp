"""Fixed snapshot reads evidenced in Nordnet's public web reducer bundle.

HTTP arrival is not a provider timestamp. No stale snapshot is cached or treated
as a reconstructed streaming book. Raw fields remain available for live contract
characterization; unknown entitlement, phase or timestamps block execution.
"""
import asyncio
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import httpx
from nordnet_mcp.models import ListingIdentity

_client = None


def configure(client):
    global _client
    _client = client


async def _read(path):
    try:
        return {"status": "observed", "data": await _client.get(path, decimal_strings=True), "path": path}
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code not in (403, 404, 405):
            raise
        return {"status": "unsupported", "http_status": exc.response.status_code, "path": path}


def _snapshot(observation, identity):
    data = observation.get("data")
    if isinstance(data, list) and len(data) == 1:
        data = data[0]
    if not isinstance(data, dict) or data.get("market_id") != identity.market_id or str(data.get("identifier")) != identity.identifier:
        return None
    return data


def _number(value, *, positive=False):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    if not number.is_finite() or number < 0 or (positive and number == 0):
        return None
    return format(number, "f")


def _normalize(observations, identity):
    depth = _snapshot(observations["depth"], identity)
    phase = _snapshot(observations["trading_status"], identity)
    price = _snapshot(observations["price"], identity)
    levels = {"bids": [], "asks": []}
    if depth:
        for side, name in (("bid", "bids"), ("ask", "asks")):
            for level in range(1, 6):
                amount = _number(depth.get(f"{side}{level}"), positive=True)
                quantity = _number(depth.get(f"{side}_volume{level}"), positive=True)
                if amount is not None and quantity is not None:
                    levels[name].append({"level": level, "price": amount, "quantity": quantity,
                                         "order_count": _number(depth.get(f"{side}_orders{level}"))})
    raw_halted = phase.get("halted") if phase else None
    no_halted_reason = raw_halted is None or raw_halted is False or raw_halted in ("", "false")
    return {
        "identity_matched": {"depth": depth is not None, "price": price is not None, "trading_status": phase is not None},
        "book": levels,
        "depth_source_timestamp_ms": depth.get("tick_timestamp") if depth else None,
        "price_source_timestamp_ms": price.get("tick_timestamp") if price else None,
        "phase_source_timestamp_ms": phase.get("tick_timestamp") if phase else None,
        "delay_seconds": depth.get("delay") if depth else None,
        "phase": phase.get("orderbook_status", "UNKNOWN") if phase else "UNKNOWN",
        "provider_phase_code": phase.get("status") if phase else None,
        "halted": raw_halted,
        "continuous_observed": bool(phase and phase.get("status") == "C" and phase.get("orderbook_status") == "CONTINUOUS_TRADING" and no_halted_reason),
        "contract_verified": False,
    }


async def listing_rules(identity):
    """Bind public tick and lot rules to a uniquely resolved tradable."""
    observed = await _read(f"/instruments/lookup/market_id_identifier/{identity.key}")
    rows = observed.get("data")
    matches = []
    for instrument in rows if isinstance(rows, list) else []:
        for tradable in instrument.get("tradables", []) if isinstance(instrument, dict) else []:
            if isinstance(tradable, dict) and tradable.get("market_id") == identity.market_id and str(tradable.get("identifier")) == identity.identifier:
                matches.append((instrument, tradable))
    if len(matches) != 1:
        return {"status": "unverified", "reason": "listing_identity_unverified"}
    instrument, tradable = matches[0]
    tick_id = tradable.get("tick_size_id")
    lot = _number(tradable.get("lot_size"), positive=True)
    if type(tick_id) is not int or tick_id <= 0 or lot is None or Decimal(lot) != Decimal(lot).to_integral_value():
        return {"status": "unverified", "reason": "listing_rules_unverified"}
    ticks = await _read(f"/tick_sizes/{tick_id}")
    schedules = ticks.get("data")
    schedules = [row for row in schedules if isinstance(row, dict) and row.get("tick_size_id") == tick_id] if isinstance(schedules, list) else []
    if len(schedules) != 1 or not isinstance(schedules[0].get("ticks"), list) or not 1 <= len(schedules[0]["ticks"]) <= 64:
        return {"status": "unverified", "reason": "tick_schedule_unverified"}
    bands = []
    for band in schedules[0]["ticks"]:
        if not isinstance(band, dict):
            return {"status": "unverified", "reason": "tick_schedule_unverified"}
        lower, upper, tick = _number(band.get("from_price")), _number(band.get("to_price")), _number(band.get("tick"), positive=True)
        if None in (lower, upper, tick) or Decimal(lower) > Decimal(upper):
            return {"status": "unverified", "reason": "tick_schedule_unverified"}
        bands.append({"from_price": lower, "to_price": upper, "tick": tick})
    bands.sort(key=lambda band: Decimal(band["from_price"]))
    if any(Decimal(a["to_price"]) >= Decimal(b["from_price"]) for a, b in zip(bands, bands[1:])):
        return {"status": "unverified", "reason": "tick_schedule_ambiguous"}
    return {"status": "observed", "source": "nordnet_listing_lookup_and_tick_sizes", "listing": {
        "instrument_id": str(instrument.get("instrument_id")), "market_id": identity.market_id,
        "identifier": identity.identifier, "mic": tradable.get("mic"), "isin": instrument.get("isin_code"),
        "currency": instrument.get("currency"), "price_unit": tradable.get("price_unit"), "lot_size": int(Decimal(lot)),
    }, "tick_size_id": tick_id, "tick_bands": bands, "execution_ready": False}


async def listing_market_data(market_id: int, identifier: str):
    listing = ListingIdentity(market_id, identifier)
    paths = {name: f"/tradables/{name}/{listing.key}" for name in ("depth", "price", "trading_status")}
    values = await asyncio.gather(*(_read(path) for path in paths.values()), listing_rules(listing))
    observations = dict(zip(paths, values[:len(paths)]))
    return {
        "normalized": _normalize(observations, listing),
        "listing_rules": values[-1],
        "listing": {"market_id": market_id, "identifier": identifier},
        "source": "nordnet_web_rest_snapshot",
        "received_at": datetime.now(timezone.utc).isoformat(),
        "observations": observations,
        "capabilities": {
            "depth_contract": "web_snapshot_up_to_five_levels",
            "authenticated_contract_verified": False,
            "streaming": False,
            "phase_mapping_verified": False,
            "freshness_verified": False,
            "entitlement_verified": False,
            "execution_ready": False,
        },
        "hold_reasons": ["authenticated_contract_characterization_required"],
    }


def register_tools(app):
    @app.tool()
    async def get_listing_market_data(market_id: int, identifier: str) -> str:
        """Read exact listing depth, price and phase snapshots with source metadata.

        Unknown freshness/entitlement/phase remains an execution hold. This is
        an observation surface, never a claim that a market is safe to trade.
        """
        return json.dumps(await listing_market_data(market_id, identifier), indent=2)

    @app.tool()
    async def get_listing_trading_info(market_id: int, identifier: str) -> str:
        """Read provider calendar/order-type metadata for one exact listing."""
        identity = ListingIdentity(market_id, identifier)
        return json.dumps(await _client.get(f"/tradables/info/{identity.key}"), indent=2)

    @app.tool()
    async def get_market_data_entitlements() -> str:
        """Observe web-session realtime-access metadata; no assumed entitlements."""
        return json.dumps(await _read("/realtime_access"), indent=2)

    return app

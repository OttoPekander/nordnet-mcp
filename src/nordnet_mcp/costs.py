"""Account/listing-specific preliminary costs. Estimates are never cost bounds."""
from datetime import datetime, timezone
from decimal import Decimal

import httpx

from nordnet_mcp.models import ListingIdentity, positive_id, decimal_wire, currency_code


async def estimate(client, account_id: int, market_id: int, identifier: str,
                   side: str, volume: str, price: str):
    account_id = positive_id(account_id)
    identity = ListingIdentity(market_id, identifier)
    if side not in ("BUY", "SELL"):
        raise ValueError("side must be BUY or SELL")
    quantity, limit = Decimal(decimal_wire(volume, whole=True)), Decimal(decimal_wire(price))
    rows = await client.get(f"/instruments/lookup/market_id_identifier/{identity.key}", decimal_strings=True)
    if not isinstance(rows, list):
        raise ValueError("listing response unavailable")
    matched = [(instrument, listing) for instrument in rows if isinstance(instrument, dict)
               for listing in instrument.get("tradables", []) if isinstance(listing, dict)
               and listing.get("market_id") == market_id and str(listing.get("identifier")) == identifier]
    if len(matched) != 1:
        raise ValueError("listing identity unavailable")
    instrument, listing = matched[0]
    currency = currency_code(instrument.get("currency"))
    if (instrument.get("asset_class") != "EQY" or instrument.get("instrument_type") != "ESH" or
            Decimal(str(instrument.get("multiplier"))) != 1 or listing.get("price_unit") != currency):
        raise ValueError("only cash-share currency prices are supported")
    lot = Decimal(decimal_wire(str(listing.get("lot_size")), whole=True))
    if quantity % lot:
        raise ValueError("quantity violates listing lot size")
    instrument_id = positive_id(instrument.get("instrument_id"))
    trade_amount = {"total": {"value": quantity * limit, "currency": currency},
                    "qty": quantity, "price": {"value": limit, "currency": currency}}
    fields = {"accid": account_id, "order_type": "LIMIT", "market_id": market_id}
    if side == "BUY":
        fields.update(currency=currency, instrument_id=instrument_id, trade_amount=trade_amount)
    else:
        fields["liquidation_specifications"] = [{"instrument_id": instrument_id,
                                                 "result_currency": currency, "trade_amount": trade_amount}]
    result = {"listing": {"market_id": market_id, "identifier": identifier},
              "instrument_id": str(instrument_id), "side": side, "volume": format(quantity, "f"),
              "price": format(limit, "f"), "currency": currency, "order_type": "LIMIT",
              "estimated": True, "cost_bound_verified": False, "execution_ready": False}
    try:
        result.update(status="observed", data=await client.fee_estimate(side, account_id, fields))
    except httpx.HTTPStatusError as error:
        if error.response.status_code not in (403, 404, 405):
            raise
        result.update(status="unsupported", http_status=error.response.status_code)
    if result["status"] == "observed":
        data = result["data"]
        try:
            if side == "BUY":
                charges = {key: data[key] for key in ("commission", "fx", "total")}
                uncertain = data.get("high_uncertainty") is not False
            else:
                if not isinstance(data, list) or len(data) != 1 or data[0].get("instrument_id") != instrument_id:
                    raise ValueError("estimate instrument mismatch")
                row = data[0]
                charges = {key: row["nordnet_charges"]["transactional"][key]["amount"] for key in ("commission", "fx")}
                charges["total"] = row["total"]["amount"]
                uncertain = any(row[name].get("high_uncertainty") is not False
                                for name in ("nordnet_charges", "instrument_charges", "kickback"))
            normalized = {}
            for key, amount in charges.items():
                if amount.get("currency") != currency or isinstance(amount.get("value"), (float, bool)):
                    raise ValueError("estimate currency or decimal mismatch")
                value = Decimal(amount["value"])
                if not value.is_finite() or value < 0:
                    raise ValueError("invalid estimate cost")
                normalized[key] = format(value, "f")
            if Decimal(normalized["total"]) < Decimal(normalized["commission"]) + Decimal(normalized["fx"]):
                raise ValueError("estimate totals inconsistent")
            principal = quantity * limit
            result["summary"] = {**normalized, "currency": currency, "high_uncertainty": uncertain,
                                 "principal": format(principal, "f"),
                                 "estimated_cash_amount": format(principal + Decimal(normalized["total"]) * (1 if side == "BUY" else -1), "f")}
        except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError):
            result["normalization_reason"] = "estimate_charge_contract_unverified"
    if currency == "EUR":
        result["eur_conversion"] = {"rate": "1", "source": "currency_identity", "freshness_required": False}
    else:
        try:
            rate = await client.get(f"/exchange_rates/{currency}/EUR", decimal_strings=True)
        except (httpx.RequestError, httpx.HTTPStatusError):
            result["fx_reason"] = "directional_fx_unavailable"
            rate = None
        try:
            if rate is None:
                raise ValueError("FX rate unavailable")
            if rate.get("from") != currency or rate.get("to") != "EUR":
                raise ValueError("FX direction mismatch")
            result["eur_conversion"] = {"rate": decimal_wire(str(rate["value"])),
                                        "source": "preliminary_provider_rate", "freshness_verified": False}
        except (ValueError, TypeError, KeyError, AttributeError):
            result.setdefault("fx_reason", "directional_fx_contract_unverified")
    result["received_at"] = datetime.now(timezone.utc).isoformat()
    return result

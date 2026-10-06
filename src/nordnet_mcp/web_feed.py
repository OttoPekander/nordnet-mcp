"""Bounded observation of Nordnet's public web feed, never execution authority.

Only the fixed QR-session market hosts are accepted. Each call opens a new
connection, requires the login/subscription acknowledgements, reconstructs
identity-matched initial snapshots and deltas, then closes without reconnecting.
Provider clocks and delay fields are retained; receipt/liveness never means fresh.
"""
import asyncio
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import time

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake

CHANNELS = ("depth", "price", "trading_status")
MARKET_HOSTS = frozenset(("www.nordnet.fi", "www.nordnet.se", "www.nordnet.no", "www.nordnet.dk"))
MAX_MESSAGES = 64
MAX_FRAME_BYTES = 262144
DEADLINE_SECONDS = 4


def _identity(data, listing):
    return (isinstance(data, dict) and type(data.get("m")) is int
            and data["m"] == listing.market_id and str(data.get("i")) == listing.identifier)


def _timestamp(value):
    # Observed epoch milliseconds; do not guess seconds or repair malformed clocks.
    if type(value) is not int or not 946684800000 <= value <= int(time.time() * 1000) + 5000:
        return None
    return value


def _numeric_fields(kind, data):
    if kind == "depth":
        names = {f"{side}{level}" for side in ("bid", "ask") for level in range(1, 6)}
        names |= {f"{side}_{field}{level}" for side in ("bid", "ask")
                  for field in ("volume", "orders") for level in range(1, 6)}
    else:
        names = {"bid", "ask", "last", "close", "delayed", "delay"}
    for name in names & data.keys():
        value = data[name]
        if isinstance(value, bool) or value is None:
            return False
        try:
            number = Decimal(str(value))
        except InvalidOperation:
            return False
        if not number.is_finite() or number < 0:
            return False
    return True


def _initial(kind, data):
    if kind == "depth":
        levels = data.get("levels", 5)
        if type(levels) is not int or not 1 <= levels <= 5:
            return False
        return all(f"{side}{level}" in data and f"{side}_volume{level}" in data
                   for side in ("bid", "ask") for level in range(1, levels + 1))
    if kind == "price":
        return "bid" in data and "ask" in data
    return "status" in data


def _command_channel(data, listing, *, error=False):
    command = data.get("cmd") if isinstance(data, dict) else None
    if error:
        data = command if isinstance(command, dict) else {}
        command = data.get("cmd")
    args = data.get("args") if isinstance(data, dict) else None
    if command == "subscribe" and _identity(args, listing) and args.get("t") in CHANNELS:
        return args["t"]
    return None


async def observe(client, listing):
    states = {kind: {"status": "unavailable", "reason": "subscription_not_acknowledged"}
              for kind in CHANNELS}
    result = {"source": "nordnet_public_web_feed", "channels": states,
              "connected": False, "connection_closed": True,
              "provider_timestamps_are_receipt_times": False,
              "full_exchange_depth_verified": False, "entitlement_verified": False,
              "freshness_verified": False, "execution_ready": False}
    host = client.base_url.split("/", 3)[2]
    if host not in MARKET_HOSTS or client.client_id != "NEXT" or not client.session_token:
        result["reason"] = "web_session_required"
        return result
    acknowledged = set()
    snapshots = {}
    try:
        async with asyncio.timeout(DEADLINE_SECONDS):
            async with connect(
                f"wss://{host}/ws/2/public", subprotocols=["NEXT"],
                origin=f"https://{host}",
                additional_headers={"Cookie": f"NNX_SESSION_ID={client.session_token}",
                                    "client-id": "NEXT", "User-Agent": "Mozilla/5.0"},
                open_timeout=3, close_timeout=0.25, max_size=MAX_FRAME_BYTES,
                max_queue=8, ping_interval=None,
            ) as socket:
                login = json.loads(await socket.recv(), parse_float=str)
                if (not isinstance(login, dict) or login.get("type") != "ack"
                        or not isinstance(login.get("data"), dict)
                        or login["data"].get("cmd") != "login"):
                    result["reason"] = "login_acknowledgement_missing"
                    return result
                result["connected"] = True
                for kind in CHANNELS:
                    await socket.send(json.dumps({"cmd": "subscribe", "args": {
                        "t": kind, "m": listing.market_id, "i": listing.identifier}}))
                for _ in range(MAX_MESSAGES):
                    settled = all(state["status"] in ("observed", "unsupported") for state in states.values())
                    try:
                        raw = await asyncio.wait_for(socket.recv(), 0.25) if settled else await socket.recv()
                    except TimeoutError:
                        result["reason"] = "bounded_snapshot_received"
                        break
                    frame = json.loads(raw, parse_float=str)
                    if not isinstance(frame, dict):
                        raise ValueError("malformed feed frame")
                    kind, data = frame.get("type"), frame.get("data")
                    if kind == "ack":
                        channel = _command_channel(data, listing)
                        if channel:
                            acknowledged.add(channel)
                            states[channel] = {"status": "unavailable", "reason": "initial_snapshot_missing"}
                    elif kind == "err":
                        channel = _command_channel(data, listing, error=True)
                        if channel:
                            acknowledged.discard(channel)
                            snapshots.pop(channel, None)
                            states[channel] = {"status": "unsupported", "reason": "provider_subscription_rejected"}
                    elif kind in CHANNELS:
                        if kind not in acknowledged or not _identity(data, listing):
                            continue
                        timestamp = _timestamp(data.get("tick_timestamp"))
                        previous = snapshots.get(kind)
                        trade_timestamp = data.get("trade_timestamp")
                        invalid_trade_time = ("trade_timestamp" in data and (
                            _timestamp(trade_timestamp) is None or (
                                previous and previous.get("trade_timestamp") is not None
                                and trade_timestamp < previous["trade_timestamp"])))
                        if timestamp is None or invalid_trade_time or (previous and timestamp < previous["tick_timestamp"]):
                            snapshots.pop(kind, None)
                            acknowledged.discard(kind)
                            states[kind] = {"status": "unavailable", "reason": "provider_timestamp_invalid"}
                            continue
                        if not _numeric_fields(kind, data):
                            snapshots.pop(kind, None)
                            acknowledged.discard(kind)
                            states[kind] = {"status": "unavailable", "reason": "provider_values_invalid"}
                            continue
                        if previous is None and not _initial(kind, data):
                            states[kind] = {"status": "unavailable", "reason": "initial_snapshot_incomplete"}
                            continue
                        snapshot = {**(previous or {}), **data}
                        snapshots[kind] = snapshot
                        states[kind] = {"status": "observed", "data": snapshot,
                                        "received_at": datetime.now(timezone.utc).isoformat(),
                                        "initial_snapshot_received": True,
                                        "provider_timestamp_ms": timestamp,
                                        "source_age_ms": int(time.time() * 1000) - timestamp}
                else:
                    result["reason"] = "message_budget_exhausted"
    except TimeoutError:
        # A bounded intentional observation ending is distinct from transport loss.
        result["reason"] = "observation_deadline_reached"
    except (ConnectionClosed, InvalidHandshake, OSError):
        result["reason"] = "feed_connection_unavailable"
        for kind in snapshots:
            states[kind] = {"status": "unavailable", "reason": "feed_disconnected"}
    except (ValueError, TypeError):
        result["reason"] = "feed_protocol_invalid"
        for kind in snapshots:
            states[kind] = {"status": "unavailable", "reason": "feed_protocol_invalid"}
    return result

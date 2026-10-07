"""Private broker-only worker. Never expose this port through a public proxy.

Session material travels only over the private broker boundary for encrypted
vault checkpointing. Public MCP discovery does not expose restore or export.
"""
import asyncio
from contextlib import asynccontextmanager
import hmac
import json
import os
import time
from pathlib import Path

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from nordnet_mcp import create_app, auth, instruments, market_data, orders, portfolio, costs
from nordnet_mcp.client import SessionExpiredError
from nordnet_mcp.models import ListingIdentity, positive_id


_READS = {
    "accounts": lambda a: ("/accounts", None),
    "account_info": lambda a: (f"/accounts/{positive_id(a['account_id'])}/info", None),
    "positions": lambda a: (f"/accounts/{positive_id(a['account_id'])}/positions", None),
    "ledgers": lambda a: (f"/accounts/{positive_id(a['account_id'])}/ledgers", None),
    "orders": lambda a: (f"/accounts/{positive_id(a['account_id'])}/orders", {"deleted": "true"}),
    "trades": lambda a: (f"/accounts/{positive_id(a['account_id'])}/trades", {"days": _days(a.get("days", 0))}),
    "resolve_listing": lambda a: (f"/instruments/lookup/market_id_identifier/{ListingIdentity(**a).key}", None),
    "trading_info": lambda a: (f"/tradables/info/{ListingIdentity(**a).key}", None),
    "entitlements": lambda a: ("/realtime_access", None),
    "fx_rates": lambda a: ("/exchange_rates", None),
    "fx_rate": lambda a: (f"/exchange_rates/{_currency(a['from_currency'])}/{_currency(a['to_currency'])}", None),
    "commission_model": lambda a: ("/customers/commission/price_model", {"include_models": "true"}),
}
_ALLOWED_ARGUMENTS = {
    "epoch": set(), "status": set(), "checkpoint": set(), "disconnect": set(), "auth_start": set(), "auth_poll": set(),
    "restore": {"session_token", "client_id", "market", "excluded_account_numbers"}, "accounts": set(), "entitlements": set(),
    "fx_rates": set(), "fx_rate": {"from_currency", "to_currency"}, "commission_model": set(),
    "transaction_history": {"account_id", "days", "from_date", "to_date", "offset"},
    "portfolio": {"account_id"},
    "performance_history": {"account_id", "from_date", "to_date"},
    "portfolio_metrics": {"account_id", "period"},
    "cost_estimate": {"account_id", "market_id", "identifier", "side", "volume", "price"},
    "account_info": {"account_id"}, "positions": {"account_id"}, "ledgers": {"account_id"},
    "orders": {"account_id"}, "trades": {"account_id", "days"},
    "resolve_listing": {"market_id", "identifier"}, "market_data": {"market_id", "identifier"},
    "trading_info": {"market_id", "identifier"}, "search": {"query", "limit"}, "tick_sizes": {"tick_size_id"},
    "create_limit_order": {"account_id", "market_id", "identifier", "side", "volume", "price", "currency", "reference"},
    "modify_limit_order": {"account_id", "order_id", "volume", "price", "currency"},
    "cancel_order": {"account_id", "order_id"},
}


_READ_BUDGETS = {**{operation: 8 for operation in _READS},
                 'status': 8, 'market_data': 8, 'search': 8, 'tick_sizes': 8,
                 'epoch': 8, 'checkpoint': 8, 'transaction_history': 25, 'cost_estimate': 25, 'portfolio': 25,
                 'performance_history': 25, 'portfolio_metrics': 25}


def _currency(value):
    if not isinstance(value, str) or len(value) != 3 or not value.isascii() or not value.isalpha() or not value.isupper():
        raise ValueError("Currency must be a three-letter uppercase code")
    return value


def _days(value):
    if type(value) is not int or not 0 <= value <= 7:
        raise ValueError("days must be 0..7")
    return value


def _serialize(value):
    if isinstance(value, list):
        return [_serialize(item) for item in value]
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", by_alias=True, exclude_none=True)
    return value


def create_worker_app() -> Starlette:
    secret_path = Path(os.environ.get("NORDNET_BROKER_SECRET_FILE", "/run/secrets/broker-nordnet"))
    secret = secret_path.read_text().strip()
    if len(secret) < 32:
        raise RuntimeError("broker credential must contain at least 32 characters")
    mcp = create_app()
    lock = asyncio.Lock()
    generation = 0

    @asynccontextmanager
    async def lifespan(app):
        async with auth.lifespan(mcp):
            try:
                yield
            finally:
                await auth._client.close()

    async def perform(operation, arguments):
        if operation == 'epoch':
            return {'ready': True}
        if operation == "status":
            # Verify does not disclose session identifiers or provider account data.
            present = bool(auth._client.session_token)
            valid = await auth._verify_session(auth._client.session_token) if present else False
            return {"authenticated": valid, "market": auth._market, "order_writes_enabled": os.environ.get("NORDNET_ENABLE_ORDER_WRITES") == "1"}
        if operation == "checkpoint":
            return {"session_token": auth._client.session_token, "client_id": auth._client.client_id, "market": auth._market}
        if operation == "disconnect":
            auth._client.session_token = None
            auth._pending = None
            return {"authenticated": False}
        if operation == "restore":
            token = arguments.get("session_token")
            if not isinstance(token, str) or not 1 <= len(token) <= 8192 or any(c in token for c in "\r\n"):
                raise ValueError("invalid saved session")
            if arguments.get("client_id", "NEXT") != "NEXT" or arguments.get("market", "fi") != auth._market:
                raise ValueError("saved session market/client mismatch")
            excluded = arguments.get("excluded_account_numbers", [])
            if not isinstance(excluded, list) or any(type(n) is not int or n <= 0 for n in excluded):
                raise ValueError("invalid saved account exclusion")
            auth._client.session_token = token
            auth._client.client_id = "NEXT"
            try:
                if not await auth._has_valid_session():
                    raise SessionExpiredError("Saved Nordnet session expired")
                await auth._client.get("/accounts")
            except Exception:
                auth._client.session_token = None
                raise
            return {"authenticated": True}
        if operation in ("auth_start", "auth_poll"):
            name = "nordnet_auth" if operation == "auth_start" else "nordnet_login_poll"
            blocks = await mcp.call_tool(name, {})
            # SDK 1.x may return (content, structured_content), rather than
            # only content. Publish the bounded UI blocks, never the tuple.
            if isinstance(blocks, tuple):
                blocks = blocks[0]
            return {"content": _serialize(blocks)}
        if operation == "search":
            return await instruments.search_stock_listings(**arguments)
        if operation == "tick_sizes":
            return await auth._client.get(f"/tick_sizes/{positive_id(arguments['tick_size_id'])}", decimal_strings=True)
        if operation in _READS:
            path, params = _READS[operation](arguments)
            return await auth._client.get(path, params=params, decimal_strings=True)
        if operation == "portfolio":
            return await portfolio.snapshot(auth._client, positive_id(arguments["account_id"]))
        if operation == "performance_history":
            return await portfolio.performance_history(auth._client, positive_id(arguments["account_id"]), arguments["from_date"], arguments["to_date"])
        if operation == "portfolio_metrics":
            return await portfolio.metrics(auth._client, positive_id(arguments["account_id"]), arguments.get("period", "ALL"))
        if operation == "transaction_history":
            return await auth._client.transaction_history(positive_id(arguments["account_id"]), arguments.get("days", 7),
                from_date=arguments.get("from_date"), to_date=arguments.get("to_date"), offset=arguments.get("offset", 0))
        if operation == "cost_estimate":
            return await costs.estimate(auth._client, **arguments)
        if operation == "market_data":
            return await market_data.listing_market_data(**arguments)
        if operation == "create_limit_order":
            return await orders.create_limit_order(**arguments)
        if operation == "modify_limit_order":
            return await orders.modify_limit_order(**arguments)
        if operation == "cancel_order":
            return await orders.cancel_order(**arguments)
        raise ValueError("unsupported operation")

    async def action(request: Request):
        nonlocal generation
        supplied = request.headers.get("authorization", "")
        if not hmac.compare_digest(supplied.encode("utf-8"), ("Bearer " + secret).encode("utf-8")):
            return JSONResponse({"error": {"code": "broker_auth", "message": "Unauthorized"}}, status_code=401)
        try:
            chunks = []
            length = 0
            async for chunk in request.stream():
                length += len(chunk)
                if length > 32768:
                    return JSONResponse({"error": {"code": "invalid_request", "message": "Request too large"}}, status_code=413)
                chunks.append(chunk)
            raw = b"".join(chunks)
            payload = json.loads(raw)
            if not isinstance(payload, dict) or set(payload) not in ({"operation", "arguments", "generation"}, {"operation", "arguments", "generation", "deadline_ms"}):
                raise ValueError("invalid request fields")
            operation, arguments, requested = payload["operation"], payload["arguments"], payload["generation"]
            if operation not in _ALLOWED_ARGUMENTS or not isinstance(arguments, dict) or set(arguments) - _ALLOWED_ARGUMENTS[operation]:
                raise ValueError("invalid operation arguments")
            if type(requested) is not int or not 0 <= requested <= 2**63 - 1:
                raise ValueError("invalid generation")
            # Include time waiting for the serialized worker, so queued reads
            # cannot outlive the broker caller or start after it gives up.
            read_deadline = _READ_BUDGETS.get(operation)
            if 'deadline_ms' in payload:
                deadline = payload['deadline_ms']
                if read_deadline is None or type(deadline) is not int or not 0 < deadline <= 2**63 - 1:
                    raise ValueError('Only read operations accept a deadline')
                remaining = (deadline - time.time() * 1000) / 1000
                if remaining <= 0:
                    raise TimeoutError('Read deadline elapsed')
                read_deadline = min(read_deadline, remaining)
            async with asyncio.timeout(read_deadline):
                async with lock:
                    transition = operation in ("restore", "disconnect")
                    if (transition and requested <= generation) or (not transition and requested != generation):
                        return JSONResponse({"generation": generation, "error": {"code": "stale_generation", "message": "Session generation changed"}}, status_code=409)
                    if transition:
                        # Fence even a failed restore; older traffic must stay obsolete.
                        generation = requested
                        auth._client.session_token = None
                        auth._pending = None
                    result = await perform(operation, arguments)
                    return JSONResponse({"generation": generation, "result": result})
        except SessionExpiredError:
            return JSONResponse({"generation": generation, "error": {"code": "session_expired", "message": "Nordnet authentication required"}}, status_code=401)
        except PermissionError:
            return JSONResponse({"generation": generation, "error": {"code": "access_denied", "message": "Account access or trading writes are disabled"}}, status_code=403)
        except (ValueError, KeyError, TypeError):
            return JSONResponse({"generation": generation, "error": {"code": "invalid_request", "message": "Invalid worker operation arguments"}}, status_code=400)
        except httpx.HTTPStatusError as exc:
            return JSONResponse({"generation": generation, "error": {"code": "provider_http", "message": "Nordnet rejected the request", "http_status": exc.response.status_code}}, status_code=502)
        except (httpx.RequestError, TimeoutError):
            return JSONResponse({"generation": generation, "error": {"code": "provider_unavailable", "message": "Nordnet transport unavailable"}}, status_code=503)
        except Exception:
            # Avoid exception text/tracebacks containing tokens or request content.
            return JSONResponse({"generation": generation, "error": {"code": "worker_failure", "message": "Nordnet worker operation failed"}}, status_code=500)

    return Starlette(routes=[Route("/action", action, methods=["POST"])], lifespan=lifespan)


def main():
    import uvicorn
    uvicorn.run(create_worker_app(), host="0.0.0.0", port=8000, access_log=False)

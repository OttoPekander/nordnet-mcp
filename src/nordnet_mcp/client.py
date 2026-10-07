import base64
import json
from decimal import Decimal
import os
import re

import httpx


class SessionExpiredError(Exception):
    pass


class NordnetClient:
    def __init__(
        self,
        session_token: str | None,
        host: str = "public.nordnet.se",
        client_id: str | None = None,
    ):
        self.base_url = f"https://{host}/api/2"
        self.session_token = session_token
        self.client_id = client_id
        self._client = httpx.AsyncClient()
        self._allowed_account_ids: set[int] = set()
        self._account_numbers_by_id = {}
        self._history_account_ids = {}

    def _auth_header(self) -> dict:
        token = self.session_token or ""
        creds = base64.b64encode(f"{token}:{token}".encode()).decode()
        headers = {"Authorization": f"Basic {creds}"}
        # Sessions appear tied to the client that created them: QR logins
        # (created with client-id NEXT) set this on the client, manual
        # tokens opt in via NORDNET_CLIENT_ID.
        if self.client_id:
            headers["client-id"] = self.client_id
        if self.client_id == "NEXT":
            # The web client sends ntag and its session cookie on requests,
            # including mutations. Basic alone can permit reads but reject writes.
            from nordnet_mcp import auth
            headers["ntag"] = auth._ntag
            headers["Cookie"] = f"NNX_SESSION_ID={token}"
        return headers

    async def get(self, path: str, params: dict | None = None, *, decimal_strings: bool = False) -> dict | list:
        account_match = re.match(r"^/accounts/([^/]+)(?:/|$)", path)
        if account_match:
            await self._verify_account_scope(account_match.group(1))
        resp = await self._client.get(
            f"{self.base_url}{path}",
            headers=self._auth_header(),
            params=params,
        )
        if resp.status_code == 401:
            client_id_hint = ""
            # Only worth suggesting when a client ID isn't configured yet.
            if not self.client_id:
                try:
                    error = resp.json()
                except ValueError:
                    error = None
                if isinstance(error, dict) and error.get("code") == "NEXT_INVALID_SESSION":
                    client_id_hint = (
                        "\nNordnet returned NEXT_INVALID_SESSION. Session IDs appear to be "
                        "tied to the client that created them. Add NORDNET_CLIENT_ID=NEXT "
                        "to your environment and restart the server."
                    )
            raise SessionExpiredError(
                "Session expired or not yet authenticated. Call the "
                "`nordnet_auth` tool to log in via QR code — scan it "
                "with the Nordnet mobile app and the session will be "
                "established automatically, no manual token needed. "
                "Alternatively, set a token by hand: log into nordnet.se, "
                "open DevTools → Application/Storage → Cookies, find "
                "NNX_SESSION_ID and copy its value into NORDNET_SESSION_TOKEN."
                + client_id_hint
            )
        resp.raise_for_status()
        if not resp.content:
            return []
        data = resp.json(parse_float=str) if decimal_strings else resp.json()
        if path == "/accounts":
            return self._filter_accounts(data)
        return data

    def _filter_accounts(self, data):
        self._allowed_account_ids.clear()
        self._account_numbers_by_id.clear()
        self._history_account_ids.clear()
        if not isinstance(data, list):
            raise PermissionError("Account identity metadata unavailable")
        candidates = []
        identity_complete = True
        for account in data:
            if not isinstance(account, dict):
                identity_complete = False
                continue
            number, account_id, alias = account.get("accno"), account.get("accid"), account.get("alias")
            if type(number) is not int or number <= 0 or type(account_id) is not int or account_id <= 0:
                identity_complete = False
                continue
            candidates.append((number, account_id, alias, account))
        if not identity_complete:
            raise PermissionError("Stable account identities must be verified before account access")
        allowed = []
        for number, account_id, alias, account in candidates:
            if account.get("is_blocked") is True:
                continue
            if account_id in self._allowed_account_ids or number in self._account_numbers_by_id.values():
                raise PermissionError("Duplicate account identity")
            history_id = account.get("account_id")
            if ((type(history_id) is int and history_id > 0) or
                    (isinstance(history_id, str) and re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", history_id))):
                self._history_account_ids[account_id] = history_id
            self._allowed_account_ids.add(account_id)
            self._account_numbers_by_id[account_id] = number
            allowed.append({key: account[key] for key in ("accid", "accno", "alias", "type", "atyid") if key in account})
        return allowed

    async def _verify_account_scope(self, account_id):
        # Refresh the authenticated session mapping before every account request.
        if not re.fullmatch(r"[1-9][0-9]*", str(account_id)):
            raise PermissionError("Only a single verified allowed account may be accessed")
        await self.get("/accounts")
        if int(account_id) not in self._allowed_account_ids:
            raise PermissionError("Account identity is unverified or unavailable")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def close(self):
        await self._client.aclose()

    async def cloud_account_identity(self, account_id, *, unavailable_message='History account identity unavailable'):
        await self._verify_account_scope(account_id)
        key = self._history_account_ids.get(account_id)
        if key is None or list(self._history_account_ids.values()).count(key) != 1:
            raise PermissionError(unavailable_message)
        return key, self._account_numbers_by_id[int(account_id)]

    async def transaction_history(self, account_id: int, days: int = 7, *, from_date=None, to_date=None, offset=0):
        """Bounded booked-history read using the provider's separate account ID."""
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        if type(days) is not int or not 0 <= days <= 365:
            raise ValueError("history days must be 0..365")
        if type(offset) is not int or offset < 0:
            raise ValueError('history offset must be a nonnegative integer')
        if (from_date is None) != (to_date is None):
            raise ValueError('Both history dates are required')
        history_id, _ = await self.cloud_account_identity(account_id)
        today = datetime.now(ZoneInfo("Europe/Helsinki")).date()
        if from_date is not None:
            from nordnet_mcp.read_dates import read_date
            start, end = read_date(from_date), read_date(to_date)
            if start > end:
                raise ValueError('Invalid history date range')
        else:
            start, end = today - timedelta(days=days), today
        fields = {"accountIds": [history_id], "fromDate": start.isoformat(),
                  "toDate": end.isoformat(), "offset": offset, "limit": 50,
                  "sort": "ACCOUNTING_DATE", "sortOrder": "DESC", "includeCancellations": True}
        path = "/transaction/transaction-and-notes/v2/transactions/page"
        from nordnet_mcp.web_auth import authorization_token
        market = self.base_url.removesuffix("/api/2").rsplit(".", 1)[-1]
        jwt = await authorization_token(self.session_token, market)
        headers = {"Accept": "application/json", "Authorization": "Bearer " + jwt,
                   "x-locale": {"fi": "fi-FI", "se": "sv-SE", "no": "nb-NO", "dk": "da-DK"}[market]}
        # Nordnet's frontend selects this fixed cloud host. Never send its
        # separate JWT to a host or path supplied by model arguments.
        async with httpx.AsyncClient(timeout=15) as cloud:
            response = await cloud.post("https://api.prod.nntech.io" + path, headers=headers, json=fields)
            summary = await cloud.post("https://api.prod.nntech.io/transaction/transaction-and-notes/v2/transaction-summary",
                                       headers=headers, json={key: fields[key] for key in
                                       ("accountIds", "fromDate", "toDate", "includeCancellations")})
        if response.status_code == 401:
            raise SessionExpiredError("Nordnet authentication required")
        response.raise_for_status()
        rows = response.json(parse_float=str)
        if not isinstance(rows, list) or len(rows) > 50:
            raise ValueError("history response contract unavailable")
        if summary.status_code == 401:
            raise SessionExpiredError("Nordnet authentication required")
        summary.raise_for_status()
        total = summary.json().get("totalNumberOfTransactions")
        if type(total) is not int or total < offset + len(rows):
            raise ValueError("history coverage contract unavailable")
        stable = str(self._account_numbers_by_id[int(account_id)])
        if any(not isinstance(row, dict) or str(row.get("accountNumber")) != stable for row in rows):
            raise PermissionError("History account identity mismatch")
        return {"status": "observed", "data": rows, "from_date": fields["fromDate"], "to_date": fields["toDate"],
                "offset": offset, "limit": 50, "total_transactions": total, "range_complete": offset == 0 and total == len(rows),
                "next_offset": offset + len(rows) if rows and offset + len(rows) < total else None,
                "paging_atomic": False, "order_link_verified": False,
                "tax_reference_fx_is_execution_fx": False}

    async def fee_estimate(self, side: str, account_id: int, fields: dict):
        """Fixed read-estimate POSTs, independent of the order-write transport."""
        paths = {"BUY": "/fees/commission_approximation", "SELL": "/fees/liquidation_approximation"}
        if side not in paths or fields.get("accid") != account_id:
            raise ValueError("invalid fee estimate identity")
        await self._verify_account_scope(account_id)

        def encode(value):
            # Decimal amounts are exact JSON numeric literals, never binary floats.
            if isinstance(value, Decimal):
                if not value.is_finite():
                    raise ValueError("invalid estimate amount")
                return format(value, "f")
            if isinstance(value, dict):
                return "{" + ",".join(json.dumps(k) + ":" + encode(v) for k, v in value.items()) + "}"
            if isinstance(value, list):
                return "[" + ",".join(encode(v) for v in value) + "]"
            if isinstance(value, float):
                raise ValueError("floating point estimate amount")
            return json.dumps(value, allow_nan=False)

        response = await self._client.post(
            self.base_url + paths[side], headers={**self._auth_header(), "Content-Type": "application/json"},
            content=encode(fields).encode(),
        )
        if response.status_code == 401:
            raise SessionExpiredError("Nordnet authentication required")
        response.raise_for_status()
        return response.json(parse_float=str)

    async def order_mutation(self, method: str, path: str, fields: dict | None = None) -> dict:
        """Single attempt. Uncertain outcomes must be reconciled, never retried."""
        if os.environ.get('NORDNET_ENABLE_ORDER_WRITES') != '1':
            raise PermissionError('Financial writes are disabled')
        if method not in ("POST", "PUT", "DELETE"):
            raise ValueError("unsupported order mutation")
        match = re.fullmatch(r"/accounts/([1-9][0-9]*)/orders(?:/[1-9][0-9]*)?", path)
        if not match:
            raise ValueError("unsupported order path")
        mode = os.environ.get('NORDNET_ORDER_EXECUTION_MODE', 'disabled')
        if mode not in ('acceptance', 'production'):
            raise PermissionError('Financial execution mode is disabled')
        await self._verify_account_scope(match.group(1))
        permit = None
        if mode == 'acceptance':
            from nordnet_mcp.acceptance import authorize
            permit = authorize(method, self._account_numbers_by_id[int(match.group(1))], path, fields)
        try:
            response = await self._client.request(
                method, f"{self.base_url}{path}", headers=self._auth_header(), data=fields,
            )
        except httpx.RequestError:
            return {"outcome": "uncertain", "reason": "transport_failure", "retry_safe": False}
        # A gateway/server failure may follow acceptance by Nordnet.
        if response.status_code >= 500:
            return {"outcome": "uncertain", "reason": "provider_server_failure", "http_status": response.status_code, "retry_safe": False}
        if response.status_code == 401:
            raise SessionExpiredError("Nordnet session expired")
        if response.status_code == 403:
            # Documented permission denial is a rejection, including a scalar
            # or list error reply. Keep bounded sanitized detail for diagnosis.
            detail = response.text[:4096].replace(self.session_token or "__absent_token__", "<redacted>")
            if permit is not None and method == 'POST':
                from nordnet_mcp.acceptance import record_attempt
                record_attempt(permit, {"permission_denial": detail}, response.status_code)
            return {"outcome": "rejected", "reason": "provider_permission_denied", "http_status": 403,
                    "provider_error": detail, "retry_safe": False}
        try:
            payload = response.json()
        except ValueError:
            return {"outcome": "uncertain", "reason": "unparseable_reply", "http_status": response.status_code, "retry_safe": False}
        if not isinstance(payload, dict):
            return {"outcome": "uncertain", "reason": "unexpected_reply", "http_status": response.status_code, "retry_safe": False}
        if permit is not None and method == 'POST':
            from nordnet_mcp.acceptance import record_attempt
            record_attempt(permit, payload, response.status_code)
        result_code = payload.get("result_code")
        if 200 <= response.status_code < 300 and result_code == "OK" and payload.get("order_id"):
            outcome = "acknowledged"  # Not a fill or even an ON_MARKET guarantee.
        elif result_code and result_code != "OK":
            outcome = "rejected"
        elif response.status_code in (400, 403, 404, 429):
            outcome = "rejected"
        else:
            outcome = "uncertain"
        return {"outcome": outcome, "http_status": response.status_code, "provider": payload, "retry_safe": False}

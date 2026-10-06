import base64
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

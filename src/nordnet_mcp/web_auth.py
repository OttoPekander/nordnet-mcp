"""Fixed Nordnet web authorization handshake; credentials stay in memory."""
import re
from html.parser import HTMLParser
from urllib.parse import urlsplit

import httpx


class CsrfInput(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tokens = []

    def handle_starttag(self, tag, attributes):
        values = dict(attributes)
        if tag == "input" and values.get("name") == "csrf-token":
            self.tokens.append(values.get("value"))


async def authorization_token(token: str, market: str) -> str:
    from nordnet_mcp.client import SessionExpiredError
    if market not in ("fi", "se", "no", "dk") or not token:
        raise ValueError("Nordnet web session unavailable")
    origin = "https://www.nordnet." + market
    # A fresh cookie jar obtains the CSRF cookie and matching hidden input
    # together. Never persist the resulting JWT or accept a caller-supplied host.
    async with httpx.AsyncClient(timeout=15) as web:
        web.cookies.set("NNX_SESSION_ID", token, domain=urlsplit(origin).hostname, path="/")
        headers = {"Accept": "application/json", "client-id": "NEXT", "User-Agent": "Mozilla/5.0",
                   "Origin": origin, "Referer": origin + "/"}
        root = await web.get(origin + "/", headers={**headers, "Accept": "text/html"})
        root.raise_for_status()
        csrf = CsrfInput()
        csrf.feed(root.text)
        if (len(csrf.tokens) != 1 or not isinstance(csrf.tokens[0], str) or
                not 1 <= len(csrf.tokens[0]) <= 8192 or any(c in csrf.tokens[0] for c in "\r\n")):
            raise ValueError("Nordnet web CSRF contract unavailable")
        response = await web.post(origin + "/nnxapi/authorization/v1/tokens",
                                  headers={**headers, "CSRF-Token": csrf.tokens[0]}, json={})
    if response.status_code == 401:
        raise SessionExpiredError("Nordnet authentication required")
    response.raise_for_status()
    jwt = response.json().get("jwt")
    if (not isinstance(jwt, str) or not 1 <= len(jwt) <= 8192 or
            not re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", jwt)):
        raise ValueError("Nordnet authorization token unavailable")
    return jwt

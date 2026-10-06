import json
from nordnet_mcp.models import ListingIdentity

_client = None


def configure(client):
    global _client
    _client = client


async def search_stock_listings(query: str, limit: int = 20) -> dict:
    """Bounded provider search; ticker/name/ISIN is never a listing identity."""
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 128 or not query.isprintable():
        raise ValueError("query must contain 1..128 printable characters")
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError("limit must be an integer from 1 to 50")
    query = query.strip()
    data = await _client.get(
        "/instrument_search/query/stocklist",
        params={"free_text_search": query, "limit": limit, "offset": 0,
                "sort_attribute": "name", "sort_order": "asc"},
        decimal_strings=True,
    )
    if not isinstance(data, dict) or not isinstance(data.get("results", []), list):
        raise ValueError("unsupported provider search response")
    # The documented service can return up to twice limit across result groups.
    return {"query": query, "source": "nordnet_stocklist_free_text_search",
            "provider": {**data, "results": data.get("results", [])[:limit * 2]},
            "execution_ready": False}


def register_tools(app):

    @app.tool()
    async def resolve_listing(market_id: int, identifier: str) -> str:
        """Resolve one exact provider listing; never choose a venue from a ticker."""
        identity = ListingIdentity(market_id, identifier)
        data = await _client.get(f"/instruments/lookup/market_id_identifier/{identity.key}")
        return json.dumps({"requested_listing": {"market_id": market_id, "identifier": identifier}, "provider": data}, indent=2)

    @app.tool()
    async def get_instrument(instrument_id: str) -> str:
        """Get instrument details. Supports comma-separated IDs for batch lookup.

        Args:
            instrument_id: One or more instrument IDs (comma-separated)
        """
        data = await _client.get(f"/instruments/{instrument_id}")
        return json.dumps(data, indent=2)

    @app.tool()
    async def lookup_instrument(lookup_type: str, lookup_value: str) -> str:
        """Find instrument by ISIN+currency+market or market identifier.

        Args:
            lookup_type: "isin_code_currency_market_id" or "market_id_identifier"
            lookup_value: For ISIN: "ISIN:CURRENCY:MARKET_ID" (e.g. "NO0010096985:NOK:15").
                          For market: "MARKET_ID:IDENTIFIER" (e.g. "15:2274236").
                          Comma-separate for batch lookup.
        """
        data = await _client.get(
            f"/instruments/lookup/{lookup_type}/{lookup_value}"
        )
        return json.dumps(data, indent=2)

    @app.tool()
    async def search_stocks(query: str, limit: int = 20) -> str:
        """Search Nordnet's stock database with free text.

        Args:
            query: Search text (company name, ticker, etc.)
            limit: Max results (default 20)
        """
        result = await search_stock_listings(query, limit)
        return json.dumps(result["provider"], indent=2)

    @app.tool()
    async def search_listings(query: str, limit: int = 20) -> str:
        """Search candidates across listings; resolve exact venue before agreement."""
        return json.dumps(await search_stock_listings(query, limit), indent=2)

    @app.tool()
    async def check_suitability(instrument_id: int) -> str:
        """Check if an instrument is tradeable for your account.

        Args:
            instrument_id: The instrument ID to check
        """
        data = await _client.get(
            f"/instruments/validation/suitability/{instrument_id}"
        )
        return json.dumps(data, indent=2)

    return app

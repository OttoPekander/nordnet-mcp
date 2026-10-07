"""Full account reads and the native web portfolio-history contracts."""
import asyncio
from datetime import datetime, timedelta, timezone

import httpx

from nordnet_mcp.client import SessionExpiredError
from nordnet_mcp.web_auth import authorization_token
from nordnet_mcp.read_dates import read_date

PERIODS = ('DAY_1', 'WEEK_1', 'MONTH_1', 'MONTH_3', 'MONTH_6', 'YTD',
           'YEAR_1', 'YEAR_3', 'YEAR_5', 'YEAR_10', 'ALL')
_FIELDS = {'d': 'date', 'rM': 'return_monetary', 'rMA': 'return_monetary_accumulated',
           'rP': 'return_percentage', 'rPA': 'return_percentage_accumulated',
           'oC': 'own_capital', 'mV': 'market_value', 'b': 'balance',
           'cNF': 'cumulative_net_flow'}


def received_at():
    return datetime.now(timezone.utc).isoformat()




async def _sections(readers):
    async def read(source, fetch):
        try:
            data = await fetch()
            return {'status': 'observed', 'data': data, 'received_at': received_at(), 'source': source}
        except (SessionExpiredError, PermissionError):
            raise
        except httpx.HTTPStatusError as error:
            return {'status': 'unavailable', 'reason': 'provider_http', 'http_status': error.response.status_code,
                    'received_at': received_at(), 'source': source}
        except (httpx.RequestError, TimeoutError):
            return {'status': 'unavailable', 'reason': 'provider_unavailable', 'received_at': received_at(), 'source': source}
        except (ValueError, TypeError, KeyError):
            return {'status': 'unavailable', 'reason': 'provider_contract_unavailable', 'received_at': received_at(), 'source': source}
    # The two callers supply fixed sets of five/six read resources, never a
    # user-controlled task count. Cancel and join every remaining read before
    # the HTTP client closes, including on auth failure or the worker deadline.
    tasks = [asyncio.create_task(read(source, fetch)) for _, source, fetch in readers]
    try:
        values = await asyncio.gather(*tasks)
        return {name: value for (name, _, _), value in zip(readers, values)}
    finally:
        for task in tasks:
            if not task.done(): task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def _section_status(sections):
    return 'observed' if all(section['status'] == 'observed' for section in sections.values()) else 'partial'


async def snapshot(client, account_id):
    # get() independently refreshes scope before each account endpoint. Preserve
    # every returned position and field, including unfamiliar instrument types.
    async def fetch(path, params):
        data = await client.get(f'/accounts/{account_id}/{path}', params=params, decimal_strings=True)
        stable = client._account_numbers_by_id[account_id]
        rows = data if isinstance(data, list) else [data]
        for row in rows:
            if isinstance(row, dict):
                if 'accno' in row and str(row['accno']) != str(stable):
                    raise PermissionError('Portfolio account identity mismatch')
                if 'accid' in row and row['accid'] != account_id:
                    raise PermissionError('Portfolio account identity mismatch')
        return data
    paths = (
        ('account_info', 'info', None), ('holdings', 'positions', None),
        ('balances', 'ledgers', None), ('orders', 'orders', {'deleted': 'true'}),
        ('executed_trades', 'trades', {'days': 7}),
    )
    sections = await _sections([(name, 'nordnet_account_' + path,
                                lambda path=path, params=params: fetch(path, params)) for name, path, params in paths])
    return {'status': _section_status(sections), 'sections': sections, 'received_at': received_at(),
            'snapshot_atomic': False, 'execution_ready': False,
            'executed_trades_days': 7,
            'notes': ['Every provider field is retained; sections are separate reads.',
                      'Unavailable sections stay explicit; authentication/identity failures deny the read.',
                      'Receipt time is not a quote timestamp. Acquisition prices are not historical returns.',
                      'Orders include provider-returned deleted orders; inspect each order status.']}


class _CloudReads:
    def __init__(self, client, account_id):
        self.client, self.account_id = client, account_id

    async def __aenter__(self):
        self.account_key, self.stable = await self.client.cloud_account_identity(
            self.account_id, unavailable_message='Portfolio account identity unavailable')
        market = self.client.base_url.removesuffix('/api/2').rsplit('.', 1)[-1]
        jwt = await authorization_token(self.client.session_token, market)
        self.http = httpx.AsyncClient(timeout=15, follow_redirects=False,
                                     headers={'Authorization': 'Bearer ' + jwt,
                                              'x-locale': {'fi': 'fi-FI', 'se': 'sv-SE', 'no': 'nb-NO', 'dk': 'da-DK'}[market]})
        return self

    async def __aexit__(self, *exc):
        await self.http.aclose()

    async def post(self, path, **fields):
        # Every caller below supplies a literal read-only cloud resource. Neither
        # URL nor accountIds can originate in external tool arguments.
        body = {**fields, 'accountIds': [self.account_key]}
        async with self.http.stream('POST', 'https://api.prod.nntech.io' + path, json=body) as response:
            if response.status_code == 401:
                raise SessionExpiredError('Nordnet authentication required')
            response.raise_for_status()
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                if len(raw) > 1024 * 1024:
                    raise ValueError('Portfolio response too large; request a shorter history range')
        import json
        result = json.loads(raw, parse_float=str)
        # These native resources echo identity when returning per-account data.
        # A response with a foreign account must never pass to Hermes.
        def verify(value):
            if isinstance(value, dict):
                if 'accountId' in value and value['accountId'] != self.account_key:
                    raise PermissionError('Portfolio account identity mismatch')
                if 'accountNumber' in value and str(value['accountNumber']) != str(self.stable):
                    raise PermissionError('Portfolio account identity mismatch')
                if 'balancesByAccountId' in value and set(value['balancesByAccountId']) - {str(self.account_key)}:
                    raise PermissionError('Portfolio balance account identity mismatch')
                for child in value.values(): verify(child)
            elif isinstance(value, list):
                for child in value: verify(child)
        verify(result)
        return result


async def performance_history(client, account_id, from_date, to_date):
    start, end = read_date(from_date), read_date(to_date)
    if start > end:
        raise ValueError('Invalid date range')
    # Request an adjoining baseline and end boundary, retain the native series,
    # and select requested dates explicitly; do not guess missing trading days.
    provider_start, provider_end = start - timedelta(days=1), end + timedelta(days=1)
    async with _CloudReads(client, account_id) as cloud:
        data = await cloud.post('/holdings/historical-returns/v2/own-capital/graph',
                                fromDate=provider_start.isoformat(), toDate=provider_end.isoformat(), startFromZero=False)
    if not isinstance(data, dict) or not isinstance(data.get('returns'), list):
        raise ValueError('Portfolio history contract unavailable')
    daily, observed = [], []
    for row in data['returns']:
        if not isinstance(row, dict) or not isinstance(row.get('d'), str):
            raise ValueError('Portfolio history date unavailable')
        day = read_date(row['d'][:10]); observed.append(day.isoformat())
        if start <= day <= end:
            daily.append({label: row[key] for key, label in _FIELDS.items() if key in row})
    return {'status': 'observed', 'source': 'nordnet_native_own_capital_graph',
            'from_date': from_date, 'to_date': to_date, 'currency': data.get('currencyCode'),
            'daily_returns': daily, 'provider': data, 'observed_dates': observed,
            'provider_from_date': provider_start.isoformat(), 'provider_to_date': provider_end.isoformat(),
            'received_at': received_at(), 'execution_ready': False,
            'notes': ['Named return fields follow Nordnet web graph aliases; raw provider data is retained.',
                      'Daily return and accumulated return are different fields; cash-flow and own-capital changes are not investment return.',
                      'Absent dates are unavailable, never zero return. Receipt time does not prove historical publication time.']}


async def metrics(client, account_id, period='ALL'):
    if period not in PERIODS:
        raise ValueError('Unsupported portfolio period')
    paths = (
        ('development_today', '/holdings/historical-returns/v2/own-capital/development-today', {}),
        ('development_by_period', '/holdings/historical-returns/v2/own-capital/development-by-period', {'period': period}),
        ('annual_returns', '/holdings/historical-returns/v2/own-capital/returns/by-year', {}),
        ('risk_metrics', '/holdings/historical-returns/v2/own-capital/risk-metrics', {}),
        ('own_capital', '/holdings/own-capital/v2/own-capital', {}),
        ('balance_overview', '/holdings/own-capital/v2/balance/overview', {}),
    )
    async with _CloudReads(client, account_id) as cloud:
        sections = await _sections([(name, path, lambda path=path, fields=fields: cloud.post(path, **fields))
                                    for name, path, fields in paths])
    return {'status': _section_status(sections), 'period': period, 'sections': sections,
            'snapshot_atomic': False, 'received_at': received_at(), 'execution_ready': False}

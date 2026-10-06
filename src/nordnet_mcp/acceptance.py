"""A single operator-authorized transport test; never an investment mandate.

The mounted manifest is ephemeral. Durable attempts prevent replay, while only
its acknowledged provider order can be cancelled after the creation window.
"""
import json
import os
from pathlib import Path
import sqlite3
import time
from contextlib import closing


def authorize(method, account_number, path, fields):
    manifest = json.loads(Path(os.environ['NORDNET_ACCEPTANCE_MANIFEST_FILE']).read_text())
    if (manifest.get('version') != 1 or type(manifest.get('account_number')) is not int or
            manifest['account_number'] != account_number or not isinstance(manifest.get('reference'), str)):
        raise PermissionError('Acceptance account or manifest is invalid')
    location = Path(os.environ.get('NORDNET_ACCEPTANCE_LEDGER_FILE', '/policy/acceptance.sqlite'))
    location.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(location, timeout=10)) as db:
        location.chmod(0o600)
        db.execute('CREATE TABLE IF NOT EXISTS attempts (reference TEXT PRIMARY KEY, account INTEGER NOT NULL, fields TEXT NOT NULL, started REAL NOT NULL, provider_order_id TEXT, outcome TEXT, cancel_started REAL)')
        if method == 'POST':
            if (type(manifest.get('expires_at')) not in (int, float) or time.time() >= manifest['expires_at'] or
                    fields != manifest.get('fields') or fields.get('reference') != manifest['reference']):
                raise PermissionError('Acceptance terms differ or creation window expired')
            try:
                db.execute('INSERT INTO attempts (reference,account,fields,started) VALUES (?,?,?,?)',
                           (manifest['reference'], account_number, json.dumps(fields, sort_keys=True), time.time()))
                db.commit()
            except sqlite3.IntegrityError:
                raise PermissionError('Acceptance insertion was already attempted; reconcile without retry') from None
        elif method == 'DELETE':
            row = db.execute('SELECT account, provider_order_id FROM attempts WHERE reference=?', (manifest['reference'],)).fetchone()
            if row is None or row[0] != account_number or row[1] is None or path.rsplit('/', 1)[-1] != row[1]:
                raise PermissionError('Only the acknowledged acceptance order may be cancelled')
            changed = db.execute('UPDATE attempts SET cancel_started=? WHERE reference=? AND cancel_started IS NULL', (time.time(), manifest['reference'])).rowcount
            if changed != 1:
                raise PermissionError('Cancellation already attempted; reconcile without retry')
            db.commit()
        else:
            raise PermissionError('Acceptance test authorizes placement and cancellation only')
    return manifest['reference'], location


def record_attempt(permit, reply, http_status):
    reference, location = permit
    identifier = reply.get('order_id') if (isinstance(reply, dict) and 200 <= http_status < 300 and reply.get('result_code') == 'OK') else None
    identifier = str(identifier) if identifier is not None else None
    if identifier is not None and (not identifier.isdecimal() or int(identifier) <= 0):
        identifier = None
    with closing(sqlite3.connect(location, timeout=10)) as db:
        db.execute('UPDATE attempts SET provider_order_id=?,outcome=? WHERE reference=?',
                   (identifier, json.dumps(reply), reference))
        db.commit()

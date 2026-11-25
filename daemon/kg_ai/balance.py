"""Fail-closed balance admission. Never retain or report provider bodies or credentials."""
import json
import time
import urllib.error
import urllib.request
from decimal import Decimal

BALANCE_URL = 'https://api.deepseek.com/user/balance'
BALANCE_FLOOR_USD = Decimal('0.50')
BALANCE_FLOOR_CNY = Decimal('4.00')
BALANCE_TIMEOUT_SEC = 5
BALANCE_MAX_BYTES = 16 * 1024
BALANCE_TTL_SEC = 60
PAUSES = {'balance_low', 'balance_unavailable'}


def http_balance(key):
    from .deepseek import NoRedirect
    req = urllib.request.Request(BALANCE_URL, headers={'Authorization': 'Bearer ' + key}, method='GET')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(req, timeout=BALANCE_TIMEOUT_SEC) as response:
            return response.status, response.read(BALANCE_MAX_BYTES + 1)
    except Exception:
        return 0, b''


def parse_balance(status, raw):
    try:
        if status != 200 or len(raw) > BALANCE_MAX_BYTES:
            return 'balance_unavailable'
        obj = json.loads(raw)
        if type(obj['is_available']) is not bool:
            return 'balance_unavailable'
        if obj['is_available'] is False:
            return 'balance_low'
        rows = obj['balance_infos']
        if not isinstance(rows, list) or not rows:
            return 'balance_unavailable'
        seen, room = set(), Decimal(0)
        floors = {'USD': BALANCE_FLOOR_USD, 'CNY': BALANCE_FLOOR_CNY}
        for row in rows:
            currency, value = row['currency'], row['total_balance']
            if currency not in floors or currency in seen or not isinstance(value, str):
                return 'balance_unavailable'
            value = Decimal(value)
            if not value.is_finite() or value < 0:
                return 'balance_unavailable'
            seen.add(currency)
            room += value / floors[currency]
        return None if room >= 1 else 'balance_low'
    except Exception:
        return 'balance_unavailable'


class BalanceGate:
    def __init__(self, transport=None, clock=time.monotonic):
        self.transport, self.clock = transport, clock
        self.valid_until = 0
        self.reason = None
        self.reason_until = 0

    def check(self, key):
        if not key:
            return 'missing_key'
        if self.reason:
            # A failed probe is a snapshot, not a verdict: re-probe after the TTL so one
            # transient timeout (or a top-up) does not pin the whole process to the backup.
            # balance_low stays latched on purpose; only an unavailable probe expires.
            if self.reason == 'balance_low' or self.clock() < self.reason_until:
                return self.reason
            self.reason = None
        if self.clock() < self.valid_until:
            return None
        try:
            self.reason = parse_balance(*self.transport(key)) if self.transport else 'balance_unavailable'
        except Exception:
            self.reason = 'balance_unavailable'
        if self.reason:
            self.reason_until = self.clock() + BALANCE_TTL_SEC
        else:
            self.valid_until = self.clock() + BALANCE_TTL_SEC
        return self.reason

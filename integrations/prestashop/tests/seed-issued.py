"""Seed a synthetic historical issued invoice in the isolated, still-shadow ledger."""
from __future__ import annotations

import json
import sys
from datetime import date

from fiscalm.commerce import orders as commerce
from fiscalm.invoicing import engine as invoicing
from fiscalm.ledger import Ledger, is_live

assert not is_live()
ledger = Ledger()
try:
    order = commerce.get(ledger, 'NORD', json.load(sys.stdin)['id'])
    draft = invoicing.invoice(ledger, order['draft_id'], company='NORD')
    # Engine-only fixture creation, as in public invoicing tests. No API live switch,
    # SPV upload or tick: only a local historical record for PDF/refund read testing.
    invoicing.issue(ledger, draft['id'], date.today().isoformat(), company='NORD', version=draft['version'])
finally:
    ledger.close()
assert not is_live()

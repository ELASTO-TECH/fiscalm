"""Create an isolated real shop channel; credentials go only to a private file."""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from fiscalm import integrations
from fiscalm.invoicing import engine as invoicing
from fiscalm.api import tokens, users
from fiscalm.ledger import Ledger, is_live

assert not is_live()
ledger = Ledger()
try:
    invoicing.save_seller(ledger, 'NORD', dict(name='Atelier Nord SRL', cui='12345674', vat_id='RO12345674',
        country='RO', street='Strada Exemplu 1', city='Cluj-Napoca', county='RO-CJ', vat_payer=True,
        iban='RO35TEST0000000000000000', bank='Banca Exemplu', reg_com='J00/0000/2026'))
    invoicing.create_series(ledger, 'NORD', 'DEMO')
    shop = integrations.add(ledger, 'shop', 'Magazin exemplu', dict(platform='prestashop', company='NORD', series='DEMO', auto_issue='true'))
    owner = users.create(ledger, 'shop-api', 'Shop API', 'member', datetime.now(UTC), actor_id=None)
    credential = tokens.create(ledger, 'PrestaShop E2E', 'write', user_id=owner)
    os.umask(0o077)
    Path('/secrets/commerce.json').write_text(json.dumps(dict(token=credential['token'], channel=shop['id'])))

finally:
    ledger.close()

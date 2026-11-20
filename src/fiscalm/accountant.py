"""Per-firm accountant role. Bank details and payment approval remain supplier decisions."""

from __future__ import annotations

import json
import re

from fiscalm import companies
from fiscalm.ledger import Ledger

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}")


def settings(L: Ledger, co: str) -> dict:
    empty = dict(cui="", senders=[], recipient="")
    try:
        value = json.loads(L.get_meta(f"accountant:{co}", "null"))
        if (
            not isinstance(value, dict)
            or set(value) != set(empty)
            or not isinstance(value["cui"], str)
            or not isinstance(value["recipient"], str)
            or not isinstance(value["senders"], list)
            or any(not isinstance(a, str) or not EMAIL.fullmatch(a) for a in value["senders"])
            or value["recipient"]
            and not EMAIL.fullmatch(value["recipient"])
        ):
            return empty
        return value
    except (ValueError, TypeError):
        return empty


def save(L: Ledger, co: str, cui: str, senders: str, recipient: str) -> dict:
    """Choose an existing partner by exact CUI; never add or confirm a bank account."""
    from fiscalm.efactura.inbox import tax_token
    from fiscalm.identity import partner_identity

    companies.get(co, L=L)
    cui, recipient = cui.strip().upper().removeprefix("RO"), recipient.strip().lower()
    addresses = list(dict.fromkeys(a.strip().lower() for a in senders.splitlines() if a.strip()))
    if len(addresses) > 10:
        raise ValueError("Se pot salva cel mult 10 adrese de e-mail.")
    if any(len(a) > 254 or not EMAIL.fullmatch(a) for a in [*addresses, *([recipient] if recipient else [])]):
        raise ValueError("Adresele de e-mail nu sunt valide.")
    from fiscalm.payments.engine import pay_lock

    with pay_lock(), L.tx(immediate=True):
        identities = {
            tax_token(r[0])
            for r in L.db.execute(
                "SELECT COALESCE(NULLIF(supplier_tax_id,''),supplier_cui) FROM efactura WHERE company=? "
                "UNION SELECT COALESCE(NULLIF(client_tax_id,''),client_cui) FROM issued WHERE company=? "
                "UNION SELECT COALESCE(NULLIF(party_tax_id,''),party_cui) FROM history WHERE company=?",
                (co, co, co),
            )
        }
        clients = [json.loads(r[0]) for r in L.db.execute("SELECT data FROM invoice_clients WHERE company=?", (co,))]
        identities.update(partner_identity(d.get("country", ""), d.get("cui"), d.get("vat_id")) for d in clients)
        if cui and (not re.fullmatch(r"[0-9]{2,10}", cui) or cui not in identities):
            raise ValueError("Contabilul se alege dintre partenerii firmei, după CUI.")
        if not cui and (addresses or recipient):
            raise ValueError("Mai întâi trebuie ales contabilul.")
        value = dict(cui=cui, senders=addresses, recipient=recipient)
        L.set_meta(f"accountant:{co}", json.dumps(value))
    return value

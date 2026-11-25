"""Invoices the companies ISSUE through ANAF e-Factura (sent messages): fetch, parse, store, match collections.

  fiscalm efactura sync-issued   list + download new sent messages (`filtru=T`) for both CUIs, read-only toward
                                ANAF with the same token, window and paging as the received ones, keep the raw
                                ZIP, parse the UBL XML into the `issued` ledger table, then look for the
                                collections in the BT statements.

Raw ZIPs: $FISCALM_HOME/efactura/<CO>/emise/<YYYY-MM>/<message id>.zip (YYYY-MM = when ANAF listed it).

A separate table, not a direction column on `efactura`: every received-invoice rule (statement debits,
`used_txs`, approve -> payment file, autopay, judge) reads `efactura` without a direction filter, so an issued
invoice there could be "paid" by a debit or added to the payment file. Here nothing ever pays; it only reads.

Collected (`incasata`): a statement CREDIT with the amount still owed, from this client (its CUI on the credit,
its exact name, or the IBAN an earlier collection of the same client came from), or naming it, or the only
invoice of its amount (match_statement); or, from an identified client, the one exact set of its open invoices
the credit pays. Every doubt leaves it open: a credit that fits several invoices or sets settles none, and one
credit settles one invoice or one set, ever (`settled_tx`).
Collected another way (OP, cash, card, other): `mark_collected`, undone with `unmark_collected`; it waits as
"marcata" until a statement credit confirms it (cash never will: manual proof comes later).
Credit notes reduce what the invoice they reference (BillingReference) still owes.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import date, datetime, timedelta
from decimal import Decimal
from itertools import combinations
from pathlib import Path

from fiscalm import companies, history_invoices, integrations
from fiscalm.bank import statements
from fiscalm.efactura import client as anaf, inbox as efactura
from fiscalm.efactura.inbox import (
    WINDOW,
    UblError,
    _amount_matches,
    _norm,
    invoice_from_zip,
    number_of,
    same_firm,
    valid_id,
    zip_path,
)
from fiscalm.identity import firm_id, tax_token
from fiscalm.invoicing import engine as invoicing, spv as invoice_spv, ubl
from fiscalm.invoicing.errors import InvoiceError
from fiscalm.ledger import USER, Ledger, covered, now
from fiscalm.migrations import OWN_ROWS_PENDING

COLLECT_DAYS_BEFORE, COLLECT_DAYS_AFTER = 5, 365  # advances a few days early; slow clients up to a year
STATUS_RO = {"open": "neincasata", "collected": "incasata", "marked": "marcata"}


# ------------------------------------------------------------------ store


def raw_path(company: str, message_id: str, listed: str, home: Path | None = None) -> Path:
    """Numeric ANAF ids only, and always under $FISCALM_HOME/efactura (ValueError otherwise)."""
    return zip_path(home, company, "emise", message_id, listed)


def store(L: Ledger, company: str, msg: dict, raw: bytes, *, track_history: bool = False) -> dict:
    """Keep the raw ZIP, parse it, insert one `issued` row (once per ANAF message id)."""
    mid = str(msg["id"])
    path = raw_path(company, mid, str(msg.get("data_creare", "")))
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        tmp = path.with_suffix(".part")
        tmp.write_bytes(raw)
        tmp.replace(path)
    row = {
        "message_id": mid,
        "company": company,
        "upload_id": str(msg.get("id_solicitare", "")),
        "listed_at": str(msg.get("data_creare", "")),
        "raw_path": str(path),
    }
    try:
        inv, sha = invoice_from_zip(raw)
    except UblError as e:
        L.add_issued(row | {"status": "open", "parse_error": str(e)})
        return row | {"status": "unparsed", "error": str(e)}
    with L.tx():  # the duplicate check under the write lock: an import or another sync can't slip in between
        if (
            not track_history
            and history_invoices.direction(companies.get(company, L=L), inv) == "issued"
            and history_invoices.untracked(L, company, inv)
            and not invoicing.already_issued(L.db, company, inv, sha)
        ):
            history_invoices.store(L, company, "issued", inv, sha, path)
            L.set_meta(f"history_sync:issued:{company}:{mid}", "1")
            return row | {"status": "history", "number": inv.number}
        status = invoicing.record_issued(L, company, row, inv, sha)
        invoicing.link_sent(L, company, mid, inv.supplier_cui, inv.number, inv.issue_date)
    return row | {"status": status, "client": inv.buyer_name, "number": inv.number}


# ------------------------------------------------------------------ sync


def sync(L: Ledger, now_dt: datetime | None = None) -> dict:
    """Fetch new issued invoices for every company. Read-only toward ANAF; one download per message."""
    if anaf.inbox_env() == "test":
        return {"env": "test", "new": [], "errors": [], "warnings": [], "skipped_test": True}
    now_dt = now_dt or datetime.now()
    efactura.fill_party_addresses(L)
    efactura.fill_tax_ids(L, "issued", "client_tax_id", "buyer")  # rows stored before the full tax id was kept
    out: dict = {"env": anaf.inbox_env(), "new": [], "errors": [], "warnings": [], "collected": []}
    if not anaf.configured(L):
        return out | {"not_authorized": True}
    end = now_dt - timedelta(minutes=1)
    denied = []
    configured = companies.all(L)
    for key, co in configured.items():
        # Existing downloads also prove access before per-company markers were introduced.
        if L.db.execute("SELECT 1 FROM issued WHERE company=? LIMIT 1", (key,)).fetchone():
            L.set_meta(f"issued_listed:{key}", "1")
        try:
            selected = anaf.instance(L, key)
            if selected is None:
                out["warnings"].append(f"{key}: ANAF is not connected for this company.")
                continue
            if len(integrations.for_firm(L, "anaf", key)) > 1:
                out["warnings"].append(f"{key}: multiple ANAF connections cover this company.")
            token = anaf.access_token(now_dt, L=L, company=key, expected_id=selected.id)
            msgs = anaf.list_sent(co.cui, end - WINDOW, end, token)
        except anaf.CompanyUnauthorized:
            denied.append(key)
            target = "errors" if L.get_meta(f"issued_listed:{key}") else "warnings"
            out[target].append(f"{key}: SPV access is not authorised for this company.")
            continue
        except (anaf.AnafError, anaf.secrets.SecretError) as e:
            out["errors"].append(f"{key}: {e}")
            continue
        L.set_meta(f"issued_listed:{key}", "1")
        L.set_meta(f"anaf_read_connection:{key}", selected.id)
        for m in msgs:
            if not valid_id(m.get("id")):
                out["errors"].append(f"{key}: ANAF listed a non-numeric message id {str(m.get('id'))[:40]!r}")
                continue
            if L.issued(str(m["id"])) or L.get_meta(f"history_sync:issued:{key}:{m['id']}"):
                continue
            fail_key = f"issued_dl_fail:{m['id']}:{now_dt:%Y-%m-%d}"
            fails = int(L.get_meta(fail_key, "0"))
            if fails >= 3:  # ANAF allows 10 downloads per message per day; leave room for the user
                # no new attempt today, but the invoice is still missing: keep saying so
                out["errors"].append(
                    f"{key}: message {m['id']} failed to download {fails} times today; retried tomorrow"
                )
                continue
            try:
                raw = anaf.download(str(m["id"]), token)
            except anaf.AnafError as e:
                L.set_meta(fail_key, str(fails + 1))
                out["errors"].append(f"{key}: {e}")
                continue
            out["new"].append(store(L, key, m, raw))
    out["collected"] = match_recent_statements(L)
    if denied and len(denied) == len(configured):
        out["errors"].append("SPV access is not authorised for any configured company.")
    # SPV access lost, or given to no firm, needs a person; other failed reads retry on their own.
    lost = any(L.get_meta(f"issued_listed:{k}") for k in denied) or denied and len(denied) == len(configured)
    anaf.record_firm_read_status(L, "issued", configured, out, denied)
    L.set_meta("issued_revoked", "1" if lost else "")
    L.set_meta("issued_warnings", "\n".join(out["warnings"]))
    L.set_meta("issued_last_sync", now())
    if out["errors"]:
        L.set_meta("issued_last_error", f"{now()} {'; '.join(out['errors'])[:500]}")
    else:
        L.db.execute("DELETE FROM meta WHERE key='issued_last_error'")
        L.db.commit()
    return out


def reparse(L: Ledger) -> list[dict]:
    """Retry rows whose stored ZIP did not parse (after a parser fix), without calling ANAF again. A row fiscalm
    wrote itself (`invoicing.LOCAL`) has no ZIP and always parsed: never touched here."""
    done = []
    for r in L.db.execute(
        "SELECT * FROM issued WHERE parse_error IS NOT NULL AND upload_id IS NOT ?", (invoicing.LOCAL,)
    ):
        p = Path(r["raw_path"])
        if not r["raw_path"] or not p.is_file():
            continue
        L.db.execute("DELETE FROM issued WHERE message_id=?", (r["message_id"],))
        L.db.commit()
        done.append(
            store(
                L,
                r["company"],
                {"id": r["message_id"], "id_solicitare": r["upload_id"], "data_creare": r["listed_at"]},
                p.read_bytes(),
            )
        )
    return done


def backfill_own_rows(L: Ledger) -> int:
    """Finish migration `own_issued_rows` on the first run after it (journal first, finish on the next run): every
    invoice and storno issued before fiscalm wrote its own `issued` row gets one, unless the same invoice is already
    stored (an SPV copy that never linked: that pair stays as it is). Collections and credit notes already stored are
    untouched. This runs live engine code (`record_own`, `ubl.build`): a later change there changes what it writes."""
    if L.get_meta(OWN_ROWS_PENDING) is None:
        return 0
    done = 0
    with L.tx() as db:
        if not db.execute("SELECT 1 FROM meta WHERE key=?", (OWN_ROWS_PENDING,)).fetchone():
            return 0  # another process finished it first
        firms = companies.all(L)
        for r in db.execute(
            "SELECT id,company FROM invoices i WHERE status!='draft' AND NOT EXISTS (SELECT 1 FROM invoice_sent_links l"
            " JOIN issued s ON s.message_id=l.message_id WHERE l.invoice_id=i.id AND l.environment=?"
            " AND s.status!='duplicate') ORDER BY rowid",
            (anaf.inbox_env(),),
        ).fetchall():
            if r["company"] in firms:
                inv, sha = _as_sent(invoicing.invoice(L, r["id"], company=r["company"]))
                done += invoicing.record_own(L, r["company"], r["id"], inv, sha)
        db.execute("DELETE FROM meta WHERE key=?", (OWN_ROWS_PENDING,))
    return done


def _as_sent(inv: dict) -> tuple[efactura.Invoice, str | None]:
    """The invoice as SPV would parse it. A snapshot whose XML no longer builds (a rule added since it was issued) is
    read from the frozen snapshot instead, billing reference included, so it stays owed and a storno still reduces."""
    try:
        xml = ubl.build(inv)
        return efactura.parse_ubl(xml), hashlib.sha256(xml).hexdigest()
    except (InvoiceError, UblError):  # the error text may quote the invoice (an IBAN): not kept
        d, client = inv["data"], inv["data"]["client"]
        return efactura.Invoice(
            doc_type="invoice",
            number=invoicing.invoice_number(inv),
            issue_date=d["issue_date"],
            due_date=d.get("due_date", ""),
            currency=d["currency"],
            total=d["totals"]["total"],
            supplier_name=d["seller"]["name"],
            supplier_cui=d["seller"]["cui"],
            buyer_cui=re.sub(r"\D", "", client.get("cui") or ""),
            buyer_name=client["name"],
            buyer_tax_id=efactura.tax_token(client.get("vat_id") or client.get("cui") or ""),
            billing_ref=d.get("billing_ref", ""),
            issues=["read from the stored invoice: its XML no longer generates"],
        ), None


# ------------------------------------------------------------------ credit notes


def _ref(n: str | None) -> str:
    return _norm(n).replace(" ", "")


def _same_client(c, i) -> bool:
    """Only firm tax ids prove the same client here; private reversals need the authored invoice link."""
    if firm_id(tax_token(c["client_cui"])) and firm_id(tax_token(i["client_cui"])):
        return c["client_cui"] == i["client_cui"]
    return False  # Names and anonymous/personal tax tokens cannot prove the same private buyer.


def credit_links(L: Ledger, company: str | None = None) -> dict[str, str | None]:
    """{credit note message id: the invoice it reduces, or None when its reference fits no single invoice}."""
    rows = L.issued_rows(company=company)
    invoices = [r for r in rows if r["doc_type"] == "invoice" and r["status"] != "duplicate" and not r["parse_error"]]
    reversals = (
        {
            (r[0], r[1])
            for r in L.db.execute(
                "SELECT s.message_id,o.message_id FROM invoice_reversals r "
                "JOIN invoice_sent_links s ON s.invoice_id=r.storno_id "
                "JOIN invoice_sent_links o ON o.invoice_id=r.original_id "
                "WHERE s.environment=? AND o.environment=?",
                (anaf.inbox_env(), anaf.inbox_env()),
            )
        }
        if any(r["status"] == "credit" and not firm_id(tax_token(r["client_cui"])) for r in rows)
        else set()
    )
    out: dict[str, str | None] = {}
    for c in rows:
        if c["status"] != "credit":
            continue
        ref = _ref(c["billing_ref"])
        cands = [
            i
            for i in invoices
            if ref
            and _ref(i["number"]) == ref
            and i["company"] == c["company"]
            and i["currency"] == c["currency"]
            and (_same_client(c, i) or (c["message_id"], i["message_id"]) in reversals)
        ]
        out[c["message_id"]] = cands[0]["message_id"] if len(cands) == 1 else None
    return out


def _credited(L: Ledger, company: str | None = None) -> dict[str, Decimal]:
    links = credit_links(L, company)
    out: dict[str, Decimal] = {}
    for mid, inv in links.items():
        if inv:
            out[inv] = out.get(inv, Decimal(0)) + Decimal(L.issued(mid)["total"])
    return out


# ------------------------------------------------------------------ collections found in statements


def _cui_on(cui: str, text: str) -> bool:
    return len(cui) >= 6 and re.search(rf"(?<!\d){cui}(?!\d)", text or "") is not None


def _ident_re(number: str | None) -> re.Pattern | None:
    """The full invoice identifier as it may be written on a credit: 'NRD 0042' matches 'NRD 0042', 'NRD0042',
    'NRD-42', never 'NRD 420' or 'NRD 43'. Series included, short numbers included."""
    parts = re.findall(r"[A-Z]+|\d+", _norm(number))
    if not parts or not any(p.isdigit() for p in parts):
        return None
    pat = r" ?".join("0*" + (p.lstrip("0") or "0") if p.isdigit() else re.escape(p) for p in parts)
    return re.compile(rf"(?<![A-Z0-9]){pat}(?![A-Z0-9])")


def credit_from_client(
    L: Ledger,
    company: str,
    name: str,
    cui: str,
    tx: statements.Tx,
    *,
    accounts: dict[str, set[str]] | None = None,
) -> str:
    """Recognise a client credit by an exclusive known IBAN, CUI or firm name, in any currency."""
    if tx.direction != "credit" or tx.counterparty_iban in companies.get(company, L=L).own_ibans:
        return ""
    known = (
        accounts.get(tx.counterparty_iban, set())
        if accounts is not None
        else {
            r["client_cui"] or ""
            for r in L.db.execute(
                "SELECT client_cui FROM issued WHERE company=? AND collected_from_iban=?",
                (company, tx.counterparty_iban),
            )
            if tx.counterparty_iban
        }
    )
    if not firm_id(tax_token(cui)):  # a private buyer: by name, never from an account known for a client
        return "name" if not known and same_firm(name, tx.counterparty) else ""
    if known:
        return "iban" if cui and known == {cui} else ""
    if _cui_on(cui, f"{tx.counterparty} {tx.description}"):
        return "cui"
    return "name" if same_firm(name, tx.counterparty) else ""


CLIENT_PROOF = ("iban", "cui", "name")  # how a credit shows its client; "amount" and "ref" alone show none


def client_accounts(L: Ledger, company: str) -> dict[str, set[str]]:
    """The accounts earlier collections came from, each with the client CUIs it paid for."""
    out: dict[str, set[str]] = {}
    for r in L.issued_rows(company=company):
        if r["collected_from_iban"]:
            out.setdefault(r["collected_from_iban"], set()).add(r["client_cui"] or "")
    return out


def match_statement(L: Ledger, st: statements.Statement) -> list[str]:
    """Mark open issued invoices collected from statement credits. Wrongly "collected" hides money a client
    still owes, so every doubt leaves the invoice open:
      - the amount still owed (total minus its credit notes), in the invoice currency, within the date window;
      - a credit from the client (the IBAN an earlier collection of the same client came from, its CUI on the
        credit, or its exact name, legal forms aside, private buyers too) settles the one invoice it fits;
      - a credit that shows no client fits by its amount alone (maintainer decision, 2026-11-25): it settles the
        only candidate of its exact amount, and only when no other credit of the statement fits that one too.
        Without its client, only a transfer from a bank account (a counterparty IBAN), never one known for a client
        or shown with one on this statement; a credit that shows any of the firm's clients pays another's invoice
        only by naming it. Never at all: a card refund, fees or interest. Invoices this statement's credits already
        collected stay rivals when it is read again;
      - a credit that names a known invoice (its full identifier, or 3+ digits of it) settles only that one,
        and nothing when that invoice is already closed; without its client, only one whole identifier counts;
      - a credit that fits several invoices settles none; one credit settles one invoice, ever;
      - a credit from an identified client that fits no single invoice settles the one exact set of that client's
        invoices it pays (_Pool.batch: what it names, else 2 to 5 of them when it has at most 20 open, or all of
        them), all or none, when no other credit of the statement reaches any of them.
    Invoices the user marked collected are candidates too: a credit confirms their mark. History invoices (from
    before the firm's start, fiscalm.history) and invoices partly covered by the user's links are rivals that never
    settle. The paying account is remembered for the client only when the credit showed the client's CUI firm."""
    from fiscalm.settlements import engine as settlements

    batch_done = settlements.match_statement(L, st)
    with L.tx():  # decide and write under one write lock: no invoice or credit arrives in between
        pool = _Pool(L, st.company, {statements.tx_key(t) for t in st.settlement_txs})
        if not pool.open:
            return batch_done
        credits = _open_credits(L, st)
        # per statement line, never per key: two lines may share one (same day, amount and text, other payers)
        first = [pool.candidates(t, ()) for t in credits]
        claims = Counter(r["message_id"] for c in first for r, _ in c)
        # sets are found against the opening balances and accounts, before any single collection changes them
        plans = [(t, plan) for t, c in zip(credits, first, strict=True) if not c and (plan := pool.batch(t, ()))]
        shown = _clients_by_account(pool, st)
        done = _collect_singles(L, pool, st, credits, first, claims)
        return batch_done + done + _collect_sets(L, pool, plans, claims, done, shown)


def _open_credits(L: Ledger, st: statements.Statement) -> list[statements.Tx]:
    """Credits no matcher took, not the firm's own transfers or the owner's remembered advances, not claimed by a
    pending settlement report."""
    from fiscalm.settlements import engine as settlements

    used, own = L.used_txs(), companies.get(st.company, L=L).own_ibans
    advance = efactura.remembered_advance(L, st.company)
    return [
        t
        for t in st.settlement_txs
        if t.direction == "credit"
        and statements.tx_key(t) not in used
        and t.counterparty_iban not in own
        and not settlements.claimed_credit(L, st.company, t)
        and not advance(t)
    ]


def _collect_singles(
    L: Ledger, pool: _Pool, st: statements.Statement, credits: list, first: list, claims: Counter
) -> list[str]:
    # accounts that show a client on a line of this statement are nobody's anonymous payment
    shown = {t.counterparty_iban for t in st.settlement_txs if t.counterparty_iban and pool.from_client(t)}
    done: list[str] = []
    # Unambiguous credits first, those showing their client, then those naming their invoice, then the rest: once
    # they collected theirs, a plain twin may fit a single one. A credit keeps the evidence it had at the start.
    for i in sorted(range(len(credits)), key=lambda i: (len(first[i]) != 1, _rank(first[i]))):
        t, was = credits[i], {r["message_id"]: how for r, how in first[i]}
        cands = [(r, how) for r, how in pool.candidates(t, done) if _kept(was.get(r["message_id"]), how)]
        if len(cands) != 1 or not pool.settles(cands[0][0]["message_id"]):
            continue
        (r, how) = cands[0]
        if how == "amount" and (len(was) > 1 or claims[r["message_id"]] > 1 or t.counterparty_iban in shown):
            continue  # it fit others at first, another credit fits it too, or its account showed a client
        if _collect(L, pool, t, (r,), how, f"{how} REF {t.ref}"):
            done.append(r["message_id"])
    return done


def _clients_by_account(pool: _Pool, st: statements.Statement) -> dict[str, set[str]]:
    """The clients each account shows on any credit line of this statement, whatever its amount."""
    shown: dict[str, set[str]] = {}
    for t in st.settlement_txs:
        if t.direction == "credit" and t.counterparty_iban:
            shown.setdefault(t.counterparty_iban, set()).update(pool.who(t))
    return shown


def _collect_sets(
    L: Ledger, pool: _Pool, plans: list, claims: Counter, done: list, shown: dict[str, set[str]]
) -> list[str]:
    """A credit that fits no single invoice may pay several of its client's (maintainer decision, 2026-11-25):
    the one exact set (_Pool.batch), when no other credit of the statement reaches any of its invoices and its
    account shows no other client on the statement."""
    # every set a credit could pay competes, even one it can't settle for being ambiguous
    taken = Counter(m for _, (sets, _, _) in plans for m in {r["message_id"] for rows in sets for r in rows})
    out: list[str] = []
    for t, (sets, how, key) in plans:
        # the line shows this one client only, and so does its account on every line of the statement
        if len(sets) != 1 or pool.who(t) != {key} or len(shown.get(t.counterparty_iban, ())) > 1:
            continue
        rows = sets[0]
        alone = all(
            taken[r["message_id"]] == 1 and not claims[r["message_id"]] and r["message_id"] not in done for r in rows
        )
        if alone and _collect(L, pool, t, rows, how, f"{how} {len(rows)} invoices REF {t.ref}"):
            out += [r["message_id"] for r in rows]
    return out


def _collect(L: Ledger, pool: _Pool, t: statements.Tx, rows: tuple, how: str, note: str) -> bool:
    """Collect these invoices with the credit. Its account is remembered only when it showed a firm client, and
    the next credit of the statement from it sees it known."""
    learned = how in CLIENT_PROOF and firm_id(tax_token(rows[0]["client_cui"])) and t.counterparty_iban
    if not L.mark_issued_collected(
        tuple(r["message_id"] for r in rows) if len(rows) > 1 else rows[0]["message_id"],
        f"statement {t.date} {note}",
        statements.tx_key(t),
        t.counterparty_iban if learned else "",
        companies.normalize_iban(t.iban),
    ):
        return False
    if learned:
        pool.accounts.setdefault(t.counterparty_iban, set()).add(rows[0]["client_cui"] or "")
    return True


def _not_a_payment(t: statements.Tx) -> bool:
    """A card refund or the bank's own money (fees, interest): never a client's payment."""
    statements.classify_tx(t)
    return bool(t.card_last4) or t.category in (statements.BANK_FEE, statements.INTEREST)


def _kept(was: str | None, how: str) -> bool:
    """The evidence a credit had at the start still holds: the same, or its client shown (another way)."""
    return was == how or (was in (*CLIENT_PROOF, "ref") and how in CLIENT_PROOF)


_SERIES_NUMBER = re.compile(r"([A-Z][A-Z0-9 ]*?) ?0*(\d+)")  # 'B2B 0050' (normalized 'B2B-0050'): series, number
BATCH_MAX = 5  # one transfer pays at most this many of a client's invoices, unless it pays all of them
BATCH_SCAN = 20  # a client with more open invoices than this: only all of them, or the ones named, are tried


def _in_window(r, t: statements.Tx) -> bool:
    issued_on = date.fromisoformat(r["issue_date"])
    return issued_on - timedelta(days=COLLECT_DAYS_BEFORE) <= t.date <= issued_on + timedelta(days=COLLECT_DAYS_AFTER)


def _sets(mine: list, names: set[str], kind: str) -> list[tuple]:
    """The sets of a client's invoices a credit may pay: the ones it names by whole identifier (digits, or one named
    that is closed, unknown or another client's: none for sure), else every 2 to BATCH_MAX of them, and all."""
    if names:
        named = tuple(c for c in mine if c[0]["message_id"] in names)
        written = {_series_number(c[0]["number"]) for c in named}  # 'NRD-050' and 'NRD-0050' read the same
        return [named] if kind == "series" and len(named) == len(names) == len(written) else []
    sets = [c for k in range(2, min(BATCH_MAX, len(mine)) + 1) for c in combinations(mine, k)]
    return sets + ([tuple(mine)] if len(mine) > BATCH_MAX else [])


def _full_tax_id(r) -> str:
    """An invoice's whole client tax id, country prefix kept ('AT123456789'); the digits when that is all it has."""
    full = r["client_tax_id"] if "client_tax_id" in r.keys() else ""  # noqa: SIM118 (sqlite3.Row)
    full = re.sub(r"[^A-Z0-9]", "", (full or "").upper())
    return (full[2:] if re.fullmatch(r"RO\d+", full) else full) or tax_token(r["client_cui"]) or ""


_COUNTRIES = frozenset(
    {"AT", "BE", "BG", "CH", "CY", "CZ", "DE", "DK", "EE", "EL", "ES", "FI", "FR", "GB", "HR"}
    | {"HU", "IE", "IT", "LT", "LU", "LV", "MT", "NL", "NO", "PL", "PT", "SE", "SI", "SK", "XI"}
)  # VAT prefixes that make the digits after them another country's id, never a Romanian CUI


def _bare_cui_on(cui: str, text: str) -> bool:
    """A Romanian CUI written alone or as RO<cui>, never another country's VAT id ('DE<cui>', 'DE <cui>')."""
    found = re.finditer(rf"(?<![A-Z0-9])(?:([A-Z]{{2}}) ?)?{cui}(?!\d)", text.upper()) if len(cui) >= 6 else ()
    return any(m[1] not in _COUNTRIES for m in found)


def _series_number(number: str | None) -> tuple | str:
    m = _SERIES_NUMBER.fullmatch(_norm(number))
    return (m[1].replace(" ", ""), int(m[2])) if m else _norm(number)


def _client_key(r) -> str:
    """The client an invoice is for: its firm's full tax id, or a private buyer's name and personal id if any."""
    token = tax_token(r["client_cui"]) or ""
    if not firm_id(token):
        # a private buyer without a personal id may share a name with another: each invoice stands alone
        personal = f"|invoice:{r['message_id']}" if re.fullmatch(r"0*", token) else f"|{token}"
        return "name:" + " ".join(efactura.firm_core(r["client_name"])) + personal
    return "tax:" + _full_tax_id(r)


def _rank(cands: list) -> int:
    return 0 if cands and cands[0][1] in CLIENT_PROOF else 1 if cands and cands[0][1] == "ref" else 2


class _Pool:
    """Every issued invoice a credit of `company` may collect, with its rivals, read once per statement."""

    def __init__(self, L: Ledger, company: str, keys: set[str]) -> None:
        """`keys`: the statement's transactions. Invoices its credits already collected stay rivals, so matching
        the same statement again decides as the first time did."""
        from fiscalm import history

        self.L, self.company = L, company
        self.credited, self.have = _credited(L, company), covered(L, company, "issued")
        self.open = [
            r
            for r in L.issued_rows(status=("open", "marked"), company=company)
            if not r["parse_error"] and r["doc_type"] == "invoice" and self.owed(r) > 0
        ]
        old = history.rivals(L, company, "issued")
        here = [r for r in L.issued_rows(status=("collected",), company=company) if r["collected_tx"] in keys]
        self.rows = [*self.open, *here, *old]
        self.closed = {r["message_id"] for r in here}
        self._numbers([*L.issued_rows(company=company), *old])
        self.accounts = client_accounts(L, company)
        self.clients = {
            (r["client_name"] or "", r["client_cui"] or "", _full_tax_id(r), _client_key(r))
            for r in [*L.issued_rows(company=company), *old]
            if r["doc_type"] == "invoice"
        }
        self._who: dict[int, set[str]] = {}

    def _numbers(self, rows: list) -> None:
        """Every invoice number a credit may name: whole identifiers, digits, and the firm's series."""
        named = [r for r in rows if r["doc_type"] == "invoice" and r["status"] != "duplicate"]
        self.known = [(r["message_id"], _ident_re(r["number"]), number_of(r["number"])) for r in named]
        self.numbers = {r["message_id"]: r["number"] or "" for r in named}
        self.series_refs = {
            (m[1].replace(" ", ""), int(m[2]))
            for n in self.numbers.values()
            if (m := _SERIES_NUMBER.fullmatch(_norm(n)))
        }
        self.series = {s for s, _ in self.series_refs}

    def owed(self, r) -> Decimal:
        mid = r["message_id"]
        return Decimal(r["total"]) - self.credited.get(mid, Decimal(0)) - self.have.get(mid, Decimal(0))

    def settles(self, mid: str) -> bool:
        """History invoices and those partly covered by the user's links only stop others from settling."""
        return not mid.startswith("history:") and mid not in self.have and mid not in self.closed

    def fits(self, r, t: statements.Tx) -> str:
        """How the credit fits this invoice: by its client (CLIENT_PROOF), by its amount alone, or not ("")."""
        if _not_a_payment(t):
            return ""
        if not _amount_matches(t, self.owed(r), r["currency"]) or not _in_window(r, t):
            return ""
        if how := self.client_of(r, t):
            return how
        if not t.counterparty_iban or t.counterparty_iban in self.accounts:
            return ""  # no transfer from a bank account (a card refund, the bank's own money), or another client's
        # a credit showing another client (any invoice, any status) pays this one only if it names it
        return "other" if self.from_client(t) else "amount"

    def from_client(self, t: statements.Tx) -> bool:
        """The credit shows one of the firm's clients: it can pay only that client's invoices."""
        return bool(self.who(t))

    def who(self, t: statements.Tx) -> set[str]:
        """Every client of the firm (any invoice, any status, history too) the credit shows: by the account an
        earlier collection came from, the CUI on it (a foreign firm's whole VAT id, country included), or the name."""
        if id(t) not in self._who:
            text = re.sub(r"[^A-Z0-9]", "", f"{t.counterparty} {t.description}".upper())
            said = f"{t.counterparty} {t.description}"
            self._who[id(t)] = {
                key
                for name, cui, full, key in self.clients
                # the account, and also the words: a known account never hides another client named on the line
                if credit_from_client(self.L, self.company, name, cui, t, accounts=self.accounts) == "iban"
                or same_firm(name, t.counterparty)
                or (bool(full) and (full in text if not full.isdigit() else _bare_cui_on(full, said)))
            }
        return self._who[id(t)]

    def client_of(self, r, t: statements.Tx) -> str:
        cui = r["client_cui"] or ""
        return credit_from_client(self.L, self.company, r["client_name"], cui, t, accounts=self.accounts)

    def batch(self, t: statements.Tx, done) -> tuple[list[tuple], str, str] | None:
        """Every set of one client's invoices whose amounts still owed sum to the credit, in its currency (_sets),
        with how the credit shows the client and who the client is. A set settles only when it is the only one and
        all of it is open (never marked, history, partly linked or collected by this statement). None when the
        credit shows no client or several, or names an invoice of a known series that the firm doesn't have."""
        if _not_a_payment(t) or self.unknown_ref(t) or not (mine := self.of_client(t, done)):
            return None
        names, kind = self.named(t)
        if not names and len(mine) > BATCH_SCAN:  # not searched: it competes for every invoice, never settles
            return [tuple(r for r, _ in mine)] * 2, mine[0][1], _client_key(mine[0][0])
        exact = [c for c in _sets(mine, names, kind) if len(c) > 1 and sum(self.owed(r) for r, _ in c) == t.amount]
        sets = [tuple(r for r, _ in c) for c in exact]
        if len(sets) == 1 and not all(self.settles(r["message_id"]) and r["status"] == "open" for r in sets[0]):
            sets = sets * 2  # a rival in the only set: it competes, never settles
        return (sets, mine[0][1], _client_key(mine[0][0])) if sets else None

    def of_client(self, t: statements.Tx, done) -> list[tuple]:
        """(invoice, how) for every invoice in the credit's currency and window of the one client it shows; none
        when it shows no client or several."""
        mine = [
            (r, how)
            for r in self.rows
            if r["message_id"] not in done
            and r["currency"] == t.currency
            and _in_window(r, t)
            and (how := self.client_of(r, t))
        ]
        return mine if len({_client_key(r) for r, _ in mine}) == 1 else []

    def unknown_ref(self, t: statements.Tx) -> bool:
        """The credit names an invoice of one of the firm's series ('NRD 0099') that the firm doesn't have (yet)."""
        desc = _norm(t.description)
        for series in self.series:
            written = r"(?<![A-Z0-9])" + " ?".join(map(re.escape, series)) + r" ?0*(\d+)(?!\d)"
            if any((series, int(n)) not in self.series_refs for n in re.findall(written, desc)):
                return True
        return False

    def named(self, t: statements.Tx) -> tuple[set[str], str]:
        """Invoices the credit names, and how: "series" (a whole identifier, 'NRD 0042'), "digits" (3+ digits of one,
        when no whole identifier is written, so 'NRD 0042' never also names 'STD 0042') or "bare" (an invoice number
        without a series, '10', which may be a date or a quantity). Any status, so a credit for an already closed
        one settles nothing."""
        desc = _norm(t.description)
        whole = {mid for mid, rx, _ in self.known if rx and rx.search(desc)}
        series = {m for m in whole if re.search(r"[A-Z]", _norm(self.numbers[m]))}
        if series or whole:
            return (series, "series") if series else (whole, "bare")
        by_digits = {
            mid for mid, _, digits in self.known if digits and re.search(rf"(?<!\d){digits}(?!\d)", t.description)
        }
        # bare digits shared by several invoices (NRD 0042 and STD 0042) name none for sure: settle nothing
        return (by_digits if len(by_digits) <= 1 else {"__ambiguous__"}), "digits"

    def candidates(self, t: statements.Tx, done) -> list[tuple]:
        """(invoice, how) the credit may collect: those it names, else all it fits; the ones that show the
        client win over those that fit by amount alone."""
        cands = [(r, how) for r in self.rows if r["message_id"] not in done and (how := self.fits(r, t))]
        cands = [c for c in _narrow(cands, *self.named(t)) if c[1] != "other"]
        return [c for c in cands if c[1] in CLIENT_PROOF] or cands


def _narrow(cands: list, names: set[str], kind: str) -> list:
    """Keep the candidates the credit names. One whole identifier says which it pays, even without its client
    ("ref"); digits or several names prove nothing without the client; a number without a series ('10' in a date)
    may refuse, never choose among the client's invoices."""
    if not names:
        return cands
    fit = [c for c in cands if c[1] in CLIENT_PROOF]
    cands = [(r, how) for r, how in cands if r["message_id"] in names]
    if kind == "series" and len(names) == 1:
        return [(r, "ref" if how in ("amount", "other") else how) for r, how in cands]
    kept = [(r, how) for r, how in cands if how in CLIENT_PROOF]
    return kept if kind != "bare" or len(kept) == len(fit) or len(kept) <= 1 and len(fit) <= 1 else []


def match_recent_statements(L: Ledger, months: int = 13, today: date | None = None) -> list[str]:
    done = []
    today = today or date.today()
    back = today.year * 12 + today.month - 1 - months  # calendar months: October 2026, 13 -> September 2025
    cutoff = f"{back // 12:04d}-{back % 12 + 1:02d}"
    for d in L.documents(kind="statement"):
        if d["month"] < cutoff or not Path(d["path"]).exists():
            continue
        try:
            done += match_statement(L, statements.parse(Path(d["path"]), d["company"]))
        except Exception:  # an unreadable statement must not stop the sync; status shows it elsewhere
            continue
    return done


# ------------------------------------------------------------------ the user: collected another way

# How a client paid an issued invoice, in the received side's words (efactura.PAID_METHODS): by payment order into the
# firm's account (bank), in cash, by card, or another way (other: a short note, offsetting included).
COLLECTED_METHODS = ("bank", "cash", "card", "other")


def mark_collected(L: Ledger, message_id: str, method: str, detail: str = "") -> bool:
    """A person says the client paid this invoice by `method`. A fact, never proof: it stays marked until a statement
    credit confirms it. Raises efactura.PaidMethodError: method, detail (other without a note) or long."""
    detail = " ".join((detail or "").split())
    if method not in COLLECTED_METHODS:
        raise efactura.PaidMethodError("method")
    if len(detail) > efactura.PAID_DETAIL_MAX:
        raise efactura.PaidMethodError("long")
    if method == "other" and not detail:
        raise efactura.PaidMethodError("detail")
    return L.mark_issued_manual(message_id, f"{USER} {detail if method == 'other' else method}", method)


def unmark_collected(L: Ledger, message_id: str) -> bool:
    return L.unmark_issued(message_id)


# ------------------------------------------------------------------ view for fiscalm


def _add(totals: dict[str, Decimal], currency: str, amount: Decimal) -> None:
    totals[currency] = totals.get(currency, Decimal(0)) + amount


def _fmt(totals: dict[str, Decimal]) -> dict[str, str]:
    return {c: f"{v:.2f}" for c, v in sorted(totals.items(), key=lambda kv: (kv[0] != "RON", kv[0]))}


def issued(L: Ledger, company: str, today: date | None = None) -> dict:
    """What the company invoiced and what its clients still owe.

    rows: invoices, newest first, status neincasata | incasata | marcata | stornata (fully credited),
    days_overdue (> 0 only for neincasata past their due date). credit_notes: with the invoice they reduce
    (applied_to, None when the reference fits no single invoice). totals: per currency, issued_month (net of
    credit notes, current month), not_collected and overdue (what is still owed)."""
    today = today or date.today()
    month = today.strftime("%Y-%m")
    links = credit_links(L, company)
    credited = _credited(L, company)
    have = covered(L, company, "issued")
    rows, notes, unreadable = [], [], []
    t_month: dict[str, Decimal] = {}
    t_open: dict[str, Decimal] = {}
    t_over: dict[str, Decimal] = {}
    n_open = n_over = 0
    for r in L.issued_rows(company=company):
        if r["parse_error"]:
            unreadable.append({"message_id": r["message_id"], "error": r["parse_error"], "listed_at": r["listed_at"]})
            continue
        if r["status"] == "duplicate":
            continue
        total = Decimal(r["total"])
        base = {
            "message_id": r["message_id"],
            "number": r["number"],
            "client_name": r["client_name"],
            "client_cui": r["client_cui"],
            "client_tax_id": r["client_tax_id"],
            "issue_date": r["issue_date"],
            "currency": r["currency"],
            "total": r["total"],
            "issues": json.loads(r["issues"] or "[]"),
        }
        if r["status"] == "credit":
            notes.append(base | {"billing_ref": r["billing_ref"], "applied_to": links.get(r["message_id"])})
            if (r["issue_date"] or "").startswith(month):
                _add(t_month, r["currency"], -total)
            continue
        cr = credited.get(r["message_id"], Decimal(0))
        part = have.get(r["message_id"], Decimal(0))  # what statement credits the user linked to it settle
        owed = max(total - cr - part, Decimal(0))
        status = STATUS_RO[r["status"]]
        if status == "neincasata" and owed == 0 and not part:
            status = "stornata"
        overdue = 0
        if status == "neincasata" and r["due_date"]:
            overdue = max((today - date.fromisoformat(r["due_date"])).days, 0)
        if (r["issue_date"] or "").startswith(month):
            _add(t_month, r["currency"], total)
        if status == "neincasata":
            n_open += 1
            _add(t_open, r["currency"], owed)
            if overdue:
                n_over += 1
                _add(t_over, r["currency"], owed)
        rows.append(
            base
            | {
                "due_date": r["due_date"] or None,
                "credited": f"{cr:.2f}",
                "covered": f"{part:.2f}",
                "owed": f"{owed if status == 'neincasata' else Decimal(0):.2f}",
                "left": f"{owed if status in ('neincasata', 'marcata') else Decimal(0):.2f}",
                "status": status,
                "days_overdue": overdue,
                "collected_at": r["collected_at"],
                "collected_on": r["collected_on"] if r["status"] == "collected" else None,
                "collected_note": r["collected_note"],
                "collected_method": r["collected_method"] if r["status"] == "marked" else None,
                "how": "statement" if r["collected_tx"] else "manual" if r["status"] == "marked" else None,
                "can_mark": status == "neincasata",
                "can_unmark": r["status"] == "marked",
            }
        )
    rows.sort(key=lambda x: (x["issue_date"] or "", x["number"] or ""), reverse=True)
    return {
        "company": company,
        "today": today.isoformat(),
        "rows": rows,
        "credit_notes": notes,
        "unreadable": unreadable,
        "totals": {
            "issued_month": _fmt(t_month),
            "not_collected": _fmt(t_open),
            "overdue": _fmt(t_over),
            "not_collected_count": n_open,
            "overdue_count": n_over,
        },
        "last_sync": L.get_meta("issued_last_sync"),
        "last_error": L.get_meta("issued_last_error"),
    }


def natural_number(value: str) -> tuple:
    return tuple((1, int(p)) if p.isdigit() else (0, p) for p in re.split(r"(\d+)", value))


def merge_local_rows(
    L: Ledger, co: str, out: list[dict], live: bool, *, records: list[dict] | None = None
) -> list[dict]:
    """Add fiscalm's own details (SPV state, actions, local id) to the issued rows, and list each invoice whose own
    row is a credit note (a storno) beside them. What is owed comes from the `issued` rows alone: an invoice issued
    in fiscalm has its row from the moment it is issued, and its storno reduces it as a credit note."""
    production_rejected = {
        r[0]
        for r in L.db.execute(
            "SELECT s.invoice_id FROM invoice_submissions s JOIN invoices i ON i.id=s.invoice_id "
            "WHERE i.company=? AND s.environment='prod' AND s.status='rejected'",
            (co,),
        )
    }
    links = invoicing.sent_links(L, co)
    shown = {r["id"] for r in out}
    beside = {sid for mid, sid in links.items() if mid not in shown}  # credit notes are not in `out`
    local = {}
    running = invoice_spv.upload_running()
    # Sending needs an ANAF connection: without one the upload could only fail, so it is not offered.
    sendable = anaf.outward_allowed() and anaf.connection(anaf.instance(L, co), L=L)["state"] in {
        "connected",
        "expiring",
    }
    submissions = invoice_spv.submissions(L, co)
    days = invoice_spv.upload_after_days(L, co)
    manual = invoice_spv.manual_uploads(L, co) if days is not None else set()
    reversals: dict = {}
    originals = {}
    for r in L.db.execute("SELECT original_id,storno_id FROM invoice_reversals"):
        reversals.setdefault(r["original_id"], []).append(r["storno_id"])
        originals.setdefault(r["storno_id"], r["original_id"])
    for inv in records if records is not None else invoicing.issued_records(L, co):
        d, sid = inv["data"], inv["id"]
        sub = submissions.get(sid)
        reversed_by = sorted(set(reversals.get(sid, [])))
        original = originals.get(sid)
        count = len(reversals.get(sid, []))
        extra = dict(
            origin="fiscalm",
            spv=dict(
                state=sub["status"]
                if sub
                else "not_required"
                if invoice_spv.scope(inv) != "required"
                else "error"
                if L.get_meta("invoice_upload_error:" + sid)
                else "none",
                errors=sub["errors"] if sub else [],
                at=None,
                environment=anaf.env(),
                upload_on=None if sub else invoice_spv.upload_day(inv, days, sid in manual),
            ),
            can_storno=not original
            and count < len(inv["lines"])
            and bool(
                sub and sub["status"] == "accepted" or not sub and (not live or invoice_spv.scope(inv) != "required")
            ),
            can_upload=sendable
            and invoice_spv.scope(inv) != "not_required"
            and (not sub or sub["status"] == "rejected"),
            can_resolve=invoice_spv.can_resolve(sub, running=running),
            pdf=True,
            storno_of=original,
            reversed_by=reversed_by,
        )
        local[sid] = extra
        if sid in beside:
            out.append(
                dict(
                    id=sid,
                    company=co,
                    number=invoicing.invoice_number(inv),
                    client=d["client"]["name"],
                    client_cui=d["client"].get("cui", ""),
                    issue_date=d["issue_date"],
                    due_date=d["due_date"],
                    currency=d["currency"],
                    total=invoicing.money(Decimal(d["totals"]["total"])),
                    credited="0.00",
                    covered="0.00",
                    owed="0.00",
                    left="0.00",
                    status="credited",
                    days_overdue=0,
                    collected_at=None,
                    collected_on=None,
                    how=None,
                    collected_method=None,
                    note="",
                    can_mark=False,
                    can_unmark=False,
                )
                | extra
            )
    for row in out:
        if row["id"] in links:
            # Keep the collectable row's id for collection actions; local_id identifies commands/files.
            row.update(local[links[row["id"]]])
        elif "origin" not in row:
            row.update(
                origin="spv",
                spv=dict(state="accepted", errors=[], at=None),
                can_storno=False,
                can_upload=False,
                can_resolve=False,
                pdf=False,
                storno_of=None,
                reversed_by=[],
            )
        local_id = links.get(row["id"], row["id"] if row["origin"] == "fiscalm" else None)
        row["rejected"] = local_id in production_rejected or (
            row["spv"]["state"] == "rejected" and row["spv"].get("environment") != "test"
        )
        if row["rejected"]:
            row.update(owed="0.00", left="0.00", days_overdue=0, can_mark=False)
        row["local_id"] = local_id
    out.sort(key=lambda row: natural_number(row["number"] or ""))
    out.sort(key=lambda row: row["issue_date"] or "", reverse=True)
    return out

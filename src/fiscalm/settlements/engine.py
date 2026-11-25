"""Journalled report evidence and all-or-nothing collection. No outward effects."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field as dataclass_field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from fiscalm import companies
from fiscalm.bank import statements
from fiscalm.bank.csv import parse_csv
from fiscalm.bank.intake import identity as statement_identity, parse_structured
from fiscalm.company_config import CompanyError, normalize_cui
from fiscalm.efactura import client as anaf
from fiscalm.identity import firm_name, partner_id
from fiscalm.invoicing import issued
from fiscalm.ledger import Ledger, covered, finance_home
from fiscalm.pdftext import pdf_text
from fiscalm.settlements.reports import ReportError, money, parse
from fiscalm.storage import read_file
from fiscalm.validate import iban_valid


class SettlementError(ValueError):
    pass


def import_report(L: Ledger, company: str, data: bytes, spec: dict) -> str:
    """Operator-supplied spec establishes payer and report identity; attachment content never chooses either."""
    companies.get(company, L=L)
    if not isinstance(spec, dict):
        raise ReportError("Report specification must be an object")
    for field in ("payer_cui", "payer_iban"):
        value = spec.get(field, "")
        if not isinstance(value, str):
            raise ReportError("Payer identifiers must be strings")
    if spec.get("payer_cui") and not re.fullmatch(r"\d{6,12}", spec["payer_cui"]):
        raise ReportError("Payer CUI must contain digits only")
    if spec.get("payer_iban") and (
        not iban_valid(spec["payer_iban"]) or spec["payer_iban"] != spec["payer_iban"].replace(" ", "").upper()
    ):
        raise ReportError("Payer IBAN must be valid and normalized")
    required = ("source", "report_key", "date", "currency", "payer_name")
    if any(not isinstance(spec.get(k), str) or not 1 <= len(spec[k]) <= 200 for k in required):
        raise ReportError("Missing report identity")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", spec["report_key"]) or not re.fullmatch(r"[A-Z]{3}", spec["currency"]):
        raise ReportError("Invalid report key or currency")
    date.fromisoformat(spec["date"])
    lines = parse(data, spec.get("format", "csv"), spec["source"], spec.get("mapping"), spec.get("delimiter", ","))
    if any(r.get("currency", spec["currency"]).upper() != spec["currency"] for r in lines):
        raise ReportError("Mixed currencies")
    if any(r.get("batch", spec["report_key"]) != spec["report_key"] for r in lines):
        raise ReportError("Payout id differs from report key")
    for field in ("channel", "courier"):
        if field in spec and (not isinstance(spec[field], str) or not spec[field] or len(spec[field]) > 200):
            raise ReportError("Invalid channel or courier")
    check_company_cuis(lines, companies.get(company, L=L).cui)
    body = {k: spec[k] for k in required} | {k: spec.get(k, "") for k in ("payer_cui", "payer_iban")} | {"lines": lines}
    identity = spec.get("payer_cui") or spec.get("payer_iban") or issued._norm(spec["payer_name"])
    body["payer_id"] = hashlib.sha256(identity.encode()).hexdigest()[:32]
    body["partner"] = partner_id(company, spec.get("payer_cui") or None, spec["payer_name"])
    if not body["partner"]:
        raise ReportError("Missing partner identity")
    body.update({k: spec[k] for k in ("channel", "courier") if k in spec})
    rid = uuid4().hex
    with L.tx() as db:
        db.execute(
            "INSERT INTO settlement_reports(id,company,source,report_key,digest,raw,data) VALUES(?,?,?,?,?,?,?)",
            (
                rid,
                company,
                spec["source"],
                spec["report_key"],
                hashlib.sha256(data).hexdigest(),
                data,
                json.dumps(body),
            ),
        )
    return rid


def check_company_cuis(lines: list[dict], cui: str) -> None:
    """A mapped tax id only verifies ownership; it never chooses the report's company."""
    for n, line in enumerate(lines, 2):
        value = line.get("company_cui", "")
        if not value.strip():
            continue
        try:
            if normalize_cui(value) == normalize_cui(cui):
                continue
        except CompanyError:
            pass
        raise ReportError(f"Line {n}: company CUI differs or is unknown")


def _payer(report: dict, credit: statements.Tx, company: str, *, claim: bool = False) -> bool:
    """The credit comes from the report's payer. `claim` is the looser test that keeps a plausible payer credit out of
    single-invoice matching while the report is pending (its CUI anywhere on the line, an ASCII name match)."""
    if not isinstance(report.get("payer_cui", ""), str) or not isinstance(report.get("payer_iban", ""), str):
        return False
    if report.get("payer_cui") and not re.fullmatch(r"\d{6,12}", report["payer_cui"]):
        return False
    if credit.direction != "credit" or credit.counterparty_iban in companies.get(company).own_ibans:
        return False
    # An explicitly configured IBAN is authoritative: a wrong IBAN cannot fall back to a shared name.
    if report["payer_iban"]:
        return credit.counterparty_iban == report["payer_iban"]
    if claim:
        named = issued.same_firm(report["payer_name"], credit.counterparty) or same_payer(
            report["payer_name"], credit.counterparty
        )
        return (
            named
            or bool(report["payer_cui"])
            and issued._cui_on(report["payer_cui"], f"{credit.counterparty} {credit.description}")
        )
    if report["payer_cui"]:
        return _cui_named(report["payer_cui"], credit.counterparty or "")
    return same_payer(report["payer_name"], credit.counterparty)


_TAX_LABEL = re.compile(r"(?:C\.?U\.?I\.?|C\.?I\.?F\.?)[:.]?", re.I)


def _cui_named(cui: str, party: str) -> bool:
    """The paying party names this Romanian CUI as a whole word: `12345678`, `RO12345678`, `CUI:12345678`. A memo can
    name anyone's tax id; any other word holding the digits (`DE-123456789`, `12345678-2-41`, `J40/123456/2026`) is
    another id, and after another country's code (`VAT DE 123456789`) the digits are another firm's."""
    words = re.split(r"[\s,;()]+", party)
    for n, word in enumerate(words):
        bare = word[_TAX_LABEL.match(word).end() :] if _TAX_LABEL.match(word) else word
        before = words[n - 1].upper() if n else ""
        if bare.upper().removeprefix("RO") == cui and not (
            re.fullmatch(r"[A-Z]{2}", before) and before not in {"RO"} | companies.LEGAL_FORMS
        ):
            return True
    return False


def _readings(name: str) -> list[tuple[list[str], str]]:
    """A name's words (`identity.firm_name`, any alphabet) as written, and, when its last word is a legal form,
    without it: (words, the form left out)."""
    words = firm_name(name).split("-")
    last = words[-1] if len(words) > 1 and words[-1].upper() in companies.LEGAL_FORMS - {"SC"} else ""
    return [(words, ""), (words[:-1], last)] if last else [(words, "")]


def same_payer(named: str, paid: str) -> bool:
    """The report's payer name is the credit's paying party: the same words, one final legal form missing on one side
    but never two different ones ('Curier SRL' = 'Curier S.R.L.' = 'Curier', != 'Curier SA'); a form-like word inside
    a name ('Atelier Sa Maison', 'Atelier Sa SRL') is one of its words."""
    return any(
        len("".join(x)) >= 6 and x == y and not (fx and fy and fx != fy)
        for x, fx in _readings(named)
        for y, fy in _readings(paid)
    )


class Resolver:
    """Index company-local invoice and order identities once, with constant-time line lookups."""

    def __init__(self, L: Ledger, company: str, invoices: list) -> None:
        self.L, self.company = L, company
        self.invoices = {i["message_id"]: i for i in invoices}
        self.numbers: dict[str, set[str]] = {}
        for i in invoices:
            if i["status"] != "duplicate":
                self.numbers.setdefault(i["number"], set()).add(i["message_id"])
        self.orders: dict[tuple, set[str]] | None = None
        self.order_invoices: dict[str, set[str]] = {}

    def _index_orders(self) -> None:
        from fiscalm.invoicing import engine as invoicing

        links: dict[str, set[str]] = {}
        for mid, local in invoicing.sent_links(self.L, self.company).items():
            links.setdefault(local, set()).add(mid)
        self.orders = {}
        for o in self.L.db.execute(
            "SELECT * FROM commerce_orders WHERE company=? AND status!='cancelled'", (self.company,)
        ):
            awb = json.loads(o["awb"]) if o["awb"] else {}
            identities = [(o["external_id"], None, None)]
            if awb.get("number") and awb.get("courier"):
                identities += [
                    (None, awb["number"], awb["courier"].casefold()),
                    (o["external_id"], awb["number"], awb["courier"].casefold()),
                ]
            for identity in identities:
                for channel in (None, o["channel"]):
                    self.orders.setdefault((*identity, channel), set()).add(o["id"])
            self.order_invoices[o["id"]] = links.get(o["draft_id"], set())

    def resolve(self, report: dict, line: dict) -> list:
        if line.get("company", self.company) != self.company:
            return []
        choices: list[set[str]] = []
        if line.get("reference"):
            choices.append(self.numbers.get(line["reference"], set()))
        if line.get("order") or line.get("awb"):
            orders = self._orders(report, line, line.get("order"), line.get("awb"))
            choices.append(self.order_invoices[next(iter(orders))] if len(orders) == 1 else set())
        if not choices or any(len(ids) != 1 for ids in choices) or len(set.union(*choices)) != 1:
            return []
        return [self.invoices[mid] for mid in choices[0] if mid in self.invoices]

    def _orders(self, report: dict, line: dict, order: str | None, awb: str | None) -> set[str]:
        if self.orders is None:
            self._index_orders()
        courier = line.get("courier", report.get("courier", report["source"])).casefold() if awb else None
        return self.orders.get((order or None, awb or None, courier, report.get("channel")), set())

    def miss(self, report: dict, line: dict) -> str:
        """Why a line names no single invoice: an AWB or order no shop order has, else the invoice reference."""
        if line.get("awb") and not self._orders(report, line, None, line["awb"]):
            return "awb_unknown"
        if line.get("order") and not self._orders(report, line, line["order"], None):
            return "order_unknown"
        return "invoice_missing_or_ambiguous"


def verify(L: Ledger, stored: sqlite3.Row, st: statements.Statement) -> tuple[statements.Tx, list[str], Decimal, str]:
    """Called only under Ledger's write lock; exact identifiers, full amounts, no credits/storno or guesses."""
    r = json.loads(stored["data"])
    if st.company != stored["company"]:
        raise SettlementError("wrong_company")
    gross = sum((money(x["gross"]) for x in r["lines"]), Decimal(0))
    fee = sum((money(x["fee"]) for x in r["lines"]), Decimal(0))
    candidates = [
        t
        for t in st.settlement_txs
        if t.iban in companies.get(st.company, L=L).own_ibans
        and t.currency == r["currency"]
        and t.amount == gross - fee
        and 0 <= (t.date - date.fromisoformat(r["date"])).days <= 14
        and _payer(r, t, st.company, claim=True)
    ]
    # Every credit that may be this payer's counts: one only, and it must pass the strict payer check.
    if len(candidates) != 1 or not _payer(r, candidates[0], st.company):
        raise SettlementError("credit_missing_or_ambiguous")
    t = candidates[0]
    if statements.tx_key(t) in L.used_txs():
        raise SettlementError("credit_used")
    # Equal reports must not compete for one credit, even if their invoice sets differ.
    for other in L.db.execute(
        "SELECT * FROM settlement_reports WHERE company=? AND id!=? AND status='pending'", (st.company, stored["id"])
    ):
        o = json.loads(other["data"])
        net = sum((money(x["gross"]) - money(x["fee"]) for x in o["lines"]), Decimal(0))
        if (
            net == t.amount
            and o["currency"] == t.currency
            and _payer(o, t, st.company, claim=True)  # a report that may be this payer's competes
            and 0 <= (t.date - date.fromisoformat(o["date"])).days <= 14
        ):
            raise SettlementError("competing_reports")
    credited = issued._credited(L, st.company)
    partial = covered(L, st.company, "issued")  # paid in part by the user's links: never collected whole here
    all_rows = L.issued_rows(company=st.company)
    resolver = Resolver(L, stored["company"], all_rows)
    reversed_ids = {
        x[0]
        for x in L.db.execute(
            "SELECT l.message_id FROM invoice_sent_links l JOIN invoices i ON i.id=l.invoice_id "
            "WHERE i.company=? AND (i.status!='issued' OR EXISTS (SELECT 1 FROM invoice_reversals v WHERE v.original_id=i.id))",
            (st.company,),
        )
    }
    mids: list[str] = []
    seen: set[str] = set()
    for n, line in enumerate(r["lines"], 1):
        matches = resolver.resolve(r, line)
        if len(matches) != 1:
            raise SettlementError(f"line_{n}_{resolver.miss(r, line)}")
        i = matches[0]
        try:
            valid_date = bool(i["issue_date"]) and date.fromisoformat(i["issue_date"]) <= t.date
            valid_amount = Decimal(i["total"]) == money(line["gross"])
        except (ValueError, TypeError, ArithmeticError):
            raise SettlementError(f"line_{n}_invalid_invoice") from None
        if (
            i["status"] != "open"
            or i["parse_error"]
            or i["doc_type"] != "invoice"
            or i["currency"] != r["currency"]
            or not valid_amount
            or credited.get(i["message_id"], Decimal(0))
            or i["message_id"] in partial
            or i["message_id"] in seen
            or i["message_id"] in reversed_ids
            or not valid_date
        ):
            raise SettlementError(f"line_{n}_invoice_not_open_or_amount_differs")
        mids.append(i["message_id"])
        seen.add(i["message_id"])
    return t, mids, fee, r["currency"]


def match_statement(L: Ledger, st: statements.Statement) -> list[str]:
    return recheck_pending(L, st.company)


def recheck_pending(L: Ledger, company: str) -> list[str]:
    """Re-evaluate pending reports against current stored evidence through guarded settlement writes."""
    done: list[str] = []
    evidence: dict[frozenset[str], statements.Statement | str] = {}
    # `confirmation_required` (the retired payer gate, before 2026-11-25) is replaced like the other waiting reasons.
    for r in L.db.execute(
        "SELECT id,data FROM settlement_reports WHERE company=? AND status='pending' ORDER BY id", (company,)
    ).fetchall():
        try:
            months = frozenset(window_months(json.loads(r["data"])["date"]))
            if months not in evidence:
                try:
                    evidence[months] = bank_evidence(L, company, months=set(months))
                except (OSError, RuntimeError, ValueError) as exc:
                    evidence[months] = str(exc) if isinstance(exc, SettlementError) else "statement_unavailable"
            proof = evidence[months]
            if isinstance(proof, str):
                raise SettlementError(proof)
            done += L.settle_report(r["id"], proof)
        except (ValueError, TypeError, KeyError, ArithmeticError) as e:
            reason = str(e) if isinstance(e, SettlementError) else "invalid_report"
            with L.tx() as db:
                db.execute(
                    "DELETE FROM settlement_credits WHERE report_id IN (SELECT id FROM settlement_reports WHERE id=? AND status='pending')",
                    (r["id"],),
                )
                db.execute(
                    "UPDATE settlement_reports SET error=? WHERE id=? AND status='pending' "
                    "AND error IS NOT ? AND (? != 'credit_missing_or_ambiguous' OR error IS NULL OR error IN ('confirmation_required','statement_confirmation_required','competing_reports'))",
                    (reason, r["id"], reason, reason),
                )
    return done


def state(L: Ledger, company: str) -> list[dict]:
    return snapshot(L, company)[0]


def claimed_credit(L: Ledger, company: str, credit: statements.Tx) -> bool:
    """Pending reports keep their payer's credits out of single-invoice matching, including failed totals."""
    for r in L.db.execute("SELECT data FROM settlement_reports WHERE company=? AND status='pending'", (company,)):
        try:
            if _payer(json.loads(r["data"]), credit, company, claim=True):
                return True
        except (ValueError, TypeError, KeyError):
            continue  # match_statement records invalid_report; malformed evidence proves no credit
    return False


def nearest_credit(L: Ledger, company: str, report: dict, net: Decimal) -> dict | None:
    candidates = []
    for row in L.db.execute("SELECT tx_key,tx FROM statement_txs WHERE company=? AND direction='credit'", (company,)):
        try:
            tx = statements.Tx.from_dict(json.loads(row["tx"]))
            if (
                tx.currency == report["currency"]
                and tx.iban in companies.get(company, L=L).own_ibans
                and 0 <= (tx.date - date.fromisoformat(report["date"])).days <= 14
                and _payer(report, tx, company)
            ):
                candidates.append((abs(tx.amount - net), row["tx_key"], tx))
        except (ValueError, TypeError, KeyError, ArithmeticError):
            continue  # An unreadable line offers no useful comparison; it can never authorize settlement.
    if not candidates:
        return None
    _, key, tx = min(candidates, key=lambda x: (x[0], x[1]))
    return {
        "id": key,
        "date": tx.date.isoformat(),
        "amount": f"{tx.amount:.2f}",
        "currency": tx.currency,
        "payer": statements.masked(tx.counterparty),
    }


def snapshot(L: Ledger, company: str) -> tuple[list[dict], list[dict]]:
    reports, fees = [], []
    resolver = None
    for r in L.db.execute(
        "SELECT r.*,f.amount,f.currency,"
        "(SELECT json_group_array(json_object('position',l.position,'invoice',l.invoice_id)) "
        "FROM settlement_lines l JOIN issued i ON i.message_id=l.invoice_id AND i.company=r.company "
        "WHERE l.report_id=r.id) AS proof, "
        "p.credit,p.invoice_count,EXISTS(SELECT 1 FROM settlement_confirmations h WHERE h.report_id=r.id) AS human_confirmed "
        "FROM settlement_reports r LEFT JOIN settlement_credits p ON p.report_id=r.id LEFT JOIN settlement_fees f ON f.report_id=r.id "
        "WHERE r.company=? AND r.status!='withdrawn' ORDER BY r.id",
        (company,),
    ):
        data = json.loads(r["data"])
        proof = {p["position"]: p["invoice"] for p in json.loads(r["proof"])}
        if r["status"] == "pending":
            if resolver is None:
                resolver = Resolver(L, company, L.issued_rows(company=company))
            for n, line in enumerate(data["lines"], 1):
                matches = resolver.resolve(data, line)
                if len(matches) == 1:
                    proof[n] = matches[0]["message_id"]
        lines = [
            {
                "position": n,
                "reference": line.get("reference") or None,
                "awb": line.get("awb") or None,
                "order": line.get("order") or None,
                "gross": line["gross"],
                "fee": line["fee"],
                "invoice": proof.get(n),
                "proved": r["status"] == "collected" and n in proof,
            }
            for n, line in enumerate(data["lines"], 1)
        ]
        gross = sum((Decimal(x["gross"]) for x in lines), Decimal(0))
        fee = sum((Decimal(x["fee"]) for x in lines), Decimal(0))
        error = (r["error"] or "credit_missing_or_ambiguous") if r["status"] == "pending" else None
        failed = re.match(r"line_(\d+)_", error or "")
        reports.append(
            {k: r[k] for k in ("id", "source", "report_key", "status", "tx_key")}
            | {
                "kind": "settlement",
                "reference": None if r["report_key"] == "u" + r["digest"][:24] else r["report_key"],
                "candidate": nearest_credit(L, company, data, gross - fee)
                if error == "credit_missing_or_ambiguous"
                else None,
                "company": company,
                "partner": data.get("partner")
                or partner_id(company, data.get("payer_cui") or None, data["payer_name"]),
                "payer": {"id": data.get("payer_id", ""), "name": data["payer_name"]},
                "date": data["date"],
                "currency": data["currency"],
                "gross": f"{gross:.2f}",
                "fee": f"{fee:.2f}",
                "net": f"{gross - fee:.2f}",
                "lines": lines,
                "error": error,
                "credit": json.loads(r["credit"]) if r["credit"] else None,
                "invoice_count": r["invoice_count"] if r["credit"] else len(lines),
                "human_confirmed": bool(r["human_confirmed"]),
                "can_confirm": r["status"] == "pending"
                and error in ("confirmation_required", "statement_confirmation_required")  # the first: retired gate
                and bool(r["credit"]),
                "failed_line": int(failed[1]) if failed else None,
                "sheet": {"co": company, "set": r["id"]},
                "links": {"transaction": r["tx_key"], "issued": list(dict.fromkeys(proof[n] for n in sorted(proof)))},
            }
        )
        if r["amount"] is not None:
            fees.append(
                {"report_id": r["id"], "amount": r["amount"], "currency": r["currency"], "kind": "settlement_fee"}
            )
    return reports, fees


def tasks(reports: list[dict]) -> list[dict]:
    return [
        {
            "kind": "settlement_review",
            "id": r["id"],
            "company": r["company"],
            "reason": r["error"],
            "line": r["failed_line"],
            "sheet": r["sheet"],
            "credit": r["credit"],
            "invoice_count": r["invoice_count"],
            "net": r["net"],
            "currency": r["currency"],
        }
        for r in reports
        if r["status"] == "pending"
    ]


def transactions(reports: list[dict]) -> list[dict]:
    return [
        {
            "id": r["tx_key"],
            "company": r["company"],
            "settlement": r["id"],
            "sheet": {"co": r["company"], "tx": r["tx_key"]},
            "links": {"settlement": r["id"], "issued": r["links"]["issued"]},
            "fee": r["fee"],
            "currency": r["currency"],
        }
        for r in reports
        if r["status"] == "collected"
    ]


def partner_money(
    L: Ledger, company: str, partners: list[dict], invoices: list[dict], reports: list[dict], today: date
) -> None:
    """Enrich the same partners; only explicit paid-card or delivered-COD evidence counts as money held."""
    by = {p["id"]: p for p in partners}

    def entry(pid: str, name: str, cui: str | None, role: str) -> dict:
        if pid not in by:
            by[pid] = dict(
                id=pid,
                company=company,
                name=name,
                cui=cui,
                roles=[],
                last="",
                received=0,
                issued=0,
                late=0,
                payable={"total": "0.00", "other": []},
                receivable={"total": "0.00", "other": []},
            )
            partners.append(by[pid])
        p = by[pid]
        if role not in p["roles"]:
            p["roles"].append(role)
        p.setdefault("held", [])
        p.setdefault("report_formats", [])
        return p

    for row in L.db.execute(
        "SELECT partner,signature,data FROM partner_settlement_formats WHERE company=?", (company,)
    ):
        d = json.loads(row["data"])
        p = entry(
            row["partner"],
            d["name"],
            d["cui"] or None,
            "processor" if d["source"] in ("stripe", "netopia", "payu", "processor") else "courier",
        )
        p["report_formats"].append(
            {
                "signature": row["signature"],
                "source": d["source"],
                "mapping": d["mapping"],
                "courier": d.get("courier", ""),
            }
        )
    claims: dict[str, set[str]] = {}
    amounts = {i["id"]: i["total"] for i in invoices}
    for report in reports:
        p = entry(
            report["partner"],
            report["payer"]["name"],
            None,
            "processor" if report["source"] in ("stripe", "netopia", "payu", "processor") else "courier",
        )
        if report["status"] == "pending":
            for line in report["lines"]:
                if line["invoice"] and Decimal(line["gross"]) == Decimal(amounts.get(line["invoice"], "0")):
                    claims.setdefault(line["invoice"], set()).add(p["id"])
    for mid, name, role in order_holders(L, company, today):
        pid = partner_id(company, None, name)
        same = [
            p
            for p in partners
            if firm_name(p["name"]) == firm_name(name)
            or any(
                (
                    (f["source"] not in ("courier", "processor") and f["source"] == name.casefold())
                    or f.get("courier", "").casefold() == name.casefold()
                )
                for f in p.get("report_formats", [])
            )
        ]
        if len(same) > 1:
            continue  # a shared courier name/code proves no partner
        pid = same[0]["id"] if same else pid
        entry(pid, name, None, role)
        claims.setdefault(mid, set()).add(pid)
    for inv in invoices:
        ids = claims.get(inv["id"], set())
        if (
            len(ids) == 1
            and inv["status"] in ("open", "marked")
            and not inv.get("rejected")
            and Decimal(inv["owed"]) > 0
        ):
            by[next(iter(ids))]["held"].append(
                {"invoice": inv["id"], "amount": inv["owed"], "currency": inv["currency"]}
            )
    for p in partners:
        p.setdefault("held", [])
        p.setdefault("report_formats", [])


def order_holders(L: Ledger, company: str, today: date) -> list[tuple[str, str, str]]:
    """Only the company's issued, SPV-linked orders with explicit customer-payment evidence."""
    out = []
    for row in L.db.execute(
        "SELECT o.body,o.awb,d.returned_at,d.order_id AS delivery,l.message_id "
        "FROM commerce_orders o JOIN invoices i ON i.id=o.draft_id AND i.company=o.company "
        "JOIN invoice_sent_links l ON l.invoice_id=i.id AND l.environment=? JOIN issued v ON v.message_id=l.message_id AND v.company=o.company AND v.status!='duplicate' "
        "LEFT JOIN commerce_delivery d ON d.order_id=o.id "
        "WHERE o.company=? AND o.status!='cancelled' AND i.status='issued'",
        (anaf.inbox_env(), company),
    ):
        d = json.loads(row["body"])
        awb = json.loads(row["awb"]) if row["awb"] else {}
        if row["delivery"]:
            awb.update(returned_at=row["returned_at"])
        payment = d["payment"]
        name = (
            payment.get("processor")
            if payment["method"] == "card" and payment["paid"]
            else awb.get("courier")
            if payment["method"] == "cod"
            and awb.get("delivered_at")
            and awb["delivered_at"] <= today.isoformat()
            and not awb.get("returned_at")
            else None
        )
        if name:
            out.append((row["message_id"], name, "processor" if payment["method"] == "card" else "courier"))
    return out


def payout_partner(
    L: Ledger, company: str, tx: statements.Tx, mids: list[str], selected: str, identities: dict[str, str | None]
) -> str | None:
    """Only the holder explicitly chosen for the user's invoice set; `_left` already rechecked invoice eligibility."""
    rows = [
        {"id": r["message_id"], "total": r["total"], "currency": r["currency"], "owed": r["total"], "status": "open"}
        for r in L.issued_rows(company=company)
    ]
    # Use the same document identities the API supplies to ordinary links, including CUI-based partners.
    named = {}
    for prefix, field, records in (
        ("issued ", "client_name", L.issued_rows(company=company)),
        ("efactura ", "supplier_name", L.efactura_rows(company=company)),
    ):
        for record in records:
            pid = identities.get(prefix + record["message_id"])
            if pid:
                named[pid] = {"id": pid, "name": record[field], "roles": []}
    partners: list[dict] = list(named.values())
    partner_money(L, company, partners, rows, snapshot(L, company)[0], tx.date)
    found = next((p for p in partners if p["id"] == selected), None)
    ids = {h["invoice"] for h in found["held"]} if found else set()
    return selected if set(mids) <= ids else None


CONFIRMED = "Decontare confirmată."


REFUSED = "Decontarea nu se poate confirma. Verifică din nou raportul și extrasul."


_HUMAN_CONFIRMATION = object()


@dataclass
class BankEvidence(statements.Statement):
    legacy_proofs: dict[str, tuple[str, str]] = dataclass_field(default_factory=dict)
    months: frozenset[str] = frozenset()
    versions: tuple[tuple[str, str], ...] = ()


def window_months(day: str) -> set[str]:
    start = date.fromisoformat(day)
    return {(start + timedelta(days=n)).strftime("%Y-%m") for n in range(15)}


def report_months(L: Ledger, company: str, report_id: str | None = None) -> set[str]:
    months: set[str] = set()
    for r in L.db.execute(
        "SELECT data FROM settlement_reports WHERE company=? AND status='pending' AND (? IS NULL OR id=?)",
        (company, report_id, report_id),
    ):
        months.update(window_months(json.loads(r[0])["date"]))
    return months


def evidence_documents(L: Ledger, company: str, months: set[str]) -> list:
    # A partial CSV can span months and is stored under its first month.
    return [
        d
        for d in L.documents(company=company, kind="statement")
        if d["month"] in months or str(d["path"]).endswith(".csv")
    ]


def csv_evidence(path: str, root: Path) -> tuple[str, dict]:
    side = read_file(path + ".json", [root], 64 * 1024)
    try:
        mapping = json.loads(side[1]) if side else None
        if (
            not isinstance(mapping, dict)
            or not isinstance(mapping.get("iban"), str)
            or not isinstance(mapping.get("mapping"), dict)
        ):
            raise ValueError("Invalid CSV metadata")
        return mapping["iban"], mapping["mapping"]
    except (ValueError, TypeError) as exc:
        raise SettlementError("statement_unavailable") from exc


def evidence_versions(L: Ledger, company: str, months: set[str]) -> list[tuple[str, str]]:
    return sorted((d["sha"], d["path"]) for d in evidence_documents(L, company, months))


def bank_evidence(L: Ledger, company: str, *, months: set[str] | None = None) -> statements.Statement:
    months = report_months(L, company) if months is None else months
    versions = tuple(evidence_versions(L, company, months))
    txs = {}
    stored_keys: set[str] = set()

    def merge(st: statements.Statement, seen: set[str]) -> None:
        local: set[str] = set()
        for t in st.settlement_txs:
            if t.direction != "credit" or t.date.strftime("%Y-%m") not in months:
                continue
            key = statements.tx_key(t)
            if t.direction == "credit" and (key in local or key in seen):
                raise SettlementError("credit_missing_or_ambiguous")
            local.add(key)
            txs.setdefault(key, t)
        seen.update(local)

    legacy = {}
    root = finance_home().resolve() / company
    for d in evidence_documents(L, company, months):
        data = read_file(d["path"], [root], 32 * 1024 * 1024)
        if not data:
            raise SettlementError("statement_unavailable")
        csv_account = csv_evidence(d["path"], root) if data[0].endswith(".csv") else None
        actual_sha = statement_identity(data[1], csv_account)
        # CAMT is stored unchanged; PDFs have a separate decrypted-copy digest.
        expected_sha = L.get_meta("statement_stored_sha:" + d["sha"]) if data[1].startswith(b"%PDF-") else d["sha"]
        if expected_sha and actual_sha != expected_sha:
            raise SettlementError("statement_unavailable")
        if data[1].startswith(b"%PDF-"):
            try:
                text = pdf_text(data[1])
            except RuntimeError as exc:
                raise SettlementError("statement_unavailable") from exc
            if not statements.names_cui(text, companies.get(company, L=L).cui):
                raise SettlementError("statement_unavailable")
            st = statements.parse_text(text, company)
            if not expected_sha:
                for t in st.settlement_txs:
                    legacy[statements.tx_key(t)] = (d["sha"], actual_sha)
        else:
            st = parse_csv(data[1], company, *csv_account) if csv_account else parse_structured(data[1], company)[0]
        merge(st, stored_keys)
    return BankEvidence(
        company, None, None, [statements.Account("", "", list(txs.values()))], legacy, frozenset(months), versions
    )


def perform(L: Ledger, company: str, body: dict, actor: str, authorize: Callable[[], None] = lambda: None) -> dict:
    from fiscalm.api.actions import _res

    if (
        not isinstance(body, dict)
        or not all(isinstance(v, str) for v in body.values())
        or not re.fullmatch(r"[0-9a-f]{32}", body.get("id", ""))
        or not re.fullmatch(r"[0-9a-f]{20}", body.get("tx", ""))
        or body.get("confirmed") != "yes"
        or set(body) - {"co", "id", "tx", "confirmed"}
        or body.get("co", company) != company
        or not actor
    ):
        return _res(False, REFUSED)
    try:
        if not L.db.execute(
            "SELECT 1 FROM settlement_reports WHERE id=? AND company=? AND status='pending'", (body["id"], company)
        ).fetchone():
            raise SettlementError("report_unavailable")
        months = report_months(L, company, body["id"])
        versions = evidence_versions(L, company, months)
        statement = bank_evidence(L, company, months=months)
        with L.tx():
            authorize()
            if versions != evidence_versions(L, company, months):
                raise SettlementError("statement_changed")
            r = L.db.execute(
                "SELECT id FROM settlement_reports WHERE id=? AND company=? AND status='pending'",
                (body.get("id"), company),
            ).fetchone()
            if not r:
                raise SettlementError("report_unavailable")
            L.settle_report(
                r["id"], statement, confirmed_by=actor, expected_tx=body.get("tx"), _confirmation=_HUMAN_CONFIRMATION
            )
        return _res(True, CONFIRMED)
    except PermissionError:
        raise
    except (SettlementError, OSError, ValueError, RuntimeError) as exc:
        if not (isinstance(exc, SettlementError) and str(exc) == "confirmation_refused"):
            with L.tx() as db:
                authorize()
                db.execute(
                    "DELETE FROM settlement_credits WHERE report_id IN (SELECT id FROM settlement_reports WHERE id=? AND company=? AND status='pending')",
                    (body["id"], company),
                )
                db.execute(
                    "UPDATE settlement_reports SET error=? WHERE id=? AND company=? AND status='pending'",
                    (
                        str(exc) if isinstance(exc, SettlementError) else "statement_unavailable",
                        body["id"],
                        company,
                    ),
                )
        return _res(False, REFUSED)

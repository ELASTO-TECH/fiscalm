"""CAMT.053 booked entries, checked against each account's opening/closing balances."""

from __future__ import annotations

import calendar
import re
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from xml.etree.ElementTree import Element
from zoneinfo import ZoneInfo

from fiscalm import companies
from fiscalm.bank.statements import Account, PartialMonth, Statement, StatementError, Tx, iso_category, tx_key

MAX_XML = 10 * 1024 * 1024


def parse_camt(path: Path | bytes, company: str = "") -> Statement:
    """Refuse unknown ownership and unsupported shapes before exposing any transactions."""
    from fiscalm.efactura.inbox import UblError, safe_xml

    if isinstance(path, bytes):
        data = path
    else:
        with Path(path).open("rb") as stream:
            data = stream.read(MAX_XML + 1)
    if len(data) > MAX_XML:
        raise StatementError("CAMT XML too large")
    try:
        declaration = re.match(rb"\s*<\?xml[^>]*encoding=[\"']([^\"']+)[\"']", data)
        if declaration:
            encoding = declaration[1].decode("ascii").lower()
            if encoding not in ("utf-8", "utf-8-sig", "iso-8859-1", "windows-1250", "cp1250"):
                raise StatementError("Unsupported CAMT XML encoding")
            data = data.decode(encoding).encode("utf-8")
        root = safe_xml(data)
        match = re.fullmatch(r"\{(urn:iso:std:iso:20022:tech:xsd:camt\.053\.001\.0[2-8])\}Document", root.tag)
        if not match:
            raise StatementError("Expected CAMT.053.001.02 through .08 XML")
        return _Camt(root, match[1]).statement(company)
    except UblError as exc:
        raise StatementError("Invalid CAMT XML: " + str(exc)) from exc
    except (ValueError, InvalidOperation) as exc:
        raise StatementError("Invalid CAMT statement; check XML dates, amounts and structure") from exc


class _Camt:
    """Read a single namespaced statement, refusing incomplete or ambiguous bank evidence."""

    def __init__(self, root: Element, namespace: str) -> None:
        self.root = root
        self.ns = {"c": namespace}

    def nodes(self, node: Element, path: str) -> list[Element]:
        return node.findall("/".join("c:" + p for p in path.split("/")), self.ns)

    def text(self, node: Element, path: str) -> str:
        found = self.nodes(node, path)
        if len(found) > 1:
            raise StatementError("Duplicate CAMT field: " + path)
        return (found[0].text or "").strip() if found else ""

    def amount(self, node: Element, path: str = "Amt") -> tuple[Decimal, str]:
        found = self.nodes(node, path)
        if len(found) != 1:
            raise StatementError("CAMT amount missing or ambiguous")
        raw = (found[0].text or "").strip()
        if not re.fullmatch(r"\d{1,18}(?:\.\d{1,5})?", raw):
            raise StatementError("Invalid CAMT amount")
        ccy = found[0].get("Ccy", "")
        if not re.fullmatch(r"[A-Z]{3}", ccy):
            raise StatementError("CAMT currency missing")
        return Decimal(raw).quantize(Decimal("0.00001")).normalize(), ccy

    def direction(self, node: Element) -> str:
        code = self.text(node, "CdtDbtInd")
        if code not in ("DBIT", "CRDT"):
            raise StatementError("CAMT credit/debit indicator missing")
        return "debit" if code == "DBIT" else "credit"

    def day(self, node: Element, path: str) -> date | None:
        value = self.text(node, path + "/Dt") or self.text(node, path + "/DtTm")
        return self.timestamp(value) if value else None

    def timestamp(self, value: str) -> date:
        if "T" not in value:
            return date.fromisoformat(value)
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.astimezone(ZoneInfo("Europe/Bucharest")).date() if dt.tzinfo else dt.date()

    def detail_amount(self, detail: Element, details: list[Element], total: Decimal, currency: str) -> Decimal:
        value = total
        paths = ("Amt", "AmtDtls/TxAmt/Amt", "AmtDtls/CntrValAmt/Amt")
        has_amount = any(self.nodes(detail, p) for p in paths)
        if len(details) > 1 or (details and has_amount):
            candidates = [p for p in paths if self.nodes(detail, p) and self.amount(detail, p)[1] == currency]
            if not candidates:
                raise StatementError("CAMT detail has no amount in account currency")
            if len({self.amount(detail, p)[0] for p in candidates}) != 1:
                raise StatementError("CAMT detail amounts in account currency disagree")
            chosen = candidates[0]
            value, _ = self.amount(detail, chosen)
        return value

    def references(self, detail: Element, entry: Element) -> tuple[str, str]:
        remittance = [
            n.text.strip()
            for p in ("RmtInf/Ustrd", "RmtInf/Strd/CdtrRefInf/Ref")
            for n in self.nodes(detail, p)
            if n.text and n.text.strip()
        ]
        refs = [
            self.text(detail, "Refs/AcctSvcrRef") or self.text(entry, "AcctSvcrRef"),
            self.text(detail, "Refs/EndToEndId"),
            self.text(detail, "Refs/TxId"),
        ]
        ref = "|".join(r for r in refs if r and r != "NOTPROVIDED")
        if not ref:
            raise StatementError("CAMT booked transaction has no stable bank reference")
        return " ".join(remittance), ref

    def transaction(
        self, detail: Element, entry: Element, value: Decimal, flow: str, booking: date, currency: str, iban: str
    ) -> Tx:
        if self.text(detail, "CdtDbtInd") and self.direction(detail) != flow:
            raise StatementError("CAMT mixed batch directions refused")
        party = "Cdtr" if flow == "debit" else "Dbtr"
        name = self.text(detail, f"RltdPties/{party}/Nm") or self.text(detail, f"RltdPties/{party}/Pty/Nm")
        other = self.text(detail, f"RltdPties/{party}Acct/Id/IBAN")
        remittance, ref = self.references(detail, entry)
        original = (
            self.amount(detail, "AmtDtls/InstdAmt/Amt") if self.nodes(detail, "AmtDtls/InstdAmt/Amt") else (None, "")
        )
        bank_code = self.text(detail, "BkTxCd/Prtry/Cd") or self.text(entry, "BkTxCd/Prtry/Cd")
        prtry = {"CHGS": "CHRG", "FEE": "FEES"}.get(bank_code.upper(), bank_code)  # banks' spellings of ISO codes
        category = iso_category(prtry, *(self.text(node, "BkTxCd/Domn/Fmly/SubFmlyCd") for node in (entry, detail)))
        country = (
            self.text(detail, f"RltdPties/{party}/PstlAdr/Ctry")
            or self.text(detail, f"RltdPties/{party}/Pty/PstlAdr/Ctry")
        ).upper()
        value_date = self.day(entry, "ValDt")
        tx = Tx(
            booking,
            name,
            remittance,
            flow,
            value.quantize(Decimal("0.01")),
            currency,
            iban,
            ref=ref,
            bank_reference=self.text(detail, "Refs/AcctSvcrRef")
            or (self.text(entry, "AcctSvcrRef") if len(self.nodes(entry, "NtryDtls/TxDtls")) == 1 else ""),
            orig_amount=original[0],
            orig_currency=original[1],
            category=category,
            country=country if re.fullmatch(r"[A-Z]{2}", country) else "",
            counterparty=name,
            counterparty_iban=re.sub(r"\s+", "", other).upper(),
            value_date=value_date.isoformat() if value_date else "",
        )
        if value == 0:
            raise StatementError("CAMT zero amount refused")
        if value != value.quantize(Decimal("0.01")):
            raise StatementError("CAMT amount has unsupported fractional precision")
        return tx

    def booked(self, entry: Element) -> bool:
        for field in ("CdtDbtInd", "Sts", "BookgDt"):
            if len(self.nodes(entry, field)) != 1:
                raise StatementError("Missing or duplicate CAMT entry field: " + field)
        if (self.text(entry, "Sts/Cd") or self.text(entry, "Sts")) != "BOOK":
            return False
        if self.nodes(entry, "NtryDtls/TxDtls/RtrInf") or self.nodes(entry, "RtrInf"):
            raise StatementError("CAMT returned payments require manual review")
        if self.nodes(entry, "Chrgs") or self.nodes(entry, "NtryDtls/TxDtls/Chrgs"):
            raise StatementError("CAMT included charges require manual review; cannot allocate charges safely")
        if self.text(entry, "RvslInd") in ("true", "1"):
            raise StatementError("CAMT reversal entries require manual review")
        return True

    def entry(self, entry: Element, currency: str, iban: str, start: date, end: date) -> list[Tx]:
        if not self.booked(entry):
            return []
        total, ccy = self.amount(entry)
        if total == 0:
            raise StatementError("CAMT zero amount refused")
        if ccy != currency:
            raise StatementError("CAMT entry currency differs from account")
        flow = self.direction(entry)
        booking = self.day(entry, "BookgDt")
        if booking is None:
            raise StatementError("CAMT booked entry has no booking date")
        if not start <= booking <= end:
            raise StatementError("CAMT booking date outside declared period")
        details = self.nodes(entry, "NtryDtls/TxDtls")
        for batch in self.nodes(entry, "NtryDtls/Btch"):
            count = self.text(batch, "NbOfTxs")
            if not details or (count and (not count.isdigit() or int(count) != len(details))):
                raise StatementError("CAMT batch details are incomplete")
        parts = [
            self.transaction(
                detail, entry, self.detail_amount(detail, details, total, currency), flow, booking, currency, iban
            )
            for detail in details or [entry]
        ]
        if sum((p.amount for p in parts), Decimal(0)) != total:
            raise StatementError("CAMT batch does not add up to booked entry")
        return parts

    def balances(self, section: Element, currency: str) -> tuple[dict[str, Decimal], dict[str, date | None], bool, str]:
        balances: dict[str, Decimal] = {}
        balance_dates = {}
        prcd = False  # the previous statement's closing balance: dated the day before the period starts
        for bal in self.nodes(section, "Bal"):
            code = self.text(bal, "Tp/CdOrPrtry/Cd")
            if code == "PRCD":
                code, prcd = "OPBD", True
            if code not in ("OPBD", "CLBD"):
                continue
            value, ccy = self.amount(bal)
            currency = currency or ccy
            if code in balances or ccy != currency:
                raise StatementError("CAMT balances have ambiguous currency or type")
            balances[code] = value * (-1 if self.direction(bal) == "debit" else 1)
            balance_dates[code] = self.day(bal, "Dt")
        return balances, balance_dates, prcd, currency

    def period(
        self, section: Element, balances: dict[str, Decimal], balance_dates: dict[str, date | None], prcd: bool
    ) -> tuple[date, date]:
        period_start = self.text(section, "FrToDt/FrDtTm") or self.text(section, "FrToDt/FrDt")
        period_end = self.text(section, "FrToDt/ToDtTm") or self.text(section, "FrToDt/ToDt")
        start = self.timestamp(period_start) if period_start else balance_dates.get("OPBD")
        end = self.timestamp(period_end) if period_end else balance_dates.get("CLBD")
        if not start or not end:
            raise StatementError("CAMT statement period missing")
        if (
            start.day != 1
            or end.year != start.year
            or end.month != start.month
            or end.day != calendar.monthrange(start.year, start.month)[1]
        ):
            # a PRCD opening without a declared period is dated the day before the month it opens
            first = start + timedelta(days=1) if prcd and not period_start else start
            raise PartialMonth("CAMT", first.replace(day=1))
        if set(balances) != {"OPBD", "CLBD"}:
            raise StatementError("CAMT requires one opening (OPBD or PRCD) and one closing balance")
        if any(
            d is None or not (start - timedelta(days=1) if code == "OPBD" and prcd else start) <= d <= end
            for code, d in balance_dates.items()
        ):
            raise StatementError("CAMT balance date outside declared period")
        return start, end

    def account(
        self, section: Element, company: str, owners: dict[str, companies.Company], accounts: list[Account]
    ) -> tuple[str, Account, date, date]:
        iban = re.sub(r"\s+", "", self.text(section, "Acct/Id/IBAN")).upper()
        keys = [k for k, co in owners.items() if iban and iban in co.own_ibans]
        if len(keys) != 1 or (company and company != keys[0]):
            raise StatementError("CAMT account does not belong to the selected company")
        company = keys[0]
        currency = self.text(section, "Acct/Ccy")
        acct = Account(iban, currency)
        balances, balance_dates, prcd, currency = self.balances(section, currency)
        start, end = self.period(section, balances, balance_dates, prcd)
        acct.currency = currency
        if any(a.iban == iban and a.currency == currency for a in accounts):
            raise StatementError("Duplicate CAMT account and currency")
        for entry in self.nodes(section, "Ntry"):
            acct.txs.extend(self.entry(entry, currency, iban, start, end))
        debit = sum((t.amount for t in acct.txs if t.direction == "debit"), Decimal(0))
        credit = sum((t.amount for t in acct.txs if t.direction == "credit"), Decimal(0))
        acct.trustworthy = balances["OPBD"] + credit - debit == balances["CLBD"]
        if not acct.trustworthy:
            raise StatementError("CAMT account balances do not add up; import refused")
        acct.total_debit, acct.total_credit = debit, credit
        acct.closing, acct.closing_day = balances["CLBD"], balance_dates["CLBD"]
        return company, acct, start, end

    def statement(self, company: str) -> Statement:
        sections = self.nodes(self.root, "BkToCstmrStmt/Stmt")
        if not sections:
            raise StatementError("CAMT contains no statement accounts")
        accounts, starts, ends = [], [], []
        owners = companies.all()
        for section in sections:
            company, acct, start, end = self.account(section, company, owners, accounts)
            accounts.append(acct)
            starts.append(start)
            ends.append(end)
        if len(set(starts)) != 1 or len(set(ends)) != 1:
            raise StatementError("CAMT accounts must cover the same calendar month")
        keys = [tx_key(t) for a in accounts for t in a.txs]
        if len(keys) != len(set(keys)):
            raise StatementError("CAMT transaction identities are ambiguous")
        return Statement(company, min(starts), max(ends), accounts)

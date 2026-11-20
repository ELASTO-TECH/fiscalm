"""Disposable three-year ledger; no production paths or network. Run with uv run python tests/scale_benchmark.py.

Default per firm: 40,000 transactions, 15,000 received and 15,000 issued invoices,
3,000 receipts and 3,000 payments, across 15 firms. Unplaced mail is measured separately.
--divisor permits quick smoke runs. --output saves synthetic state for browser measurements.
"""

from __future__ import annotations

import argparse
import cProfile
import hashlib
import json
import os
import resource
import tempfile
from contextlib import nullcontext
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from time import perf_counter
from unittest.mock import patch

import tick_benchmark

from fiscalm import companies, secrets as secret_store
from fiscalm.ledger import Ledger
from fiscalm.mail import invoices as mail_invoices

NOW = datetime(2026, 10, 27, 9, 30)


def build(ledger, divisor: int) -> None:
    from fiscalm import companies
    from fiscalm.bank.statements import Tx

    days = [(NOW.date() - timedelta(days=i)).isoformat() for i in range(1095)]
    for ci, co in enumerate(companies.all(ledger)):
        for table, party, status in [("efactura", "supplier", "paid"), ("issued", "client", "collected")]:
            ledger.db.executemany(
                f"""INSERT INTO {table}(message_id,company,doc_type,number,issue_date,due_date,
                currency,total,{party}_name,{party}_cui,issues,raw_path,status,created_at)
                VALUES(?,?,'invoice',?,?,?,'RON','123.45',?,?,'[]','missing.zip',?,?)""",
                (
                    (
                        f"{table}-{ci}-{i}",
                        co,
                        f"SYN{i}",
                        days[i % 1095],
                        days[i % 1095],
                        f"Partener Sintetic {i % 200} SRL",
                        str(90000000 + i % 200),
                        "open" if i % 100 == 0 else status,
                        days[i % 1095],
                    )
                    for i in range(15000 // divisor)
                ),
            )
        ledger.db.executemany(
            """INSERT INTO documents(sha,company,month,kind,path,vendor,doc_date,amount,currency,created_at)
            VALUES(?,?,?,'receipt','missing.jpg','Magazin Sintetic',?,'42.00','RON',?)""",
            ((f"d-{ci}-{i}", co, days[i % 1095][:7], days[i % 1095], days[i % 1095]) for i in range(3000 // divisor)),
        )
        ledger.db.executemany(
            """INSERT INTO payments(item_id,company,kind,month,amount,status,created_at)
            VALUES(?,?,'op',?,'42.00','paid',?)""",
            ((f"p-{ci}-{i}", co, days[i % 1095][:7], days[i % 1095]) for i in range(3000 // divisor)),
        )

        def lines(ci=ci, co=co):
            for i in range(40000 // divisor):
                day = days[i % 1095]
                direction = "debit" if i % 2 else "credit"
                tx = Tx(
                    date.fromisoformat(day),
                    "Transfer",
                    "Synthetic",
                    direction,
                    Decimal("123.45"),
                    "RON",
                    companies.get(co, L=ledger).ron_iban,
                    ref=f"SYN{i}",
                    counterparty="Partener Sintetic",
                )
                yield (f"t-{ci}-{i}", co, direction, tx.iban, day, "123.45", json.dumps(tx.to_dict()))

        ledger.db.executemany(
            """INSERT INTO statement_txs(tx_key,company,direction,iban,day,amount,source,tx)
            VALUES(?,?,?,?,?,?,'file',?)""",
            lines(),
        )
    ledger.db.commit()


def seed_mail_questions(ledger: Ledger, count: int) -> None:
    data = json.dumps({"reason": "no-firm", "companies": [], "totals": [["123.45", "RON"]]})
    ledger.db.executemany(
        """INSERT INTO mail_questions(sha,account,message_id,address,sender,subject,received,data,path,created_at)
        VALUES(?,'mail-synthetic',?,'unknown@example.test','Synthetic','Synthetic','2026-10-27',?,'missing.pdf',?)""",
        (
            (hashlib.sha256(f"mail-{i}".encode()).hexdigest(), f"synthetic-{i}", data, NOW.isoformat())
            for i in range(count)
        ),
    )
    ledger.db.commit()


def mail_question_benchmark(ledger: Ledger, count: int) -> None:
    """Isolate unplaced-mail projection cost from historical rows, PDF parsing and model inference."""
    seed_mail_questions(ledger, count)
    connections = 0
    original = companies.all

    def registry(L=None):
        nonlocal connections
        connections += L is None
        return original(L)

    changes = ledger.db.total_changes
    start = perf_counter()
    with patch.object(companies, "all", registry):
        rows = mail_invoices.questions(ledger)
    elapsed = perf_counter() - start
    encoded = json.dumps(rows, ensure_ascii=False, sort_keys=True).encode()
    print(
        json.dumps(
            dict(
                phase="unplaced_mail_questions",
                rows=len(rows),
                seconds=round(elapsed, 3),
                registry_connections=connections,
                writes=ledger.db.total_changes - changes,
                bytes=len(encoded),
                digest=hashlib.sha256(encoded).hexdigest(),
                max_rss_mib=round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1),
            )
        ),
        flush=True,
    )


def detail_identity(kind: str, firm: int, divisor: int) -> str:
    table, count = {
        "invoice": ("efactura", 15000),
        "issued": ("issued", 15000),
        "document": ("d", 3000),
        "transaction": ("t", 40000),
    }[kind]
    index = count // divisor - 1
    if kind == "transaction":
        index -= index % 1095  # The sheet only lists the last 13 months; select today's cycle.
    return f"{table}-{firm}-{index}"


def measure_offline(ledger: Ledger, args: argparse.Namespace) -> bool:
    """Offline work has a different workload and timer from the state/read benchmarks."""
    if args.tick:
        start = perf_counter()
        build(ledger, args.divisor)
        seed_mail_questions(ledger, args.mail_questions or 30000)
        print(json.dumps({"build_s": round(perf_counter() - start, 3)}), flush=True)
        print(json.dumps(tick_benchmark.run(ledger, NOW, legacy_registry=args.tick == "legacy")), flush=True)
        return True
    if args.mail_questions:
        mail_question_benchmark(ledger, args.mail_questions)
        return True
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--firms", type=int, default=15)
    parser.add_argument("--divisor", type=int, default=1)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--profile", action="store_true")
    parser.add_argument("--summary", action="store_true", help="measure the bounded summary contract")
    parser.add_argument("--partner-pages", action="store_true", help="measure one partner's bounded initial histories")
    parser.add_argument(
        "--detail", choices=("invoice", "issued", "document", "transaction"), help="measure a sheet read"
    )
    parser.add_argument(
        "--scan-detail-owners", action="store_true", help="reproduce sequential ALL owner lookup with --detail"
    )
    parser.add_argument("--mail-questions", type=int, default=0, help="measure this many unplaced mails separately")
    parser.add_argument(
        "--tick",
        choices=("current", "legacy"),
        help="measure the full offline CLI tick; legacy reproduces pre-#396 registry reads",
    )
    parser.add_argument("--dashboard", choices=("upcoming", "held_money"), help="measure a bounded dashboard panel")
    parser.add_argument(
        "--full-dashboard", action="store_true", help="measure complete state on the same dashboard fixture"
    )
    parser.add_argument("--search", action="store_true", help="measure API search for the last synthetic invoice")
    parser.add_argument(
        "--browse",
        choices=(
            "vanzari",
            "cheltuieli",
            "bonuri",
            "parteneri",
            "tranzactii",
            "payment_file",
            "payment_files",
            "report_received",
            "report_issued",
            "report_tasks",
            "report_bank",
            "report_advances",
            "report_docs",
            "report_not_mailed",
        ),
        help="measure a first API list page",
    )
    parser.add_argument(
        "--report-open",
        choices=("separate", "shared"),
        help="measure all initial report sections, old or shared projection",
    )
    parser.add_argument("--month", help="report scope in YYYY-MM")
    parser.add_argument("--without-indexes", action="store_true", help="reproduce pre-index baseline")
    args = parser.parse_args()
    if args.scan_detail_owners and not args.detail:
        parser.error("scan-detail-owners requires detail")
    if args.firms < 1 or args.divisor < 1 or args.mail_questions < 0:
        parser.error("firms/divisor must be positive and mail-questions nonnegative")
    with tempfile.TemporaryDirectory(prefix="scale-") as tmp:  # under $TMPDIR
        home = Path(tmp)
        for name in ("HOME", "SECRETS", "WEB_CONFIG"):
            os.environ["FISCALM_" + name] = str(home / name.lower())
        from fiscalm.api import browse as browsing, lists, state as api

        ledger = Ledger()
        # Disposable installs share their synthetic public-ID key for exact response parity.
        secret_store.write_secret("payment-id-key", hashlib.sha256(b"synthetic-scale-fixture").hexdigest())
        for i in range(args.firms):
            ledger.save_company(
                companies.company_from(
                    dict(
                        key=f"SYN{i}",
                        name=f"Sintetic {i}",
                        legal_name=f"Firma Sintetica {i} SRL",
                        cui=str(80000000 + i),
                        accounts=[dict(iban=f"RO00TEST{i:016d}", currency="RON", pays=True)],
                    ),
                    i,
                )
            )
        try:
            if measure_offline(ledger, args):
                return
            start = perf_counter()
            build(ledger, args.divisor)
            if args.dashboard == "upcoming":
                # Exercise more than one page with the same open invoices and complete money in both modes.
                due = (NOW.date() + timedelta(days=10)).isoformat()
                for table in ("efactura", "issued"):
                    ledger.db.execute(f"UPDATE {table} SET due_date=? WHERE status='open'", (due,))
                ledger.db.commit()
            print(json.dumps({"build_s": round(perf_counter() - start, 3)}), flush=True)
            if args.without_indexes:
                for name in (
                    "payments_company_status",
                    "documents_company_month_kind",
                    "efactura_company_status",
                    "issued_company_status",
                    "statement_company_day",
                    "history_company_direction",
                ):
                    ledger.db.execute("DROP INDEX IF EXISTS " + name)
            for co in ("SYN0",) if args.report_open else ("SYN0", "ALL"):
                queries = 0

                def trace(sql: str) -> None:
                    nonlocal queries
                    if sql.lstrip().upper().startswith(("SELECT", "WITH")):
                        queries += 1

                ledger.db.set_trace_callback(trace)
                start = perf_counter()
                profiler = cProfile.Profile()
                if args.profile:
                    profiler.enable()
                if args.partner_pages:
                    firm = args.firms - 1 if co == "ALL" else 0
                    state = lists.partner_pages(ledger, co, {"id": f"SYN{firm}.90000199"}, NOW)
                elif args.detail:
                    firm = args.firms - 1 if co == "ALL" else 0
                    legacy = patch.object(lists.details, "owners", lambda _L, _kind, _id, keys: keys)
                    with legacy if args.scan_detail_owners else nullcontext():
                        state = lists.detail(
                            ledger,
                            co,
                            {"kind": args.detail, "id": detail_identity(args.detail, firm, args.divisor)},
                            NOW,
                        )
                elif args.dashboard:
                    state = (
                        api.state(ledger, co, NOW)
                        if args.full_dashboard
                        else lists.dashboard_page(ledger, co, {"collection": args.dashboard}, NOW)
                    )
                elif args.report_open:
                    query = {"month": args.month or NOW.strftime("%Y-%m")}
                    state = (
                        lists.report_pages(ledger, co, query, NOW)
                        if args.report_open == "shared"
                        else {
                            "data": [
                                {"screen": screen, "page": lists.browse(ledger, co, query | {"screen": screen}, NOW)}
                                for screen in browsing.REPORT_SCREENS
                            ],
                            "total": len(browsing.REPORT_SCREENS),
                            "next_cursor": None,
                        }
                    )
                else:
                    state = (
                        lists.search(ledger, co, {"q": f"SYN{15000 // args.divisor - 1}"}, NOW)
                        if args.search
                        else lists.browse(
                            ledger, co, {"screen": args.browse, **({"month": args.month} if args.month else {})}, NOW
                        )
                        if args.browse
                        else (lists.summary if args.summary else api.state)(ledger, co, NOW)
                    )
                if args.profile:
                    profiler.disable()
                    profiler.dump_stats(str(Path(tempfile.gettempdir()) / f"scale-{co}.prof"))
                elapsed = perf_counter() - start
                ledger.db.set_trace_callback(None)
                start = perf_counter()
                encoded = json.dumps(state, ensure_ascii=False).encode()
                print(
                    json.dumps(
                        dict(
                            co=co,
                            **(
                                {"total": state["total"], "returned": len(state["data"])}
                                if args.dashboard and not args.full_dashboard
                                else {}
                            ),
                            state_s=round(elapsed, 3),
                            json_s=round(perf_counter() - start, 3),
                            bytes=len(encoded),
                            **({"digest": hashlib.sha256(encoded).hexdigest()} if args.detail else {}),
                            queries=queries,
                            max_rss_mib=round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1),
                        )
                    ),
                    flush=True,
                )
                if args.output:
                    args.output.mkdir(parents=True, exist_ok=True)
                    (args.output / f"state-{co}.json").write_bytes(encoded)
                del state, encoded

        finally:
            ledger.close()


if __name__ == "__main__":
    main()

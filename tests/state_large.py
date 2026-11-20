"""Synthetic five-year state benchmark. Run: uv run python tests/state_large.py [--profile]."""

from __future__ import annotations

import calendar
import cProfile
import json
import os
import pstats
import subprocess
import sys
import tempfile
from collections.abc import Callable
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path
from time import perf_counter
from unittest.mock import patch

from fiscalm import companies
from fiscalm.api import state as api
from fiscalm.efactura import client as anaf
from fiscalm.ledger import Ledger

NOW = datetime(2026, 10, 27, 9, 30)


def build(L: Ledger, home: Path, scale: int = 100, *, received: int | None = None) -> None:
    """Per company: 30 statements, 30*scale received, 15*scale issued, 10*scale receipts."""
    environment = anaf.env()
    for ci, co in enumerate(companies.all()):
        folder = home / co
        folder.mkdir(parents=True, exist_ok=True)
        for i in range(30 * scale if received is None else received):
            month = api.months_back(59, NOW.date())[i % 60]
            day = month + "-10"
            mid = str(100000 + ci * 100000 + i)
            status = ("open", "paid", "linked", "skipped")[i % 4]
            pid = "p" + mid if status == "linked" else None
            if pid:
                L.db.execute(
                    "INSERT INTO payments(item_id,company,kind,month,amount,status,created_at) VALUES(?,?,'efactura',?,'123.45',?,?)",
                    (pid, co, month, "paid" if i % 8 == 2 else "pending", day),
                )
            L.db.execute(
                """INSERT INTO efactura(message_id,company,doc_type,number,issue_date,due_date,currency,total,
                supplier_name,supplier_cui,ibans,issues,raw_path,status,item_id,created_at,party_address)
                VALUES(?,?,'invoice',?,?,?,'RON','123.45',?,?,'[]','[]','missing.zip',?,?,?,?)""",
                (
                    mid,
                    co,
                    f"R{i}",
                    day,
                    day,
                    f"Furnizor Inventat {i % 200} SRL",
                    str(90000000 + i % 200),
                    status,
                    pid,
                    day,
                    json.dumps({"country": "RO", "city": "Oras Exemplu"}),
                ),
            )
        for i in range(15 * scale):
            day = api.months_back(59, NOW.date())[i % 60] + "-15"
            L.db.execute(
                """INSERT INTO issued(message_id,company,doc_type,number,issue_date,due_date,currency,total,
                client_name,client_cui,issues,raw_path,status,created_at) VALUES(?,?,'invoice',?,?,?,'RON','321.00',?,?,'[]','missing.zip',?,?)""",
                (
                    str(500000 + ci * 100000 + i),
                    co,
                    f"E{i}",
                    day,
                    day,
                    f"Client Inventat {i % 200} SRL",
                    str(90000000 + i % 200),
                    "open" if i % 4 == 0 else "collected",
                    day,
                ),
            )
        for i in range(10 * scale):
            month = api.months_back(59, NOW.date())[i % 60]
            day = month + "-10"
            path = folder / f"{day}_{co}_Shop{i % 200}_42.00RON_bon-{i}_bon-fiscal.jpg"
            path.write_bytes(b"synthetic image")
            L.db.execute(
                """INSERT INTO documents(sha,company,month,kind,path,vendor,doc_date,amount,currency,source,created_at)
                VALUES(?,?,?,'receipt',?,?,?,'42.00','RON','manual',?)""",
                (f"d{ci}-{i}", co, month, str(path), f"Shop{i % 200}", day, day),
            )
        # Trusted takeover: the file stays in the original company's inbox.
        original = next(c for c in companies.all() if c != co)
        inbox = home / original / "documents" / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        taken = inbox / f"takeover-{co}.jpg"
        taken.write_bytes(b"synthetic takeover")
        L.db.execute(
            """INSERT INTO upload_links(id,company,label,secret_hash,created_at,expires_at)
                        VALUES(?,?,'Synthetic','fake','2026-09-01','2027-01-01')""",
            (co, original),
        )
        L.db.execute(
            """INSERT INTO documents(sha,company,month,kind,path,source,created_at)
                        VALUES(?,?,'2026-09','receipt',?,'takeover:synthetic','2026-10-20')""",
            ("takeover-" + co, co, str(taken)),
        )
        L.db.execute(
            "INSERT INTO receipt_uploads(sha,link_id,company,uploaded_at,file_type) VALUES(?,?,?,'2026-09-01','image')",
            ("takeover-" + co, co, original),
        )
        # September was sent; its unsent receipt is a late arrival in October's pack.
        L.db.execute("INSERT INTO packs(company,month,status,note) VALUES(?,'2026-09','sent','sent 2026-10-01')", (co,))
        L.db.execute(
            "UPDATE documents SET sent_at='2026-10-01' WHERE company=? AND month='2026-09' AND sha!=?",
            (co, "takeover-" + co),
        )
        # The local command id differs from the linked SPV display id.
        iid, mid = "local-" + co, str(500000 + ci * 100000)
        data = dict(
            client=dict(name="Client Inventat 0 SRL", cui="90000000", country="RO", email="client@example.invalid"),
            totals=dict(total="321.00"),
            issue_date="2026-10-15",
            due_date="2026-10-15",
            currency="RON",
        )
        L.db.execute(
            "INSERT INTO invoices(id,company,series,number,status,data) VALUES(?,?,'SYN',1,'issued',?)",
            (iid, co, json.dumps(data)),
        )
        L.db.execute("UPDATE issued SET issue_date='2026-10-15',due_date='2026-10-15' WHERE message_id=?", (mid,))
        L.db.execute("INSERT INTO invoice_sent_links VALUES(?,?,?)", (mid, iid, environment))
        # Isolated merchants cover both inclusive edges and the first excluded day.
        for offset in (0, 5, 6):
            sha, mid = f"edge-{co}-{offset}", str(80000000 + ci * 100000 + offset)
            vendor = {0: "Orchard", 5: "Copper", 6: "Marble"}[offset]
            amount = f"{77 + offset}.00"
            path = folder / f"2026-10-10_{co}_{vendor}_{amount}RON_bon-{offset}_bon-fiscal.jpg"
            path.write_bytes(b"synthetic edge")
            L.db.execute(
                """INSERT INTO documents(sha,company,month,kind,path,vendor,doc_date,amount,currency,source,created_at)
                            VALUES(?,?,'2026-10','receipt',?,?,'2026-10-10',?,'RON','manual','2026-10-10')""",
                (sha, co, str(path), vendor, amount),
            )
            L.db.execute(
                """INSERT INTO efactura(message_id,company,doc_type,number,issue_date,currency,total,
                            supplier_name,ibans,issues,raw_path,status,created_at)
                            VALUES(?,?,'invoice',?,?,'RON',?,?,'[]','[]','missing.zip','open','2026-10-10')""",
                (mid, co, sha, f"2026-10-{10 + offset}", amount, vendor + " SRL"),
            )
        # Recurring invoices, a client per rule (every third one gone) and a skip: listing them costs a fixed few reads.
        L.db.execute("INSERT INTO invoice_series(company,code,start_number,next_number) VALUES(?,'SYN',1,2)", (co,))
        for i in range(scale):
            rid, client = f"rec-{co}-{i}", f"cli-{co}-{i}"
            if i % 3:
                L.db.execute(
                    "INSERT INTO invoice_clients(id,company,identity,data) VALUES(?,?,?,?)",
                    (client, co, client, json.dumps({"name": f"Client Inventat {i} SRL"})),
                )
            L.db.execute(
                """INSERT INTO invoice_recurrences(id,company,client_id,series,template,day,due_days,start_period,paused,
                            last_period) VALUES(?,?,?,'SYN',?,?,15,'2026-01',?,'2026-09')""",
                (rid, co, client, json.dumps({"currency": "RON", "lines": []}), 1 + i % 31, i % 2),
            )
            L.set_meta(
                "recurrence_skip:" + rid,
                json.dumps(
                    dict(
                        recurrence=rid,
                        company=co,
                        from_period="2026-07",
                        through_period="2026-08",
                        count=2,
                        at="2026-10-20T00:00:00",
                    )
                ),
            )
        for i, month in enumerate(api.months_back(29, NOW.date())):
            # Actual supported CAMT input; distinct content exercises the statement cache.
            path = folder / f"statement-{month}.xml"
            iban = companies.get(co).ron_iban
            balances = "".join(
                f"<Bal><Tp><CdOrPrtry><Cd>{code}</Cd></CdOrPrtry></Tp>"
                f'<Amt Ccy="RON">1000.00</Amt><CdtDbtInd>CRDT</CdtDbtInd><Dt><Dt>{month}-{day}</Dt></Dt></Bal>'
                for code, day in (("OPBD", "01"), ("CLBD", str(calendar.monthrange(int(month[:4]), int(month[5:]))[1])))
            )
            path.write_text(
                f'<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.02">'
                f"<BkToCstmrStmt><Stmt><Id>{co}-{month}</Id><Acct><Id><IBAN>{iban}</IBAN></Id>"
                f"<Ccy>RON</Ccy></Acct>{balances}</Stmt></BkToCstmrStmt></Document>"
            )
            L.db.execute(
                "INSERT INTO documents(sha,company,month,kind,path,created_at) VALUES(?,?,?,'statement',?,?)",
                (f"s{ci}-{i}", co, month, str(path), month + "-28"),
            )
    L.db.commit()


def measure(L: Ledger, co: str, *, read: Callable[..., dict] = api.state) -> tuple[dict, dict]:
    queries = []
    L.db.set_trace_callback(queries.append)
    try:
        with patch.object(subprocess, "Popen", wraps=subprocess.Popen) as processes:
            start = perf_counter()
            result = read(L, co, NOW)
            elapsed = perf_counter() - start
        return result, dict(ms=round(elapsed * 1000, 2), queries=len(queries), subprocesses=processes.call_count)
    finally:
        L.db.set_trace_callback(None)


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="fiscalm-benchmark-") as tmp:
        home = Path(tmp)
        for name in ("HOME", "SECRETS", "WEB_CONFIG", "WEBHOOK_CONFIG"):
            os.environ["FISCALM_" + name] = str(home / name.lower())
        L = Ledger()
        from firms import seed

        seed(L)
        try:
            build(L, home / "home")
            for co in (next(iter(companies.all())), api.ALL):
                from state_reference import original_readers

                with original_readers() if "--baseline" in sys.argv else nullcontext():
                    result, metrics = measure(L, co)
                    parts = result.get("parts", [result])
                    assert sum(len(d["files"]) for part in parts for d in part["documents"]) == 1004 * len(parts)
                print(co, metrics)
            if "--profile" in sys.argv:
                profiler = cProfile.Profile()
                with original_readers() if "--baseline" in sys.argv else nullcontext():
                    profiler.runcall(api.state, L, api.ALL, NOW)
                pstats.Stats(profiler).sort_stats("cumtime").print_stats(25)
        finally:
            L.close()

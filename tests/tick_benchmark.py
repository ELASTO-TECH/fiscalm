"""Offline tick measurement helpers; all data and provider boundaries are synthetic."""

from __future__ import annotations

import hashlib
import json
import resource
import socket
import subprocess
from contextlib import ExitStack
from datetime import date, datetime
from time import perf_counter
from unittest.mock import patch

from imap_fake import Fake, mail, plain_connect
from pdf_fake import text_pdf

from fiscalm import ai, cli, companies, integrations
from fiscalm.ledger import Ledger, finance_home, is_live
from fiscalm.mail import invoices


def money_digest(L: Ledger) -> str:
    digest = hashlib.sha256()
    for table in ("efactura", "issued", "payments", "statement_txs", "settled_tx"):
        for row in L.db.execute(f"SELECT * FROM {table} ORDER BY rowid"):
            data = {k: v for k, v in dict(row).items() if k not in ("created_at", "updated_at")}
            digest.update(json.dumps(data, sort_keys=True, default=str).encode())
    return digest.hexdigest()


def receipt_inputs(L: Ledger, co: str, now: datetime) -> None:
    L.db.execute(
        "INSERT INTO upload_links(id,company,label,secret_hash,created_at,expires_at) VALUES(?,?,'Synthetic','fake',?,?)",
        ("tick-upload", co, now.isoformat(), "2027-01-01"),
    )
    for i in range(4):
        raw = text_pdf([f"Synthetic receipt {i}", "Total 12.10 EUR"])
        sha = hashlib.sha256(raw).hexdigest()
        path = finance_home() / co / "documents" / "inbox" / f"{sha}.pdf"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        L.add_document(
            sha,
            co,
            now.strftime("%Y-%m"),
            "receipt",
            str(path),
            vendor="Synthetic",
            amount="12.10",
            currency="EUR",
            source="upload",
        )
        L.db.execute(
            "INSERT INTO receipt_uploads(sha,link_id,company,uploaded_at,file_type) VALUES(?,'tick-upload',?,?,'pdf')",
            (sha, co, now.isoformat()),
        )
    L.db.commit()


def mailbox_inputs(L: Ledger, now: datetime, count: int) -> Fake:
    fake = Fake()
    try:
        first = next(iter(companies.all(L).values()))
        integrations.add(
            L,
            "mail",
            "Synthetic",
            {
                "host": "127.0.0.1",
                "port": str(fake.port),
                "username": "reader",
                "password": "fake-password",
                "from_address": "books@example.test",
            },
        )
        for i in range(count):
            raw = text_pdf(
                [
                    "INVOICE",
                    f"Invoice number: OFFLINE-{900000 + i}",
                    f"Date: {now.date().isoformat()}",
                    f"Bill to: {first.legal_name}, VAT RO{first.cui}",
                    "Total 12.10 EUR",
                ]
            )
            fake.add(
                mail(
                    message_id=f"<offline-{i}@example.test>",
                    day=now.strftime("%a, %d %b %Y 09:30:00 +0000"),
                    attachments=(("invoice.pdf", "application/pdf", raw),),
                ),
                now.date(),
            )
        return fake
    except BaseException:
        fake.close()
        raise


def run(L: Ledger, now: datetime, *, legacy_registry: bool = False, mail_count: int = 12) -> dict:
    """Real CLI stages, IMAP protocol and PDF extraction; remote model inference is a deterministic fake."""
    assert not is_live()
    co = next(iter(companies.all(L)))
    receipt_inputs(L, co, now)
    fake = mailbox_inputs(L, now, mail_count)
    phases, output = {}, []
    registry_connections = inference_calls = external_calls = 0
    original_registry, offered, mail_run = companies.all, invoices.offered, invoices.run
    connect, popen = socket.create_connection, subprocess.Popen

    def timed(name, fn):
        def call(*args, **kwargs):
            start = perf_counter()
            try:
                return fn(*args, **kwargs)
            finally:
                phases[name] = phases.get(name, 0) + perf_counter() - start

        return call

    def registry(L=None):
        nonlocal registry_connections
        registry_connections += L is None
        return original_registry(L)

    def network(address, *args, **kwargs):
        nonlocal external_calls
        if address != ("127.0.0.1", fake.port):
            external_calls += 1
            raise AssertionError("Offline benchmark attempted an external connection")
        return connect(address, *args, **kwargs)

    def infer(*args, **kwargs):
        nonlocal inference_calls
        inference_calls += 1
        return {
            "lines": [{"name": "Synthetic item", "unit": "H87", "quantity": "1", "net": "10.00", "vat": "21"}],
            "total": "12.10",
            "currency": "EUR",
            "payment": "card",
        }

    class Date(date):
        @classmethod
        def today(cls):
            return now.date()

    class Datetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    before = money_digest(L)
    documents = L.db.execute("SELECT count(*) FROM documents").fetchone()[0]
    try:
        with ExitStack() as stack:
            for module, name, fn in (
                (cli, "recover", cli.recover),
                (invoices, "run", lambda L, today: mail_run(L, today, connect=plain_connect)),
                (cli.archive, "archive_all", cli.archive.archive_all),
                (cli.bank_feed, "run_all", cli.bank_feed.run_all),
                (cli.pay, "prepare_files", cli.pay.prepare_files),
                (cli.pay, "refresh_to_pay", cli.pay.refresh_to_pay),
                (cli.pack, "ready", cli.pack.ready),
            ):
                stack.enter_context(patch.object(module, name, timed(name, fn)))
            for module, name, value in (
                (companies, "all", registry),
                (socket, "create_connection", network),
                (ai, "available", lambda L: True),
                (ai, "ask_json", infer),
                (cli, "_out", output.append),
                (cli, "date", Date),
                (cli, "datetime", Datetime),
            ):
                stack.enter_context(patch.object(module, name, value))
            if legacy_registry:
                stack.enter_context(patch.object(invoices, "offered", lambda data, firms=None: offered(data)))
            processes = stack.enter_context(patch.object(subprocess, "Popen", wraps=popen))
            start = perf_counter()
            assert cli.cmd_sync(None, L) == 0
            elapsed = perf_counter() - start
        assert money_digest(L) == before
        assert not is_live() and not external_calls and inference_calls == 0
        assert not (fake.verbs() & {"STORE", "APPEND", "EXPUNGE"})
        added = L.db.execute("SELECT count(*) FROM documents").fetchone()[0] - documents
        assert added == mail_count
        return {
            "phase": "offline_tick",
            "mode": output[0]["mode"],
            "legacy_registry": legacy_registry,
            "seconds": round(elapsed, 3),
            "stages": {k: round(v, 3) for k, v in phases.items()},
            "registry_connections": registry_connections,
            "pdf_subprocesses": processes.call_count,
            "mail_messages": mail_count,
            "receipt_extractions": inference_calls,
            "external_connections": external_calls,
            "money_digest": before,
            "max_rss_mib": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1),
        }
    finally:
        fake.close()

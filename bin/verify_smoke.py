"""bin/verify's safety report and API smoke, against the disposable engine it started. Synthetic data only."""

from __future__ import annotations

import base64
import json
import os
import re
import secrets
import shutil
import ssl
import sys
import time
import urllib.error
import urllib.request
from contextlib import closing
from pathlib import Path

from firms import iban

from fiscalm import paths
from fiscalm.api import actions, totp
from fiscalm.bank import upload as statement_upload
from fiscalm.efactura import client as anaf
from fiscalm.ledger import Ledger, is_live

ROOT = Path(os.environ.get("FISCALM_DEV_ROOT", "")).resolve()
URL = os.environ.get("FISCALM_PUBLIC_URL", "")
ADMIN = ROOT / "admin.json"  # this run's synthetic admin password and TOTP seed, mode 0600
DEV_SANDBOX = "Instalarea de dezvoltare rămâne în Sandbox."  # bin/dev_sandbox.py's refusal of Live
LOCAL = re.compile(r"https://localhost:\d+")
CLEAN = re.compile(  # exactly what bin/verify passes; anything else came from the caller's shell (a token, another install)
    r"PATH|HOME|LANG|TERM|PYTHON(UNBUFFERED|PATH)|UV_CACHE_DIR|npm_config_cache|NEXT_(TELEMETRY_DISABLED|PUBLIC_FISCALM_ENGINE)"
    r"|FISCALM_(CLAUDE|DEV_OFFLINE|HOME|SECRETS|BACKUPS|PUBLIC_URL|TRUST_PROXY|ANAF_ENV|DEV_(ROOT|PORT|APP_PORT|ENGINE_PORT))"
)


def statement() -> bytes:
    """NORD's whole October at BT as CAMT.053 (the shape of tests/test_camt.complete): one debit, invented data."""
    debit = (
        '<Ntry><Amt Ccy="RON">250.00</Amt><CdtDbtInd>DBIT</CdtDbtInd><Sts>BOOK</Sts><BookgDt><Dt>2026-10-25</Dt>'
        "</BookgDt><ValDt><Dt>2026-10-26</Dt></ValDt><AcctSvcrRef>verify-1</AcctSvcrRef><NtryDtls><TxDtls><Refs>"
        "<AcctSvcrRef>verify-1</AcctSvcrRef></Refs><RmtInf><Ustrd>Synthetic payment</Ustrd></RmtInf></TxDtls></NtryDtls></Ntry>"
    )

    def account(ccy: str, closing: str, entries: str = "") -> str:
        balances = "".join(
            f'<Bal><Tp><CdOrPrtry><Cd>{cd}</Cd></CdOrPrtry></Tp><Amt Ccy="{ccy}">{amount}</Amt>'
            f"<CdtDbtInd>CRDT</CdtDbtInd><Dt><Dt>2026-10-{day}</Dt></Dt></Bal>"
            for cd, amount, day in (("OPBD", "1000.00", "01"), ("CLBD", closing, "31"))
        )
        number = iban(f"RO00BTRL{ccy}CRT0000000001")
        return f"<Stmt><Acct><Id><IBAN>{number}</IBAN></Id><Ccy>{ccy}</Ccy></Acct>{balances}{entries}</Stmt>"

    body = account("RON", "750.00", debit) + account("EUR", "1000.00")
    return f'<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.02"><BkToCstmrStmt>{body}</BkToCstmrStmt></Document>'.encode()


def safety() -> None:
    """Print every effective target; exit 1 if any is not this worktree's disposable install."""
    ledger = paths.finance_home() / "state" / "ledger.db"
    rows = [
        ("data (FISCALM_HOME)", paths.finance_home()),
        ("ledger", ledger),
        ("secrets (FISCALM_SECRETS)", paths.secrets_dir()),
        ("backups (FISCALM_BACKUPS)", Path(os.environ.get("FISCALM_BACKUPS", "?"))),
    ]
    problems = [f"{name} is outside {ROOT}" for name, p in rows if not p.resolve().is_relative_to(ROOT)]
    if not (ROOT / ".fiscalm-dev").is_file() or ROOT.parent.name != ".dev":
        problems.append(f"{ROOT} is not a bin/verify directory")
    for name, p in rows:
        print(f"[verify:safety] {name}: {p}")
    print(f"[verify:safety] app and API: {URL} (/web/api, /api/v1), engine on 127.0.0.1")
    if not LOCAL.fullmatch(URL):
        problems.append(f"FISCALM_PUBLIC_URL {URL!r} is not https://localhost")
    print(f"[verify:safety] mode: {'LIVE' if is_live() else 'Sandbox (no state/LIVE)'}; mode-switch to Live refused")
    problems += ["state/LIVE exists"] if is_live() else []
    print(f"[verify:safety] ANAF: uploads {anaf.env()}, every *.anaf.ro refused, CUI lookup from tests/anaf_fake.py")
    problems += [] if anaf.env() == "test" else ["ANAF upload environment is not test"]
    offline = os.environ.get("FISCALM_DEV_OFFLINE") == "1"
    print(f"[verify:safety] network: {'loopback only (FISCALM_DEV_OFFLINE=1)' if offline else 'OPEN'}")
    problems += [] if offline else ["the engine would reach the network"]
    programs = {exe: shutil.which(exe) or "" for exe in ("claude", "codex", "rclone")}
    stubbed = all(Path(p).parent == ROOT / "offline" for p in programs.values())
    print(
        f"[verify:safety] programs the engine can start (claude, codex, rclone): {'refusing stubs' if stubbed else programs}"
    )
    problems += [] if stubbed else ["a network-capable program is on the engine's PATH"]
    configured = {"mail, bank and other integrations": 0, "bank connections": 0}
    if ledger.exists():
        with closing(Ledger(readonly=True)) as L:
            for name, table in zip(configured, ("integrations", "bank_connections"), strict=True):
                configured[name] = L.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    for name, n in configured.items():
        print(f"[verify:safety] {name}: {n} configured (expected 0: no IMAP, no bank API, no AI)")
        problems += [f"{n} {name} configured"] if n else []
    leaked = sorted(k for k in os.environ if not CLEAN.fullmatch(k))
    print(f"[verify:safety] variables inherited from the shell: {', '.join(leaked) or 'none'}")
    problems += [f"{k} reached the clean environment" for k in leaked]
    for p in problems:
        print(f"[verify:safety] FAIL {p}", file=sys.stderr)
    if problems:
        raise SystemExit(1)
    print("[verify:safety] OK: every target is local, disposable or disabled")


def call(path: str, body: object = None, *, cookie: str = "", ctype: str = "application/json") -> tuple[int, dict, str]:
    data = body if isinstance(body, bytes) else None if body is None else json.dumps(body).encode()
    headers = {"Content-Type": ctype, "Origin": URL} if data is not None else {}
    request = urllib.request.Request(URL + path, data, headers | ({"Cookie": cookie} if cookie else {}))
    try:
        tls = ssl.create_default_context(cafile=str(ROOT / "cert.pem"))  # bin/verify's own localhost certificate
        with urllib.request.urlopen(request, context=tls, timeout=30) as r:
            return r.status, json.loads(r.read() or b"{}"), r.headers.get("Set-Cookie", "")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}"), ""


def check(name: str, ok: object, seen: object) -> None:
    print(f"[verify:smoke] {'PASS' if ok else 'FAIL'} {name}: {seen}")
    if not ok:
        raise SystemExit(1)


def code(admin: dict) -> str:
    """A TOTP code for a step not used yet (each step is accepted once)."""
    now = int(time.time()) // 30
    step = max(now, admin.get("step", 0) + 1)
    if step > now + 1:
        time.sleep(30 - time.time() % 30)
        step = max(int(time.time()) // 30, step)
    ADMIN.write_text(json.dumps(admin | {"step": step}))
    return totp.code(base64.b32decode(admin["secret"]), step)


def payment_files(cookie: str) -> None:
    _, created, _ = call(
        "/web/api/payment-files",
        {"action": "payment_file_create", "co": "NORD", "name": "Verificare", "invoice_ids": []},
        cookie=cookie,
    )
    status, pay, _ = call(
        "/web/api/payment-files",
        {"action": "payment_file_download", "file_id": created["done"]["file_id"]},
        cookie=cookie,
    )
    check(
        "a payment file is refused in Sandbox",
        (status, pay.get("message")) == (200, actions.SHADOW),
        pay.get("message"),
    )
    ident = created["done"]["file_id"]
    _, renamed, _ = call(
        "/web/api/payment-files",
        {"action": "payment_file_rename", "file_id": ident, "name": "Verificare locală"},
        cookie=cookie,
    )
    status, detail, _ = call(f"/web/api/payment-files/{ident}", cookie=cookie)
    check(
        "named files can be created, renamed and read in Sandbox",
        status == 200 and renamed.get("ok") and detail.get("name") == "Verificare locală",
        ident,
    )
    _, deleted, _ = call("/web/api/payment-files", {"action": "payment_file_delete", "file_id": ident}, cookie=cookie)
    check("an empty open named file can be deleted in Sandbox", deleted.get("ok"), ident)


def smoke() -> None:
    safety()
    status, meta, _ = call("/api/v1/meta")
    check("engine through the HTTPS proxy", status == 200 and call("/healthz")[0] == 200, meta)
    link = re.search(r"/setup#(\S+)", (ROOT / "logs/engine.log").read_text())
    if not link:
        check("first run printed an admin setup link", False, "missing")
        return
    admin = {"password": secrets.token_urlsafe(18), "link": link[1]}
    status, begun, _ = call("/auth/setup/begin", {"link": admin["link"], "password": admin["password"]})
    check("setup accepts a password and issues a TOTP seed", status == 200 and begun.get("ok"), status)
    admin["secret"] = begun["secret"]
    finish = {"link": admin["link"], "ticket": begun["ticket"], "code": code(admin)}
    status, done, set_cookie = call("/auth/setup/finish", finish)
    check("setup with TOTP signs in and gives recovery codes", status == 200 and bool(done.get("codes")), status)
    cookie = set_cookie.split(";", 1)[0]
    status, me, _ = call("/web/api/me", cookie=cookie)
    who = me.get("me", {})
    check(
        "session is the administrator",
        (status, who.get("username"), who.get("role")) == (200, "admin", "admin"),
        who.get("role"),
    )
    names = json.dumps(call("/web/api/companies", cookie=cookie)[1], ensure_ascii=False)
    check("both invented firms are listed", "Atelier Nord" in names and "Studio 9" in names, "Atelier Nord, Studio 9")
    upload = "/web/api/statement-upload?co=NORD"
    status, first, _ = call(upload, statement(), cookie=cookie, ctype="application/xml")
    check("a synthetic CAMT.053 statement is imported", status == 200 and first.get("ok"), first.get("done"))
    status, again, _ = call(upload, statement(), cookie=cookie, ctype="application/xml")
    with closing(Ledger(readonly=True)) as L:
        stored = len(L.documents(kind="statement"))
    duplicate = (status, again.get("message"), stored) == (200, statement_upload.DUPLICATE, 1)
    check("the same statement again is refused as a duplicate", duplicate, again.get("message"))
    payment_files(cookie)
    live = {"mode": "live", "expected": "sandbox", "confirmed": "yes", "request_id": secrets.token_hex(16)}
    status, switch, _ = call("/web/api/mode-switch", live, cookie=cookie)
    refused = (status, switch.get("ok"), switch.get("message")) == (200, False, DEV_SANDBOX) and not is_live()
    check("switching to Live is refused", refused, switch.get("message") or status)
    mail = {"from_address": "verify@example.com", "password": "synthetic", "host": "imap.example.com", "port": "993"}
    status, probe, _ = call("/web/api/mail-test", mail, cookie=cookie)
    # dnspython retries a refused datagram until its lifetime ends, so the refusal reads as either code
    unreachable = status == 200 and probe.get("done", {}).get("code") in ("host_unreachable", "timeout")
    check("a mail server outside the machine is unreachable", unreachable, probe.get("done") or status)


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) == 2 else ""
    if command in ("safety", "smoke"):
        {"safety": safety, "smoke": smoke}[command]()
    elif command in ("password", "totp") and ADMIN.is_file():
        stored = json.loads(ADMIN.read_text())
        print(stored["password"] if command == "password" else code(stored))
    else:
        raise SystemExit("usage: bin/verify_smoke.py safety | smoke | password | totp (after smoke)")

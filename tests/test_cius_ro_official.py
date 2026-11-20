"""Synthetic corpus through the same draft/issue/storno paths as the application.

FISCALM_CIUS_CORPUS writes XML outside the repo for manual/ANAF validation.
The normal suite stays offline and skips Schematron when artifacts are absent.
"""

import os
import subprocess
from pathlib import Path

import pytest
from firms import seed
from test_invoicing import DATA, LINE, PARTY, confirm_rate

from fiscalm.invoicing import engine as i, ubl

CASES = [
    "ron21",
    "ron11",
    "mixed",
    "eur",
    "storno-full",
    "storno-partial",
    "AE",
    "K",
    "G",
    "E",
    "O",
    "Z",
    "private",
    "foreign",
    "long",
    "many",
    "fractional",
    "delivery",
    "non-eu",
    "foreign-private",
]
CASES += [
    "ro-cui-only",
    "bucharest",
    "units",
    "mixed-AE",
    "mixed-K",
    "mixed-E",
    "mixed-G",
    "mixed-Z",
    "exempt-reasons",
    "exempt-codes",
    "storno-eur",
    "storno-K",
    "storno-AE",
    "storno-G",
    "storno-rounding",
    "notes",
    "escaped",
    "seller-minimal",
    "K-text-region",
]
CASES += ["credit-" + case for case in CASES if not case.startswith("storno-")]
ROOT = Path(__file__).resolve().parents[1]


def corpus_xml(ledger, cid: int, case: str) -> bytes:
    """The issued (or credited) UBL of one corpus case, through the application's draft, issue and storno paths."""
    credit = case.startswith("credit-")
    case = case.removeprefix("credit-")
    i.save_seller(ledger, "NORD", PARTY | dict(cui="99999992", vat_id="RO99999992"))
    i.save_client(ledger, "NORD", PARTY | dict(cui="99999984", vat_id="RO99999984"), cid)
    rows = [LINE]
    data = dict(DATA)
    if case == "ron11":
        rows = [LINE | dict(vat_rate="11")]
    if case in {"mixed", "storno-full", "storno-partial"}:
        rows += [LINE | dict(vat_rate="11")]
    if case == "eur":
        data.update(currency="EUR", exchange_rate="4,9753", exchange_date="2026-10-30")
    if case in {"AE", "K", "G", "E", "O", "Z"}:
        rows = [
            LINE
            | dict(
                vat_category=case,
                vat_rate="0",
                exemption_reason={
                    "AE": "VATEX-EU-AE",
                    "K": "VATEX-EU-IC",
                    "G": "VATEX-EU-G",
                    "E": "Scutire test",
                    "O": "art. 310",
                    "Z": "",
                }[case],
            )
        ]
    if case in {"K", "G", "foreign"}:
        i.save_client(
            ledger,
            "NORD",
            PARTY | dict(country="DE", county="DE-BE" if case == "K" else "", cui="DE999999999", vat_id="DE999999999"),
            cid,
        )
    if case == "non-eu":
        i.save_client(ledger, "NORD", PARTY | dict(country="US", county="", cui="US-TEST-999", vat_id=""), cid)
    if case == "foreign-private":
        i.save_client(ledger, "NORD", PARTY | dict(country="DE", county="", cui="", vat_id="", private=True), cid)
    if case == "O":
        i.save_seller(ledger, "NORD", PARTY | dict(cui="99999992", vat_payer=False, vat_id=""))
    if case == "private":
        i.save_client(ledger, "NORD", PARTY | dict(cui="", vat_id="", private=True, vat_payer=False), cid)
    if case in {"K", "delivery"}:
        data["delivery_date"] = "2026-10-29"
    if case == "long":
        rows = [LINE | dict(description="Descriere fictivă ".ljust(100, "x"))]
    if case == "many":
        rows = [LINE | dict(description=f"Poziția {n}") for n in range(1, 101)]
    if case == "fractional":
        rows = [LINE | dict(quantity=q, unit_price="2,125") for q in ["0,125", "2,5"]]
    variant = case.removeprefix("mixed-").removeprefix("storno-")
    if variant in {"K", "AE", "G", "E", "Z"} and case not in {"K", "AE", "G", "E", "Z"}:
        rows = [
            LINE
            | dict(
                vat_category=variant,
                vat_rate="0",
                exemption_reason={
                    "K": "VATEX-EU-IC",
                    "AE": "VATEX-EU-AE",
                    "G": "VATEX-EU-G",
                    "E": "Scutire test",
                    "Z": "",
                }[variant],
            )
        ]
        if variant in {"K", "G"}:
            i.save_client(
                ledger,
                "NORD",
                PARTY | dict(country="DE", county="Berlin", cui="DE999999999", vat_id="DE999999999"),
                cid,
            )
        if variant == "K":
            data["delivery_date"] = DATA["issue_date"] if case == "mixed-K" else "2026-10-29"
        if case.startswith("mixed-"):
            rows += [LINE]
    if case == "storno-eur":
        data.update(currency="EUR", exchange_rate="4.9753", exchange_date="2026-10-30")
    if case == "storno-rounding":
        rows = [LINE | dict(quantity="0.125", unit_price="2.125"), LINE]
    if case == "ro-cui-only":
        i.save_client(ledger, "NORD", PARTY | dict(cui="99999984", vat_id="", vat_payer=False), cid)
    if case == "bucharest":
        i.save_client(
            ledger, "NORD", PARTY | dict(cui="99999984", vat_id="RO99999984", county="RO-B", city="Sector 2"), cid
        )
    if case == "units":
        rows = [LINE | dict(unit=u) for u in ["H87", "DAY", "KGM", "MTR", "LTR"]]
    if case in {"exempt-reasons", "exempt-codes"}:
        reasons = (
            ["Scutire test A", "Scutire test B"] if case == "exempt-reasons" else ["VATEX-EU-132-1B", "VATEX-EU-132-1C"]
        )
        rows = [LINE | dict(vat_category="E", vat_rate="0", exemption_reason=r) for r in reasons]
    if case == "notes":
        data["notes"] = "Notă fictivă pentru verificare & corecție <test>"
    if case == "escaped":
        i.save_client(
            ledger, "NORD", PARTY | dict(cui="99999984", vat_id="RO99999984", name="Știință & Artă <Test> SRL"), cid
        )
        rows = [LINE | dict(description="Șurub & piuliță <test>")]
    if case == "seller-minimal":
        i.save_seller(ledger, "NORD", PARTY | dict(cui="99999992", vat_id="RO99999992", phone="", email="", reg_com=""))
    if case == "K-text-region":
        i.save_client(
            ledger, "NORD", PARTY | dict(country="DE", county="Berlin", cui="DE999999999", vat_id="DE999999999"), cid
        )
        rows = [LINE | dict(vat_category="K", vat_rate="0", exemption_reason="VATEX-EU-IC")]
        data["delivery_date"] = "2026-10-29"
    iid = i.save_draft(ledger, "NORD", "TEST", cid, data | dict(lines=rows))
    confirm_rate(ledger, iid)
    i.issue(ledger, iid, DATA["issue_date"], company="NORD", version=1)
    if credit or case.startswith("storno-"):
        iid = i.storno(
            ledger,
            iid,
            "2026-11-01",
            "Corecție test",
            [1] if case.endswith(("partial", "rounding")) else None,
            company="NORD",
        )["id"]
    raw = ubl.generate(ledger, iid, company="NORD")
    # Foreign businesses retain their identity through issuance and reversal.
    if case in {"K", "G", "foreign", "mixed-K", "mixed-G", "storno-K", "storno-G", "K-text-region", "non-eu"}:
        assert (b"US-TEST-999" if case == "non-eu" else b"DE999999999") in raw
        assert b"0000000000000" not in raw
    return raw


def test_official_corpus(tmp_path, monkeypatch):
    """Every case passes the official UBL schema and both Schematron stages without a warning, in one validator run."""
    from fiscalm import bnr
    from fiscalm.ledger import Ledger

    def unavailable(_):
        raise bnr.RateError("BNR unavailable in synthetic test")

    monkeypatch.setattr(bnr, "fetch_xml", unavailable)
    monkeypatch.delenv("FISCALM_HEADLESS", raising=False)
    destination = Path(os.environ.get("FISCALM_CIUS_CORPUS", tmp_path / "corpus")).resolve()
    assert destination != ROOT and ROOT not in destination.parents
    destination.mkdir(parents=True, exist_ok=True)
    paths = []
    for case in CASES:
        monkeypatch.setenv("FISCALM_HOME", str(tmp_path / "homes" / case))
        ledger = Ledger()
        seed(ledger)
        try:
            i.save_seller(ledger, "NORD", PARTY)
            cid = i.save_client(
                ledger, "NORD", PARTY | dict(name="Client Fictiv SRL", cui="99999992", vat_id="RO99999992")
            )
            i.create_series(ledger, "NORD", "TEST", 42)
            paths.append(destination / (case + ".xml"))
            paths[-1].write_bytes(corpus_xml(ledger, cid, case))
        finally:
            ledger.close()
    cache = Path(os.environ.get("FISCALM_CIUS_CACHE", Path.home() / ".cache/fiscalm-cius-ro-validation"))
    if not all((cache / name).is_file() for name in ["ubl.zip", "cen.zip", "ro.zip", "ro-business.xslt", "saxon.jar"]):
        pytest.skip("Official validation artifacts not cached; no network in tests.")
    result = subprocess.run(
        ["uv", "run", "--no-sync", "python", str(ROOT / "tools/validate_cius_ro.py"), "--offline", *map(str, paths)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=900,
    )
    clean = set(result.stdout.splitlines())
    assert [c for c in CASES if f"{c}.xml: 0 errors" not in clean] == [], result.stdout + result.stderr
    assert result.returncode == 0, result.stdout + result.stderr
    assert "WARNING" not in result.stdout, result.stdout

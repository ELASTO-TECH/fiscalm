"""A synthetic ANAF integration for tests: the Integrări row, its saved tokens, and a fake token endpoint. No network."""

from __future__ import annotations

import base64
import json
import urllib.parse
from datetime import datetime

from fiscalm import integrations as ig
from fiscalm.efactura import client as anaf, public

CLIENT_ID, CLIENT_SECRET = "cid-synthetic", "csec-synthetic"


def setup(L) -> str:
    """An enabled ANAF integration, not connected yet. Returns its id."""
    existing = ig.default(L, "anaf")
    if existing:
        return existing.id
    return ig.add(L, "anaf", "ANAF", {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET})["id"]


def connect(L, access: str = "tok", refresh: str = "r", now: datetime | None = None) -> str:
    """An ANAF integration connected with these tokens (a fresh authorization at `now`). Returns its id."""
    iid = setup(L)
    tok = anaf._merged({"access_token": access, "refresh_token": refresh}, None, now or datetime.now())
    assert ig.put_oauth(L, iid, json.dumps(tok), client_id=CLIENT_ID, replace=True)
    return iid


def tokens(L) -> dict | None:
    inst = ig.default(L, "anaf")
    return anaf._tokens(inst) if inst else None


def jwt(exp: datetime) -> str:
    """An unsigned JWT with only `exp` (fiscalm reads it to plan refreshes)."""
    part = base64.urlsafe_b64encode(json.dumps({"exp": int(exp.timestamp())}).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJub25lIn0.{part}.sig"


class TokenEndpoint:
    """ANAF's token endpoint as the tests want it: checks the client's Basic auth, records each form, and answers
    from `answers` in order (a dict, or an exception to raise)."""

    def __init__(self, *answers):
        self.answers, self.forms = list(answers), []

    def __call__(self, url, body, headers):
        assert url == anaf.TOKEN_URL
        assert headers["Authorization"] == "Basic " + base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
        assert headers["Content-Type"] == "application/x-www-form-urlencoded"
        self.forms.append(dict(urllib.parse.parse_qsl(body.decode())))
        a = self.answers.pop(0)
        if isinstance(a, BaseException):
            raise a
        return a


def public_registry(url: str, body: bytes) -> bytes:
    """ANAF's public taxpayer lookup (v9) answered from an invented registry: every requested CUI is an active,
    VAT-registered firm with a made-up name and address. Same signature as `efactura.public.fetch_json`."""
    assert url == public.URL
    found = []
    for item in json.loads(body):
        cui, day = item["cui"], item["data"]
        found.append(
            dict(
                date_generale=dict(
                    cui=cui,
                    data=day,
                    denumire=f"Firma Inventată {cui} SRL",
                    adresa="Municipiul Cluj-Napoca, Str. Exemplu nr. 1",
                    nrRegCom=f"J12/{cui % 1000}/2020",
                    telefon="",
                    statusRO_e_Factura=True,
                    stare_inregistrare="INREGISTRAT din data 01.01.2020",
                ),
                adresa_sediu_social=dict(
                    stara="",
                    sdenumire_Strada="Str. Exemplu",
                    snumar_Strada="1",
                    sdetalii_Adresa="",
                    sdenumire_Localitate="Mun. Cluj-Napoca",
                    scod_JudetAuto="CJ",
                    scod_Postal="400001",
                ),
                inregistrare_scop_Tva=dict(
                    scpTVA=True, perioade_TVA=[dict(data_inceput_ScpTVA="2020-01-01", data_sfarsit_ScpTVA="")]
                ),
                inregistrare_RTVAI=dict(statusTvaIncasare=False),
                stare_inactiv=dict(statusInactivi=False, dataRadiere=""),
            )
        )
    return json.dumps(dict(cod=200, found=found, notFound=[])).encode()

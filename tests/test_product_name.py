"""The product is called fiscalm (maintainer decision 2026-11-02, OSIM application M2026/010799). Every place a user,
a self-hoster or an API client reads the name says it, in lowercase; identifiers follow their language's casing."""

from __future__ import annotations

import re
from pathlib import Path

import fiscalm
from fiscalm import companies, integrations
from fiscalm.api import actions, mcp, schema as api_schema, web

ROOT = Path(__file__).resolve().parents[1]
# The name in any casing, except identifiers: `Fiscalm-API-Version` and `X-Fiscalm-*` headers, `FISCALM` in a script.
BRAND = re.compile(r"\bfiscalm\b(?!-)", re.IGNORECASE)


def test_the_engine_names_the_product():
    assert fiscalm.PRODUCT == "fiscalm"
    m = web.manifest()
    assert (m["name"], m["short_name"]) == ("fiscalm", "fiscalm")
    assert actions.SHADOW == "Disponibil doar în modul Live."
    assert companies.NEEDS_COMPANY == "Add a company in fiscalm first."
    assert companies.NEEDS_COMPANY_RO == "Adaugă mai întâi o firmă în fiscalm."
    assert mcp.INFO["name"] == "fiscalm"
    spec = api_schema.openapi()
    assert spec["info"]["title"] == "fiscalm API"
    assert "fiscalm token" in spec["components"]["securitySchemes"]["bearerAuth"]["description"]
    labels = {f["key"]: f["label"] for k in integrations.kinds() for f in k["fields"]}
    assert labels["fiscalm_url"] == "Adresă fiscalm"


def test_product_spelling_is_consistent_in_what_people_read():
    """Code, app, shop integrations and documentation use the product name."""
    roots = ["src", "app/src", "app/scripts", "app/test", "integrations", "docs", "bin", "deploy"]
    files = [
        p
        for r in roots
        for p in (ROOT / r).rglob("*")
        if p.is_file()
        and p.suffix
        in {".py", ".ts", ".tsx", ".mjs", ".css", ".md", ".json", ".php", ".sh", ".in", ".service", ".xml", ".dist"}
    ]
    files += [
        ROOT / n
        for n in (
            "README.md",
            "AGENTS.md",
            "CONTRIBUTING.md",
            "CLA.md",
            "SECURITY.md",
            "CHANGELOG.md",
            "pyproject.toml",
            "Dockerfile",
            "compose.yaml",
        )
    ]
    found = []
    for p in files:
        rel = p.relative_to(ROOT).as_posix()
        if "/node_modules/" in rel or "/vendor/" in rel:
            continue
        for n, line in enumerate(p.read_text(errors="replace").splitlines(), 1):
            # The PrestaShop module's class is named after its folder (`fiscalm`).
            line = line.replace("class Fiscalm extends Module", "")
            if any(match.group() not in {"fiscalm", "FISCALM"} for match in BRAND.finditer(line)):
                found.append(f"{rel}:{n}")
    assert found == []

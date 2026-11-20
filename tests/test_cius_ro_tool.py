from __future__ import annotations

import importlib.util
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
from test_invoicing import DATA, LINE, PARTY

from fiscalm.efactura.inbox import NS
from fiscalm.invoicing import engine as i, ubl

pytest.importorskip("lxml")

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("validator", ROOT / "tools/validate_cius_ro.py")
validator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validator)


@pytest.mark.parametrize(
    "cached,refusal", [(b"corrupted cached artifact", "SHA256 mismatch: ubl.zip"), (None, "Missing ubl.zip")]
)
def test_offline_cache_refuses_a_corrupt_or_missing_artifact_and_never_downloads(
    tmp_path, monkeypatch, cached, refusal
):
    monkeypatch.setattr(
        validator.urllib.request, "urlopen", lambda *a, **kw: pytest.fail("Offline validator downloaded")
    )
    if cached is not None:
        (tmp_path / "ubl.zip").write_bytes(cached)
    with pytest.raises(ValueError, match=refusal):
        validator.artifacts(tmp_path, offline=True)


def test_refuse_dtd_and_repository_cache(monkeypatch):
    with pytest.raises(ValueError, match="DTDs are refused"):
        validator.xml(b'<!DOCTYPE x [<!ENTITY e SYSTEM "file:///nonexistent">]><x>&e;</x>')
    monkeypatch.setenv("FISCALM_CIUS_CACHE", str(ROOT / "downloaded"))
    with pytest.raises(ValueError, match="outside the repository"):
        validator.cache_dir()


KINDS = {
    "xsd": ("XSD", "ERROR"),
    "cen": ("BR-CO-10", "ERROR"),
    "ro": ("BR-RO-211", "ERROR"),
    "warning": ("UBL-CR-002", "WARNING"),
}


def mutated(kind: str) -> bytes:
    """A standalone normalized invoice with one defect, independent of the ledger."""
    data, rows = i._payload(DATA, PARTY)
    data.update(seller=PARTY, client=PARTY | dict(country="DE", county="DE-BE"))
    if kind == "ro":
        data["delivery_date"] = "2026-10-29"
        rows, data["totals"] = i.calculate(
            [LINE | dict(vat_category="K", vat_rate="0", exemption_reason="VATEX-EU-IC")], True
        )
    root = ET.fromstring(ubl.build(dict(status="issued", series="TEST", number=1, data=data, lines=rows)))
    if kind == "xsd":
        root.remove(root.find("cbc:ID", NS))
    elif kind == "cen":
        root.find("cac:LegalMonetaryTotal/cbc:LineExtensionAmount", NS).text = "99.00"
    elif kind == "ro":
        addr = root.find("cac:Delivery/cac:DeliveryLocation/cac:Address", NS)
        addr.remove(addr.find("cbc:CountrySubentity", NS))
    elif kind == "warning":
        version = ET.Element("{" + NS["cbc"] + "}UBLVersionID")
        version.text = "2.0"
        root.insert(0, version)
    return ET.tostring(root)


def test_official_validator_detects_errors_and_warnings(tmp_path, capsys):
    cache = validator.cache_dir()
    if not all((cache / name).is_file() for name in validator.ARTIFACTS):
        pytest.skip("Validation artifacts not cached")
    validator.artifacts(cache, offline=True)
    paths = []
    for kind in KINDS:
        paths.append(tmp_path / f"{kind}.xml")
        paths[-1].write_bytes(mutated(kind))
    # One run over all of them: each file's findings stay its own, and one bad file fails the run.
    assert validator.validate(paths, cache) == 1
    lines = capsys.readouterr().out.splitlines()
    for kind, (rule, severity) in KINDS.items():
        own = [line for line in lines if line.startswith(f"{kind}.xml ")]
        assert any(rule in line and severity in line for line in own), (kind, own)
    assert "warning.xml: 0 errors" in lines and "cen.xml: 0 errors" not in lines
    assert validator.validate([tmp_path / "warning.xml"], cache) == 0
    assert validator.validate([tmp_path / "xsd.xml"], cache) == 1  # alone: the schema error sets the exit code


@pytest.mark.parametrize("report", [None, b""])  # a non-SVRL or empty report: test_refuse_empty_svrl_without_cache
def test_batch_refuses_a_missing_or_unusable_report(tmp_path, monkeypatch, report):
    cache = validator.cache_dir()
    if not all((cache / name).is_file() for name in ("ubl.zip", "cen.zip")):
        pytest.skip("Validation artifacts not cached")

    def saxon(cmd, **kwargs):
        out = Path(next(a for a in cmd if a.startswith("-o:"))[3:])
        if report is not None:
            (out / "00000.xml").write_bytes(report)
        return validator.subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(validator.subprocess, "run", saxon)
    (tmp_path / "ok.xml").write_bytes(mutated("none"))
    # The same refusals main() turns into exit 2.
    with pytest.raises((ValueError, OSError, validator.ET.Error)):
        validator.validate([tmp_path / "ok.xml"], cache)


def test_zip_traversal_without_cache(tmp_path):
    cache, work = tmp_path / "cache", tmp_path / "work"
    cache.mkdir()
    work.mkdir()
    with zipfile.ZipFile(cache / "ubl.zip", "w") as z:
        z.writestr("xsd/../../escaped.xsd", "<x/>")
    with pytest.raises(ValueError, match="Unsafe schema path"):
        validator.unpack_xsd(cache, work)
    assert not (tmp_path / "escaped.xsd").exists()


@pytest.mark.parametrize(
    "severity,expected", [("warning", "WARNING"), ("info", "WARNING"), ("fatal", "ERROR"), ("", "ERROR")]
)
def test_svrl_severity_without_cache(severity, expected):
    raw = f'<schematron-output xmlns="{validator.SVRL["s"]}"><fired-rule/><failed-assert role="{severity}" id="TEST"><text>Test</text></failed-assert></schematron-output>'
    assert validator.findings(raw.encode()) == [(expected, "TEST", "Test")]


@pytest.mark.parametrize("raw", [b"<other/>", b'<schematron-output xmlns="http://purl.oclc.org/dsdl/svrl"/>'])
def test_refuse_empty_svrl_without_cache(raw):
    with pytest.raises(ValueError, match="Expected SVRL"):
        validator.findings(raw)


@pytest.mark.parametrize("expression", ["doc", "unparsed-text", "collection"])
def test_saxon_refuses_external_reads(tmp_path, expression):
    cache = validator.cache_dir()
    if not (cache / "saxon.jar").is_file():
        pytest.skip("Saxon not cached")
    validator.artifacts(cache, offline=True)
    secret = tmp_path / "external.xml"
    secret.write_text("<secret>EXTERNAL_READ</secret>")
    source = tmp_path / "input.xml"
    source.write_text("<input/>")
    style = tmp_path / "read.xsl"
    style.write_text(
        '<xsl:stylesheet xmlns:xsl="http://www.w3.org/1999/XSL/Transform" version="2.0">'
        f'<xsl:template match="/"><xsl:value-of select="{expression}(\'{secret.as_uri()}\')"/>'
        "</xsl:template></xsl:stylesheet>"
    )
    result = validator.subprocess.run(
        validator.saxon_command(cache, tmp_path) + [f"-s:{source}", f"-xsl:{style}"],
        capture_output=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert b"disallowed" in result.stderr
    assert b"EXTERNAL_READ" not in result.stdout


def test_pinned_romanian_source_matches_compiled_rule_expressions():
    """Comparison of the mirror pins, not authentication of the Ministry download."""
    cache = validator.cache_dir()
    if not all((cache / name).is_file() for name in validator.ARTIFACTS):
        pytest.skip("Validation artifacts not cached")
    validator.artifacts(cache, offline=True)
    with zipfile.ZipFile(cache / "ro.zip") as archive:
        source = validator.xml(archive.read("ro16931-ubl-1.0.9/cius-ro/RO16931-rules.sch"))
    compiled = validator.xml((cache / "ro-business.xslt").read_bytes())
    ns = {
        "s": "http://purl.oclc.org/dsdl/schematron",
        "x": "http://www.w3.org/1999/XSL/Transform",
        "v": validator.SVRL["s"],
    }
    expected = []
    for node in source.findall(".//s:assert", ns) + source.findall(".//s:report", ns):
        test = node.get("test")
        if node.tag.endswith("report"):
            test = f"not({test})"
        expected.append((node.get("id"), " ".join(test.split()), node.getparent().get("context")))
    actual = [
        (
            node.find('x:attribute[@name="id"]', ns).text,
            " ".join(node.get("test").split()),
            node.getparent().getparent().getparent().get("match"),
        )
        for node in compiled.findall(".//v:failed-assert", ns)
    ]
    for node in compiled.findall(".//v:failed-assert", ns):
        otherwise = node.getparent()
        assert otherwise.tag == "{" + ns["x"] + "}otherwise"
        executed = otherwise.getparent().find("x:when", ns).get("test")
        assert " ".join(executed.split()) == " ".join(node.get("test").split())
    assert len(expected) == len(actual) == 122
    assert sorted(expected) == sorted(actual)
    for node in source.findall("s:let", ns):
        variables = compiled.findall(f'.//x:variable[@name="{node.get("name")}"]', ns)
        assert len(variables) == 1 and variables[0].get("select") == node.get("value")

"""Offline-capable developer validator. Downloaded code/data never enter the repository."""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from lxml import etree as ET

ROOT = Path(__file__).resolve().parents[1]
MIRROR = (
    "https://raw.githubusercontent.com/phax/phive-rules/9ce8f8ca508468b7d937125c6aa0278f93b828c9/phive-rules-cius-ro/"
)
ARTIFACTS = {
    "ubl.zip": (
        "https://docs.oasis-open.org/ubl/os-UBL-2.1/UBL-2.1.zip",
        "60b80d76394a8a2add90723ecb8e0e2e9d826775de9749df37a72d60703f86ed",
    ),
    "cen.zip": (
        "https://github.com/ConnectingEurope/eInvoicing-EN16931/releases/download/validation-1.3.8/en16931-ubl-1.3.8.zip",
        "4ff460a64b04df132b750e80c49d88f7452e2f1cad720606c1793253e653622b",
    ),
    "ro.zip": (
        MIRROR + "docs/1.0.9/ro16931-ubl-1.0.9.zip",
        "818e7dd77e429f5dbde7c60b43dddbdf1a9a82a1773c359caa80a36ef89ea7f2",
    ),
    "ro-business.xslt": (
        MIRROR + "src/main/resources/external/schematron/1.0.9/ROeFactura-UBL-validation-Invoice_v1.0.9.xslt",
        "c941e480dff38fd7f871882c177cfbc954d4a3361cd596195b31326ccb595879",
    ),
    "saxon.jar": (
        "https://repo.maven.apache.org/maven2/net/sf/saxon/Saxon-HE/10.9/Saxon-HE-10.9.jar",
        "491d8edf4ec811d15c2b2417b007218b9b938f15e4dfbad004025beb4e70e960",
    ),
}
SVRL = {"s": "http://purl.oclc.org/dsdl/svrl"}


def cache_dir() -> Path:
    path = (
        Path(os.environ.get("FISCALM_CIUS_CACHE", Path.home() / ".cache/fiscalm-cius-ro-validation"))
        .expanduser()
        .resolve()
    )
    if path == ROOT or ROOT in path.parents:
        raise ValueError("FISCALM_CIUS_CACHE must be outside the repository.")
    return path


def artifacts(cache: Path, offline: bool) -> None:
    cache.mkdir(parents=True, exist_ok=True)
    for name, (url, digest) in ARTIFACTS.items():
        target = cache / name
        if not target.exists():
            if offline:
                raise ValueError(f"Missing {name}; run uv run python tools/validate_cius_ro.py once without --offline.")
            with urllib.request.urlopen(url, timeout=90) as response:
                data = response.read(80 * 1024 * 1024 + 1)
            if hashlib.sha256(data).hexdigest() != digest:
                raise ValueError(f"SHA256 mismatch: {name}; delete the cache directory {cache} and download again.")
            target.write_bytes(data)
        if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            raise ValueError(f"SHA256 mismatch: {name}; delete the cache directory {cache} and download again.")


def xml(data: bytes) -> ET._Element:
    root = ET.fromstring(data, ET.XMLParser(resolve_entities=False, load_dtd=False, no_network=True))
    if root.getroottree().docinfo.doctype:
        raise ValueError("DTDs are refused.")
    return root


def unpack_xsd(cache: Path, work: Path) -> Path:
    # Read only schema members; never extract archive paths or symlinks directly.
    with zipfile.ZipFile(cache / "ubl.zip") as archive:
        for name in archive.namelist():
            if name.startswith("xsd/") and name.endswith(".xsd"):
                target = (work / name).resolve()
                if work not in target.parents:
                    raise ValueError("Unsafe schema path.")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(name))
    return work / "xsd/maindoc/UBL-Invoice-2.1.xsd"


def findings(data: bytes) -> list[tuple[str, str, str]]:
    """Refuse empty/non-SVRL reports; unknown severities remain errors."""
    report = xml(data)
    if report.tag != "{" + SVRL["s"] + "}schematron-output" or not report.findall("s:fired-rule", SVRL):
        raise ValueError("Expected SVRL schematron-output with at least one fired rule.")
    out = []
    for node in report.findall("s:failed-assert", SVRL) + report.findall("s:successful-report", SVRL):
        severity = (node.get("flag") or node.get("role") or "error").strip().lower()
        out.append(
            (
                "WARNING" if severity in {"warning", "info", "information"} else "ERROR",
                node.get("id", "UNKNOWN"),
                " ".join(" ".join(node.itertext()).split()),
            )
        )
    return out


def saxon_command(cache: Path, work: Path) -> list[str]:
    """Allow no dereferenceable URI schemes, including file, jar, HTTP and HTTPS.

    An empty allowedProtocols is ignored by Saxon 10.9. URNs have no resource
    loader; principal inputs/stylesheets are explicit local command arguments.
    """
    config = work / "saxon.xml"
    config.write_text(
        '<configuration xmlns="http://saxon.sf.net/ns/configuration" edition="HE">'
        '<global allowedProtocols="urn"/></configuration>'
    )
    return [
        "java",
        "-Xmx512m",
        "-Djavax.xml.accessExternalDTD=",
        "-Djavax.xml.accessExternalSchema=",
        "-jar",
        str(cache / "saxon.jar"),
        f"-config:{config}",
        "-ext:off",
        "-dtd:off",
        "-xi:off",
    ]


def validate(files: list[Path], cache: Path) -> int:
    """Check every file against the UBL schema, then both Schematron stages; 1 if any file has an error.

    Saxon runs once per stage over all the files: starting the JVM and compiling the stylesheet cost far more than
    one invoice, so a corpus validates in two runs instead of two per file.
    """
    errors = 0
    with tempfile.TemporaryDirectory(dir=cache) as tmp:
        work = Path(tmp).resolve()
        schema = ET.XMLSchema(
            ET.parse(str(unpack_xsd(cache, work)), ET.XMLParser(no_network=True, resolve_entities=False))
        )
        with zipfile.ZipFile(cache / "cen.zip") as archive:
            cen = work / "cen.xslt"
            cen.write_bytes(archive.read("en16931-ubl-1.3.8/xslt/EN16931-UBL-validation.xslt"))
        inputs = work / "in"
        inputs.mkdir()
        valid: dict[str, Path] = {}
        for n, source in enumerate(files):
            try:
                root = xml(source.read_bytes())
                schema.assertValid(root)
            except (ET.Error, ValueError) as exc:
                print(f"{source.name} ERROR XSD: {exc}")
                errors += 1
                continue
            # Pass sanitized XML to Saxon, never a source with external entities; numbered, as names may repeat.
            safe = f"{n:05d}.xml"
            (inputs / safe).write_bytes(ET.tostring(root))
            valid[safe] = source
        counts = dict.fromkeys(valid, 0)
        for stage, style in [("EN16931", cen), ("RO-business", cache / "ro-business.xslt")] if valid else []:
            out = work / stage
            out.mkdir()
            result = subprocess.run(
                saxon_command(cache, work) + [f"-s:{inputs}", f"-o:{out}", f"-xsl:{style}"],
                capture_output=True,
                timeout=600,
            )
            if result.returncode:
                raise ValueError(f"{stage} processor failed: {result.stderr.decode(errors='replace')}")
            for safe, source in valid.items():
                for severity, rule, message in findings((out / safe).read_bytes()):
                    counts[safe] += severity != "WARNING"
                    print(f"{source.name} {severity} {stage} {rule}: {message}")
        for safe, source in valid.items():
            errors += counts[safe]
            print(f"{source.name}: {counts[safe]} errors")
    return int(errors > 0)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", help="Never download; require verified cached artifacts.")
    parser.add_argument("files", type=Path, nargs="+")
    args = parser.parse_args()
    try:
        cache = cache_dir()
        artifacts(cache, args.offline)
        return validate(args.files, cache)
    except (ValueError, OSError, subprocess.SubprocessError, ET.Error, zipfile.BadZipFile) as exc:
        parser.exit(2, f"Validator failed: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())

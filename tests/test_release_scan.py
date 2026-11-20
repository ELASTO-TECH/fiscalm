"""The release gate uses only invented inputs and never emits their contents. One case per detection rule and per
allowlist or policy path. Values are assembled from pieces so this file is no finding of the gate itself."""

import importlib.machinery
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "bin/release-scan"
loader = importlib.machinery.SourceFileLoader("release_scan", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
scan = importlib.util.module_from_spec(spec)
loader.exec_module(scan)

RANDOM = "aB3dE5gH7jK9" + "mN2pQ4sT6vW8xY0z"
TOKEN = "ghp_" + "x" * 36
SHORT_CUI = "123" + "40"  # an invented checksum-valid short CUI


def make_iban():
    body = "TEST" + "0" * 15 + "1"
    digits = "".join(str(ord(c) - 55) if c.isalpha() else c for c in body + "RO00")
    return "RO" + str(98 - int(digits) % 97).zfill(2) + body


def make_cnp(county="01"):
    body = "5" + "010101" + county + "001"
    check = sum(int(a) * int(b) for a, b in zip(body, "279146358279", strict=True)) % 11
    return body + str(1 if check == 10 else check)


IBAN, CNP = make_iban(), make_cnp()


def command(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    command(root, "init", "-q")
    command(root, "config", "user.email", "test@example.invalid")
    command(root, "config", "user.name", "Synthetic Tester")
    return root


def commit(root, message):
    command(root, "add", "-A")
    command(root, "commit", "-qm", message)


def run(root, *args, script=SCRIPT):
    return subprocess.run([sys.executable, str(script), str(root), *args], capture_output=True, text=True)


def report(result):
    return json.loads(result.stdout.splitlines()[-1])


def found(path, content, terms=(), allowed=(), fake=None):
    data = content if isinstance(content, bytes) else content.encode()
    return list(scan.findings(path, data, list(terms), set(allowed), fake=fake))


S, T, N = [(1, "secrets")], [(1, "structural")], []
SHORT_TAX_SHAPES = [  # one per tax label and connector the context rule accepts
    '"cui": "{id}"',
    "CUI: **{id}**",
    "| CUI | Nume | {id} |",
    "CUI nr. {id}",
    "CUI-ul {id}",
    "VAT ID: {id}",
    "VAT number {id}",
    "cod fiscal (CUI) {id}",
    "<cbc:CompanyID>{id}</cbc:CompanyID>",
    "C.U.I.: {id}",
    "C.I.F. {id}",
    "cod_fiscal = {id}",
    "tax_id = {id}",
    "CIF no. [{id}]",
]
FINDINGS = [
    # token shapes, one per alternative
    ("plain.txt", "-----BEGIN " + "PRIVATE KEY-----", S),
    ("plain.txt", "-----BEGIN " + "PGP PRIVATE KEY BLOCK-----", S),
    ("plain.txt", TOKEN, S),
    ("plain.txt", "AIza" + "b" * 35, S),
    ("plain.txt", "sk-ant-" + "c" * 40, S),
    ("plain.txt", "AKIA" + "Z" * 16, S),
    ("plain.txt", "xoxb-" + "x" * 20, S),
    ("plain.txt", "xoxa-" + "x" * 20, S),
    ("plain.txt", "xoxp-" + "x" * 20, S),
    ("plain.txt", "sk_live_" + "z" * 24, S),
    ("plain.txt", "111111:" + "x" * 35, S),  # a bot token
    ("plain.txt", "eyJ" + "a" * 12 + "." + "b" * 12 + "." + "c" * 12, S),
    ("plain.txt", "https://" + "user:pass@" + "fake.invalid", S),
    ("binary", b"\0" + TOKEN.encode(), S),  # binary content is still read for tokens
    ("font.ttf", b"\0\x8019", N),  # but not for numbers
    # assignments: a sensitive key in a config file, a random-looking value anywhere
    ("settings.json", "accessToken = " + "invented-" + "value", S),
    ("settings.json", "api_key = " + "invented-" + "value", S),
    ("settings.json", "client.secret = " + "invented-" + "value", S),
    ("settings.json", "tokenizer = short", N),
    ("settings.json", "monkey = short", N),
    ("settings.json", "password = /tmp/secret\ntoken = ${MY_TOKEN}", N),  # a path, an environment reference
    ("settings.json", "password = /" + RANDOM, S),  # the path exemption does not disable entropy
    ("settings.json", "label = long-example-config-key-name-value", N),  # prose
    ("settings.json", "label = token-with-2026-01-01-" + RANDOM, N),  # a date
    ("data.txt", "hash = " + RANDOM, S),
    ("lock.json", "integrity = sha512-" + "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ", N),
    ("package-lock.json", "password = " + RANDOM, N),  # lockfiles: no assignment rules
    ("package-lock.json", "key = " + TOKEN, S),  # but tokens
    ("uv.lock", "url = https://" + "user:pass@" + "fake.invalid", S),
    ("uv.lock", IBAN + " CUI 19", N),  # nor numbers
    (".env", "", S),
    (".env.example", 'PASSWORD=""\nTOKEN= # empty\n', N),
    (".env.example", "PASSWORD=" + "fake-pass", S),
    # IBAN, CUI, CNP: checksum-valid and in context
    ("note.txt", IBAN, T),
    ("note.txt", "-".join([IBAN[:4], IBAN[4:8], IBAN[8:]]), T),
    ("note.txt", "account" + IBAN, T),
    ("note.txt", IBAN[:-1] + "2", N),
    ("note.txt", "CUI 19", T),
    ("note.txt", "CUI 18", N),
    ("note.txt", "CIF: RO19", T),
    ("note.txt", "cod fiscal = 19", T),
    ("note.txt", "cui" + "1234" + "5674", T),
    ("note.txt", "100" + "000", T),  # long enough to be one without context
    ("note.txt", "." + "100" + "000", N),
    ("note.txt", "19 19.0 0.19 19% 19px 19 px", N),
    ("note.txt", "private 19", N),
    ("note.txt", "date: 2026-08-19, CUI unknown", N),
    ("note.txt", "CUI absent; art. 19", N),
    ("note.txt", "19 CUI", N),
    ("note.txt", "TVA RON: 19", N),
    ("note.txt", "c.text(seller['name'] + ', CUI ' + seller['cui'], 45, 27, 6.75, fill=MUTED, width=380)", N),
    ("note.txt", CNP, T),
    ("note.txt", make_cnp("48"), T),
    ("note.txt", CNP[:-1] + str((int(CNP[-1]) + 1) % 10), N),
    ("note.txt", CNP[:3] + "13" + CNP[5:], N),  # month 13
    (
        "note.md",
        "| Nume | CUI | Zile |\n| --- | --- | --- |\n| Exemplu | " + SHORT_CUI + " | 60 |",
        [(3, "structural")],
    ),
    # a header names only its own column of its own table
    (
        "note.md",
        "| CUI | Zile |\n| --- | --- |\n| absent | 60 |\n\n| Nume | Zile |\n| --- | --- |\n| Exemplu | 60 |",
        N,
    ),
    *[("note.txt", shape.format(id=SHORT_CUI), T) for shape in SHORT_TAX_SHAPES],
]


@pytest.mark.parametrize("path,content,expected", FINDINGS)
def test_findings(path, content, expected):
    assert found(path, content) == expected


def test_terms_match_normalized_joined_names_and_redact_paths():
    terms = ["Alfa Consult"]
    for name in ["AlfaConsult", "ALFA_CONSULT", "alfa-consult"]:
        assert found(name + ".txt", name, terms) == [(1, "denylist"), (1, "denylist")]  # filename and content
        assert scan.safe_path(name + ".txt", terms) == "[redacted filename]"
    assert not scan.term_match("unrelated", terms)


def test_config_terms_and_public_email_domains():
    data = {
        "owner_name": "Invented Owner",
        "companies": [
            {
                "key": "FAKECO",
                "legal_name": "Fake Company",
                "cui": "RO" + "987654",
                "bic": "FAKEBANK",
                "email": "office@invented.invalid",
            }
        ],
        "foreign_entities": {"FAKEFOREIGN": ["Invented Foreign", "7654320"]},
        "cui": True,
        "email": False,
        "owner_date": "2026-01-01",
        "short": {"name": "ABC"},
        "dated": {"name": "2026-01-01"},
    }
    derived = set(scan.config_terms(data))
    assert {
        "Invented Owner",
        "FAKECO",
        "Fake Company",
        "RO" + "987654",
        "987654",
        "FAKEBANK",
        "office@invented.invalid",
        "invented.invalid",
        "FAKEFOREIGN",
        "Invented Foreign",
        "7654320",
    } <= derived
    assert not {"True", "False", "ABC", "2026-01-01"} & derived  # a bool is an int, but never a term
    for domain in scan.PUBLIC_EMAIL_DOMAINS:  # a public domain: the full address only
        email = "synthetic-contact@" + domain.upper()
        terms = list(scan.config_terms({"email": email}))
        assert terms == [email] and not list(scan.config_terms({"sender": domain.upper()}))
        assert not scan.term_match("unrelated@" + domain, terms) and scan.term_match(email.lower(), terms)


def test_fake_values_are_exempt_only_exactly_and_the_policy_needs_a_reason(tmp_path, monkeypatch):
    fake = scan.fake_secrets()
    policy = scan.POLICY_ROOT / "release-allow-secrets.txt"
    assert not found(str(policy), policy.read_bytes(), fake=fake)  # the policy is no finding of its own
    urls = [value for value in fake if "://" in value]
    assert urls
    assert found("test.py", "https://" + "user:pa'ss@" + "other.invalid", fake=fake) == S
    for url in urls:
        assert not found("test.py", 'url = "' + url + '"', fake=fake) and found("test.py", url) == S
        changed = url.rsplit("@", 1)[0] + "@other.invalid"
        for line in (url + "/extra", changed, url + " " + changed):
            assert found("test.py", line, fake=fake) == S
    sk_fake, sk_real = "sk-" + "test-" + "x" * 30, "sk-" + "y" * 35
    assert not found("settings.json", "token = " + sk_fake, fake={sk_fake})
    assert found("settings.json", "token = " + sk_fake + "\nsecond = " + sk_real, fake={sk_fake}) == [(2, "secrets")]
    assert found("settings.json", sk_fake + " " + sk_real, fake={sk_fake}) == S  # never a whole line
    monkeypatch.setattr(scan, "POLICY_ROOT", tmp_path)
    entry = "sk-" + "test-" + "a" * 30
    (tmp_path / "release-allow-secrets.txt").write_text(entry + " # invented test token\n")
    assert scan.fake_secrets() == {entry}
    for bad in ("sk-" + "z" * 35 + " # fake reason alone is insufficient\n", entry + "\n"):
        (tmp_path / "release-allow-secrets.txt").write_text(bad)
        with pytest.raises(ValueError):
            scan.fake_secrets()


def test_public_assignment_pins_are_scoped_and_never_hide_credentials():
    key, value = "token-create", "local"
    digest = scan.hashlib.sha256((key + "\0" + value).encode()).hexdigest()
    fake = {f"assignment | actions.json | {digest}"}
    line = '"token-create": "local"'
    assert not found("actions.json", line, fake=fake)
    assert found("other.json", line, fake=fake) == S
    assert found("actions.json", line.replace("local", "changed"), fake=fake) == S
    assert found("actions.json", line.replace("create", "revoke"), fake=fake) == S
    secret = "sk-" + "z" * 35
    assert found("actions.json", line + " " + secret, fake=fake) == S
    assert found("actions.json", line + ' "password": "unexpected"', fake=fake) == S


def test_synthetic_ids_have_reasons_and_count_only_at_their_paths(tmp_path):
    entries = (scan.POLICY_ROOT / "synthetic-ids.txt").read_text().splitlines()
    repeat = "100" + "000"
    entry = next(line for line in entries if line.split(" | ")[0] == repeat)
    assert "test_receipt_upload.py" in entry and "repeat count" in entry
    for value in ["19", "7654" + "320"]:  # this file's positive controls
        entry = next(line for line in entries if line.split(" | ")[0] == value)
        assert "test_release_scan.py" in entry and ("synthetic" in entry or "invented" in entry)
    assert not found("note.txt", IBAN, allowed={scan.normalized(IBAN)})
    root = repo(tmp_path)
    path = root / "tests/test_receipt_upload.py"
    path.parent.mkdir()
    path.write_text("payload * " + repeat)
    command(root, "add", ".")
    assert report(run(root, "--no-denylist", "--summary"))["structural"] == 0
    (root / "unrelated.py").write_text(path.read_text())
    command(root, "add", ".")
    assert report(run(root, "--no-denylist", "--summary"))["structural"] == 1
    path.write_text("payload * " + "RO" + "19")
    assert report(run(root, "--no-denylist", "--summary"))["structural"] == 2


def test_cli_counts_and_never_prints_a_value_or_an_identifying_path(tmp_path):
    root = repo(tmp_path)
    values = [TOKEN, "AIza" + "b" * 35, "sk-ant-" + "c" * 40, "eyJ" + "a" * 12 + "." + "b" * 12 + "." + "c" * 12]
    (root / "settings.json").write_text("\n".join(values) + "\npassword = " + "invented-" + "password\nkey = " + RANDOM)
    (root / ".env").write_text("")
    (root / "key.pem").write_text("-----BEGIN " + "PRIVATE KEY-----")
    identifiers = [TOKEN, IBAN, "RO" + "1854" + "7291", CNP, "AbCdEfGh" * 3]
    for identifier in identifiers:  # every filename is redacted, whether or not it is a finding itself
        (root / ("client_" + identifier + "_contract")).write_text(TOKEN)
    command(root, "add", ".")
    result = run(root, "--no-denylist")
    assert result.returncode == 1 and ":1: secrets [redacted]" in result.stdout
    for value in values + identifiers + ["invented-password", RANDOM]:
        assert value not in result.stdout + result.stderr
    for path in [line.split(":", 1)[0] for line in result.stdout.splitlines()[:-1]]:
        assert not scan.re.search(r"\d{2,}|[A-Za-z0-9]{16,}", path)
    assert report(result)["secrets"] == 8 + len(identifiers) + 1  # a token in a filename counts too


def test_cli_denylist_config_and_history(tmp_path):
    root = repo(tmp_path)
    assert list(scan.history_blobs(root)) == []
    invented = "Invented Violet Widgets"
    (root / "note.txt").write_text(invented.upper())
    (root / "one.txt").write_text(TOKEN)
    (root / "two.txt").write_text(TOKEN)  # the same blob at another path
    commit(root, "Synthetic first revision")
    (root / "note.txt").write_text("clean")
    for name in ("one.txt", "two.txt"):
        (root / name).write_text("clean")
    commit(root, "Remove synthetic values")
    with pytest.raises(ValueError):
        list(scan.history_blobs(root, max_blob=8))
    deny = tmp_path / "deny.txt"
    deny.write_text(invented)
    result = run(root, "--denylist", str(deny), "--history")
    assert result.returncode == 1 and invented.casefold() not in (result.stdout + result.stderr).casefold()
    assert (report(result)["denylist"], report(result)["secrets"]) == (1, 1)
    assert run(root, "--denylist", str(deny)).returncode == 0
    (root / "deny.txt").write_text("synthetic")
    assert report(run(root, "--denylist", str(root / "deny.txt")))["errors"] == 1  # a list inside the repository
    config = tmp_path / "private.json"
    config.write_text(
        json.dumps(
            {
                "companies": [
                    {
                        "legal_name": invented,
                        "ibans": {"RON": "invented-iban"},
                        "cui": "87654321",
                        "email": "fake@example.invalid",
                    }
                ]
            }
        )
    )
    (root / "note.txt").write_text("invented-iban\nFAKE@example.invalid\n87654321")
    assert report(run(root, "--no-denylist", "--config", str(config), "--summary"))["denylist"] == 3
    assert run(root, "--denylist", str(tmp_path / "absent")).returncode == 2
    assert run(root, "--no-denylist", "--config", str(tmp_path / "absent")).returncode == 2
    (root / "note.txt").write_text("clean")
    missing = run(root, "--summary")  # no default list: the choice is explicit, never a pass
    assert missing.returncode == 2 and "--denylist" in missing.stderr and not missing.stdout
    baseline = run(root, "--no-denylist", "--summary")
    assert baseline.returncode == 0 and report(baseline)["baseline_only"] is True
    assert report(baseline)["denylist_checked"] is False
    deny.write_text("")
    clean = run(root, "--denylist", str(deny), "--summary")
    assert (
        clean.returncode == 0 and report(clean)["denylist_checked"] is True and report(clean)["baseline_only"] is False
    )


def test_cli_normalizes_terms_in_paths_and_contents(tmp_path):
    root = repo(tmp_path)
    deny = tmp_path / "deny.txt"
    terms = ["Benzinăria Fictivă", "Iaşi Inventat", "Atelier Exemplu"]
    deny.write_text("\n".join(terms), encoding="utf-8-sig")
    variants = ["benzinaria_fictiva", "Iași Inventat", scan.unicodedata.normalize("NFD", terms[2])]
    for value in variants:
        (root / (value + ".txt")).write_text(value)
    command(root, "add", ".")
    result = run(root, "--denylist", str(deny))
    assert result.returncode == 1
    assert report(result)["denylist"] == 6  # filename and content
    assert result.stdout.count("'[redacted filename]':1: denylist") == 6
    assert not any(value in result.stdout for value in variants)


def test_cli_files_ignored_private_forbidden_binary_and_links(tmp_path):
    root = repo(tmp_path)
    (root / ".gitignore").write_text("*.db\n")
    (root / "secret.db").write_bytes(b"")
    (root / "tests_private").mkdir()
    (root / "tests_private/data.txt").write_text("")
    (root / "tests/fixtures").mkdir(parents=True)
    (root / "tests/fixtures/sample.csv").write_text("")
    (root / "file.pdf").write_bytes(b"")
    (root / "unknown.dat").write_bytes(b"\0fake binary")
    (tmp_path / "outside").write_text(TOKEN)
    (root / "link").symlink_to(tmp_path / "outside")
    (root / "dir").mkdir()
    (root / "dir/data.txt").write_text("safe")
    command(root, "add", "-f", ".")
    commit(root, "Synthetic files")
    counts = report(run(root, "--no-denylist", "--summary"))
    assert (counts["files"], counts["secrets"], counts["errors"]) == (5, 0, 0)  # a link target is never read
    assert report(run(root, "--no-denylist", "--ref", "HEAD", "--summary")) == counts
    shutil.rmtree(root / "dir")
    private = tmp_path / "private"
    private.mkdir()
    (private / "data.txt").write_text("sk-" + "x" * 40)
    (root / "dir").symlink_to(private, target_is_directory=True)  # a tracked file now behind a linked folder
    result = run(root, "--no-denylist", "--summary")
    assert report(result)["errors"] == 1 and "sk-" not in result.stdout + result.stderr


# Named one by one, not read from scan.FORBIDDEN: dropping a suffix from the scanner must fail here.
PRIVATE_SUFFIXES = (
    ".pdf",
    ".csv",
    ".db",
    ".zip",
    ".jpg",
    ".jpeg",
    ".heic",
    ".png",
    ".xml",
    ".xlsx",
    ".ods",
    ".sqlite",
    ".p12",
    ".pfx",
    ".key",
    ".eml",
    ".mbox",
)


def test_cli_refuses_every_private_file_kind(tmp_path):
    root = repo(tmp_path)
    for suffix in PRIVATE_SUFFIXES:
        (root / ("invented" + suffix)).write_bytes(b"")
    command(root, "add", "-f", ".")
    assert report(run(root, "--no-denylist", "--summary"))["files"] == len(PRIVATE_SUFFIXES)


def test_cli_fixture_hash_exemption_is_exact(tmp_path):
    root = repo(tmp_path)
    fixture = root / "tests/fixtures/fake.png"
    fixture.parent.mkdir(parents=True)
    fixture.write_bytes(b"\0invented-pixels")
    command(root, "add", ".")
    tool = tmp_path / "tool/bin/release-scan"
    tool.parent.mkdir(parents=True)
    shutil.copyfile(SCRIPT, tool)
    policy = tool.parents[1] / "tests"
    policy.mkdir()
    for name in ["synthetic-ids.txt", "release-allow-secrets.txt"]:
        shutil.copyfile(scan.POLICY_ROOT / name, policy / name)
    (policy / "release-assets.txt").write_text(scan.blob_oid(fixture.read_bytes()) + "  tests/fixtures/fake.png\n")
    assert report(run(root, "--no-denylist", "--summary", script=tool))["files"] == 0
    fixture.write_bytes(b"\0changed-pixels")  # a pin is for these bytes, never for a name or a folder
    assert report(run(root, "--no-denylist", "--summary", script=tool))["files"] == 1

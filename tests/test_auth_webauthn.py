"""Synthetic WebAuthn software keys and protocol refusals; no browser or network."""

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from functools import partial
from threading import Barrier

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa
from test_api import _get_json, _req, server as server  # noqa: F401
from test_auth_signin import auth_request
from test_auth_users import accounts as accounts  # noqa: F401

from fiscalm.api import (
    auth,
    cose,
    enrolment,
    limits,
    login,
    passkeys,
    passwords,
    recovery_codes,
    sessions,
    setup_links,
    signin,
    tickets,
    transport,
    users as account_users,
    webauthn,
)
from fiscalm.ledger import Ledger


def encoded(value):
    """A software authenticator's small test encoder, retaining map insertion order."""
    if isinstance(value, dict):
        return bytes([0xA0 + len(value)]) + b"".join(encoded(k) + encoded(v) for k, v in value.items())
    if isinstance(value, bytes):
        major, size, tail = 2, len(value), value
    elif isinstance(value, str):
        major, size, tail = 3, len(value.encode()), value.encode()
    elif isinstance(value, bool):
        return b"\xf5" if value else b"\xf4"
    else:
        major, size, tail = (0, value, b"") if value >= 0 else (1, -1 - value, b"")
    if size < 24:
        return bytes([(major << 5) | size]) + tail
    width = next(n for n in (1, 2, 4, 8) if size < 1 << (8 * n))
    return bytes([(major << 5) | {1: 24, 2: 25, 4: 26, 8: 27}[width]]) + size.to_bytes(width, "big") + tail


def software_authenticator(algorithm):
    if algorithm == -7:
        private = ec.generate_private_key(ec.SECP256R1())
        point = private.public_key().public_numbers()
        values = {1: 2, 3: -7, -1: 1, -2: point.x.to_bytes(32, "big"), -3: point.y.to_bytes(32, "big")}
        sign = partial(private.sign, signature_algorithm=ec.ECDSA(hashes.SHA256()))
    elif algorithm == -8:
        private = ed25519.Ed25519PrivateKey.generate()
        values = {1: 1, 3: -8, -1: 6, -2: private.public_key().public_bytes_raw()}
        sign = private.sign
    else:
        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public = private.public_key().public_numbers()
        values = {1: 3, 3: -257, -1: public.n.to_bytes(256, "big"), -2: b"\x01\x00\x01"}
        sign = partial(private.sign, padding=padding.PKCS1v15(), algorithm=hashes.SHA256())
    return values, sign


@pytest.fixture
def authenticator(request):
    """ES256, unless the test runs over every approved algorithm (`every_algorithm`): every test that makes the engine
    verify a signature does, and so do the COSE/attestation parsing tests. The rest only register keys or change stored
    ones, whose parsing those tests cover for each algorithm."""
    return software_authenticator(getattr(request, "param", -7))


every_algorithm = pytest.mark.parametrize("authenticator", [-7, -8, -257], indirect=True)


@every_algorithm
def test_cose_verifies_only_the_approved_signature_and_exact_message(authenticator):
    values, sign = authenticator
    raw, message = encoded(values), b"synthetic-authenticator-data-and-client-hash"
    key = cose.parse(raw)
    signature = sign(message)
    assert key.algorithm == values[3] and key.encoded == raw and key.verify(signature, message)
    assert not key.verify(signature, message + b"changed")
    for bad in (b"", signature[:-1], signature[:-1] + bytes([signature[-1] ^ 1]), b"\x00" * 1025, None):
        assert not key.verify(bad, message)
    assert not key.verify(signature, None)
    assert not key.verify(signature, b"\x00" * (cose.cbor.MAX_BYTES + 33))


@every_algorithm
@pytest.mark.parametrize(
    "change", ["kty", "algorithm", "boolean", "curve", "short", "private", "optional", "type", "large"]
)
def test_cose_refuses_unapproved_public_parameters(authenticator, change):
    values, _ = authenticator
    algorithm = values[3]
    binary = -1 if algorithm == -257 else -2
    mutations = {
        "kty": {1: 99},
        "algorithm": {3: -35},
        "boolean": {1: True},
        "curve": {-1: b"\x00" * 256} if algorithm == -257 else {-1: 99},
        "short": {binary: b"\x00" * (255 if algorithm == -257 else 31)},
        "private": {-3 if algorithm == -257 else -4: b"private-input-must-never-be-stored"},
        "optional": {2: b"unrequested-key-id"},
        "type": {binary: "not-bytes"},
        "large": {binary: b"\x00" * (1025 if algorithm == -257 else 33)},
    }
    with pytest.raises(cose.CoseError):
        cose.parse(encoded(values | mutations[change]))


def test_cose_refuses_invalid_curve_point_and_weak_or_invalid_rsa():
    bad_point = {1: 2, 3: -7, -1: 1, -2: b"\x00" * 32, -3: b"\x00" * 32}
    weak = rsa.generate_private_key(public_exponent=65537, key_size=1024).public_key().public_numbers()
    for values in (
        bad_point,
        {1: 3, 3: -257, -1: weak.n.to_bytes(128, "big"), -2: b"\x01\x00\x01"},
        {1: 3, 3: -257, -1: b"\x80" + b"\x01" * 255, -2: b"\x02"},
    ):
        with pytest.raises(cose.CoseError):
            cose.parse(encoded(values))


@pytest.mark.parametrize("sign", [0, 1])
def test_cose_refuses_small_order_ed25519_and_noncanonical_aliases(sign):
    # All eight torsion points, both sign encodings, and out-of-field encodings.
    coordinates = (
        0,
        1,
        (1 << 255) - 20,
        int.from_bytes(bytes.fromhex("26e8958fc2b227b045c3f489f2ef98f0d5dfac05d3c63339b13802886d53fc05"), "little"),
        int.from_bytes(bytes.fromhex("c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac037a"), "little"),
        *range((1 << 255) - 19, 1 << 255),
    )
    for y in coordinates:
        public = (y | (sign << 255)).to_bytes(32, "little")
        with pytest.raises(cose.CoseError):
            cose.parse(encoded({1: 1, 3: -8, -1: 6, -2: public}))


@every_algorithm
def test_cose_w3c_public_vector_and_canonical_encoding(authenticator):
    vector = bytes.fromhex(
        "a501020326200121582065eda5a12577c2bae829437fe338701a10aaa375e1bb5b5de108de439c08551d2258201e52ed75701163f7f9e40ddf9f341b3dc9ba860af7e0ca7ca7e9eecd0084d19c"
    )
    assert cose.parse(vector).algorithm == -7
    values, _ = authenticator
    if values[3] == -257:
        for label in (-1, -2):
            with pytest.raises(cose.CoseError):
                cose.parse(encoded(values | {label: b"\x00" + values[label]}))
    raw = encoded(values)
    for invalid in (
        raw + b"\x00",
        raw[:1] + b"\x18\x01" + raw[2:],
        encoded(dict(reversed(values.items()))),
        None,
        b"\xa0",
    ):
        with pytest.raises(cose.CoseError):
            cose.parse(invalid)
    for i in range(len(raw)):
        with suppress(cose.CoseError):
            cose.parse(raw[:i] + bytes([raw[i] ^ 255]) + raw[i + 1 :])


ORIGIN = "https://fiscalm.example:8443"
CHALLENGE = b"c" * 32
IDENT = b"synthetic-credential"


def client(kind, **changes):
    return json.dumps(dict(type=kind, challenge=webauthn.b64(CHALLENGE), origin=ORIGIN, **changes)).encode()


def auth_data(values=None, *, flags=None, count=0, ident=IDENT, tail=b""):
    flags = (0x45 if values else 5) if flags is None else flags
    header = hashlib.sha256(b"fiscalm.example").digest() + bytes([flags]) + count.to_bytes(4, "big")
    return header + (b"a" * 16 + len(ident).to_bytes(2, "big") + ident + encoded(values) if values else b"") + tail


def attest(raw, **changes):
    return encoded(dict(fmt="none", attStmt={}, authData=raw) | changes)


@every_algorithm
def test_webauthn_registration_and_assertion_bind_all_signed_bytes(authenticator):
    values, sign = authenticator
    verifier = webauthn.Verifier(ORIGIN)
    raw = auth_data(values, flags=0xDD, count=7, tail=encoded({"synthetic": True}))
    result = verifier.registration(attest(raw), client("webauthn.create"), CHALLENGE, IDENT)
    assert result.credential_id == IDENT and result.public_key.encoded == encoded(values)
    assert result.aaguid == b"a" * 16 and result.counter == 7 and result.backup_eligible and result.backed_up
    raw = auth_data(flags=0x9D, count=8, tail=encoded({"synthetic": True}))
    data = client("webauthn.get")
    signature = sign(raw + hashlib.sha256(data).digest())
    assert verifier.assertion(raw, data, CHALLENGE, signature, encoded(values)).counter == 8
    for modified, supplied, proof in (
        (raw[:36] + b"\x09" + raw[37:], data, signature),
        (raw, data + b" ", signature),
        (raw, data, sign(raw + data)),
    ):
        with pytest.raises(webauthn.WebAuthnError):
            verifier.assertion(modified, supplied, CHALLENGE, proof, encoded(values))


@every_algorithm
@pytest.mark.parametrize("kind", ["webauthn.create", "webauthn.get"])
@pytest.mark.parametrize(
    "bad",
    [
        "type",
        "challenge",
        "origin",
        "cross",
        "number",
        "null",
        "top",
        "duplicate",
        "nan",
        "utf8",
        "large",
        "nested",
        "array",
    ],
)
def test_webauthn_client_data_refusals(authenticator, kind, bad):
    values, sign = authenticator
    data = json.loads(client(kind))
    changes = {
        "type": {"type": "webauthn.get" if kind.endswith("create") else "webauthn.create"},
        "challenge": {"challenge": webauthn.b64(b"different")},
        "origin": {"origin": "https://other.example"},
        "cross": {"crossOrigin": True},
        "number": {"crossOrigin": 0},
        "null": {"crossOrigin": None},
        "top": {"topOrigin": ORIGIN},
        "nan": {"extra": float("nan")},
    }
    supplied = json.dumps(data | changes.get(bad, {})).encode()
    supplied = {
        "duplicate": supplied[:-1] + b',"origin":"https://other.example"}',
        "utf8": b"\xff",
        "large": supplied + b" " * 4096,
        "nested": b"[" * 1200 + b"0" + b"]" * 1200,
        "array": b"[]",
    }.get(bad, supplied)
    verifier = webauthn.Verifier(ORIGIN)
    with pytest.raises(webauthn.WebAuthnError):
        if kind.endswith("create"):
            verifier.registration(attest(auth_data(values)), supplied, CHALLENGE, IDENT)
        else:
            raw = auth_data()
            verifier.assertion(raw, supplied, CHALLENGE, sign(raw + hashlib.sha256(supplied).digest()), encoded(values))


@every_algorithm
@pytest.mark.parametrize(
    "bad",
    [
        "short",
        "rp",
        "presence",
        "verification",
        "backup",
        "attested",
        "truncated",
        "empty_id",
        "large_id",
        "key",
        "tail",
        "missing_ext",
        "array_ext",
        "key_ext",
        "duplicate_ext",
    ],
)
def test_webauthn_authenticator_refusals(authenticator, bad):
    values, _ = authenticator
    raw = auth_data(values)
    invalid = {
        "short": raw[:36],
        "rp": b"\x00" * 32 + raw[32:],
        "presence": auth_data(values, flags=0x44),
        "verification": auth_data(values, flags=0x41),
        "backup": auth_data(values, flags=0x55),
        "attested": auth_data(),
        "truncated": raw[:54],
        "empty_id": auth_data(values, ident=b""),
        "large_id": auth_data(values, ident=b"x" * 1024),
        "key": raw[: 55 + len(IDENT)] + b"\xa0",
        "tail": raw + b"\x00",
        "missing_ext": auth_data(values, flags=0xC5),
        "array_ext": auth_data(values, flags=0xC5, tail=b"\x80"),
        "key_ext": auth_data(values, flags=0xC5, tail=encoded({1: True})),
        "duplicate_ext": auth_data(values, flags=0xC5, tail=b"\xa2\x61a\x01\x61a\x02"),
    }[bad]
    with pytest.raises(webauthn.WebAuthnError):
        webauthn.Verifier(ORIGIN).registration(attest(invalid), client("webauthn.create"), CHALLENGE, IDENT)


@every_algorithm
def test_webauthn_none_attestation_and_external_id_refusals(authenticator):
    values, _ = authenticator
    raw, data = auth_data(values), client("webauthn.create")
    verifier = webauthn.Verifier(ORIGIN)
    for obj in (
        attest(raw, fmt="packed"),
        attest(raw, attStmt={"sig": b"untrusted"}),
        attest(raw, extra=True),
        b"\xa0",
    ):
        with pytest.raises(webauthn.WebAuthnError):
            verifier.registration(obj, data, CHALLENGE, IDENT)
    with pytest.raises(webauthn.WebAuthnError):
        verifier.registration(attest(raw), data, CHALLENGE, b"swapped")
    largest = auth_data(values, ident=b"x" * 1023)
    assert verifier.registration(attest(largest), data, CHALLENGE, b"x" * 1023).credential_id == b"x" * 1023
    obj = attest(raw)
    for i in range(len(obj)):
        with suppress(webauthn.WebAuthnError):
            verifier.registration(obj[:i] + bytes([obj[i] ^ 255]) + obj[i + 1 :], data, CHALLENGE, IDENT)


@every_algorithm
def test_webauthn_assertion_authenticator_refusals_even_with_valid_signature(authenticator):
    values, sign = authenticator
    verifier, data = webauthn.Verifier(ORIGIN), client("webauthn.get")
    raw = auth_data()
    invalid = [raw[:36], b"\x00" * 32 + raw[32:], raw + b"\x00", auth_data(values)]
    invalid += [auth_data(flags=flags) for flags in (4, 1, 0x15, 0x85)]
    invalid += [auth_data(flags=0x85, tail=b"\x80")]
    for raw in invalid:
        signature = sign(raw + hashlib.sha256(data).digest())
        with pytest.raises(webauthn.WebAuthnError):
            verifier.assertion(raw, data, CHALLENGE, signature, encoded(values))


@pytest.mark.parametrize("bad", ["", "a", "a=", "+w", "/w", "_x", "AAAA", "ñ", None])
def test_webauthn_base64url_refusals(bad):
    with pytest.raises(webauthn.WebAuthnError):
        webauthn.unb64(bad, 2)


def test_webauthn_base64url_and_rp_configuration():
    normalized = webauthn.Verifier("https://FISCALM.example:443")
    assert normalized.origin == "https://fiscalm.example" and normalized.rp_id == "fiscalm.example"
    for raw in (b"\xff", b"\x00\xff", b"\x00\x80\xff"):
        assert webauthn.unb64(webauthn.b64(raw), len(raw)) == raw
    for bad in ("http://fiscalm.example", "https://fiscalm.example/path", "https://127.0.0.1", "https://[::1]"):
        with pytest.raises(ValueError):
            webauthn.Verifier(bad)


@pytest.fixture
def stored_key(ledger, accounts, authenticator):
    ids, at = accounts
    values, _ = authenticator
    params = dict(
        credential_id=IDENT,
        user_id=ids["first"],
        public_key=encoded(values),
        algorithm=values[3],
        sign_count=0,
        backup_eligible=0,
        backed_up=0,
        aaguid=b"a" * 16,
        name="Dispozitiv",
        transports='["internal"]',
        created_at=sessions.stamp(at),
    )
    assert ledger.add_passkey(params)
    return params, ids, sessions.stamp(at)


@every_algorithm
def test_passkey_storage_refusals_audit_and_outer_rollback(ledger, stored_key):
    params, ids, stamp = stored_key
    row = dict(ledger.db.execute("SELECT * FROM passkeys").fetchone())
    assert row == params | {"last_used_at": None}
    assert not ledger.add_passkey(params) and not ledger.add_passkey(params | {"user_id": ids["second"]})
    assert not ledger.add_passkey(params | {"credential_id": b"missing", "user_id": "missing"})
    ledger.change_user(ids["second"], None, stamp, enabled=False)
    assert not ledger.add_passkey(params | {"credential_id": b"disabled", "user_id": ids["second"]})
    with pytest.raises(ValueError), ledger.tx():
        assert ledger.add_passkey(params | {"credential_id": b"rolled-back"})
        raise ValueError("synthetic registration final-write failure")
    assert ledger.db.execute("SELECT count(*) FROM passkeys").fetchone()[0] == 1
    events = [dict(r) for r in ledger.db.execute("SELECT * FROM auth_events WHERE kind='factor_added'")]
    assert len(events) == 1 and events[0]["actor"] == "user " + ids["first"]
    assert json.loads(events[0]["detail"]) == {"method": "passkey"}
    assert IDENT.decode() not in str(events) and params["public_key"].hex() not in str(events)


def test_passkey_counter_replay_backup_and_transaction_rollback(ledger, stored_key):
    params, _, stamp = stored_key
    assert ledger.use_passkey(params, 0, False, False, stamp)  # Synced credentials may always report zero.
    with pytest.raises(ValueError), ledger.tx():
        assert ledger.use_passkey(params, 1, False, False, stamp)
        raise ValueError("synthetic challenge-consumption failure")
    assert ledger.db.execute("SELECT sign_count FROM passkeys").fetchone()[0] == 0
    assert ledger.use_passkey(params, 1, False, False, stamp)
    current = params | {"sign_count": 1}
    for count in (0, 1):
        assert not ledger.use_passkey(current, count, False, False, stamp)
    for count in (-1, 4294967296, True, 1.5):
        assert not ledger.use_passkey(current, count, False, False, stamp)
    assert not ledger.use_passkey(current, 2, True, False, stamp)
    assert not ledger.use_passkey(current, 2, False, True, stamp)
    assert not ledger.use_passkey(params, 2, False, False, stamp)  # Stale snapshot.
    assert ledger.use_passkey(current, 2, False, False, stamp)
    assert ledger.db.execute("SELECT sign_count,last_used_at FROM passkeys").fetchone()[:] == (2, stamp)
    assert ledger.db.execute("SELECT count(*) FROM auth_events WHERE kind='passkey_counter_refused'").fetchone()[0] == 2


def test_passkey_counter_two_connections_have_one_winner(ledger, stored_key):
    params, _, stamp = stored_key
    assert ledger.use_passkey(params, 1, False, False, stamp)
    expected, barrier = params | {"sign_count": 1}, Barrier(2)

    def consume(_):
        other = Ledger(ledger.path)
        try:
            barrier.wait(timeout=5)
            return other.use_passkey(expected, 2, False, False, stamp)
        finally:
            other.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(consume, range(2))) == [False, True]
    assert ledger.db.execute("SELECT count(*) FROM auth_events WHERE kind='passkey_counter_refused'").fetchone()[0] == 1


def test_passkey_backup_state_may_change_but_eligibility_may_not(ledger, stored_key):
    params, _, stamp = stored_key
    synced = params | {"credential_id": b"synced", "backup_eligible": 1}
    assert ledger.add_passkey(synced)
    assert ledger.use_passkey(synced, 0, True, True, stamp)
    assert ledger.use_passkey(synced, 0, True, False, stamp)
    assert not ledger.use_passkey(synced, 0, False, False, stamp)
    row = ledger.db.execute(
        "SELECT backup_eligible,backed_up FROM passkeys WHERE credential_id=?", (b"synced",)
    ).fetchone()
    assert row[:] == (1, 0)


def test_passkey_use_rechecks_persisted_owner_key_and_disabled_state(ledger, stored_key):
    params, ids, stamp = stored_key
    other = Ledger(ledger.path)
    try:
        for column, replacement in (("user_id", ids["second"]), ("public_key", b"changed"), ("algorithm", -35)):
            if column == "algorithm":
                assert not ledger.use_passkey(params | {column: replacement}, 1, False, False, stamp)
                continue
            with other.tx() as db:
                db.execute(f"UPDATE passkeys SET {column}=?", (replacement,))
            assert not ledger.use_passkey(params, 1, False, False, stamp)
            with other.tx() as db:
                db.execute(f"UPDATE passkeys SET {column}=?", (params[column],))
        other.change_user(ids["first"], None, stamp, enabled=False)
        assert not ledger.use_passkey(params, 1, False, False, stamp)
    finally:
        other.close()


@pytest.mark.parametrize(
    "column,value",
    [
        ("credential_id", b""),
        ("credential_id", b"x" * 1024),
        ("public_key", b""),
        ("algorithm", -35),
        ("sign_count", -1),
        ("sign_count", 4294967296),
        ("sign_count", 1.5),
        ("backed_up", 1),
        ("backup_eligible", 2),
        ("aaguid", b"short"),
        ("name", ""),
        ("user_id", "missing"),
    ],
)
def test_passkey_schema_refuses_invalid_records(ledger, stored_key, column, value):
    ledger.db.execute("PRAGMA foreign_keys=ON")
    with pytest.raises(sqlite3.IntegrityError), ledger.tx() as db:
        db.execute(f"UPDATE passkeys SET {column}=?", (value,))


@pytest.fixture
def key_only(ledger, stored_key):
    params, ids, _ = stored_key
    with ledger.tx() as db:
        db.execute("UPDATE users SET password_hash=NULL,totp_version=NULL WHERE id=?", (ids["first"],))
    return stored_key


def test_passkey_lifecycle_owner_noops_disable_and_outer_rollback(ledger, stored_key):
    params, ids, stamp = stored_key
    assert not ledger.rename_passkey(IDENT, ids["second"], "Alt nume", stamp)
    assert not ledger.remove_passkey(IDENT, ids["second"], stamp)
    assert not ledger.rename_passkey(IDENT, ids["first"], params["name"], stamp)
    with pytest.raises(ValueError), ledger.tx():
        assert ledger.rename_passkey(IDENT, ids["first"], "Alt nume", stamp)
        assert ledger.remove_passkey(IDENT, ids["first"], stamp)
        raise ValueError("synthetic lifecycle final-write failure")
    assert ledger.db.execute("SELECT name FROM passkeys").fetchone()[0] == params["name"]
    assert ledger.rename_passkey(IDENT, ids["first"], "Alt nume", stamp)
    assert ledger.remove_passkey(IDENT, ids["first"], stamp)  # Password plus TOTP remain.
    assert not ledger.remove_passkey(IDENT, ids["first"], stamp)
    events = list(
        ledger.db.execute("SELECT kind,actor FROM auth_events WHERE kind IN ('passkey_renamed','factor_removed')")
    )
    assert [r[:] for r in events] == [(kind, "user " + ids["first"]) for kind in ("passkey_renamed", "factor_removed")]
    assert ledger.add_passkey(params)
    ledger.change_user(ids["first"], None, stamp, enabled=False)
    assert not ledger.rename_passkey(IDENT, ids["first"], "Dispozitiv", stamp)
    assert not ledger.remove_passkey(IDENT, ids["first"], stamp)


@pytest.mark.parametrize("operation", ["disable", "demote", "reset"])
def test_passkey_only_administrator_is_working_and_last_admin_is_preserved(ledger, key_only, operation):
    _, ids, stamp = key_only
    ledger.change_user(ids["second"], None, stamp, role="member")  # The passkey-only admin counts.
    with pytest.raises(ValueError):
        if operation == "reset":
            setup_links.issue(ledger, ids["first"], datetime.fromisoformat(stamp), actor_id=ids["first"], reset=True)
        else:
            ledger.change_user(
                ids["first"], None, stamp, **({"enabled": False} if operation == "disable" else {"role": "member"})
            )
    assert ledger.db.execute("SELECT role,disabled_at FROM users WHERE id=?", (ids["first"],)).fetchone()[:] == (
        "admin",
        None,
    )
    if operation == "reset":
        setup_links.issue(ledger, ids["first"], datetime.fromisoformat(stamp), actor_id=None, reset=True)
        assert not ledger.db.execute("SELECT 1 FROM passkeys").fetchone()


def test_removing_two_passkeys_concurrently_preserves_one_strong_factor(ledger, key_only):
    params, ids, stamp = key_only
    assert ledger.add_passkey(params | {"credential_id": b"second-key"})
    barrier = Barrier(2)

    def remove(ident):
        other = Ledger(ledger.path)
        try:
            barrier.wait(timeout=5)
            try:
                return other.remove_passkey(ident, ids["first"], stamp)
            except ValueError:
                return False
        finally:
            other.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(remove, (IDENT, b"second-key"))) == [False, True]
    assert ledger.db.execute("SELECT count(*) FROM passkeys").fetchone()[0] == 1
    assert ledger.db.execute("SELECT count(*) FROM auth_events WHERE kind='factor_removed'").fetchone()[0] == 1


def test_incomplete_password_totp_is_not_an_alternative_to_the_last_passkey(ledger, key_only):
    _, ids, stamp = key_only
    for column in ("password_hash", "totp_version"):
        with ledger.tx() as db:
            db.execute("UPDATE users SET password_hash=NULL,totp_version=NULL WHERE id=?", (ids["first"],))
            db.execute(f"UPDATE users SET {column}='synthetic-incomplete-factor' WHERE id=?", (ids["first"],))
        with pytest.raises(ValueError):
            ledger.remove_passkey(IDENT, ids["first"], stamp)


def test_concurrent_passkey_admin_disable_and_demote_leave_a_working_admin(ledger, key_only):
    params, ids, stamp = key_only
    assert ledger.add_passkey(params | {"credential_id": b"second-key", "user_id": ids["second"]})
    with ledger.tx() as db:
        db.execute("UPDATE users SET password_hash=NULL,totp_version=NULL WHERE id=?", (ids["second"],))
    barrier = Barrier(2)

    def change(name):
        other = Ledger(ledger.path)
        try:
            barrier.wait(timeout=5)
            try:
                other.change_user(
                    ids[name], None, stamp, **({"role": "member"} if name == "first" else {"enabled": False})
                )
                return True
            except ValueError:
                return False
        finally:
            other.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(change, ("first", "second"))) == [False, True]
    assert ledger.db.execute("SELECT count(*) FROM users WHERE role='admin' AND disabled_at IS NULL").fetchone()[0] == 1


def test_passkey_only_codes_setup_refusal_and_reset_are_atomic(ledger, key_only):
    params, ids, stamp = key_only
    at = datetime.fromisoformat(stamp)
    codes = recovery_codes.generate(ledger, ids["first"], at)
    ident = sessions.issue(ledger, ids["first"], at)
    token = tickets.issue(ledger, ids["first"], "signin", at)
    with pytest.raises(ValueError):
        setup_links.issue(ledger, ids["first"], at, actor_id=None)
    with pytest.raises(ValueError), ledger.tx():
        setup_links.issue(ledger, ids["first"], at, actor_id=ids["second"], reset=True)
        assert not ledger.db.execute("SELECT 1 FROM passkeys").fetchone()
        raise ValueError("synthetic reset final-write failure")
    assert ledger.db.execute("SELECT count(*) FROM passkeys").fetchone()[0] == 1
    assert sessions.identify(ledger, ident, at) and tickets.attempt(ledger, token, "signin", at)
    assert recovery_codes.consume(ledger, ids["first"], codes[0], at)
    link = setup_links.issue(ledger, ids["first"], at, actor_id=ids["second"], reset=True).partition("#")[2]
    assert not ledger.db.execute("SELECT 1 FROM passkeys").fetchone()
    assert not ledger.db.execute("SELECT 1 FROM recovery_codes").fetchone()
    assert sessions.identify(ledger, ident, at) is None and tickets.attempt(ledger, token, "signin", at) is None
    assert ledger.add_passkey(params)  # A competing setup completed after the link was created.
    flow = enrolment.Enrolment(lambda: at, passwords.Passwords())
    assert flow.begin_setup(ledger, link, "synthetic-setup-password").status == 401
    assert setup_links.inspect(ledger, link, at)


def test_totp_recovery_retires_only_the_owners_passkeys_atomically(ledger, stored_key):
    params, ids, stamp = stored_key
    assert ledger.add_passkey(params | {"credential_id": b"other-owner", "user_id": ids["second"]})
    with pytest.raises(ValueError), ledger.tx():
        ledger.finish_recovery(ids["first"], stamp)
        raise ValueError("synthetic recovery final-write failure")
    assert ledger.db.execute("SELECT count(*) FROM passkeys").fetchone()[0] == 2
    assert not ledger.db.execute("SELECT 1 FROM auth_events WHERE kind='recovery_completed'").fetchone()
    ledger.finish_recovery(ids["first"], stamp)
    assert ledger.db.execute("SELECT user_id FROM passkeys").fetchone()[0] == ids["second"]


@pytest.fixture
def registration(ledger, accounts, authenticator):
    ids, at = accounts
    clock = [at]
    link = setup_links.issue(ledger, ids["first"], at, actor_id=None, reset=True).partition("#")[2]
    return passkeys.Passkeys(lambda: clock[0], ORIGIN), clock, link, ids, authenticator[0]


def registration_body(options, values, ident=IDENT):
    data = json.loads(client("webauthn.create")) | {"challenge": options.body["publicKey"]["challenge"]}
    return dict(
        credential_id=webauthn.b64(ident),
        client_data=webauthn.b64(json.dumps(data).encode()),
        attestation=webauthn.b64(attest(auth_data(values, ident=ident))),
        transports='["internal","future-transport","internal"]',
    )


@pytest.mark.parametrize("name", ["", " ", "a" * 81, "con\ntrol", "\ud800"])
def test_passkey_registration_refuses_invalid_labels_without_publishing_a_ticket(ledger, registration, name):
    service, clock, link, _, _ = registration
    assert service.begin_setup(ledger, link, name).status == 400
    assert not ledger.db.execute("SELECT 1 FROM auth_tickets WHERE purpose='passkey_setup'").fetchone()
    assert setup_links.inspect(ledger, link, clock[0])


def test_passkey_setup_is_strong_atomic_and_replaces_only_the_presented_cookie(ledger, registration):
    service, clock, link, ids, values = registration
    options = service.begin_setup(ledger, link, " Ｃheie ")
    public = options.body["publicKey"]
    assert public["rp"]["id"] == "fiscalm.example" and public["attestation"] == "none"
    assert public["authenticatorSelection"] == {"residentKey": "required", "userVerification": "required"}
    assert [v["alg"] for v in public["pubKeyCredParams"]] == [-7, -8, -257]
    assert not public["excludeCredentials"] and not ledger.db.execute("SELECT 1 FROM sessions").fetchone()
    handle = ledger.db.execute("SELECT webauthn_handle FROM users WHERE id=?", (ids["first"],)).fetchone()[0]
    assert webauthn.unb64(public["user"]["id"], 16) == handle
    old = sessions.issue(ledger, ids["second"], clock[0])
    untouched = sessions.issue(ledger, ids["second"], clock[0])
    result = service.finish_setup(
        ledger,
        link,
        options.body["ticket"],
        registration_body(options, values),
        presented=sessions.identify(ledger, old, clock[0]),
    )
    assert result.status == 200 and len(result.body["codes"]) == 10
    principal = sessions.identify(ledger, result.session_id, clock[0])
    assert principal["user_id"] == ids["first"] and principal["method"] == "passkey"
    assert sessions.identify(ledger, old, clock[0]) is None and sessions.identify(ledger, untouched, clock[0])
    assert ledger.db.execute("SELECT password_hash,totp_version FROM users WHERE id=?", (ids["first"],)).fetchone()[
        :
    ] == (None, None)
    row = ledger.db.execute("SELECT name,transports FROM passkeys").fetchone()
    assert row["name"] == "Cheie" and json.loads(row["transports"]) == ["internal"]
    assert service.finish_setup(ledger, link, options.body["ticket"], registration_body(options, values)).status == 401


def test_passkey_setup_wrong_proofs_keep_their_budget_and_can_restart(ledger, registration):
    service, clock, link, _, values = registration
    options = service.begin_setup(ledger, link, "Cheie")
    body = registration_body(options, values)
    for _ in range(5):
        assert (
            service.finish_setup(ledger, link, options.body["ticket"], body | {"client_data": "invalid"}).status == 401
        )
    assert ledger.db.execute("SELECT attempts FROM auth_tickets WHERE purpose='passkey_setup'").fetchone()[0] == 5
    assert service.finish_setup(ledger, link, options.body["ticket"], body).status == 401
    assert setup_links.inspect(ledger, link, clock[0]) and not ledger.db.execute("SELECT 1 FROM passkeys").fetchone()
    fresh = service.begin_setup(ledger, link, "Cheie")
    assert service.finish_setup(ledger, link, fresh.body["ticket"], body).status == 401  # Old challenge.
    assert service.finish_setup(ledger, link, fresh.body["ticket"], registration_body(fresh, values)).status == 200


@pytest.mark.parametrize("change", ["owner", "link", "ticket_expiry", "link_expiry"])
def test_passkey_setup_refuses_swapped_or_expired_capabilities(ledger, registration, change):
    service, clock, link, ids, values = registration
    if change == "link_expiry":
        clock[0] += timedelta(hours=71, minutes=59)
    options = service.begin_setup(ledger, link, "Cheie")
    body = registration_body(options, values)
    if change in ("owner", "link"):
        owner = ids["second"] if change == "owner" else ids["first"]
        link = setup_links.issue(ledger, owner, clock[0], actor_id=None, reset=True).partition("#")[2]
    else:
        clock[0] += timedelta(minutes=5 if change == "ticket_expiry" else 2)
    assert service.finish_setup(ledger, link, options.body["ticket"], body).status == 401
    assert not ledger.db.execute("SELECT 1 FROM passkeys").fetchone()


@pytest.mark.parametrize("failure", ["ticket", "audit"])
def test_passkey_setup_final_failure_restores_every_write_and_the_old_cookie(
    ledger, registration, monkeypatch, failure
):
    service, clock, link, ids, values = registration
    options = service.begin_setup(ledger, link, "Cheie")
    body = registration_body(options, values)
    old = sessions.issue(ledger, ids["second"], clock[0])

    def fault(*args):
        if failure == "ticket":
            return False
        raise ValueError("synthetic final passkey setup audit failure")

    with monkeypatch.context() as patch, pytest.raises((ValueError, PermissionError)):
        patch.setattr(ledger, "consume_auth_ticket" if failure == "ticket" else "record_signin", fault)
        service.finish_setup(
            ledger, link, options.body["ticket"], body, presented=sessions.identify(ledger, old, clock[0])
        )
    assert sessions.identify(ledger, old, clock[0]) and setup_links.inspect(ledger, link, clock[0])
    for table in ("passkeys", "recovery_codes"):
        assert ledger.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    assert ledger.db.execute("SELECT attempts,used_at FROM auth_tickets WHERE purpose='passkey_setup'").fetchone()[
        :
    ] == (0, None)
    assert service.finish_setup(ledger, link, options.body["ticket"], body).status == 200


def test_passkey_setup_two_connections_publish_one_account_session(ledger, registration):
    service, _, link, _, values = registration
    options = service.begin_setup(ledger, link, "Cheie")
    body, barrier = registration_body(options, values), Barrier(2)

    def finish(_):
        other = Ledger(ledger.path)
        try:
            barrier.wait(timeout=5)
            return service.finish_setup(other, link, options.body["ticket"], body).status
        finally:
            other.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(finish, range(2))) == [200, 401]
    assert ledger.db.execute("SELECT count(*) FROM passkeys").fetchone()[0] == 1
    assert ledger.db.execute("SELECT count(*) FROM sessions").fetchone()[0] == 1


@pytest.mark.parametrize("role", ["admin", "member", "readonly"])
def test_adding_passkey_needs_recent_owner_session_for_every_role(ledger, registration, role):
    service, clock, link, ids, values = registration
    setup = service.begin_setup(ledger, link, "Cheie")
    result = service.finish_setup(ledger, link, setup.body["ticket"], registration_body(setup, values))
    principal = sessions.identify(ledger, result.session_id, clock[0])
    ledger.change_user(ids["first"], None, sessions.stamp(clock[0]), role=role)
    other = sessions.issue(ledger, ids["first"], clock[0])
    options = service.begin_add(ledger, principal, "Dispozitiv nou")
    assert options.body["publicKey"]["excludeCredentials"] == [{"type": "public-key", "id": webauthn.b64(IDENT)}]
    body = registration_body(options, values, b"added-credential")
    assert (
        service.finish_add(ledger, sessions.identify(ledger, other, clock[0]), options.body["ticket"], body).status
        == 401
    )
    assert service.finish_add(ledger, principal, options.body["ticket"], body).status == 200
    assert sessions.identify(ledger, result.session_id, clock[0]) and sessions.identify(ledger, other, clock[0]) is None
    assert service.finish_add(ledger, principal, options.body["ticket"], body).status == 401
    clock[0] += timedelta(minutes=10)
    with pytest.raises(PermissionError, match="reauth_required"):
        service.begin_add(ledger, principal, "Alt dispozitiv")


@pytest.mark.parametrize("denial", ["recovery", "revoked", "disabled", "other_user", "duplicate", "transports"])
def test_passkey_add_refusals_keep_the_old_credential(ledger, registration, denial):
    service, clock, link, ids, values = registration
    setup = service.begin_setup(ledger, link, "Cheie")
    result = service.finish_setup(ledger, link, setup.body["ticket"], registration_body(setup, values))
    principal = sessions.identify(ledger, result.session_id, clock[0])
    options = service.begin_add(ledger, principal, "Alt dispozitiv")
    body = registration_body(options, values, IDENT if denial == "duplicate" else b"new-credential")
    if denial in ("recovery", "other_user"):
        raw = sessions.issue(
            ledger, ids["first" if denial == "recovery" else "second"], clock[0], recovery=denial == "recovery"
        )
        principal = sessions.identify(ledger, raw, clock[0])
    if denial in ("revoked", "disabled"):
        other = Ledger(ledger.path)
        try:
            if denial == "revoked":
                other.revoke_session(principal["id_hash"], ids["first"], sessions.stamp(clock[0]))
            else:
                other.change_user(ids["first"], None, sessions.stamp(clock[0]), enabled=False)
        finally:
            other.close()
    if denial == "transports":
        body["transports"] = "[1]"
    if denial in ("recovery", "revoked", "disabled"):
        with pytest.raises(PermissionError):
            service.finish_add(ledger, principal, options.body["ticket"], body)
    else:
        assert service.finish_add(ledger, principal, options.body["ticket"], body).status == 401
    assert ledger.db.execute("SELECT credential_id FROM passkeys").fetchone()[0] == IDENT
    assert ledger.db.execute("SELECT count(*) FROM auth_events WHERE kind='factor_added'").fetchone()[0] == 1


@pytest.fixture
def passkey_account(ledger, registration, authenticator):
    service, clock, link, ids, values = registration
    options = service.begin_setup(ledger, link, "Cheie")
    result = service.finish_setup(ledger, link, options.body["ticket"], registration_body(options, values))
    principal = sessions.identify(ledger, result.session_id, clock[0])
    handle = ledger.db.execute("SELECT webauthn_handle FROM users WHERE id=?", (ids["first"],)).fetchone()[0]
    return service, clock, ids, authenticator[1], handle, principal


def recover(ledger, clock, username, code, **kwargs):
    """Recovery has one entry, shared with password accounts, in signin."""
    return signin.Signin(lambda: clock[0]).recover(ledger, username, code, **kwargs)


def assertion_body(options, sign, handle, *, count=1, ident=IDENT):
    data = json.loads(client("webauthn.get")) | {"challenge": options.body["publicKey"]["challenge"]}
    client_data = json.dumps(data).encode()
    raw = auth_data(count=count)
    return dict(
        credential_id=webauthn.b64(ident),
        user_handle=webauthn.b64(handle),
        client_data=webauthn.b64(client_data),
        authenticator_data=webauthn.b64(raw),
        signature=webauthn.b64(sign(raw + hashlib.sha256(client_data).digest())),
    )


@every_algorithm
def test_discoverable_signin_ignores_password_busy_lease_and_rotates_presented_owner(ledger, passkey_account):
    service, clock, ids, sign, handle, _ = passkey_account
    previous = sessions.issue(ledger, ids["second"], clock[0])
    untouched = sessions.issue(ledger, ids["second"], clock[0])
    reservation = limits.reserve(ledger, "first", clock[0])
    options = service.begin_signin(ledger)
    assert "allowCredentials" not in options.body["publicKey"] and "user" not in options.body["publicKey"]
    row = ledger.db.execute("SELECT user_id,session_hash FROM auth_tickets WHERE purpose='passkey_signin'").fetchone()
    assert row[:] == (None, None)
    result = service.finish_signin(
        ledger,
        options.body["ticket"],
        assertion_body(options, sign, handle),
        presented=sessions.identify(ledger, previous, clock[0]),
    )
    assert result.status == 200 and sessions.identify(ledger, result.session_id, clock[0])["user_id"] == ids["first"]
    assert sessions.identify(ledger, previous, clock[0]) is None and sessions.identify(ledger, untouched, clock[0])
    assert not limits.release(ledger, "first", reservation, clock[0])


@every_algorithm
@pytest.mark.parametrize(
    "denial", ["handle", "signature", "unknown", "disabled", "locked", "delayed", "expired", "malformed"]
)
def test_passkey_signin_refuses_without_issuing_a_session(ledger, passkey_account, denial):
    service, clock, ids, sign, handle, _ = passkey_account
    options = service.begin_signin(ledger)
    body = assertion_body(options, sign, handle)
    if denial in ("handle", "signature", "unknown", "malformed"):
        field = {
            "handle": "user_handle",
            "signature": "signature",
            "unknown": "credential_id",
            "malformed": "client_data",
        }[denial]
        body[field] = "invalid" if denial == "malformed" else webauthn.b64(b"wrong")
    if denial == "disabled":
        ledger.change_user(ids["first"], None, sessions.stamp(clock[0]), enabled=False)
    if denial == "locked":
        with ledger.tx() as db:
            db.execute(
                "INSERT INTO auth_limits(key_hash,user_id,failures,since,locked_until) VALUES(?,?,20,?,?)",
                (
                    limits.key("first"),
                    ids["first"],
                    sessions.stamp(clock[0]),
                    sessions.stamp(clock[0] + timedelta(days=1)),
                ),
            )
    if denial == "expired":
        clock[0] += timedelta(minutes=5)
    if denial == "delayed":
        for _ in range(5):
            limits.failure(ledger, "first", clock[0], reason="passkey")
    result = service.finish_signin(ledger, options.body["ticket"], body)
    assert result.status == (429 if denial == "delayed" else 401) and result.session_id is None
    assert ledger.db.execute("SELECT count(*) FROM sessions").fetchone()[0] == 1
    assert ledger.db.execute("SELECT sign_count FROM passkeys").fetchone()[0] == 0
    if denial in ("handle", "signature", "malformed"):
        assert ledger.db.execute("SELECT failures FROM users WHERE id=?", (ids["first"],)).fetchone()[0] == 1
        assert ledger.db.execute("SELECT attempts FROM auth_tickets WHERE purpose='passkey_signin'").fetchone()[0] == 1


@every_algorithm
def test_passkey_wrong_proofs_exhaust_ticket_and_restart_keeps_account_backoff(ledger, passkey_account):
    service, clock, _, sign, handle, _ = passkey_account
    options = service.begin_signin(ledger)
    body = assertion_body(options, sign, handle)
    for _ in range(5):
        assert service.finish_signin(ledger, options.body["ticket"], body | {"signature": "invalid"}).status == 401
    assert service.finish_signin(ledger, options.body["ticket"], body).status == 401
    fresh = service.begin_signin(ledger)
    fresh_body = assertion_body(fresh, sign, handle)
    assert service.finish_signin(ledger, fresh.body["ticket"], fresh_body).status == 429
    clock[0] += timedelta(seconds=1)
    assert service.finish_signin(ledger, fresh.body["ticket"], fresh_body).status == 200


@every_algorithm
def test_passkey_counter_replay_and_zero_are_bound_to_fresh_challenges(ledger, passkey_account):
    service, clock, _, sign, handle, _ = passkey_account
    for count, expected in ((0, 200), (0, 200), (1, 200), (1, 401), (0, 401), (2, 200)):
        options = service.begin_signin(ledger)
        body = assertion_body(options, sign, handle, count=count)
        result = service.finish_signin(ledger, options.body["ticket"], body)
        assert result.status == expected
        assert service.finish_signin(ledger, options.body["ticket"], body).status == 401
    assert ledger.db.execute("SELECT count(*) FROM auth_events WHERE kind='passkey_counter_refused'").fetchone()[0] == 4
    fresh = service.begin_signin(ledger)
    assert service.finish_signin(ledger, fresh.body["ticket"], body).status == 401
    assert ledger.db.execute("SELECT sign_count FROM passkeys").fetchone()[0] == 2


@every_algorithm
@pytest.mark.parametrize("race", ["ticket", "counter"])
def test_passkey_assertion_two_connections_produce_one_session(ledger, passkey_account, race):
    service, _, _, sign, handle, _ = passkey_account
    first = service.begin_signin(ledger)
    second = first if race == "ticket" else service.begin_signin(ledger)
    barrier = Barrier(2)

    def finish(options):
        other = Ledger(ledger.path)
        try:
            body = assertion_body(options, sign, handle, count=0 if race == "ticket" else 1)
            barrier.wait(timeout=5)
            return service.finish_signin(other, options.body["ticket"], body).status
        finally:
            other.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(finish, (first, second))) == [200, 401]
    assert ledger.db.execute("SELECT count(*) FROM sessions").fetchone()[0] == 2


@every_algorithm
@pytest.mark.parametrize("failure", ["ticket", "audit"])
def test_passkey_assertion_final_failure_restores_counter_ticket_and_cookie(
    ledger, passkey_account, monkeypatch, failure
):
    service, clock, _, sign, handle, principal = passkey_account
    options = service.begin_signin(ledger)
    body = assertion_body(options, sign, handle)

    def fault(*args):
        if failure == "ticket":
            return False
        raise ValueError("synthetic passkey sign-in final audit failure")

    with monkeypatch.context() as patch, pytest.raises((PermissionError, ValueError)):
        patch.setattr(ledger, "consume_auth_ticket" if failure == "ticket" else "record_signin", fault)
        service.finish_signin(ledger, options.body["ticket"], body, presented=principal)
    assert ledger.db.execute("SELECT sign_count FROM passkeys").fetchone()[0] == 0
    assert ledger.db.execute("SELECT attempts,used_at FROM auth_tickets WHERE purpose='passkey_signin'").fetchone()[
        :
    ] == (0, None)
    assert sessions.lookup(ledger, principal["id_hash"], clock[0])
    assert service.finish_signin(ledger, options.body["ticket"], body, presented=principal).status == 200


@every_algorithm
@pytest.mark.parametrize("denial", ["none", "other_user", "other_session", "revoked", "recovery"])
def test_passkey_stepup_is_bound_to_the_same_live_owner_session(ledger, passkey_account, denial):
    service, clock, ids, sign, handle, principal = passkey_account
    clock[0] += timedelta(minutes=11)
    options = service.begin_signin(ledger, principal=principal)
    body = assertion_body(options, sign, handle)
    if denial == "other_user":
        key = dict(ledger.db.execute("SELECT * FROM passkeys").fetchone())
        assert ledger.add_passkey(key | {"credential_id": b"other-user", "user_id": ids["second"]})
        other_handle = ledger.db.execute("SELECT webauthn_handle FROM users WHERE id=?", (ids["second"],)).fetchone()[0]
        body = assertion_body(options, sign, other_handle, ident=b"other-user")
    if denial == "other_session":
        principal = sessions.identify(ledger, sessions.issue(ledger, ids["first"], clock[0]), clock[0])
    if denial == "recovery":
        principal = sessions.identify(ledger, sessions.issue(ledger, ids["first"], clock[0], recovery=True), clock[0])
    if denial == "revoked":
        ledger.revoke_session(principal["id_hash"], ids["first"], sessions.stamp(clock[0]))
    if denial in ("revoked", "recovery"):
        with pytest.raises(PermissionError):
            service.finish_signin(ledger, options.body["ticket"], body, principal=principal)
    else:
        result = service.finish_signin(ledger, options.body["ticket"], body, principal=principal)
        assert result.status == (200 if denial == "none" else 401)
        if denial == "none":
            assert sessions.lookup(ledger, principal["id_hash"], clock[0]) is None
            assert sessions.recent(sessions.identify(ledger, result.session_id, clock[0]), clock[0])


@every_algorithm
def test_code_only_recovery_is_restricted_and_replaces_all_lost_factors(ledger, passkey_account):
    service, clock, ids, old_sign, handle, old_principal = passkey_account
    with ledger.tx() as db:
        db.execute("UPDATE users SET password_hash='synthetic-incomplete-password' WHERE id=?", (ids["first"],))
    codes = recovery_codes.generate(ledger, ids["first"], clock[0])
    result = recover(ledger, clock, "ＦＩＲＳＴ", codes[0])
    principal = sessions.identify(ledger, result.session_id, clock[0])
    assert principal["method"] == "recovery_code" and principal["recovery_only"]
    assert not auth.require(principal, "read") and not sessions.recent(principal, clock[0])
    assert sessions.lookup(ledger, old_principal["id_hash"], clock[0]) is None
    totp_flow = enrolment.Enrolment(lambda: clock[0], passwords.Passwords())
    assert totp_flow.begin_recovery(ledger, principal).status == 401
    with pytest.raises(PermissionError):
        service.begin_signin(ledger, principal=principal)
    new_values, new_sign = software_authenticator(ledger.db.execute("SELECT algorithm FROM passkeys").fetchone()[0])
    options = service.begin_recovery(ledger, principal, "Cheie de înlocuire")
    body = registration_body(options, new_values, b"replacement")
    assert (
        service.finish_recovery(ledger, principal, options.body["ticket"], body | {"client_data": "invalid"}).status
        == 401
    )
    finished = service.finish_recovery(ledger, principal, options.body["ticket"], body)
    assert finished.status == 200 and finished.clear_session and finished.session_id is None
    assert len(finished.body["codes"]) == 10
    assert sessions.lookup(ledger, principal["id_hash"], clock[0]) is None
    assert ledger.db.execute("SELECT credential_id FROM passkeys").fetchone()[0] == b"replacement"
    assert ledger.db.execute("SELECT password_hash,totp_version FROM users WHERE id=?", (ids["first"],)).fetchone()[
        :
    ] == (None, None)
    assert all(not recovery_codes.consume(ledger, ids["first"], code, clock[0]) for code in codes)
    old = service.begin_signin(ledger)
    assert service.finish_signin(ledger, old.body["ticket"], assertion_body(old, old_sign, handle)).status == 401
    fresh = service.begin_signin(ledger)
    wrong = assertion_body(fresh, old_sign, handle, ident=b"replacement")
    assert service.finish_signin(ledger, fresh.body["ticket"], wrong).status == 401
    valid = assertion_body(fresh, new_sign, handle, ident=b"replacement")
    assert service.finish_signin(ledger, fresh.body["ticket"], valid).status == 200


@pytest.mark.parametrize(
    "denial",
    [
        "unknown",
        "surrogate",
        "wrong",
        "reused",
        "no_key",
        "disabled",
        "locked",
        "delayed",
        "presented",
    ],
)
def test_code_only_recovery_refusals_do_not_consume_an_unproved_code(ledger, passkey_account, denial):
    service, clock, ids, _, _, principal = passkey_account
    codes = recovery_codes.generate(ledger, ids["first"], clock[0])
    username, code = "first", codes[0]
    if denial in ("unknown", "surrogate"):
        username = "missing" if denial == "unknown" else "\ud800"
    if denial == "wrong":
        code = "invalid"
    if denial == "reused":
        assert recover(ledger, clock, username, code).status == 200
    if denial == "no_key":
        with ledger.tx() as db:
            db.execute("DELETE FROM passkeys WHERE user_id=?", (ids["first"],))
    if denial == "disabled":
        ledger.change_user(ids["first"], None, sessions.stamp(clock[0]), enabled=False)
    if denial in ("locked", "delayed"):
        with ledger.tx() as db:
            column = "locked_until" if denial == "locked" else "retry_at"
            db.execute(
                f"INSERT INTO auth_limits(key_hash,user_id,failures,since,{column}) VALUES(?,?,5,?,?)",
                (
                    limits.key(username),
                    ids["first"],
                    sessions.stamp(clock[0]),
                    sessions.stamp(clock[0] + timedelta(hours=1)),
                ),
            )
    before = ledger.db.execute("SELECT count(*) FROM sessions").fetchone()[0]
    result = recover(ledger, clock, username, code, presented=principal if denial == "presented" else None)
    assert result.status == (429 if denial == "delayed" else 401) and result.session_id is None
    assert ledger.db.execute("SELECT count(*) FROM sessions").fetchone()[0] == before
    used = ledger.db.execute(
        "SELECT used_at FROM recovery_codes WHERE code_hash=?", (sessions.digest(codes[0].replace("-", "")),)
    ).fetchone()[0]
    assert bool(used) == (denial == "reused")


def test_code_only_recovery_entry_final_failure_restores_code_limits_and_sessions(ledger, passkey_account, monkeypatch):
    service, clock, ids, _, _, principal = passkey_account
    codes = recovery_codes.generate(ledger, ids["first"], clock[0])
    limits.failure(ledger, "first", clock[0], reason="recovery")

    def fault(*args):
        raise ValueError("synthetic recovery-session audit failure")

    with monkeypatch.context() as patch, pytest.raises(ValueError):
        patch.setattr(ledger, "record_signin", fault)
        recover(ledger, clock, "first", codes[0])
    assert sessions.lookup(ledger, principal["id_hash"], clock[0])
    assert ledger.db.execute("SELECT failures FROM users WHERE id=?", (ids["first"],)).fetchone()[0] == 1
    assert ledger.db.execute("SELECT count(*) FROM recovery_codes WHERE used_at IS NOT NULL").fetchone()[0] == 0
    assert ledger.db.execute("SELECT count(*) FROM sessions").fetchone()[0] == 1


def test_passkey_recovery_final_failure_restores_lost_keys_codes_and_restricted_session(
    ledger, passkey_account, monkeypatch
):
    service, clock, ids, _, _, _ = passkey_account
    codes = recovery_codes.generate(ledger, ids["first"], clock[0])
    recovered = recover(ledger, clock, "first", codes[0])
    principal = sessions.identify(ledger, recovered.session_id, clock[0])
    options = service.begin_recovery(ledger, principal, "Înlocuire")
    new_values, _ = software_authenticator(ledger.db.execute("SELECT algorithm FROM passkeys").fetchone()[0])
    body = registration_body(options, new_values, b"replacement")
    original = ledger.replace_recovery_codes
    before = [tuple(r) for r in ledger.db.execute("SELECT * FROM recovery_codes ORDER BY code_hash")]

    def fault(*args):
        original(*args)
        raise ValueError("synthetic final recovery-code publication failure")

    with monkeypatch.context() as patch, pytest.raises(ValueError):
        patch.setattr(ledger, "replace_recovery_codes", fault)
        service.finish_recovery(ledger, principal, options.body["ticket"], body)
    assert sessions.lookup(ledger, principal["id_hash"], clock[0])
    assert ledger.db.execute("SELECT credential_id FROM passkeys").fetchone()[0] == IDENT
    assert [tuple(r) for r in ledger.db.execute("SELECT * FROM recovery_codes ORDER BY code_hash")] == before
    assert ledger.db.execute("SELECT attempts,used_at FROM auth_tickets WHERE purpose='passkey_recovery'").fetchone()[
        :
    ] == (0, None)
    assert service.finish_recovery(ledger, principal, options.body["ticket"], body).status == 200


@pytest.mark.parametrize("denial", ["normal", "password_recovery", "other_user", "other_session", "revoked", "purpose"])
def test_passkey_recovery_rejects_swapped_session_owner_and_proof_kind(ledger, passkey_account, denial):
    service, clock, ids, _, _, _ = passkey_account
    code = recovery_codes.generate(ledger, ids["first"], clock[0])[0]
    recovered = recover(ledger, clock, "first", code)
    principal = sessions.identify(ledger, recovered.session_id, clock[0])
    options = service.begin_recovery(ledger, principal, "Înlocuire")
    values, _ = software_authenticator(ledger.db.execute("SELECT algorithm FROM passkeys").fetchone()[0])
    body, token = registration_body(options, values, b"replacement"), options.body["ticket"]
    if denial in ("normal", "password_recovery", "other_user", "other_session"):
        raw = sessions.issue(
            ledger,
            ids["second" if denial == "other_user" else "first"],
            clock[0],
            recovery=denial != "normal",
            method="password_totp" if denial == "password_recovery" else "passkey",
        )
        principal = sessions.identify(ledger, raw, clock[0])
    if denial == "revoked":
        other = Ledger(ledger.path)
        try:
            other.revoke_session(principal["id_hash"], ids["first"], sessions.stamp(clock[0]))
        finally:
            other.close()
    if denial == "purpose":
        proof = ledger.db.execute("SELECT challenge FROM auth_tickets WHERE purpose='passkey_recovery'").fetchone()[0]
        token = tickets.issue(
            ledger, ids["first"], "passkey_add", clock[0], session_hash=principal["id_hash"], challenge=proof
        )
    assert service.finish_recovery(ledger, principal, token, body).status == 401
    if denial in ("normal", "password_recovery", "revoked"):
        assert service.begin_recovery(ledger, principal, "Altă cheie").status == 401
    assert ledger.db.execute("SELECT credential_id FROM passkeys").fetchone()[0] == IDENT
    assert not ledger.db.execute("SELECT 1 FROM auth_events WHERE kind='recovery_completed'").fetchone()


def test_code_only_recovery_two_connections_consume_the_code_once(ledger, passkey_account):
    service, clock, ids, _, _, _ = passkey_account
    code = recovery_codes.generate(ledger, ids["first"], clock[0])[0]
    barrier = Barrier(2)

    def attempt(_):
        other = Ledger(ledger.path)
        try:
            barrier.wait(timeout=5)
            return recover(other, clock, "first", code).status
        finally:
            other.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(attempt, range(2))) == [200, 401]
    assert ledger.db.execute("SELECT count(*) FROM recovery_codes WHERE used_at IS NOT NULL").fetchone()[0] == 1
    assert ledger.db.execute("SELECT count(*) FROM sessions WHERE revoked_at IS NULL").fetchone()[0] == 1


def test_recovery_cannot_remove_last_factor_without_a_verified_owned_replacement(ledger, passkey_account):
    _, clock, ids, _, _, _ = passkey_account
    original = dict(ledger.db.execute("SELECT * FROM passkeys").fetchone())
    assert ledger.add_passkey(original | {"credential_id": b"foreign", "user_id": ids["second"]})
    for replacement in (None, b"missing", b"foreign"):
        with pytest.raises(PermissionError):
            ledger.finish_recovery(ids["first"], sessions.stamp(clock[0]), keep_passkey=replacement)
    assert dict(ledger.db.execute("SELECT * FROM passkeys WHERE credential_id=?", (IDENT,)).fetchone()) == original


@pytest.fixture
def http_keys(server, ledger, authenticator):
    clock = [datetime(2026, 11, 4, tzinfo=UTC)]
    ids = {
        name: account_users.create(ledger, name, name, "admin", clock[0], actor_id=None) for name in ("first", "second")
    }
    link = setup_links.issue(ledger, ids["first"], clock[0], actor_id=None).partition("#")[2]
    service = passkeys.Passkeys(lambda: clock[0], ORIGIN)
    server.origin = ORIGIN
    server.service.boundary = transport.Boundary(ORIGIN, trust_proxy=True)
    server.service.clock = service.clock
    engine = server.service.browser_auth
    engine.clock = engine.signin.clock = engine.enrolment.clock = service.clock
    engine.passkeys = service
    return server, (service, clock, link, ids, authenticator[0])


def http_setup(ledger, http_keys):
    server, (service, clock, link, ids, values) = http_keys
    status, _, data = auth_request(server, "/auth/setup/passkey/options", {"link": link, "name": "Cheie"})
    assert status == 200
    body = registration_body(signin_result(data), values)
    status, headers, result = auth_request(
        server, "/auth/setup/passkey/finish", {"link": link, "ticket": data["ticket"], **body}
    )
    assert status == 200
    return headers["set-cookie"].split(";", 1)[0], result


def signin_result(body):
    return signin.Result(200, body)


@every_algorithm
def test_http_passkey_signup_signin_stepup_and_owned_controls(ledger, http_keys, authenticator):
    server, (_, clock, _, ids, values) = http_keys
    cookie, result = http_setup(ledger, http_keys)
    assert len(result["codes"]) == 10
    identity = _get_json(server, "/web/api/me", Cookie=cookie)[1]
    assert identity["auth"]["method"] == "passkey" and identity["factors"]["passkeys_available"]
    assert not identity["factors"]["password_totp"]
    assert set(identity["factors"]["passkeys"][0]) == {
        "id",
        "name",
        "backup_eligible",
        "backed_up",
        "created_at",
        "last_used_at",
    }
    listed = _get_json(server, "/web/api/users", Cookie=cookie)[1]
    assert next(r for r in listed if r["id"] == ids["first"])["passkeys"] == 1
    assert auth_request(server, "/auth/signout", {}, Cookie=cookie)[0] == 200
    status, _, options = auth_request(server, "/auth/passkey/options", {})
    assert status == 200 and "user" not in options["publicKey"]
    handle = ledger.db.execute("SELECT webauthn_handle FROM users WHERE id=?", (ids["first"],)).fetchone()[0]
    body = assertion_body(signin_result(options), authenticator[1], handle)
    status, headers, _ = auth_request(server, "/auth/passkey/finish", {"ticket": options["ticket"], **body})
    assert status == 200 and "Path=/; Secure; HttpOnly; SameSite=Strict" in headers["set-cookie"]
    cookie = headers["set-cookie"].split(";", 1)[0]
    clock[0] += timedelta(minutes=11)
    assert auth_request(
        server, "/auth/me/passkey/rename", {"credential_id": webauthn.b64(IDENT), "name": "Nou"}, Cookie=cookie
    )[2]["reauth"]
    options = auth_request(server, "/auth/step-up/passkey/options", {}, Cookie=cookie)[2]
    body = assertion_body(signin_result(options), authenticator[1], handle, count=2)
    status, headers, _ = auth_request(
        server, "/auth/step-up/passkey/finish", {"ticket": options["ticket"], **body}, Cookie=cookie
    )
    assert status == 200 and _get_json(server, "/web/api/me", Cookie=cookie)[0] == 401
    cookie = headers["set-cookie"].split(";", 1)[0]
    options = auth_request(server, "/auth/me/passkey/options", {"name": "Rezervă"}, Cookie=cookie)[2]
    extra, _ = software_authenticator(values[3])
    body = registration_body(signin_result(options), extra, b"extra")
    assert (
        auth_request(server, "/auth/me/passkey/finish", {"ticket": options["ticket"], **body}, Cookie=cookie)[0] == 200
    )
    other = "__Host-session=" + sessions.issue(ledger, ids["second"], clock[0])
    rename = {"credential_id": webauthn.b64(b"extra"), "name": "Altă cheie"}
    assert auth_request(server, "/auth/me/passkey/rename", rename, Cookie=other)[0] == 404
    assert auth_request(server, "/auth/me/passkey/rename", rename, Cookie=cookie)[0] == 200
    assert (
        auth_request(server, "/auth/me/passkey/remove", {"credential_id": webauthn.b64(IDENT)}, Cookie=cookie)[0] == 200
    )
    assert (
        auth_request(server, "/auth/me/passkey/remove", {"credential_id": webauthn.b64(b"extra")}, Cookie=cookie)[0]
        == 400
    )


@every_algorithm
def test_http_passkey_recovery_stays_restricted_and_requires_new_signin(ledger, http_keys, authenticator):
    server, (_, clock, _, ids, values) = http_keys
    cookie, result = http_setup(ledger, http_keys)
    request = {"username": "first", "code": result["codes"][0]}
    assert auth_request(server, "/auth/recover/code", request, Cookie=cookie)[0] == 401
    status, headers, _ = auth_request(server, "/auth/recover/code", request)
    assert status == 200
    restricted = headers["set-cookie"].split(";", 1)[0]
    identity = _get_json(server, "/web/api/me", Cookie=restricted)[1]
    assert identity["auth"]["method"] == "recovery_code" and identity["auth"]["recovery_only"]
    assert _get_json(server, "/web/api/state?co=NORD", Cookie=restricted)[0] == 403
    assert auth_request(server, "/auth/me/passkey/options", {"name": "Interzis"}, Cookie=restricted)[0] == 401
    assert auth_request(server, "/auth/step-up/passkey/options", {}, Cookie=restricted)[0] == 401
    assert auth_request(server, "/auth/recover/begin", {}, Cookie=restricted)[0] == 401
    options = auth_request(server, "/auth/recover/passkey/options", {"name": "Înlocuire"}, Cookie=restricted)[2]
    replacement, sign = software_authenticator(values[3])
    body = registration_body(signin_result(options), replacement, b"replacement")
    status, headers, done = auth_request(
        server, "/auth/recover/passkey/finish", {"ticket": options["ticket"], **body}, Cookie=restricted
    )
    assert status == 200 and "Max-Age=0" in headers["set-cookie"] and len(done["codes"]) == 10
    assert _get_json(server, "/web/api/me", Cookie=restricted)[0] == 401
    options = auth_request(server, "/auth/passkey/options", {})[2]
    handle = ledger.db.execute("SELECT webauthn_handle FROM users WHERE id=?", (ids["first"],)).fetchone()[0]
    body = assertion_body(signin_result(options), sign, handle, ident=b"replacement")
    assert auth_request(server, "/auth/passkey/finish", {"ticket": options["ticket"], **body})[0] == 200


def test_every_passkey_endpoint_refuses_csrf_plain_http_and_owner_override(ledger, http_keys):
    server, (_, clock, _, ids, _) = http_keys
    normal = "__Host-session=" + sessions.issue(ledger, ids["first"], clock[0])
    recovery = "__Host-session=" + sessions.issue(ledger, ids["first"], clock[0], recovery=True, method="passkey")
    for route in login.ENDPOINTS:
        if not route.passkey:
            continue
        body = {key: "synthetic" for key in route.fields.split()}
        cookie = recovery if route.session == "recovery" else normal
        for headers, expected in (
            ({"Origin": "https://other.example"}, 403),
            ({"X-Forwarded-Proto": None}, 403),
            ({"Sec-Fetch-Site": "cross-site"}, 403),
            ({"Host": "other.example"}, 404),
        ):
            status, _, _ = _req(
                server,
                "POST",
                route.path,
                json.dumps(body).encode(),
                {"Content-Type": "application/json", "Cookie": cookie, **headers},
            )
            assert status == expected
        assert auth_request(server, route.path, body | {"user_id": ids["second"]}, Cookie=cookie)[0] == 400
        if route.session != "public":
            assert auth_request(server, route.path, body)[0] == 401
    assert not ledger.db.execute("SELECT 1 FROM auth_tickets").fetchone()
    assert not ledger.db.execute("SELECT 1 FROM passkeys").fetchone()


def test_https_ip_install_refuses_passkeys_without_weakening_cookie_transport(ledger, http_keys):
    server, (_, clock, _, _, _) = http_keys
    server.origin = "https://127.0.0.1"
    server.service.boundary = transport.Boundary(server.origin, trust_proxy=True)
    server.service.browser_auth = login.BrowserAuth(lambda: clock[0], server.origin)
    assert server.service.browser_auth.passkeys is None
    assert auth_request(server, "/auth/passkey/options", {})[0] == 400
    assert auth_request(server, "/auth/passkey/options", {}, **{"X-Forwarded-Proto": None})[0] == 403
    assert not ledger.db.execute("SELECT 1 FROM auth_tickets").fetchone()

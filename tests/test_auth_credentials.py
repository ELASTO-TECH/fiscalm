"""Synthetic password and second-factor primitives, with real scrypt and SQLite."""

import base64
import random
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress

import pytest

from fiscalm.api import cbor, passwords, totp
from fiscalm.ledger import Ledger


def test_password_roundtrip_normalization_and_old_cost(monkeypatch):
    service = passwords.Passwords()
    encoded = service.hash("example-password-Ａ")
    assert service.verify("example-password-A", encoded) == (True, False)
    assert service.verify("wrong-password", encoded) == (False, False)
    salt = b"s" * 16
    old = service._derive("example-password-A", salt, 14)
    encoded = "scrypt$14$8$1$" + "$".join(base64.b64encode(v).decode() for v in (salt, old))
    assert service.verify("example-password-A", encoded) == (True, True)
    calls = []
    monkeypatch.setattr(service, "_derive", lambda *args: calls.append(args) or b"\x00" * 32)
    for stored in (None, "bad", "scrypt$99$8$1$a$b"):
        assert service.verify("example-password-A", stored) == (False, False)
    assert len(calls) == 3 and all(args[2] == 17 for args in calls)


@pytest.mark.parametrize("password", ["a" * 11, "a" * 129, "a" * 12 + "\udcff"])
def test_password_length_refused(password):
    with pytest.raises(ValueError):
        passwords.normalize(password)


def test_password_concurrency_cap_releases_slots(monkeypatch):
    service = passwords.Passwords()

    class Busy:
        def acquire(self, *, timeout):
            assert timeout == 5
            return False

    monkeypatch.setattr(service, "slots", Busy())
    with pytest.raises(passwords.PasswordBusy):
        service.hash("synthetic-password")
    monkeypatch.setattr(service, "slots", threading.BoundedSemaphore(2))

    def fail(*args, **kwargs):
        raise ValueError("synthetic failure")

    monkeypatch.setattr(passwords.hashlib, "scrypt", fail)
    with pytest.raises(ValueError):
        service.hash("synthetic-password")
    assert service.slots.acquire(blocking=False)
    assert service.slots.acquire(blocking=False)
    assert not service.slots.acquire(blocking=False)


@pytest.mark.parametrize(
    "timestamp,expected",
    [
        (59, "287082"),
        (1111111109, "081804"),
        (1111111111, "050471"),
        (1234567890, "005924"),
        (2000000000, "279037"),
        (20000000000, "353130"),
    ],
)
def test_rfc6238_vectors_and_window(timestamp, expected):
    seed = b"12345678901234567890"
    step = timestamp // 30
    assert totp.code(seed, step) == expected
    assert totp.match(seed, expected, timestamp) == step
    assert totp.match(seed, expected, (step - 1) * 30) == step
    assert totp.match(seed, expected, (step + 1) * 30) == step
    assert totp.match(seed, expected, (step + 2) * 30) is None
    assert totp.match(seed, "１２３４５６", timestamp) is None


def test_totp_replay_reset_disable_and_race(ledger):
    ledger.db.execute("""INSERT INTO users(id,username,display_name,role,webauthn_handle,created_at,totp_version)
        VALUES ('synthetic','synthetic','Synthetic','admin',X'01','2026-11-04','v1')""")
    ledger.db.commit()
    barrier = threading.Barrier(2)

    def consume(_):
        other = Ledger(ledger.path)
        try:
            barrier.wait(timeout=5)
            return other.consume_totp("synthetic", "v1", 100)
        finally:
            other.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(consume, range(2))) == [False, True]
    for version, step in [("v1", 100), ("v1", 99), ("old", 101)]:
        assert not ledger.consume_totp("synthetic", version, step)
    assert ledger.consume_totp("synthetic", "v1", 101)
    ledger.db.execute("UPDATE users SET disabled_at='2026-11-04'")
    ledger.db.commit()
    assert not ledger.consume_totp("synthetic", "v1", 102)


@pytest.mark.parametrize(
    "encoded,expected",
    [
        ("00", 0),
        ("1818", 24),
        ("190100", 256),
        ("1a00010000", 65536),
        ("1bffffffffffffffff", 2**64 - 1),
        ("20", -1),
        ("38ff", -256),
        ("40", b""),
        ("43010203", b"\x01\x02\x03"),
        ("60", ""),
        ("64c89bc483", "ță"),
        ("80", []),
        ("830141006161", [1, b"\x00", "a"]),
        ("a0", {}),
        ("a20120617881f5", {1: -1, "x": [True]}),
        ("a1410001", {b"\x00": 1}),
        ("f4", False),
        ("f5", True),
        ("f6", None),
    ],
)
def test_cbor_supported_values_and_embedded_prefix(encoded, expected):
    data = bytes.fromhex(encoded)
    assert cbor.decode(data) == expected
    assert cbor.decode_prefix(data + b"\x00") == (expected, len(data))
    with pytest.raises(cbor.CBORError):
        cbor.decode(data + b"\x00")


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"\x18",
        b"\x19\x00",
        b"\x1a\x00",
        b"\x1b\x00",
        b"\x43\x01",
        b"\x82\x00",
        b"\xa1\x00",
        b"\xc0\x00",
        b"\x5f\xff",
        b"\x7f\xff",
        b"\x9f\xff",
        b"\xbf\xff",
        b"\xff",
        b"\xf7",
        b"\xf8\x14",
        b"\xf9\x00\x00",
        b"\xfa\x00\x00\x00\x00",
        b"\xfb" + b"\x00" * 8,
        b"\x1c",
        b"\x1d",
        b"\x1e",
        b"\x61\xff",
        b"\xa2\x00\x01\x18\x00\x02",
        b"\xa1\x80\x00",
        b"\xa1\xa0\x00",
        b"\xa1\xf5\x00",
        b"\xa1\xf6\x00",
        b"\x81" * 9 + b"\x00",
        b"\x98\x81" + b"\x00" * 129,
        b"\x84" + (b"\x98\x80" + b"\x00" * 128) * 4,
        b"\x00" * (cbor.MAX_BYTES + 1),
        None,
        "00",
    ],
)
def test_cbor_refuses_unsupported_invalid_or_excessive_input(data):
    with pytest.raises(cbor.CBORError):
        cbor.decode(data)


def test_cbor_depth_boundary_and_deterministic_fuzz_only_raise_own_refusal():
    nested = cbor.decode(b"\x81" * 8 + b"\x00")
    for _ in range(8):
        [nested] = nested
    assert nested == 0
    source = random.Random(20261105)
    samples = [source.randbytes(source.randrange(256)) for _ in range(3000)]
    seed = bytes.fromhex("a20120617881f5")
    samples += [seed[:i] + bytes([value]) + seed[i + 1 :] for i in range(len(seed)) for value in range(256)]
    for data in samples:
        for decode in (cbor.decode, cbor.decode_prefix):
            with suppress(cbor.CBORError):
                decode(data)

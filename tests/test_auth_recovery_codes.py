"""One-time recovery code rotation, ownership and concurrent consumption."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from test_auth_users import accounts as accounts  # noqa: F401

from fiscalm.api import recovery_codes, sessions
from fiscalm.ledger import Ledger


def test_recovery_codes_display_hashes_regeneration_and_audit(ledger, accounts):
    ids, at = accounts
    codes = recovery_codes.generate(ledger, ids["first"], at)
    assert len(codes) == len(set(codes)) == 10
    assert all(len(v) == 19 and all(len(group) == 4 for group in v.split("-")) for v in codes)
    rows = [dict(r) for r in ledger.db.execute("SELECT * FROM recovery_codes")]
    assert {r["code_hash"] for r in rows} == {sessions.digest(v.replace("-", "")) for v in codes}
    assert not any(v.replace("-", "") in str(rows) for v in codes)
    assert not recovery_codes.consume(ledger, ids["second"], codes[0], at)
    for malformed in ("wrong", "A" * 15, "A" * 17, "１" * 16):
        assert not recovery_codes.consume(ledger, ids["first"], malformed, at)
    assert recovery_codes.consume(ledger, ids["first"], codes[0].lower().replace("-", " "), at)
    assert not recovery_codes.consume(ledger, ids["first"], codes[0], at)
    fresh = recovery_codes.generate(ledger, ids["first"], at)
    assert all(not recovery_codes.consume(ledger, ids["first"], code, at) for code in codes)
    assert recovery_codes.consume(ledger, ids["first"], fresh[0], at)
    events = [dict(r) for r in ledger.db.execute("SELECT kind,actor,detail FROM auth_events")]
    assert [r["kind"] for r in events][-4:] == ["recovery_codes_regenerated", "recovery_code_used"] * 2
    assert not any(v in str(events) or v.replace("-", "") in str(events) for v in codes + fresh)


def test_recovery_code_race_and_outer_rollback(ledger, accounts):
    ids, at = accounts
    code = recovery_codes.generate(ledger, ids["first"], at)[0]
    with pytest.raises(ValueError), ledger.tx():
        assert recovery_codes.consume(ledger, ids["first"], code, at)
        raise ValueError("synthetic recovery-session failure")
    barrier = Barrier(2)

    def consume(_):
        other = Ledger(ledger.path)
        try:
            barrier.wait(timeout=5)
            return recovery_codes.consume(other, ids["first"], code, at)
        finally:
            other.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(consume, range(2))) == [False, True]


def test_recovery_refuses_disabled_missing_and_factorless_users(ledger, accounts):
    ids, at = accounts
    code = recovery_codes.generate(ledger, ids["second"], at)[0]
    ledger.change_user(ids["second"], None, sessions.stamp(at), enabled=False)
    assert not recovery_codes.consume(ledger, ids["second"], code, at)
    ledger.db.execute("UPDATE users SET totp_version=NULL WHERE id=?", (ids["member"],))
    ledger.db.commit()
    for user_id in (ids["second"], ids["member"], "missing"):
        with pytest.raises(PermissionError):
            recovery_codes.generate(ledger, user_id, at)
    with pytest.raises(ValueError):
        ledger.replace_recovery_codes(ids["first"], [], sessions.stamp(at))

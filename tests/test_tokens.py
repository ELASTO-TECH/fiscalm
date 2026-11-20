import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from fiscalm import cli
from fiscalm.api import tokens
from fiscalm.api.responses import Error
from fiscalm.ledger import Ledger


def test_token_lifecycle(token_owner, ledger, capsys):
    assert cli.main(["token", "create", "--user", "token-owner", "--name", "Example script", "--scope", "write"]) == 0
    made = json.loads(capsys.readouterr().out)
    secret = made["token"]
    assert len(secret.rsplit("_", 1)[1]) == 64
    assert tokens.verify(ledger, secret, "write")
    assert tokens.list_tokens(ledger)[0]["last_used_at"]
    assert secret not in str(tokens.list_tokens(ledger))
    assert secret not in str(list(map(tuple, ledger.db.execute("SELECT * FROM api_tokens"))))
    assert cli.main(["token", "list"]) == 0
    assert secret not in capsys.readouterr().out
    assert cli.main(["token", "revoke", made["id"]]) == 0
    assert not tokens.revoke(ledger, made["id"])
    assert tokens.verify(ledger, secret) is None


def test_wrong_scope_and_constant_time_compare(token_owner, ledger, monkeypatch):
    made = tokens.create(ledger, "Reader", "read", user_id=token_owner)
    assert tokens.verify(ledger, made["token"])
    assert tokens.verify(ledger, made["token"], "write") is None
    original = tokens.hmac.compare_digest
    calls = []
    monkeypatch.setattr(tokens.hmac, "compare_digest", lambda a, b: calls.append((a, b)) or original(a, b))
    for bad in ("wrong", made["token"][:-1] + "x", "fiscalm_" + "f" * 16 + made["token"][24:]):
        assert tokens.verify(ledger, bad) is None
    assert len(calls) == 3 and all(len(a) == len(b) == 64 for a, b in calls)


def test_usage_is_throttled_and_busy_writer_does_not_break_auth(token_owner, ledger):
    made = tokens.create(ledger, "Busy reader", "read", user_id=token_owner)
    other = sqlite3.connect(ledger.path)
    try:
        other.execute("BEGIN IMMEDIATE")
        assert tokens.verify(ledger, made["token"])
        other.rollback()
        assert tokens.verify(ledger, made["token"])
        changes = ledger.db.total_changes
        assert tokens.verify(ledger, made["token"])
        assert ledger.db.total_changes == changes
    finally:
        other.close()


def test_revocation_between_read_and_guarded_update_refuses(token_owner, ledger, monkeypatch):
    made = tokens.create(ledger, "Racing revocation", "read", user_id=token_owner)
    original = tokens.hmac.compare_digest

    def compare(a, b):
        equal = original(a, b)
        tokens.revoke(ledger, made["id"])
        return equal

    monkeypatch.setattr(tokens.hmac, "compare_digest", compare)
    assert tokens.verify(ledger, made["token"]) is None


def test_concurrent_token_revocation(token_owner, ledger):
    made = tokens.create(ledger, "Concurrent", "read", user_id=token_owner)
    barrier = Barrier(2)

    def revoke():
        other = Ledger(ledger.path)
        try:
            barrier.wait(timeout=10)
            return tokens.revoke(other, made["id"])
        finally:
            other.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: revoke(), range(2)))
    assert sorted(results) == [False, True]


@pytest.mark.parametrize("change,status", [("disabled", 401), ("readonly", 403), ("revoked", 401), ("swapped", 401)])
def test_token_authority_rechecks_owner_on_the_writer_connection(token_owner, ledger, change, status):
    made = tokens.create(ledger, "Writer", "write", user_id=token_owner)
    credential = tokens.verify(ledger, made["token"])
    other = Ledger(ledger.path)
    try:
        with other.tx() as db:
            if change == "disabled":
                db.execute("UPDATE users SET disabled_at='2026-11-04' WHERE id=?", (token_owner,))
            elif change == "readonly":
                db.execute("UPDATE users SET role='readonly' WHERE id=?", (token_owner,))
            elif change == "revoked":
                tokens.revoke(other, made["id"])
            else:
                credential = credential | {"user_id": "another-user"}
        with ledger.tx(), pytest.raises(Error) as refused:
            tokens.authorize(ledger, credential)
        assert refused.value.status == status
        current = tokens.verify(ledger, made["token"])
        assert (current is None) == (change in ("disabled", "revoked"))
        if change == "readonly":
            assert current["scope"] == "read" and tokens.verify(ledger, made["token"], "write") is None
            assert tokens.list_tokens(ledger)[0]["scope"] == "write"
    finally:
        other.close()


def test_token_creation_refuses_unknown_disabled_and_excessive_scope(token_owner, ledger, capsys):
    for owner, state, scope in [
        ("missing", None, "read"),
        (token_owner, "disabled", "read"),
        (token_owner, "readonly", "write"),
    ]:
        with ledger.tx() as db:
            db.execute(
                "UPDATE users SET disabled_at=?,role=? WHERE id=?",
                (
                    "2026-11-04" if state == "disabled" else None,
                    "readonly" if state == "readonly" else "member",
                    token_owner,
                ),
            )
        with pytest.raises(ValueError):
            tokens.create(ledger, "Refused", scope, user_id=owner)
        assert (
            cli.main(
                [
                    "token",
                    "create",
                    "--user",
                    "missing" if owner == "missing" else "token-owner",
                    "--name",
                    "Refused",
                    "--scope",
                    scope,
                ]
            )
            == 1
        )
        assert capsys.readouterr().out == ""
    assert tokens.list_tokens(ledger) == []
    assert ledger.db.execute("SELECT count(*) FROM auth_events WHERE kind LIKE 'api_token_%'").fetchone()[0] == 0
    made = tokens.create(ledger, "Reader", "read", user_id=token_owner)
    assert tokens.list_tokens(ledger)[0]["owner"] == {"id": token_owner, "display_name": "Token owner"}
    assert tokens.revoke(ledger, made["id"])
    assert not tokens.revoke(ledger, made["id"])
    assert [r[0] for r in ledger.db.execute("SELECT kind FROM auth_events")] == [
        "api_token_created",
        "api_token_revoked",
    ]
    assert made["token"] not in str(list(map(tuple, ledger.db.execute("SELECT detail FROM auth_events"))))

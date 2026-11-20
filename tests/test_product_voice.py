"""Product voice: the ledger, the mail draft and the live switch name no person or machine. Synthetic data only."""

from fiscalm import ledger as ledger_mod


def test_only_the_neutral_marker_is_the_users_word(ledger):
    """A note that starts with a name is just a note."""
    assert ledger_mod.user_note("user: already paid") and ledger_mod.user_words("user: already paid") == "already paid"
    assert (
        not ledger_mod.user_note("Ana: already paid")
        and ledger_mod.user_words("Ana: already paid") == "Ana: already paid"
    )

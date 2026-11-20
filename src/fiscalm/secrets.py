"""Secrets from 0600 files outside any repo. Values are returned in memory only; never logged.

Directory: $FISCALM_SECRETS, else ${XDG_CONFIG_HOME:-~/.config}/fiscalm/secrets (fiscalm.paths). One file per secret, written by fiscalm from the write-only
fields of Setări, Integrări (an integration's secret, an OAuth connection's tokens); nobody types them into a chat or a
command line.
"""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

from fiscalm.paths import secrets_dir


class SecretError(RuntimeError):
    pass


def _name_ok(name: str) -> None:
    if not name or "/" in name or "\x00" in name or name in (".", ".."):
        raise SecretError("invalid secret name")


def read_secret(name: str) -> str:
    """Return the secret stored in <secrets_dir>/<name>. The file is opened without following a symlink and checked on
    the open descriptor: a regular file, owned by this user, readable by nobody else. Anything else is refused."""
    _name_ok(name)
    p = secrets_dir() / name
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:  # fail closed: without it a symlink could point the read anywhere
        raise SecretError("this system can't open secret files without following links (no O_NOFOLLOW)")
    try:
        fd = os.open(p, os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0))
    except FileNotFoundError:
        raise SecretError(f"secret {name!r} not found in {secrets_dir()}") from None
    except OSError:  # ELOOP: a symlink
        raise SecretError(f"secret {name!r} is not a regular file") from None
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
            raise SecretError(f"secret {name!r} is not a regular file of this user")
        if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise SecretError(f"secret {name!r} is readable by group/others; chmod 600 {p}")
        value = f.read().decode("utf-8").strip("\r\n")
    if not value:
        raise SecretError(f"secret {name!r} is empty")
    return value


def statement_password(company_key: str, bank: str) -> str:
    """The company's statement password at `bank` (a catalogue id), set in Setări › Firme › Bănci
    (`integrations.statement_instance`). There is no other place a password is read from: without one, SecretError says
    where to add it."""
    from fiscalm import integrations
    from fiscalm.ledger import Ledger

    L = Ledger()
    try:
        found = integrations.statement_instance(L, company_key, bank)
    finally:
        L.close()
    if not found:
        raise SecretError(f"no statement password for {company_key} at {bank}: add it in Setări, Firme, Bănci")
    return found.secret("statement_password")


def bt_statement_password(company_key: str) -> str:
    """The password the BT PDF statement reader tries for a company."""
    return statement_password(company_key, "bt")


def has_secret(name: str) -> bool:
    _name_ok(name)
    return (secrets_dir() / name).is_file()


def write_secret(name: str, value: str) -> None:
    """Atomically (re)write <secrets_dir>/<name> as 0600. Used for tokens fiscalm obtains itself."""
    _name_ok(name)
    d = secrets_dir()
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    st = d.lstat()
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise SecretError("the secrets directory is not a directory of this user")
    if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        os.chmod(d, 0o700)  # an old, too open directory is closed before a secret goes in
    fd, tmp = tempfile.mkstemp(dir=d, prefix=f".{name}.")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(value)
        os.replace(tmp, d / name)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def has_statement_password(company_key: str) -> bool:
    """Whether a credential reference exists; absence does not mean a statement is foreign."""
    from fiscalm import integrations
    from fiscalm.ledger import Ledger

    L = Ledger()
    try:
        return any(
            i.config.get("company") == company_key and "statement_password" in i.versions
            for i in integrations.enabled(L, "bank")
        )
    finally:
        L.close()

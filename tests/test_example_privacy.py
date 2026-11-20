"""Keep private company names out of tracked examples, including filenames."""

from __future__ import annotations

import re
import subprocess
from hashlib import sha256
from pathlib import Path

# SHA-256 of lowercased forbidden terms; never print matching text in failures.
FORBIDDEN = {
    6: "97c110614720639d1bc8dc1d90671f5c71a8da816c2a86107a145fd3e9edc147",
    4: "8b571af9c7996aefede790cb77ae0a64ac1a7af9080de74c9b73471e85297fc5",
}


def contains_forbidden(data: bytes, forbidden: dict[int, str]) -> bool:
    tokens = set(re.findall(rb"[a-z0-9]+", data.lower()))  # a repeated token or window is hashed once
    for size, digest in forbidden.items():
        windows = {token[i : i + size] for token in tokens for i in range(len(token) - size + 1)}
        if any(sha256(window).hexdigest() == digest for window in windows):
            return True
    return False


def tracked_violations(root: Path, forbidden: dict[int, str]) -> list[str]:
    names = subprocess.check_output(["git", "ls-files", "-z"], cwd=root).split(b"\0")
    hits = []
    for name in filter(None, names):
        path = root / name.decode()  # a file deleted in the working tree has no content left to check
        content = path.read_bytes() if path.exists() else b""
        # The public repository's owner is part of its address; examples still refuse the name anywhere else.
        content = content.replace(bytes.fromhex("454c4153544f2d544543482f66697363616c6d"), b"")
        # The approved public CLA names its legal counterparty; examples still refuse it.
        if name == b"CLA.md":
            content = content.replace(bytes.fromhex("454c4153544f20544543484e4f4c4f474945532053524c"), b"")
        if contains_forbidden(name, forbidden) or contains_forbidden(content, forbidden):
            hits.append(name.decode())
    return hits


def test_tracked_examples_use_invented_companies() -> None:
    root = Path(__file__).resolve().parents[1]
    assert not tracked_violations(root, FORBIDDEN), "Private company name in tracked files"


def test_guard_checks_mixed_case_embedded_names_and_binary_content(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    forbidden = {11: sha256(b"fixturecorp").hexdigest()}
    (tmp_path / "photo_FixtureCorp_receipt.txt").write_bytes(b"safe")
    (tmp_path / "binary.dat").write_bytes(b"\x00prefixFIXTURECORPsuffix\xff")
    (tmp_path / "untracked.txt").write_bytes(b"fixturecorp")
    subprocess.run(["git", "add", "photo_FixtureCorp_receipt.txt", "binary.dat"], cwd=tmp_path, check=True)
    assert tracked_violations(tmp_path, forbidden) == ["binary.dat", "photo_FixtureCorp_receipt.txt"]
    assert not contains_forbidden(b"invented company", forbidden)

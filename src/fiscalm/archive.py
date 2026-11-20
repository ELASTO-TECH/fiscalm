"""Archive documents to a firm's configured Google Drive root, without overwriting files."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

from fiscalm import companies, integrations, ledger as ledger_module, storage
from fiscalm.ledger import Ledger, finance_home, now
from fiscalm.pdftext import sha256

RCLONE = os.environ.get("FISCALM_RCLONE", str(Path.home() / ".local/bin/rclone"))


class DriveError(RuntimeError):
    pass


def folder_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", value):
        raise DriveError("invalid Drive folder identifier")
    return value


def target(doc) -> tuple[str, str, str]:
    """Month, document kind folder and safe filename for one ledger document."""
    month = (doc["doc_date"] or doc["month"])[:7]
    if not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", month):
        raise DriveError("invalid document month")
    name = Path(doc["path"]).name
    if not re.fullmatch(r"[\w][\w. -]{0,199}", name):
        suffix = Path(name).suffix
        suffix = suffix if re.fullmatch(r"\.[A-Za-z0-9]{1,8}", suffix) else ""
        stem = re.sub(r"[^\w. -]", "_", name[: -len(suffix)] if suffix else name).strip(". ")[:160]
        name = f"{stem or 'document'}-{hashlib.sha256(name.encode()).hexdigest()[:12]}{suffix}"
    return month, "Extrase" if doc["kind"] == "statement" else "Cheltuieli", name


def folders(month: str, layout: str, kind: str) -> list[str]:
    if layout == "quarter":
        return [month[:4], f"Q{(int(month[5:]) - 1) // 3 + 1}", month, kind]
    return [month, kind]


class Drive:
    """Thin rclone wrapper. Every call is bounded; output with the shared-client notice is ignored."""

    def __init__(self, config: dict):
        self.remote = config["remote"]
        remotes = [line.split() for line in self._run("listremotes", "--long").splitlines()]
        if [r[1] for r in remotes if len(r) >= 2 and r[0] == self.remote] != ["drive"]:
            raise DriveError("configure a Google Drive rclone remote")

    def _run(self, *args: str, timeout: int = 300) -> str:
        proc = subprocess.run([RCLONE, *args], capture_output=True, text=True, timeout=timeout)
        if proc.returncode != 0:
            raise DriveError(f"rclone {args[0]} failed; check the remote and folder permissions")
        return proc.stdout

    def list(self, folder_id: str) -> list[dict]:
        try:
            rows = json.loads(self._run("lsjson", self.remote, f"--drive-root-folder-id={folder_id}", "--hash"))
            if not isinstance(rows, list) or any(
                not isinstance(e, dict)
                or not isinstance(e.get("Name"), str)
                or (e.get("IsDir") and not isinstance(e.get("ID"), str))
                for e in rows
            ):
                raise ValueError()
            return rows
        except ValueError:
            raise DriveError("invalid Drive listing; check the rclone installation") from None

    def mkdir(self, parent_id: str, name: str) -> str:
        """Create (or find, if rclone made it before) a sub folder; return its ID."""
        for create in (False, True):
            if create:
                self._run("mkdir", f"{self.remote}{name}", f"--drive-root-folder-id={parent_id}")
            matches = [e for e in self.list(parent_id) if e.get("Name") == name]
            if matches:
                if len(matches) != 1 or not matches[0].get("IsDir"):
                    raise DriveError("ambiguous Drive folder name")
                return folder_id(matches[0]["ID"])
        raise DriveError(f"created folder {name!r} but cannot find it again")

    def upload(self, src: Path, folder_id: str, name: str) -> None:
        self._run(
            "copyto", str(src), f"{self.remote}{name}", f"--drive-root-folder-id={folder_id}", "--ignore-existing"
        )


def archive_all(ledger: Ledger, dry_run: bool = False, drive_factory=Drive) -> list[dict]:
    """Copy only in live mode; refuse ambiguous destinations and unsafe company files.

    Folder IDs are rediscovered from Drive on retries, so no local folder map is needed.
    A checked, bounded source read is staged privately so a path swap cannot change the upload.
    """
    out = []
    drives: dict[str, Drive] = {}
    folder_cache: dict[tuple[str, str, str], str] = {}
    registry = companies.all(ledger)
    for doc in ledger.documents():
        if (doc["source"] or "").startswith("upload:"):
            continue
        if ledger.db.execute("SELECT 1 FROM archive WHERE sha=?", (doc["sha"],)).fetchone():
            continue
        rec = {"file": Path(doc["path"]).name, "dest": ""}
        try:
            company = doc["company"]
            if company not in registry:
                raise DriveError("unknown company")
            choices = [i for i in integrations.enabled(ledger, "storage") if i.config["company"] == company]
            if len(choices) != 1:
                raise DriveError("configure exactly one enabled archive for this company")
            cfg = choices[0].config
            month, kind, name = target(doc)
            parts = folders(month, cfg["layout"], kind)
            rec["dest"] = "/".join([company, *parts, name])
            root = finance_home().resolve() / company
            if root.resolve() != root:
                raise DriveError("unsafe company directory")
            source = storage.read_file(doc["path"], [root], 50 * 1024 * 1024)
            if source is None:
                raise DriveError("source missing, unsafe or larger than 50 MiB")
            if not ledger_module.is_live() or dry_run:
                out.append(rec | {"status": "would copy"})
                continue  # no rclone calls, even reads, in shadow or dry-run mode
            iid = choices[0].id
            if iid not in drives:
                drives[iid] = drive_factory(cfg)
            drive = drives[iid]
            folder = folder_id(cfg["root"])
            for part in parts:
                key = (iid, folder, part)
                if key not in folder_cache:
                    folder_cache[key] = drive.mkdir(folder, part)
                folder = folder_cache[key]
            matches = [e for e in drive.list(folder) if e.get("Name") == name]
            if matches:
                if (
                    len(matches) != 1
                    or matches[0].get("IsDir")
                    or (matches[0].get("Hashes") or {}).get("md5") != hashlib.md5(source[1]).hexdigest()
                ):
                    raise DriveError("a different or ambiguous file with this name exists; not overwritten")
                rec["status"] = "already there"
            else:
                with tempfile.TemporaryDirectory(dir=finance_home() / "state") as tmp:
                    staged = Path(tmp) / name
                    staged.write_bytes(source[1])
                    drive.upload(staged, folder, name)
                rec["status"] = "copied"
            with ledger.tx() as db:
                db.execute(
                    "INSERT OR IGNORE INTO archive VALUES(?,?,?,?)",
                    (doc["sha"], f"drive:{rec['dest']}", rec["status"], now()),
                )
        except DriveError as e:
            rec |= {"status": "error", "note": str(e)}
        except (subprocess.TimeoutExpired, OSError):
            rec |= {
                "status": "error",
                "note": "Archive unavailable; check rclone, source permissions and available disk space",
            }
        out.append(rec)
    return out


def record_external(ledger: Ledger, sha: str, label: str, status: str) -> None:
    """Record a document already archived externally."""
    with ledger.tx() as db:
        db.execute("INSERT OR IGNORE INTO archive VALUES(?,?,?,?)", (sha, label, status, now()))


__all__ = ["archive_all", "record_external", "sha256"]

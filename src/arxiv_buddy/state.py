"""Persisted record of what has already been sent, so papers never repeat."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

STATE_VERSION = 1


def default_state_path() -> Path:
    override = os.environ.get("ARXIV_BUDDY_STATE")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_STATE_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".local" / "state"
    return root / "arxiv-buddy" / "state.json"


@dataclass
class SentPaper:
    arxiv_id: str
    title: str
    url: str
    sent_at: str

    def to_dict(self) -> dict[str, str]:
        return {
            "arxiv_id": self.arxiv_id,
            "title": self.title,
            "url": self.url,
            "sent_at": self.sent_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, str]) -> "SentPaper":
        return cls(
            arxiv_id=raw.get("arxiv_id", ""),
            title=raw.get("title", ""),
            url=raw.get("url", ""),
            sent_at=raw.get("sent_at", ""),
        )


class StateError(RuntimeError):
    """Raised when the state file cannot be read or written."""


class State:
    """Append-only log of delivered papers, stored as a single JSON file."""

    def __init__(self, path: Path, sent: list[SentPaper]) -> None:
        self.path = path
        self.sent = sent

    def ensure_writable(self) -> None:
        """Check up front that we can record a send.

        Called before the expensive work, so a read-only state directory fails
        immediately rather than after paying for Claude calls and emailing a
        paper we then can't remember having sent.
        """
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            probe = self.path.parent / f".write-probe-{os.getpid()}"
            probe.touch()
            probe.unlink()
        except OSError as exc:
            raise StateError(
                f"Cannot write to the state directory {self.path.parent}: {exc}. "
                f"Set ARXIV_BUDDY_STATE to a writable path, e.g. "
                f"ARXIV_BUDDY_STATE=./state.json"
            ) from exc

    @classmethod
    def load(cls, path: Path | None = None) -> "State":
        path = path or default_state_path()
        if not path.exists():
            return cls(path, [])
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StateError(f"Could not read state file {path}: {exc}") from exc
        entries = raw.get("sent", []) if isinstance(raw, dict) else []
        return cls(path, [SentPaper.from_dict(e) for e in entries if isinstance(e, dict)])

    def save(self) -> None:
        payload = {
            "version": STATE_VERSION,
            "sent": [entry.to_dict() for entry in self.sent],
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Write to a temp file in the same directory, then rename, so an
            # interrupted run can't leave a half-written state file behind.
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=self.path.parent, delete=False
            ) as handle:
                json.dump(payload, handle, indent=2)
                handle.write("\n")
                temp_path = Path(handle.name)
            temp_path.replace(self.path)
        except OSError as exc:
            raise StateError(f"Could not write state file {self.path}: {exc}") from exc

    @property
    def sent_ids(self) -> set[str]:
        return {entry.arxiv_id for entry in self.sent}

    def recent(self, limit: int = 20) -> list[SentPaper]:
        return self.sent[-limit:][::-1]

    def record(self, arxiv_id: str, title: str, url: str) -> SentPaper:
        entry = SentPaper(
            arxiv_id=arxiv_id,
            title=title,
            url=url,
            sent_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        self.sent.append(entry)
        return entry

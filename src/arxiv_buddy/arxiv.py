"""Thin client for the public arXiv Atom API."""

from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Iterable

import httpx

API_URL = "https://export.arxiv.org/api/query"

ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV = "{http://arxiv.org/schemas/atom}"

# arXiv asks API clients to leave ~3s between requests.
_MIN_REQUEST_INTERVAL = 3.0
_last_request_at = 0.0

VALID_SORT_BY = ("relevance", "lastUpdatedDate", "submittedDate")


class ArxivError(RuntimeError):
    """Raised when the arXiv API cannot be reached or returns junk."""


@dataclass(frozen=True)
class Paper:
    arxiv_id: str
    title: str
    authors: tuple[str, ...]
    summary: str
    published: str
    updated: str
    categories: tuple[str, ...]
    abs_url: str
    pdf_url: str
    comment: str | None
    journal_ref: str | None

    @property
    def year(self) -> str:
        return self.published[:4]

    def author_line(self, limit: int = 6) -> str:
        if len(self.authors) <= limit:
            return ", ".join(self.authors)
        return ", ".join(self.authors[:limit]) + f", +{len(self.authors) - limit} more"


def _text(node: ET.Element | None) -> str:
    if node is None or node.text is None:
        return ""
    return " ".join(node.text.split())


def _strip_version(entry_id: str) -> str:
    # http://arxiv.org/abs/2401.12345v2 -> 2401.12345
    tail = entry_id.rsplit("/", 1)[-1]
    if "v" in tail:
        head, _, version = tail.rpartition("v")
        if head and version.isdigit():
            return head
    return tail


def _parse_entry(entry: ET.Element) -> Paper | None:
    raw_id = _text(entry.find(f"{ATOM}id"))
    if not raw_id:
        return None
    arxiv_id = _strip_version(raw_id)

    pdf_url = f"https://arxiv.org/pdf/{arxiv_id}"
    for link in entry.findall(f"{ATOM}link"):
        if link.get("title") == "pdf" and link.get("href"):
            pdf_url = link.get("href", pdf_url)

    authors = tuple(
        _text(a.find(f"{ATOM}name"))
        for a in entry.findall(f"{ATOM}author")
        if _text(a.find(f"{ATOM}name"))
    )
    categories = tuple(
        c.get("term", "") for c in entry.findall(f"{ATOM}category") if c.get("term")
    )

    comment = _text(entry.find(f"{ARXIV}comment")) or None
    journal_ref = _text(entry.find(f"{ARXIV}journal_ref")) or None

    return Paper(
        arxiv_id=arxiv_id,
        title=_text(entry.find(f"{ATOM}title")),
        authors=authors,
        summary=_text(entry.find(f"{ATOM}summary")),
        published=_text(entry.find(f"{ATOM}published")),
        updated=_text(entry.find(f"{ATOM}updated")),
        categories=categories,
        abs_url=f"https://arxiv.org/abs/{arxiv_id}",
        pdf_url=pdf_url,
        comment=comment,
        journal_ref=journal_ref,
    )


def _throttle() -> None:
    global _last_request_at
    elapsed = time.monotonic() - _last_request_at
    if _last_request_at and elapsed < _MIN_REQUEST_INTERVAL:
        time.sleep(_MIN_REQUEST_INTERVAL - elapsed)
    _last_request_at = time.monotonic()


def search(
    search_query: str,
    *,
    max_results: int = 25,
    sort_by: str = "relevance",
    sort_order: str = "descending",
    timeout: float = 30.0,
    attempts: int = 3,
) -> list[Paper]:
    """Run one arXiv query and return the parsed entries."""
    if sort_by not in VALID_SORT_BY:
        sort_by = "relevance"

    params = {
        "search_query": search_query,
        "start": 0,
        "max_results": max(1, min(max_results, 100)),
        "sortBy": sort_by,
        "sortOrder": sort_order,
    }
    headers = {"User-Agent": "arxiv-buddy/0.1 (https://arxiv.org/help/api)"}

    last_error: Exception | None = None
    for attempt in range(attempts):
        _throttle()
        try:
            response = httpx.get(
                API_URL, params=params, headers=headers, timeout=timeout,
                follow_redirects=True,
            )
            response.raise_for_status()
            root = ET.fromstring(response.text)
        except (httpx.HTTPError, ET.ParseError) as exc:
            last_error = exc
            if attempt == attempts - 1:
                break
            time.sleep(2.0 * (attempt + 1))
            continue

        papers = [p for p in (_parse_entry(e) for e in root.findall(f"{ATOM}entry")) if p]
        return papers

    raise ArxivError(f"arXiv query failed after {attempts} attempts: {last_error}")


def dedupe(papers: Iterable[Paper]) -> list[Paper]:
    """Collapse duplicates by arXiv ID, preserving first-seen order."""
    seen: set[str] = set()
    out: list[Paper] = []
    for paper in papers:
        if paper.arxiv_id in seen:
            continue
        seen.add(paper.arxiv_id)
        out.append(paper)
    return out

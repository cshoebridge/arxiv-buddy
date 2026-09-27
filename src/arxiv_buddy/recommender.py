"""Claude-driven paper selection: plan searches, then pick the single best paper."""

from __future__ import annotations

import json
from dataclasses import dataclass

import anthropic

from .arxiv import Paper, dedupe, search
from .state import SentPaper

MAX_PLAN_QUERIES = 6

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "queries": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "search_query": {
                        "type": "string",
                        "description": (
                            "An arXiv API search_query string, e.g. "
                            'all:"diffusion model" AND cat:cs.LG'
                        ),
                    },
                    "sort_by": {
                        "type": "string",
                        "enum": ["relevance", "submittedDate", "lastUpdatedDate"],
                    },
                    "rationale": {
                        "type": "string",
                        "description": "One sentence on what this query is meant to surface.",
                    },
                },
                "required": ["search_query", "sort_by", "rationale"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["queries"],
    "additionalProperties": False,
}

PICK_SCHEMA = {
    "type": "object",
    "properties": {
        "arxiv_id": {
            "type": "string",
            "description": "The arXiv ID of the chosen paper, copied exactly from the candidate list.",
        },
        "headline": {
            "type": "string",
            "description": "A short, plain-language framing of why this paper matters (max ~12 words).",
        },
        "why_read_it": {
            "type": "string",
            "description": (
                "Two to four sentences addressed to the reader, connecting the paper to "
                "their stated interests and saying what they will get out of it."
            ),
        },
        "key_points": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Two to four short bullet points on the paper's core contributions.",
        },
    },
    "required": ["arxiv_id", "headline", "why_read_it", "key_points"],
    "additionalProperties": False,
}

PLAN_SYSTEM = """\
You plan searches against the arXiv API for a single reader, based on a description of \
their research interests.

Produce between 3 and 6 queries that together cover the reader's interests. Aim for a mix:

- Some queries sorted by submittedDate, to surface what is new.
- Some queries sorted by relevance, which will surface well-matched papers of any age. \
Use these to reach influential older work when the reader's interests would be served by a \
classic, foundational, or widely-cited paper rather than only the latest preprint.

arXiv search_query syntax: field prefixes are ti: (title), abs: (abstract), au: (author), \
cat: (category), all: (any field). Combine with AND, OR, ANDNOT. Quote multi-word phrases, \
e.g. abs:"mechanistic interpretability". Category examples: cs.LG, cs.CL, cs.CV, stat.ML, \
math.OC, q-bio.NC, econ.EM, astro-ph.CO.

Keep each query broad enough to return results — over-constrained boolean chains return \
nothing. Prefer two or three terms per query over five.\
"""

PICK_SYSTEM = """\
You choose exactly one arXiv paper for a reader to read today, from a list of candidates, \
based on their stated interests.

How to choose:

- Fit to the reader's stated interests comes first. A strong paper on the wrong topic is \
the wrong answer.
- Recency is not a requirement. If a landmark or foundational paper from several years ago \
is what this reader most needs, pick it. If the freshest preprint is the better read, pick \
that instead.
- Prefer substance over novelty-for-its-own-sake: papers with real results, clear methods, \
or lasting influence.
- Do not pick anything from the "already sent" list.

Write for the reader directly, in second person. Be specific about this paper — never \
generic praise. Your `arxiv_id` must be copied exactly from a candidate entry.\
"""


class RecommenderError(RuntimeError):
    """Raised when Claude cannot produce a usable plan or pick."""


@dataclass(frozen=True)
class Recommendation:
    paper: Paper
    headline: str
    why_read_it: str
    key_points: tuple[str, ...]


def _parse_json_response(response: anthropic.types.Message, what: str) -> dict:
    text = next((b.text for b in response.content if b.type == "text"), "")
    if not text.strip():
        raise RecommenderError(
            f"Claude returned no {what} (stop_reason={response.stop_reason})."
        )
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise RecommenderError(f"Claude returned malformed {what} JSON: {exc}") from exc


def plan_queries(
    client: anthropic.Anthropic, interests: str, *, model: str
) -> list[dict[str, str]]:
    """Ask Claude to turn the free-text interest prompt into arXiv API queries."""
    response = client.messages.create(
        model=model,
        max_tokens=4000,
        system=PLAN_SYSTEM,
        output_config={"format": {"type": "json_schema", "schema": PLAN_SCHEMA}},
        messages=[
            {
                "role": "user",
                "content": f"The reader describes their interests as:\n\n{interests}",
            }
        ],
    )
    if response.stop_reason == "refusal":
        raise RecommenderError(
            "Claude declined to plan searches for this interest prompt."
        )

    plan = _parse_json_response(response, "search plan")
    queries = [q for q in plan.get("queries", []) if q.get("search_query")]
    if not queries:
        raise RecommenderError("Claude produced an empty search plan.")
    return queries[:MAX_PLAN_QUERIES]


def gather_candidates(
    queries: list[dict[str, str]], *, exclude: set[str], pool_size: int
) -> list[Paper]:
    """Run the planned queries and return a deduplicated, unseen candidate pool."""
    per_query = max(5, pool_size // max(1, len(queries)) + 5)
    collected: list[Paper] = []
    for query in queries:
        collected.extend(
            search(
                query["search_query"],
                max_results=per_query,
                sort_by=query.get("sort_by", "relevance"),
            )
        )
    fresh = [p for p in dedupe(collected) if p.arxiv_id not in exclude]
    return fresh[:pool_size]


def _format_candidates(papers: list[Paper]) -> str:
    blocks = []
    for paper in papers:
        summary = paper.summary
        if len(summary) > 1200:
            summary = summary[:1200].rsplit(" ", 1)[0] + "..."
        lines = [
            f"arxiv_id: {paper.arxiv_id}",
            f"title: {paper.title}",
            f"authors: {paper.author_line()}",
            f"published: {paper.published[:10]}",
            f"categories: {', '.join(paper.categories)}",
        ]
        if paper.comment:
            lines.append(f"comment: {paper.comment}")
        if paper.journal_ref:
            lines.append(f"journal_ref: {paper.journal_ref}")
        lines.append(f"abstract: {summary}")
        blocks.append("\n".join(lines))
    return "\n\n---\n\n".join(blocks)


def _format_already_sent(sent: list[SentPaper]) -> str:
    if not sent:
        return "(nothing sent yet)"
    return "\n".join(f"- {entry.arxiv_id}: {entry.title}" for entry in sent)


def pick_paper(
    client: anthropic.Anthropic,
    *,
    interests: str,
    candidates: list[Paper],
    recently_sent: list[SentPaper],
    model: str,
) -> Recommendation:
    """Ask Claude to choose one paper from the pool and explain the choice."""
    if not candidates:
        raise RecommenderError("No candidate papers to choose from.")

    by_id = {paper.arxiv_id: paper for paper in candidates}
    user_content = (
        f"The reader describes their interests as:\n\n{interests}\n\n"
        f"Papers already sent to them (do not pick these):\n{_format_already_sent(recently_sent)}\n\n"
        f"Candidates:\n\n{_format_candidates(candidates)}"
    )

    response = client.messages.create(
        model=model,
        max_tokens=8000,
        system=PICK_SYSTEM,
        output_config={"format": {"type": "json_schema", "schema": PICK_SCHEMA}},
        messages=[{"role": "user", "content": user_content}],
    )
    if response.stop_reason == "refusal":
        raise RecommenderError(
            "Claude declined to pick a paper for this interest prompt."
        )

    pick = _parse_json_response(response, "paper pick")
    arxiv_id = str(pick.get("arxiv_id", "")).strip()
    paper = by_id.get(arxiv_id)
    if paper is None:
        raise RecommenderError(
            f"Claude picked {arxiv_id!r}, which is not in the candidate pool."
        )

    key_points = tuple(
        str(point).strip() for point in pick.get("key_points", []) if str(point).strip()
    )
    return Recommendation(
        paper=paper,
        headline=str(pick.get("headline", "")).strip(),
        why_read_it=str(pick.get("why_read_it", "")).strip(),
        key_points=key_points,
    )


def recommend(
    *,
    interests: str,
    exclude: set[str],
    recently_sent: list[SentPaper],
    model: str,
    pool_size: int,
    client: anthropic.Anthropic | None = None,
    verbose: bool = False,
) -> Recommendation:
    """End-to-end: plan searches, gather candidates, pick one paper."""
    client = client or anthropic.Anthropic()

    queries = plan_queries(client, interests, model=model)
    if verbose:
        print("Search plan:")
        for query in queries:
            print(f"  [{query.get('sort_by', 'relevance')}] {query['search_query']}")
            print(f"      {query.get('rationale', '')}")

    candidates = gather_candidates(queries, exclude=exclude, pool_size=pool_size)
    if verbose:
        print(f"Gathered {len(candidates)} unseen candidate papers.")
    if not candidates:
        raise RecommenderError(
            "Every paper the searches returned has already been sent. "
            "Try broadening the interest prompt or raising ARXIV_BUDDY_POOL_SIZE."
        )

    return pick_paper(
        client,
        interests=interests,
        candidates=candidates,
        recently_sent=recently_sent,
        model=model,
    )

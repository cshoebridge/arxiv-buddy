"""Command line entry point. Designed to be run once a day from cron."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .arxiv import ArxivError
from .config import Config, ConfigError, load_dotenv
from .mailer import EmailError, build_message, render_text, send, subject_for
from .recommender import RecommenderError, recommend
from .state import State, StateError


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arxiv-buddy",
        description="Email yourself one arXiv paper a day, chosen by Claude.",
    )
    subparsers = parser.add_subparsers(dest="command")

    run = subparsers.add_parser("run", help="Pick today's paper and email it.")
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the email instead of sending it, and don't record it as sent.",
    )
    run.add_argument(
        "--verbose",
        action="store_true",
        help="Show the search plan and candidate count.",
    )
    run.add_argument(
        "--state",
        type=Path,
        default=None,
        help="Path to the state file (overrides ARXIV_BUDDY_STATE).",
    )

    history = subparsers.add_parser("history", help="List papers already sent.")
    history.add_argument("--limit", type=int, default=20, help="How many to show.")
    history.add_argument(
        "--state", type=Path, default=None, help="Path to the state file."
    )

    check = subparsers.add_parser(
        "check", help="Validate configuration without sending anything."
    )
    check.add_argument(
        "--probe",
        action="store_true",
        help="Also make live connections to the Claude API, arXiv, and SMTP.",
    )

    return parser


def _cmd_run(args: argparse.Namespace) -> int:
    config = Config.from_env(require_smtp=not args.dry_run)
    state = State.load(args.state or config.state_path)

    # Fail before spending anything if we won't be able to record the send.
    if not args.dry_run:
        state.ensure_writable()

    recommendation = recommend(
        interests=config.interests,
        exclude=state.sent_ids,
        recently_sent=state.recent(30),
        model=config.model,
        pool_size=config.candidate_pool_size,
        verbose=args.verbose,
    )

    if args.dry_run:
        print(f"To: {config.email}")
        print(f"Subject: {subject_for(recommendation)}")
        print()
        print(render_text(recommendation))
        print()
        print("(dry run — nothing sent, nothing recorded)")
        return 0

    assert config.smtp is not None  # require_smtp=True on this path
    message = build_message(recommendation, to_address=config.email, smtp=config.smtp)
    send(message, config.smtp)

    paper = recommendation.paper
    print(f"Sent {paper.arxiv_id} ({paper.title}) to {config.email}")

    # The mail is already gone. If recording it fails now, say so loudly —
    # silently losing the record means this paper gets sent again tomorrow.
    state.record(paper.arxiv_id, paper.title, paper.abs_url)
    try:
        state.save()
    except StateError as exc:
        print(
            f"warning: the email was sent, but recording it failed: {exc}\n"
            f"         {paper.arxiv_id} may be sent again on the next run.",
            file=sys.stderr,
        )
        return 1
    return 0


def _cmd_history(args: argparse.Namespace) -> int:
    state = State.load(args.state)
    entries = state.recent(max(1, args.limit))
    if not entries:
        print(f"No papers sent yet (state file: {state.path}).")
        return 0
    print(f"{len(entries)} most recent of {len(state.sent)} sent — {state.path}\n")
    for entry in entries:
        print(f"{entry.sent_at[:10]}  {entry.arxiv_id:<16}  {entry.title}")
        print(f"{'':12}{entry.url}")
    return 0


def _cmd_check(args: argparse.Namespace) -> int:
    import os

    config = Config.from_env(require_smtp=True)
    state = State.load(config.state_path)
    smtp = config.smtp
    assert smtp is not None

    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        print(
            "Warning: ANTHROPIC_API_KEY is not set. The Anthropic SDK will fall back to "
            "an `ant auth login` profile if one exists; otherwise runs will fail.",
            file=sys.stderr,
        )

    try:
        state.ensure_writable()
        state_note = f"{len(state.sent)} papers sent"
    except StateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    transport = (
        "SSL" if smtp.use_ssl else ("STARTTLS" if smtp.use_starttls else "plain")
    )
    print("Configuration looks complete.\n")
    print(f"  recipient:  {config.email}")
    print(f"  model:      {config.model}")
    print(f"  pool size:  {config.candidate_pool_size}")
    endpoint = config.api_base_url or "https://api.anthropic.com (default)"
    print(f"  api:        {endpoint}")
    print(f"  smtp:       {smtp.host}:{smtp.port} as {smtp.username} ({transport})")
    print(f"  from:       {smtp.from_name} <{smtp.from_address}>")
    print(f"  state file: {state.path} ({state_note})")
    print(f"\n  interests:  {config.interests.strip()}")

    if not args.probe:
        print("\nRun `arxiv-buddy check --probe` to test the three connections,")
        print("or `arxiv-buddy run --dry-run` to see a real recommendation.")
        return 0

    print("\nProbing connections...\n")
    return _probe(config, smtp)


def _probe(config: Config, smtp) -> int:
    """Make one live connection to each dependency. Useful behind a VPN."""
    import smtplib
    import ssl

    import anthropic

    from .arxiv import ArxivError
    from .arxiv import search as arxiv_search

    failures = 0

    try:
        client = anthropic.Anthropic()
        client.messages.create(
            model=config.model,
            max_tokens=16,
            messages=[{"role": "user", "content": "Reply with: ok"}],
        )
        print(f"  [ok]   claude api    {client.base_url}")
    except Exception as exc:
        failures += 1
        print(f"  [FAIL] claude api    {type(exc).__name__}: {exc}")

    try:
        found = arxiv_search("all:electron", max_results=1, attempts=1)
        print(f"  [ok]   arxiv api     export.arxiv.org ({len(found)} result)")
    except (ArxivError, Exception) as exc:
        failures += 1
        print(f"  [FAIL] arxiv api     {type(exc).__name__}: {exc}")

    try:
        context = ssl.create_default_context()
        if smtp.use_ssl:
            with smtplib.SMTP_SSL(
                smtp.host, smtp.port, timeout=20, context=context
            ) as server:
                server.login(smtp.username, smtp.password)
        else:
            with smtplib.SMTP(smtp.host, smtp.port, timeout=20) as server:
                server.ehlo()
                if smtp.use_starttls:
                    server.starttls(context=context)
                    server.ehlo()
                server.login(smtp.username, smtp.password)
        print(f"  [ok]   smtp auth     {smtp.host}:{smtp.port}")
    except Exception as exc:
        failures += 1
        print(f"  [FAIL] smtp auth     {type(exc).__name__}: {exc}")

    if failures:
        print(f"\n{failures} of 3 connections failed.")
        print("Behind a VPN, check ANTHROPIC_BASE_URL and HTTPS_PROXY / SSL_CERT_FILE.")
        return 1
    print("\nAll three connections OK.")
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = _build_parser()

    # No subcommand means "run" — the common case for a cron entry. Leave
    # a bare --help alone so it shows the top-level command list.
    argv = sys.argv[1:] if argv is None else list(argv)
    known = {"run", "history", "check", "-h", "--help"}
    if not any(arg in known for arg in argv):
        argv = ["run", *argv]
    args = parser.parse_args(argv)

    handlers = {"run": _cmd_run, "history": _cmd_history, "check": _cmd_check}
    handler = handlers[args.command]

    try:
        return handler(args)
    except (ConfigError, ArxivError, RecommenderError, EmailError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

"""Render the daily paper as an email and send it over SMTP."""

from __future__ import annotations

import html
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

from .config import SmtpConfig
from .recommender import Recommendation


class EmailError(RuntimeError):
    """Raised when the message cannot be delivered."""


def subject_for(rec: Recommendation) -> str:
    title = rec.paper.title
    if len(title) > 120:
        title = title[:117].rsplit(" ", 1)[0] + "..."
    return f"Today's paper: {title}"


def render_text(rec: Recommendation) -> str:
    paper = rec.paper
    lines = [paper.title, ""]
    if rec.headline:
        lines += [rec.headline, ""]
    lines += [
        paper.author_line(),
        f"{paper.published[:10]} · {', '.join(paper.categories)}",
        "",
        paper.abs_url,
        f"PDF: {paper.pdf_url}",
        "",
        "WHY THIS ONE",
        rec.why_read_it,
    ]
    if rec.key_points:
        lines += ["", "KEY POINTS"]
        lines += [f"  * {point}" for point in rec.key_points]
    if paper.journal_ref:
        lines += ["", f"Published as: {paper.journal_ref}"]
    lines += [
        "",
        "ABSTRACT",
        paper.summary,
        "",
        "-- ",
        "arxiv-buddy",
    ]
    return "\n".join(lines)


def render_html(rec: Recommendation) -> str:
    paper = rec.paper
    e = html.escape

    key_points = ""
    if rec.key_points:
        items = "".join(
            f'<li style="margin:0 0 8px 0;">{e(point)}</li>' for point in rec.key_points
        )
        key_points = (
            '<h2 style="font-size:13px;letter-spacing:.08em;text-transform:uppercase;'
            'color:#6b6b6b;margin:32px 0 12px 0;">Key points</h2>'
            f'<ul style="margin:0;padding-left:20px;color:#2b2b2b;">{items}</ul>'
        )

    journal = ""
    if paper.journal_ref:
        journal = (
            '<p style="margin:16px 0 0 0;color:#6b6b6b;font-size:14px;">'
            f"Published as: {e(paper.journal_ref)}</p>"
        )

    headline = ""
    if rec.headline:
        headline = (
            '<p style="margin:0 0 20px 0;font-size:17px;line-height:1.5;color:#4a4a4a;'
            f'font-style:italic;">{e(rec.headline)}</p>'
        )

    return f"""\
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f6f5f2;">
  <div style="max-width:640px;margin:0 auto;padding:32px 24px;
              font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;
              color:#1a1a1a;line-height:1.6;">

    <p style="margin:0 0 28px 0;font-size:12px;letter-spacing:.12em;text-transform:uppercase;color:#8a8a8a;">
      Your paper for today
    </p>

    <h1 style="margin:0 0 12px 0;font-size:24px;line-height:1.3;font-weight:600;">
      <a href="{e(paper.abs_url)}" style="color:#1a1a1a;text-decoration:none;">{e(paper.title)}</a>
    </h1>

    <p style="margin:0 0 4px 0;font-size:14px;color:#4a4a4a;">{e(paper.author_line())}</p>
    <p style="margin:0 0 24px 0;font-size:13px;color:#8a8a8a;">
      {e(paper.published[:10])} &middot; {e(', '.join(paper.categories))} &middot; arXiv:{e(paper.arxiv_id)}
    </p>

    {headline}

    <p style="margin:0 0 28px 0;">
      <a href="{e(paper.abs_url)}"
         style="display:inline-block;background:#1a1a1a;color:#ffffff;text-decoration:none;
                padding:11px 20px;border-radius:6px;font-size:14px;font-weight:500;">Read on arXiv</a>
      <a href="{e(paper.pdf_url)}"
         style="display:inline-block;margin-left:10px;color:#4a4a4a;text-decoration:none;
                padding:11px 16px;border:1px solid #d8d6d0;border-radius:6px;font-size:14px;">PDF</a>
    </p>

    <h2 style="font-size:13px;letter-spacing:.08em;text-transform:uppercase;color:#6b6b6b;margin:32px 0 12px 0;">
      Why this one
    </h2>
    <p style="margin:0;color:#2b2b2b;">{e(rec.why_read_it)}</p>

    {key_points}

    <h2 style="font-size:13px;letter-spacing:.08em;text-transform:uppercase;color:#6b6b6b;margin:32px 0 12px 0;">
      Abstract
    </h2>
    <p style="margin:0;color:#4a4a4a;font-size:15px;">{e(paper.summary)}</p>
    {journal}

    <p style="margin:40px 0 0 0;padding-top:20px;border-top:1px solid #e2e0da;
              font-size:12px;color:#9a9a9a;">
      Sent by arxiv-buddy.
    </p>
  </div>
</body>
</html>
"""


def build_message(rec: Recommendation, *, to_address: str, smtp: SmtpConfig) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = subject_for(rec)
    message["From"] = formataddr((smtp.from_name, smtp.from_address))
    message["To"] = to_address
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain=smtp.from_address.rpartition("@")[2] or None)
    message.set_content(render_text(rec))
    message.add_alternative(render_html(rec), subtype="html")
    return message


def send(message: EmailMessage, smtp: SmtpConfig, *, timeout: float = 30.0) -> None:
    context = ssl.create_default_context()
    try:
        if smtp.use_ssl:
            with smtplib.SMTP_SSL(
                smtp.host, smtp.port, timeout=timeout, context=context
            ) as server:
                server.login(smtp.username, smtp.password)
                server.send_message(message)
        else:
            with smtplib.SMTP(smtp.host, smtp.port, timeout=timeout) as server:
                server.ehlo()
                if smtp.use_starttls:
                    server.starttls(context=context)
                    server.ehlo()
                server.login(smtp.username, smtp.password)
                server.send_message(message)
    except (smtplib.SMTPException, OSError) as exc:
        raise EmailError(f"Could not send mail via {smtp.host}:{smtp.port}: {exc}") from exc

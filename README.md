# arxiv-buddy

One arXiv paper in your inbox every morning, picked by Claude to match your interests.

You give it two things — an email address and a free-text description of what you
care about. Each run, Claude turns that description into a set of arXiv searches,
reads the candidate abstracts, picks the single best paper for you, and writes a
short note on why it's worth your time. Papers already sent are never repeated.

Recency is not a requirement. If a foundational paper from 2017 is the better read
than today's preprint, that's what you get.

## Setup

```bash
uv sync
cp .env.example .env    # then fill it in
uv run arxiv-buddy check
```

`check` validates your configuration without sending anything. Once it passes:

```bash
uv run arxiv-buddy run --dry-run    # see a real recommendation, printed
uv run arxiv-buddy run              # actually send it
```

### Configuration

All configuration is environment variables, read from the real environment or a
`.env` file in the working directory (the real environment wins).

| Variable | Required | Description |
| --- | --- | --- |
| `ARXIV_BUDDY_EMAIL` | yes | Where to send the daily paper. |
| `ARXIV_BUDDY_INTERESTS` | yes | Free text. The more specific, the better the picks. |
| `ANTHROPIC_API_KEY` | yes | Or an `ant auth login` profile. |
| `SMTP_HOST` | yes | e.g. `smtp.gmail.com` |
| `SMTP_USERNAME` | yes | Usually your full email address. |
| `SMTP_PASSWORD` | yes | For Gmail, a 16-character [App Password](https://myaccount.google.com/apppasswords). |
| `SMTP_PORT` | no | Defaults to `587`. |
| `SMTP_FROM` | no | Defaults to `SMTP_USERNAME`. |
| `SMTP_FROM_NAME` | no | Defaults to `arXiv Buddy`. |
| `SMTP_USE_SSL` | no | Defaults to true when the port is 465. |
| `SMTP_USE_STARTTLS` | no | Defaults to true unless SSL is in use. |
| `ARXIV_BUDDY_MODEL` | no | Defaults to `claude-opus-5`. |
| `ARXIV_BUDDY_POOL_SIZE` | no | Candidate abstracts shown to Claude. Defaults to `60`. |
| `ARXIV_BUDDY_STATE` | no | State file path. Defaults to `~/.local/state/arxiv-buddy/state.json`. |

Writing a good interests prompt matters more than any of the tuning knobs. Name
subfields, methods, and the kind of paper you want — for example:

> Mechanistic interpretability of language models — sparse autoencoders, circuit
> analysis, feature steering. I also care about evaluation methodology for LLM
> agents. I value foundational papers as much as brand-new preprints.

## Scheduling

The command is one-shot: it picks a paper, sends it, and exits.

### macOS (launchd) — recommended

```bash
./launchd/install.sh
```

Generates a plist into `~/Library/LaunchAgents` and loads it. Runs daily at
07:30, survives reboots, and if the Mac is asleep at that time launchd runs the
job shortly after it wakes rather than skipping the day. Use `--at HH:MM` for a
different time, and re-run the script to update an installed job.

The plist is generated rather than committed: launchd needs absolute paths to
`uv`, to the project, and to the log, all of which are machine-specific. The
script derives them and labels the job `com.<your-username>.arxiv-buddy`.

Run it from your own terminal — `~/Library/LaunchAgents` is protected by macOS
TCC, so the copy has to come from a process you launched interactively.

Before installing, the script runs `arxiv-buddy check` with an empty environment
and refuses to install if it fails — see the caveat below for why.

```bash
LABEL="com.$(id -un | tr -cd '[:alnum:]').arxiv-buddy"
launchctl print gui/$(id -u)/$LABEL | head -20   # status
launchctl kickstart -p gui/$(id -u)/$LABEL       # run now
tail -f launchd/arxiv-buddy.log                  # logs
./launchd/install.sh remove                      # uninstall
```

### cron

```cron
30 7 * * * cd /path/to/arxiv-buddy && /path/to/uv run arxiv-buddy run >> /tmp/arxiv-buddy.log 2>&1
```

### Two things any scheduler needs

Both matter because schedulers run with almost no environment:

- **Working directory.** `.env` is read from the current directory, so the job
  must `cd` into the project (the plist does this via `WorkingDirectory`).
  Without it the run fails immediately with `ARXIV_BUDDY_EMAIL is not set`.
- **Absolute paths, and config in `.env` not your shell.** There is no useful
  `PATH`, and your shell's exported variables are not inherited. Anything your
  shell profile sets that the run depends on must be repeated in `.env` —
  typically a custom `ANTHROPIC_BASE_URL`, an `HTTPS_PROXY`, or an
  `SSL_CERT_FILE` pointing at a corporate CA bundle. Verify with:

  ```bash
  env -i HOME="$HOME" /path/to/uv run arxiv-buddy check --probe
  ```

  That strips the environment the way launchd does. If it passes there, the
  scheduled run will work.

## Commands

```
arxiv-buddy run [--dry-run] [--verbose] [--state PATH]
arxiv-buddy history [--limit N] [--state PATH]
arxiv-buddy check
```

`--dry-run` prints the email and records nothing. `--verbose` shows the search
plan Claude generated and how many candidates it considered.

## How it works

1. **Plan** — Claude converts your interests into 3–6 arXiv API queries, mixing
   `submittedDate` sorts (new work) with `relevance` sorts (which reach older,
   influential papers).
2. **Gather** — The queries run against the arXiv Atom API, results are
   deduplicated by ID, and anything already sent is dropped.
3. **Pick** — Claude reads the remaining abstracts and returns one paper plus a
   headline, a "why read it" note, and key points — via a JSON schema, so the
   response is always well-formed and the chosen ID is verified against the pool.
4. **Send** — A multipart email (plain text + HTML) goes out over SMTP, and only
   then is the paper recorded as sent.

That last ordering matters: if the send fails, nothing is recorded, so the next
run will try again rather than silently skipping a day.

### Cost

One run is two Claude calls — a small planning call and one call carrying ~60
abstracts. On `claude-opus-5` that lands at roughly 2–5 cents per day. Setting
`ARXIV_BUDDY_MODEL=claude-sonnet-5` cuts that further if you'd rather trade some
selection quality for cost.

## Layout

```
src/arxiv_buddy/
  cli.py           argument parsing, command handlers
  config.py        environment/.env loading and validation
  arxiv.py         arXiv Atom API client (rate-limited, retrying)
  recommender.py   Claude planning + selection
  mailer.py        text/HTML rendering and SMTP delivery
  state.py         JSON record of sent papers (atomic writes)
```

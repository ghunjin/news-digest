# Biweekly News Digest

A small Python script that produces a personal intelligence briefing every
two weeks. It searches the web for news across five subject areas from the
last 14 days, keeps only stories that clear a strict "does this actually
matter" bar, ranks them from most to least consequential, and summarizes
each one with a short **Why it matters** line and a source link. Stories
that almost made the cut appear as honorable mentions, with a one-line
reason why they fell short. A final synthesis section connects the events
of the fortnight.

The result is saved as a dated Markdown file and delivered to your phone
via a Telegram bot. Once deployed to GitHub Actions, it runs entirely in
the cloud — your own computer can be asleep, off, or lost at sea.

Runs on the Claude API — web search is a built-in tool, so there's no
separate search service to sign up for. Bring your own Anthropic API key
(see `.env.example`).

## How it works

Two API calls per run:

1. **Gather** (Claude Sonnet): searches the web, filters for genuine
   changes of state (decisions, launches, results, reversals — not
   commentary or rumors), ranks by consequence, and writes each story as:
   emoji + headline + date, a 3–4 sentence factual summary, a "Why it
   matters" line, and a source. Stories covered in the previous edition
   are skipped unless there's a material development.
2. **Synthesis** (Claude Sonnet): a tight 2–3 paragraph comparison of the
   fortnight's events — what connects to what.

Between the two calls sits a defensive parsing layer that treats the
model's output as untrusted input (see [Design notes](#design-notes)
below) — freshness, deduplication, and memory integrity are enforced in
code, not left as promises in the prompt.

The gather model is set by one line in `digest.py`. A cheaper Haiku
option sits commented out directly below it; swapping which line is
commented switches models. (Haiku was A/B tested and demoted: across two
full runs it repeatedly shipped stories older than the explicit date
cutoff and drifted the output format. The ~$20/year premium for Sonnet
buys out a class of silent failures.)

## One-time setup

1. **Add a payment method** on the Anthropic console (console.anthropic.com
   → Billing). Your card lives there, on Anthropic's side. It never goes in
   the code.

2. **Set a monthly spend limit** in the same billing settings (e.g.
   $5/month). A cap guarantees a bug or misconfiguration can't run up a
   real bill.

3. **Create an API key** (console → API Keys → Create Key). Copy it.

4. **Install the dependencies:**

pip install anthropic python-dotenv requests

5. **Set up your key locally:**

cp .env.example .env

   Open `.env` and paste your real key after `ANTHROPIC_API_KEY=`. For
   phone delivery, also fill in `TELEGRAM_BOT_TOKEN` and
   `TELEGRAM_CHAT_ID` (if these are missing, the digest still runs and
   saves to a file — Telegram is just skipped).
   The `.gitignore` already keeps `.env` out of git — do not remove that
   line.

6. **Set your start date:** edit `BIWEEKLY_ANCHOR` in `digest.py` to the
   date of your first run. The digest then runs every 14 days from that
   date.

## Run it

python digest.py # runs only on an on-cycle day
python digest.py --force # runs right now regardless of the cycle


Edit `TOPICS`, `STYLE_INSTRUCTION`, and the model choices near the top of
`digest.py` to taste.

## Testing (free)

python test_digest.py


The test harness imports the real module and exercises the parsing,
filtering, memory, scheduling, and Telegram-chunking logic — 43 checks —
against fixtures modeled on real model output, including every format
drift observed in live runs. It makes **no API calls** (the network layer
is monkeypatched), needs no key, and costs nothing. Run it after any
change to the parser or prompt format, before pushing: the cloud runs
whatever the repo holds.

Only true end-to-end behavior needs a paid run; a cheap smoke test is
temporarily lowering `max_uses` to 3–5 and running with `--force`.

## Scheduling (biweekly)

Neither cron nor GitHub Actions can natively express "every 14 days," so
the pattern is: **schedule the script weekly, and the script itself skips
the off-weeks** using `BIWEEKLY_ANCHOR`. Off-week runs exit immediately
and cost nothing (no API calls are made).

**Recommended — GitHub Actions (cloud, free, no machine needed):**
A workflow file runs the script on GitHub's servers on a weekly schedule,
so your own computer can be asleep or off. Your API key and Telegram
tokens go in the repo's encrypted Secrets (Settings → Secrets and
variables → Actions), never in the code. Two caveats the workflow
handles: each cloud run starts on a fresh machine, so the workflow
commits `last_headlines.txt` (the de-duplication memory) back to the repo
after each run — otherwise repeat-skipping would silently never trigger;
and the workflow sets `TZ` to the local timezone, because GitHub runners
think in UTC and would otherwise read the trigger moment as the wrong
calendar day. Note that GitHub schedules may run a few minutes late, and
auto-pause after 60 days of repo inactivity (the digest's own biweekly
commits count as activity, so in practice this never triggers).

**Alternative — cron (Mac/Linux):**

crontab -e

Add (8am every Wednesday; adjust the path):

0 8 * * 3 cd /path/to/news-digest && /usr/bin/python3 digest.py >> cron.log 2>&1

Caveat: cron only fires if the machine is awake at that moment. Missed
runs are silently skipped, not queued — fine for an always-on machine,
unreliable for a laptop that sleeps.

## Design notes

The interesting engineering in this project is not the API calls — it's
treating an LLM as an unreliable component and building the guarantees
around it in ordinary code. Each decision below traces to a failure
actually observed in a live run.

- **Freshness is a code guarantee, not a model promise.** The prompt
  states an explicit cutoff date, but the script also parses the date out
  of every headline and deterministically drops stories older than 14
  days (the cutoff is computed in Python — LLMs are unreliable at date
  arithmetic, but code isn't). Added after test runs shipped stale
  stories straight past the prompt's instructions.
- **Fail open on ambiguity.** A headline date that can't be parsed to a
  specific day ("June 2026") keeps its story. Deterministically deleting
  good stories would be worse than occasionally letting a vague one
  through.
- **Parse by shape, not by exact format.** Headlines are recognized by
  their overall shape — a bold chunk with a trailing parenthesized date —
  with tolerance for the drift models actually produce (emoji inside or
  outside the bold, date inside or outside the bold markers). An earlier
  parser that assumed an exact format was silently defeated by drift and
  corrupted the de-dup memory.
- **Guard the memory writes.** `last_headlines.txt` is only overwritten
  when at least one headline was successfully extracted, so a parser
  failure can never wipe the memory. And if filtering leaves zero
  stories, the run aborts *before* paying for the synthesis call, with a
  non-zero exit code — a red X in Actions instead of a deceptive green.
- **A sentinel line beats positional trimming.** The honorable-mentions
  section is split off at an exact marker line the model is told to
  print. Mentions legitimately have no `Source:` line and would be
  deleted by the strict story filter; the sentinel keeps the filter's
  strictness without collateral damage.
- **Delivery must never block state.** The Telegram send is fully
  isolated — one retry on network errors, then give up with a note. A
  phone-delivery hiccup can never crash the run, because a crash would
  also skip committing the de-dup memory.
- **Degrade gracefully.** If the model's output drifts so severely that
  no headline-shaped lines exist at all, the whole text is kept as long
  as it cites a source: a messy digest beats a vanished one.

## Rough cost

Biweekly (26 runs/year), capped at 15 web searches per run: roughly
**$25–30/year** with Sonnet as the gather model, or **$6–8/year** with the
Haiku option. Most of the cost is input tokens: multi-search turns resend
accumulated search results on each continuation, which adds up. Lowering
`max_uses` in `digest.py` (e.g. to 8) reduces cost roughly proportionally.

## Notes

- The web search tool version string (`web_search_20250305`) is
  periodically superseded. If a run fails with an error about the tool
  type, check the current string in Anthropic's web search docs and swap
  it in — one line.
- Multi-search turns can pause mid-way (`pause_turn`); the script handles
  the continuation loop automatically.
- Telegram delivery strips Markdown for clean plain text on the phone
  (section headers become framed divider lines); the saved `.md` file
  keeps its formatting. Messages are split at story boundaries to stay
  under Telegram's 4,096-character limit; a single over-long story is
  hard-split losslessly.
- A wrong Telegram secret fails *quietly*: the run stays green, the
  digest is generated and saved, but no message arrives. If the phone
  doesn't buzz on a digest day, read the Actions log — "not configured"
  means a secret-name typo, a 401 means a wrong token value.
- To share the digest with others at no extra cost, create a Telegram
  channel, add your bot as an admin, and use the channel's chat ID as
  `TELEGRAM_CHAT_ID` — generation happens once regardless of how many
  people subscribe.
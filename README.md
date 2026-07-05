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
via a Telegram bot.

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

The gather model is set by one line in `digest.py`. A cheaper Haiku
option sits commented out directly below it; swapping which line is
commented switches models.

## One-time setup

1. **Add a payment method** on the Anthropic console (console.anthropic.com
   → Billing). Your card lives there, on Anthropic's side. It never goes in
   the code.

2. **Set a monthly spend limit** in the same billing settings (e.g.
   $5/month). A cap guarantees a bug or misconfiguration can't run up a
   real bill.

3. **Create an API key** (console → API Keys → Create Key). Copy it.

4. **Install the dependencies:**
   ```
   pip install anthropic python-dotenv requests
   ```

5. **Set up your key locally:**
   ```
   cp .env.example .env
   ```
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

```
python digest.py            # runs only on an on-cycle day
python digest.py --force    # runs right now regardless of the cycle
```

Edit `TOPICS`, `STYLE_INSTRUCTION`, and the model choices near the top of
`digest.py` to taste.

## Scheduling (biweekly)

Neither cron nor GitHub Actions can natively express "every 14 days," so
the pattern is: **schedule the script weekly, and the script itself skips
the off-weeks** using `BIWEEKLY_ANCHOR`. Off-week runs exit immediately
and cost nothing (no API calls are made).

**Recommended — GitHub Actions (cloud, free, no machine needed):**
A workflow file runs the script on GitHub's servers on a weekly schedule,
so your own computer can be asleep or off. Your API key and Telegram
tokens go in the repo's encrypted Secrets (Settings → Secrets and
variables → Actions), never in the code. One caveat handled by the
workflow: each cloud run starts on a fresh machine, so the workflow
commits `last_headlines.txt` (the de-duplication memory) back to the repo
after each run — otherwise repeat-skipping would silently never trigger.
Note that GitHub schedules use UTC time, and may run a few minutes late.

**Alternative — cron (Mac/Linux):**
```
crontab -e
```
Add (8am every Wednesday; adjust the path):
```
0 8 * * 3 cd /path/to/news-digest && /usr/bin/python3 digest.py >> cron.log 2>&1
```
Caveat: cron only fires if the machine is awake at that moment. Missed
runs are silently skipped, not queued — fine for an always-on machine,
unreliable for a laptop that sleeps.

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
- The model's output is parsed defensively: only segments containing a
  `Source:` line count as stories, so any stray narration is dropped, and
  the honorable-mentions section is split off at a sentinel line before
  filtering.
- Telegram delivery strips Markdown for clean plain text on the phone;
  the saved `.md` file keeps its formatting. Messages are split at story
  boundaries to stay under Telegram's 4,096-character limit.
- To share the digest with others at no extra cost, create a Telegram
  channel, add your bot as an admin, and use the channel's chat ID as
  `TELEGRAM_CHAT_ID` — generation happens once regardless of how many
  people subscribe.

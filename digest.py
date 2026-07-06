"""
Biweekly news digest.

What it does, in order:
  1. Asks Claude to search the web for news on your topics from the last
     14 days, rank them by consequence, and summarize each story in your
     chosen style with a "Why it matters" line. Each summary carries a
     source link and its publication date. Stories already covered in the
     last edition are skipped unless there's a material development. A short
     "honorable mentions" list notes what almost made the cut and why not.
  2. Feeds the summaries back to Claude (Sonnet) for a comparative synthesis
     that draws connections between events.
  3. Prints status lines, saves the digest to a dated file, sends it to
     Telegram, and records this edition's headlines so the next run can
     avoid repeats.

Scheduling: cron and GitHub Actions cannot express "every 14 days" natively,
so schedule this WEEKLY (e.g. every Wednesday) and the script itself skips
the off-weeks using BIWEEKLY_ANCHOR below. Run manually anytime with
`python digest.py --force` to bypass the skip.

Your API key is NEVER written in this file. It is read from a .env file that
you keep out of git (locally) or from GitHub Actions Secrets (in the cloud).
"""

import os
import re
import sys
import time
from datetime import date, timedelta

import requests
from anthropic import Anthropic
from dotenv import load_dotenv

# Loads ANTHROPIC_API_KEY from a .env file in this folder into the environment.
load_dotenv()

# The SDK automatically picks up ANTHROPIC_API_KEY from the environment.
# You never pass the key as a string here.
client = Anthropic()

HERE = os.path.dirname(__file__)
# Where we remember last week's headlines, so we can skip repeats.
LAST_HEADLINES_PATH = os.path.join(HERE, "last_headlines.txt")


# ---- YOUR SETTINGS -----------------------------------------------------------
# Edit these three things and nothing else needs to change.

TOPICS = [
    "Global macroeconomics & financial markets",
    "AI, computing & frontier technology",
    "Geopolitics & international relations",
    "Korea (domestic news)",
    "Sciences & long-term civilization (energy, medicine, aging, astronomy, "
    "neuroscience, and similar research)",
]

# Describe the summary style however you like — plain instruction, in English
# or Korean. This string is handed to Claude verbatim.
# NOTE: the summary is pure what-happened; significance lives in the separate
# "Why it matters" line (see the prompt), so we don't say it twice.
STYLE_INSTRUCTION = (
    "Summarize what happened in 3-4 sentences, neutral and factual, no hype. "
    "Plain language. Keep the summary to the facts -- the significance goes "
    "in the separate 'Why it matters' line, not here."
)

# Gather model. Sonnet follows the format rules and the date window far more
# reliably (two Haiku A/B runs in July 2026 each violated the explicit date
# cutoff); token cost is ~3x Haiku (~$1/run vs ~$0.22 -- search fees are
# flat either way). Reverting is exactly this: swap which line is commented.
SEARCH_MODEL = "claude-sonnet-4-6"
# SEARCH_MODEL = "claude-haiku-4-5-20251001"   # cheaper, weaker instruction-following

SYNTHESIS_MODEL = "claude-sonnet-4-6"        # better at cross-event reasoning

# The date of your FIRST run. The script only runs on days that are a whole
# number of 14-day periods after this date; scheduled weekly, it therefore
# fires every second week. Set this to the day you actually start.
BIWEEKLY_ANCHOR = date(2026, 7, 8)
# ------------------------------------------------------------------------------

# The model is told to print this exact line between the ranked stories and
# the honorable mentions. The code splits on it, so the strict story filter
# (which requires a Source: line) never deletes the mentions, and the
# mentions never leak into last_headlines.txt.
MENTIONS_SENTINEL = "---HONORABLE MENTIONS---"

# What a story HEADLINE looks like, regardless of exact model formatting:
# up to ~10 characters of prefix (an emoji and a space, or nothing), then a
# **bold** chunk, then a (date) in parentheses at the end of the line.
# Matches both the requested format:   **🚀 Headline** (June 25, 2026)
# and the drifted variant seen from Haiku: 🚀 **Headline** (June 25, 2026)
# but NOT planning junk like:          **SpaceX IPO** - Within 14 days
# (no trailing parenthesized date). Anchoring on the SHAPE of a headline,
# instead of assuming the line starts with **, is what makes the parser
# survive small format drift.
HEADLINE_RE = re.compile(
    r"^.{0,10}\*\*.+\*\*\s*\(.+\)\s*$"      # date after the bold (requested format)
    r"|"
    r"^.{0,10}\*\*.+\(.+\)\s*\*\*\s*$"      # date INSIDE the bold (Haiku drift, 2026-07-06)
)

# Lines that are pure decoration/litter the model sometimes emits between
# stories (---, ***, a stray **). Dropped from stories and mentions alike.
JUNK_LINE_RE = re.compile(r"^\s*[-*_]{2,}\s*$")

# Month names (and common abbreviations) -> month number, for parsing the
# (date) at the end of headlines.
_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5,
    "june": 6, "july": 7, "august": 8, "september": 9, "october": 10,
    "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7,
    "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}


def is_headline(line: str) -> bool:
    """True if this line has the shape of a story headline."""
    return bool(HEADLINE_RE.match(line.strip()))


def parse_headline_date(headline: str):
    """Best-effort parse of the (date) at the end of a headline line.

    Handles "(June 17, 2026)", "(June 2-3, 2026)" (takes the first day),
    and ISO "(2026-06-17)". Returns a datetime.date, or None when no
    specific day can be determined (e.g. "(June 2026)").

    None means "can't tell", NOT "stale" -- callers must FAIL OPEN and keep
    the story. Deterministically dropping good stories would be worse than
    occasionally letting a vague-dated one through.
    """
    m = re.search(r"\(([^()]*)\)\s*\**\s*$", headline.strip())
    if not m:
        return None
    s = m.group(1)

    iso = re.search(r"(20\d{2})-(\d{1,2})-(\d{1,2})", s)
    if iso:
        try:
            return date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
        except ValueError:
            return None

    year_m = re.search(r"\b(20\d{2})\b", s)
    if not year_m:
        return None
    year = int(year_m.group(1))

    month = None
    for token in re.findall(r"[A-Za-z]+", s):
        if token.lower() in _MONTHS:
            month = _MONTHS[token.lower()]
            break
    if month is None:
        return None

    # First standalone 1-2 digit number is the day. In "June 2026" there is
    # no such number (2026 is 4 digits), so month-only dates return None.
    day_m = re.search(r"\b(\d{1,2})\b", s)
    if not day_m:
        return None
    try:
        return date(year, month, int(day_m.group(1)))
    except ValueError:
        return None


def is_on_week(today: date) -> bool:
    """True if today falls on the biweekly cycle counted from the anchor."""
    return (today - BIWEEKLY_ANCHOR).days % 14 == 0


def load_last_headlines() -> str:
    """Read last edition's headlines, if we saved any. Empty string on first run."""
    if os.path.exists(LAST_HEADLINES_PATH):
        with open(LAST_HEADLINES_PATH, encoding="utf-8") as f:
            return f.read().strip()
    return ""


def save_headlines(headlines: str) -> None:
    """Overwrite the headlines file with this week's, for next edition to read."""
    with open(LAST_HEADLINES_PATH, "w", encoding="utf-8") as f:
        f.write(headlines.strip())


def strip_junk_lines(text: str) -> str:
    """Remove decoration-only lines (---, stray **) the model sometimes emits."""
    return "\n".join(
        line for line in text.splitlines() if not JUNK_LINE_RE.match(line)
    ).strip()


def extract_stories(text: str, cutoff: date = None) -> str:
    """Keep only real, fresh stories; drop narration, junk, and stale news.

    Strategy: find every line shaped like a headline (see HEADLINE_RE),
    carve the text into segments from one headline to the next, and keep a
    segment only if it contains a "Source:" line. Narration before the
    first headline is discarded by construction; headline-shaped junk
    without a real story under it fails the Source: check.

    If a cutoff date is given, stories whose headline date parses to BEFORE
    the cutoff are dropped (both Haiku test runs included stale stories
    despite an explicit cutoff in the prompt -- this makes freshness a code
    guarantee instead of a model promise). Unparseable dates FAIL OPEN: the
    story is kept.

    Fallback: if NO headline-shaped lines are found at all (severe format
    drift), keep the whole text when it mentions a source rather than
    silently returning nothing -- a messy digest beats a vanished one.
    """
    lines = text.splitlines()
    headline_idxs = [i for i, line in enumerate(lines) if is_headline(line)]

    if not headline_idxs:
        return strip_junk_lines(text) if "Source:" in text else ""

    stories = []
    for n, start in enumerate(headline_idxs):
        end = headline_idxs[n + 1] if n + 1 < len(headline_idxs) else len(lines)
        segment = strip_junk_lines("\n".join(lines[start:end]))
        if "Source:" not in segment:
            continue
        if cutoff is not None:
            pub = parse_headline_date(lines[start])
            if pub is not None and pub < cutoff:
                print(f"(Dropped stale story dated {pub}: "
                      f"{lines[start].strip()[:70]}...)")
                continue
        stories.append(segment)
    return "\n\n".join(stories)


def gather_and_summarize(last_headlines: str) -> tuple:
    """Call 1: search the web, rank, and summarize. Returns (stories, mentions)."""
    topics_text = "\n".join(f"- {t}" for t in TOPICS)
    today = date.today().isoformat()
    # Compute the window boundary HERE, in Python. LLMs are unreliable at
    # date arithmetic ("count back 14 days"), but comparing a date against
    # an explicit boundary date is easy for them.
    cutoff_date = date.today() - timedelta(days=14)
    cutoff = cutoff_date.isoformat()

    # De-dup block: only added if we have last week's headlines on file.
    dedup_block = ""
    if last_headlines:
        dedup_block = (
            f"\nThese stories were covered in the LAST edition's briefing:\n\n"
            f"{last_headlines}\n\n"
            f"Skip any of these unless there's a genuinely new, material "
            f"development — in which case cover only the new development and "
            f"say briefly what changed. Prioritize fresh stories.\n"
        )

    prompt = (
        f"You are the chief analyst preparing a biweekly intelligence "
        f"briefing for a single, highly analytical reader. Your judgment "
        f"about what matters and why is the product. The register is calm "
        f"and precise — an analyst's brief, never a news channel's hype.\n\n"
        f"Today is {today}. Search the web for the most important news "
        f"published ON OR AFTER {cutoff} across these subject areas:\n\n"
        f"{topics_text}\n"
        f"{dedup_block}\n"
        f"Treat the subject areas as GUIDELINES for breadth, not quotas. It is "
        f"fine — expected, even — for some areas to contribute no stories in a "
        f"given edition if nothing there clears the bar below. Do not include a "
        f"weak story just to represent an area.\n\n"
        f"WHAT COUNTS AS BRIEFING-WORTHY. Include a story only if it meets at "
        f"least one of these:\n"
        f"- It marks a genuine change of state (a decision made, a threshold "
        f"crossed, a launch, a result, a reversal) rather than commentary, "
        f"speculation, or an incremental update to an ongoing situation.\n"
        f"- It has consequences beyond its own immediate subject — it shifts a "
        f"market, a policy, a field, or how people understand something.\n"
        f"- It would still matter, or be worth having known about, two weeks "
        f"from now — not just today's noise.\n\n"
        f"EXCLUDE: routine price movements without a clear cause, opinion and "
        f"punditry, minor personnel news, product rumors, single-company PR, "
        f"and stories that are only restating a trend already well known.\n\n"
        f"RANKING: Order the stories from most to least consequential — the "
        f"single most important story of the fortnight comes first. Judge "
        f"consequence by breadth and durability of impact, not by drama. Do "
        f"NOT assign numeric scores or priority labels; the order itself is "
        f"the ranking.\n\n"
        f"For each included story, apply this style:\n\n{STYLE_INSTRUCTION}\n\n"
        f"REQUIREMENTS for each story:\n"
        f"- State the exact publication date (month, day, and year). If a "
        f"story was published BEFORE {cutoff}, drop it entirely -- do not "
        f"include it. Check each story's date against {cutoff} before "
        f"including it.\n"
        f"- After the summary, add one labeled line: 'Why it matters: ' "
        f"followed by 1-2 sentences on the significance for the reader — "
        f"consequences, what it changes, what to watch. Do not repeat the "
        f"summary.\n"
        f"- End each story with its source on its own line, formatted as: "
        f"Source: <publication name> -- <url>\n\n"
        f"Format each story as:\n"
        f"**<one relevant emoji> <short headline>** (<Month Day, Year>)\n"
        f"<summary in the style above>\n"
        f"Why it matters: <1-2 sentences>\n"
        f"Source: <publication> -- <url>\n\n"
        f"The emoji goes INSIDE the ** markers, and the (<Month Day, Year>) "
        f"at the end of the headline line is mandatory, with the specific "
        f"day included.\n\n"
        f"Aim for 6-9 stories total: around 6 in a quiet fortnight, up to 9 "
        f"when there is genuinely a lot worth covering. Quality over filling a "
        f"quota — never pad to reach 9.\n\n"
        f"AFTER the last story, print this exact line by itself:\n"
        f"{MENTIONS_SENTINEL}\n"
        f"Then list 2-4 honorable mentions — stories that almost made the "
        f"briefing — each as a single plain line (no bold, no Source line):\n"
        f"- <topic> (<date>): <1-2 sentences on why it fell short of the bar>\n"
        f"If nothing almost made the cut, write: None this edition.\n\n"
        f"OUTPUT RULES: Output ONLY the stories, the sentinel line, and the "
        f"mentions, in the exact formats above. Do NOT list, plan, or "
        f"organize the stories before writing them. No introduction, no "
        f"narration of your search process, no closing remarks, no markdown "
        f"headers (#, ##, ###), no section titles per subject area. Begin "
        f"directly with the first story's headline."
    )

    tools = [{
        "type": "web_search_20250305",
        "name": "web_search",
        "max_uses": 15,              # hard cap on searches -- bounds your cost
    }]
    messages = [{"role": "user", "content": prompt}]

    response = client.messages.create(
        model=SEARCH_MODEL,
        max_tokens=6000,   # stories + why-it-matters + mentions need headroom
        messages=messages,
        tools=tools,
    )

    # Multi-search turns can come back paused ("pause_turn"); the API expects
    # us to send the partial turn back so Claude can continue. Without this
    # loop, an unlucky run would silently return a half-finished digest.
    while response.stop_reason == "pause_turn":
        messages.append({"role": "assistant", "content": response.content})
        response = client.messages.create(
            model=SEARCH_MODEL,
            max_tokens=6000,
            messages=messages,
            tools=tools,
        )

    # The response can contain search blocks + text blocks. Join text blocks
    # with "" — web search splits a single sentence across many small text
    # blocks (each fragment carries its own citation), so joining with "\n"
    # snaps sentences apart mid-line. "" reconstructs them exactly as written.
    text = "".join(
        block.text for block in response.content if block.type == "text"
    ).strip()

    # Split at the sentinel: the strict Source:-line filter applies only to
    # the stories half (mentions legitimately have no Source line and would
    # be deleted by it). Missing sentinel -> no mentions this edition.
    if MENTIONS_SENTINEL in text:
        stories_part, mentions_part = text.split(MENTIONS_SENTINEL, 1)
    else:
        stories_part, mentions_part = text, ""

    stories = extract_stories(stories_part, cutoff=cutoff_date)
    mentions = strip_junk_lines(mentions_part)
    return stories, mentions


def extract_headlines(summaries: str) -> str:
    """Pull just the headline lines out, to remember for next week.

    Uses the same headline-shape test as the story filter, so it works
    whether or not the model put the emoji inside the bold markers. Runs on
    the filtered STORIES only — honorable mentions are plain "-" lines and
    never reach this function, so a near-miss this edition stays eligible to
    be a full story next edition.
    """
    return "\n".join(
        line.strip() for line in summaries.splitlines() if is_headline(line)
    )


def synthesize(summaries: str) -> str:
    """Call 2: compare the events and draw connections. No search needed here."""
    prompt = (
        f"Here are news summaries covering the past TWO WEEKS:\n\n{summaries}\n\n"
        f"Now write a final synthesis section. Compare the events: which ones "
        f"connect to or influence each other, what themes cut across them, and "
        f"what these two weeks mean taken as a whole. Be specific about the "
        f"links -- don't just restate the summaries.\n\n"
        f"Keep it TIGHT: 2-3 short paragraphs, roughly 150-220 words total. "
        f"Name the two or three strongest connections and stop -- no exhaustive "
        f"coverage of every story. Plain prose only: no markdown headers "
        f"(#, ##, ###), no bullet points, no title line."
    )

    response = client.messages.create(
        model=SYNTHESIS_MODEL,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )

    return "".join(
        block.text for block in response.content if block.type == "text"
    ).strip()


def format_for_telegram(text: str) -> str:
    """Strip markdown for clean plain-text display on the phone.

    The saved .md file keeps its formatting (it renders properly there);
    Telegram shows raw symbols, so we remove them: header #s go away,
    **bold** loses its asterisks, section headers become spaced caps.
    Emojis pass through untouched -- they render natively in Telegram.
    """
    out_lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            # "# News Digest -- date" -> "NEWS DIGEST -- date" etc.
            title = stripped.lstrip("#").strip()
            out_lines.append(title.upper())
        else:
            out_lines.append(line.replace("**", ""))
    return "\n".join(out_lines)


def send_to_telegram(text: str) -> None:
    """Send the digest to your Telegram chat, split into <4096-char chunks.

    Reads TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID from the environment.
    If either is missing, prints a note and skips. Network hiccups get ONE
    retry; a final failure prints a note instead of raising -- Telegram
    trouble must never crash the run, because in GitHub Actions a crash
    here would also skip the step that saves the de-dup memory. The digest
    is always safe on disk by the time this runs.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("(Telegram not configured -- skipping phone delivery.)")
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"

    # Telegram caps one message at 4096 characters. Split on blank lines
    # (paragraph/story boundaries) so no story is cut mid-sentence.
    LIMIT = 4000  # a little under the cap, for safety
    chunks = []
    current = ""
    for paragraph in text.split("\n\n"):
        candidate = (current + "\n\n" + paragraph) if current else paragraph
        if len(candidate) > LIMIT:
            if current:
                chunks.append(current)
            # A single paragraph longer than the limit gets hard-split.
            while len(paragraph) > LIMIT:
                chunks.append(paragraph[:LIMIT])
                paragraph = paragraph[LIMIT:]
            current = paragraph
        else:
            current = candidate
    if current:
        chunks.append(current)

    for i, chunk in enumerate(chunks, 1):
        sent = False
        for attempt in (1, 2):   # one retry on network trouble
            try:
                resp = requests.post(
                    url,
                    data={"chat_id": chat_id, "text": chunk},
                    # 60s: one observed timeout at 30s (2026-07-06). Longer
                    # would slow down noticing a real outage; 60 absorbs a
                    # slow moment.
                    timeout=60,
                )
                if resp.status_code == 200:
                    sent = True
                    break
                print(f"(Telegram send failed on part {i}/{len(chunks)}: "
                      f"{resp.status_code} {resp.text[:200]})")
                return
            except Exception as e:
                print(f"(Telegram network error on part {i}/{len(chunks)}, "
                      f"attempt {attempt}: {e})")
                if attempt == 1:
                    time.sleep(5)
        if not sent:
            print("(Giving up on Telegram delivery -- the digest is still "
                  "saved to its file.)")
            return
    print(f"Sent to Telegram in {len(chunks)} message(s).")


def main() -> None:
    today_d = date.today()
    forced = "--force" in sys.argv
    if not forced and not is_on_week(today_d):
        print(
            f"Off-week: next scheduled run is "
            f"{(BIWEEKLY_ANCHOR + timedelta(days=((today_d - BIWEEKLY_ANCHOR).days // 14 + 1) * 14)).isoformat()}. "
            f"Use --force to run anyway."
        )
        return

    last_headlines = load_last_headlines()

    print("Gathering and summarizing this week's news...\n")
    summaries, mentions = gather_and_summarize(last_headlines)

    # Guard: if filtering left nothing, stop BEFORE paying for a synthesis
    # of nothing. Exit non-zero so a cloud run shows a red X instead of a
    # deceptive green, and so the old de-dup memory is left untouched.
    if not summaries.strip():
        print("ERROR: no stories survived filtering -- aborting without "
              "synthesis. The previous headlines memory is preserved.")
        sys.exit(1)

    print("Writing the comparative synthesis...\n")
    synthesis = synthesize(summaries)

    today = date.today().isoformat()
    # Order: ranked stories -> synthesis (the capstone) -> honorable mentions
    # as an appendix. The mentions section only appears if the model made one.
    digest = (
        f"# News Digest -- {today}\n\n"
        f"## Stories\n\n{summaries}\n\n"
        f"## Synthesis\n\n{synthesis}\n"
    )
    if mentions:
        digest += f"\n## Honorable Mentions\n\n{mentions}\n"

    # Forced runs are test runs: show the full digest in the terminal so it
    # can be read (and copy-pasted for feedback) without opening the file.
    # Scheduled runs stay quiet -- status lines only.
    if forced:
        print(digest)

    # Save a dated file next to the script.
    out_path = os.path.join(HERE, f"digest-{today}.md")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(digest)

    # Remember this week's headlines so next week can skip repeats -- but
    # NEVER overwrite the memory with nothing. If headline extraction came
    # up empty (severe format drift), keeping last edition's memory beats
    # silently wiping it (which happened once, on 2026-07-06).
    headlines = extract_headlines(summaries)
    if headlines:
        save_headlines(headlines)
    else:
        print("(Warning: no headlines extracted -- keeping the previous "
              "de-dup memory instead of wiping it.)")

    # Deliver to your phone (markdown stripped for clean plain text).
    send_to_telegram(format_for_telegram(digest))

    print(f"\nSaved to {out_path}")


if __name__ == "__main__":
    main()
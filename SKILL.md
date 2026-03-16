---
name: ai-talks-monitor
description: Monitors YouTube for new long-form original talks and interviews with AI thought leaders. Use this skill whenever the user asks about new AI talks or interviews, says things like "any new Sam Altman talks?", "check YouTube for new AI interviews", "run the talks monitor", "who's on the watchlist?", "add [name] to the watchlist", or wants to manage, schedule, or configure the AI talks monitor. Also use when the user wants to set up automated YouTube monitoring, integrate AI talk discovery with Telegram, Feishu, OpenClaw, or an RSS reader, or enable topic-based searches for conference keynotes or unknown speakers. Filters out derivative content (reactions, summaries, explainers) using LLM classification. Writes RSS 2.0 feeds and optionally sends a chat notification.
metadata:
  {
    "openclaw":
      {
        "emoji": "🎙",
        "requires":
          {
            "bins": ["python3"],
            "env": ["YOUTUBE_API_KEY"],
          },
      },
  }
---

# AI Talks Monitor

Watches YouTube for new long-form original talks and interviews featuring AI thought leaders.
The script handles YouTube search and state; you (Claude) handle the classification step.

Supports three modes:
- **Person watchlist** (default): tracks specific people across any channel they appear on
- **Topic search** (opt-in): catches lab insiders and researchers not on the person watchlist, using org-confirmation to filter noise
- **Channel watchlist** (opt-in): monitors specific high-signal channels for any AI talk

## Setup

1. Set environment variables:
   - `YOUTUBE_API_KEY` — YouTube Data API v3 key (free; get from Google Cloud Console)
   - `TELEGRAM_BOT_TOKEN` — Telegram bot token (optional; only for `notifications.backend: "telegram"`)
   - `TELEGRAM_CHAT_ID` — Telegram chat/channel ID to send notifications to (optional; only for `notifications.backend: "telegram"`)
   - `AI_TALKS_FEEDS_REPO` — absolute path to a local git repo (optional; if set, `--commit` copies `ai_talks.xml` there and pushes automatically)

2. Install Python dependencies:
   ```
   pip install requests pyyaml
   ```

## Usage

### Check for new talks now

This is a two-phase process: the script fetches candidates, you classify them, then the script commits the ones you accept.

**Phase 1 — fetch candidates from YouTube:**
```bash
python3 SKILL_DIR/scripts/check_talks.py --fetch-candidates
```
Use `--lookback-days N` to override the default rolling window — helpful for backfilling after a long gap:
```bash
python3 SKILL_DIR/scripts/check_talks.py --fetch-candidates --lookback-days 30
```
Use `--limit N` to process only the first N entries per category — useful for quick tests without running the full watchlist:
```bash
python3 SKILL_DIR/scripts/check_talks.py --fetch-candidates --limit 2
```

**Phase 2 — classify and translate (your job):**

First, read `SKILL_DIR/output/state.json` (if it exists) to load the `items` list — the recently committed talks. You'll need this for cross-run deduplication below.

Then read `SKILL_DIR/output/candidates.json`. For each video, decide based on its label:
- **Person watchlist entries** (`label` = a person's name): Is the named person a direct guest/participant in this video, or just being discussed (reaction, summary, news report about them)?
- **Channel entries** (`label` = "Channel: X"): Is this a genuine AI-related talk or interview? These come from curated high-signal channels, so the bar is: does it feature an AI researcher, founder, or thought leader speaking in their own words?
- **Topic search entries** (`label` = "Topic: X"): Two checks must both pass:
  1. Is this a genuine first-person talk or interview (not a third-party explainer/commentary)?
  2. If the topic has `require_org_confirmation: true` in config.yaml: can you confirm from the title or description that the speaker is affiliated with one of the `trusted_orgs`? If the description is absent or too vague to confirm affiliation, reject.
  Reject if either check fails.

**Same-event deduplication — across candidates:**
Multiple candidates may be different channels reuploading the same underlying event (same person, same venue/date, nearly identical descriptions). Accept only one. Prefer: original/official channel > named news outlet > generic news reupload. Reject the rest.

**Same-event deduplication — against state.json:**
If a candidate appears to be the same event as a talk already in `output/state.json items` (same person, same event, similar timeframe), reject it even though the video ID is new.

For each candidate, assign it to one of three buckets:
- **Accept**: genuine first-person talk, enough information to be confident.
- **Definitive reject**: clearly derivative, duplicate, or confirmed irrelevant — enough information to be certain it should never appear again.
- **Uncertain**: not enough information to decide (e.g. description is empty or too vague, title is ambiguous). Leave these in neither bucket — they will resurface on the next run and can be re-evaluated once a description is available.

For each **accepted** video, also generate:
- `description_clean`: a cleaned version of the available video description in its original language
- `title_zh`: a concise Chinese translation of the title (not a literal word-for-word translation — make it natural and informative)
- `description_zh`: a Chinese translation of `description_clean`, written for a Chinese-speaking audience

For `description_clean`:
- Base it only on the available metadata you actually have: title, channel, and video description
- Do not pretend you watched the full video
- Remove unhelpful links, sponsor boilerplate, social handles, and repetitive calls to action
- Keep useful structure such as chapter/timestamp topic breakdowns when they help readers understand the talk
- If the source description is sparse, write a short, conservative cleaned blurb in the original language rather than inventing details

For `description_zh`:
- Translate `description_clean` faithfully into natural Chinese
- Do not add any information not present in the metadata

**Important:** `output/accepted.json` must be valid JSON. Use Chinese-style quotation marks `「」` instead of ASCII `"` inside Chinese text to avoid breaking the JSON string delimiters.

Write `SKILL_DIR/output/accepted.json` with this structure:
```json
{
  "accepted": [
    {
      "id": "VIDEO_ID",
      "description_clean": "OpenAI CEO Sam Altman and Amazon CEO Andy Jassy discuss OpenAI's latest funding, AI infrastructure expansion, and where the industry is heading.",
      "title_zh": "山姆·奥特曼与安迪·贾西对话：OpenAI融资与AI未来",
      "description_zh": "OpenAI CEO 山姆·奥特曼与亚马逊 CEO 安迪·贾西在 CNBC 节目中讨论 OpenAI 最新融资、AI 基础设施扩张以及行业未来走向。"
    }
  ],
  "rejected": ["VIDEO_ID_1", "VIDEO_ID_2"]
}
```

IDs in neither `accepted` nor `rejected` are left unmarked in state and will reappear on the next run.

**Phase 3 — commit accepted videos:**
```bash
python3 SKILL_DIR/scripts/check_talks.py --commit-file SKILL_DIR/output/accepted.json
```
This writes both `ai_talks.xml` (English) and `ai_talks_zh.xml` (Chinese titles and translated descriptions), updates `state.json`, sends a notification if configured, and pushes to the feeds repo if `AI_TALKS_FEEDS_REPO` is set.

Note: `--commit-file` will refuse to publish any item missing a `published_at` date (can happen with yt-dlp fallback). If this occurs, re-run `--fetch-candidates` with `YOUTUBE_API_KEY` set, or set `ytdlp_search.cookies_from_browser` in `config.yaml`, then retry.

Use `--dry-run` to preview without writing files or updating state:
```bash
python3 SKILL_DIR/scripts/check_talks.py --commit-file SKILL_DIR/output/accepted.json --dry-run
```

After committing, report what was accepted to the user.

### Add a person to the watchlist

Edit `SKILL_DIR/config.yaml` and add an entry under `thought_leaders`:
```yaml
- name: "[Full Name]"
  search_query: '"[Full Name]" interview'
```

For bilingual subjects, add a second entry with their native name:
```yaml
- name: "Fei-Fei Li (Chinese)"
  search_query: '"李飞飞" 访谈'
```

Confirm the addition to the user.

### Remove from watchlist

Edit `SKILL_DIR/config.yaml` and remove the relevant entry from `thought_leaders`.

### Enable topic-based searches

Edit `SKILL_DIR/config.yaml`, set `topics.enabled: true`, and add entries under `topics.searches`:
```yaml
topics:
  enabled: true
  searches:
    - name: "AI Safety"
      search_query: '"AI safety" interview OR talk OR podcast'
      min_duration_minutes: 30
```

Topic searches are noisier than person searches — a higher `min_duration_minutes` helps.

### Show current watchlist

Read and display `thought_leaders`, `channels.list` (if enabled), and `topics.searches` (if enabled) from `SKILL_DIR/config.yaml`.

### Adjust settings
- `min_duration_minutes` — minimum video length to consider (default: 20). Raise to cut more noise.
- `lookback_days` — rolling search window in days (default: 5). Every run searches this far back.
- `ytdlp_search.enabled` — set to `true` to enable yt-dlp as a fallback when `YOUTUBE_API_KEY` is not set (default: `false`). yt-dlp has known limitations: approximate date filtering, descriptions require per-video fetches that often hit bot-checks. Prefer setting `YOUTUBE_API_KEY`.
- `ytdlp_search.use_this_month_filter` — when using yt-dlp fallback, optionally apply a YouTube-side "This month" prefilter before local date filtering (default: `true`). Reduces stale results but may also reduce recall; local date filtering remains the authoritative cutoff either way.
- `ytdlp_search.cookies_from_browser` — browser to pull cookies from when using yt-dlp (`chrome`, `firefox`, or `safari`; default: `""`). Set this if yt-dlp hits YouTube bot-checks or if you want full video descriptions (descriptions require per-video fetches that fail without auth).
- `notifications.backend` — `telegram`, `openclaw`, or `none`
- `notifications.openclaw.channel` / `notifications.openclaw.target` / `notifications.openclaw.account` — used when routing notifications through OpenClaw, including Feishu support

## Automated Daily Check

The fetch step can be scheduled unattended — `--fetch-candidates` only writes `candidates.json` and never modifies state, so it's safe to run on a timer. The commit step happens via the skill when you review.

**macOS (launchd)** — runs even after reboots:
```bash
# Create ~/Library/LaunchAgents/com.openclaw.ai-talks-monitor.plist
# then: launchctl load ~/Library/LaunchAgents/com.openclaw.ai-talks-monitor.plist
```
```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.openclaw.ai-talks-monitor</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>SKILL_DIR/scripts/check_talks.py</string>
    <string>--fetch-candidates</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>9</integer><key>Minute</key><integer>0</integer></dict>
  <key>EnvironmentVariables</key>
  <dict>
    <key>YOUTUBE_API_KEY</key><string>YOUR_KEY_HERE</string>
  </dict>
</dict>
</plist>
```

**Linux/cron** — runs daily at 9am:
```
0 9 * * * YOUTUBE_API_KEY=your_key python3 SKILL_DIR/scripts/check_talks.py --fetch-candidates
```

## How it works

1. **YouTube search** (`--fetch-candidates`): Runs three source types — person watchlist (keyword search via API/yt-dlp), channel watchlist (channel feed via API with `channelId` or yt-dlp with `@handle`), topic searches (keyword search). All use a rolling `lookback_days` window.
2. **Heuristic pre-filter** (`--fetch-candidates`): Rejects titles containing "reaction", "summary", "explained", "breakdown", "解读", "总结", "面试", etc. Writes survivors to `output/candidates.json`
3. **Classification (you)**: Read `output/state.json` + `output/candidates.json`; decide which are genuine original talks, deduplicating same-event uploads both within candidates and against already-committed items
4. **Commit** (`--commit ID ...`): Builds feeds with a rolling 30-day RSS window, updates `state.json`, sends a notification if configured

## TrendRadar integration

Both feeds are written on every `--commit-file` run. Add either or both to TrendRadar's `config/config.yaml`:
```yaml
rss:
  feeds:
    - id: "ai-talks"
      name: "AI Thought Leader Talks"
      url: "file:///path/to/skills/ai-talks-monitor/ai_talks.xml"
      max_age_days: 30
      enabled: true
    - id: "ai-talks-zh"
      name: "AI大咖讲座精选"
      url: "file:///path/to/skills/ai-talks-monitor/ai_talks_zh.xml"
      max_age_days: 30
      enabled: true
```

## Files

- `SKILL.md` — this file
- `scripts/check_talks.py` — main script: `--fetch-candidates` fetches to output/candidates.json; `--commit` writes RSS + state
- `output/candidates.json` — auto-written by --fetch-candidates; read by you for classification
- `output/accepted.json` — written by you during Phase 2 (IDs + description_clean + title_zh + description_zh); input to --commit-file
- `config.yaml` — watchlist and settings (edit this to customize)
- `output/state.json` — auto-managed: seen video IDs, last_checked timestamp, rolling item list
- `output/ai_talks.xml` — auto-generated RSS 2.0 feed (English)
- `output/ai_talks_zh.xml` — auto-generated RSS 2.0 feed (Chinese titles and translated descriptions)

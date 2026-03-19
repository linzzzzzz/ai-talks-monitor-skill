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
The script handles YouTube search and state; you handle the classification step.

Supports three modes:
- **Person watchlist** (default): tracks specific people across any channel they appear on
- **Topic search** (opt-in): catches lab insiders and researchers not on the person watchlist, using org-confirmation to filter noise
- **Channel watchlist** (opt-in): monitors specific high-signal channels for any AI talk

## Setup

1. Set environment variables:
   - `YOUTUBE_API_KEY` — YouTube Data API v3 key (free; get from Google Cloud Console)
   - `TELEGRAM_BOT_TOKEN` — Telegram bot token (optional; only for `notifications.backend: "telegram"`)
   - `TELEGRAM_CHAT_ID` — Telegram chat/channel ID to send notifications to (optional; only for `notifications.backend: "telegram"`)
   - `AI_TALKS_FEEDS_REPO` — absolute path to a local git repo (optional; if set, `--commit-file` copies RSS feeds there and pushes automatically)

2. Install Python dependencies:
   ```
   pip install requests pyyaml
   ```

## Usage

### Check for new talks now

This is a four-phase process: the script fetches candidates, you classify them in smaller batches, the script prepares an accepted-items draft, then you enrich only those accepted items before commit.

**Phase 1 — fetch candidates from YouTube:**
```bash
python3 SKILL_DIR/scripts/check_talks.py --fetch-candidates
```
Do NOT add `--lookback-days` unless the user explicitly asks to backfill a longer period. The default rolling window in `config.yaml` is correct for normal runs.

**Phase 2 — classify candidates (subagent parallel):**

The `--fetch-candidates` output ends with a **CLASSIFICATION PLAN** listing candidate files grouped by category. Spawn up to 3 subagents in parallel — one each for people, topics, and channels.

- **OpenClaw:** use `sessions_spawn` (with `runTimeoutSeconds: 300`)
- **Claude Code:** use the `Agent` tool (with `model: "sonnet"`, `run_in_background: true`)

**Do NOT read any `candidates_*.json` files yourself.** The subagents will read them. You only need the reference files below.

**Step 1 — read reference context.** Before spawning, read these files yourself so you can include their content in each subagent task:
- `SKILL_DIR/CLASSIFY.md` — classification rules
- `SKILL_DIR/output/state.json` (if it exists) — `items` array for cross-run deduplication
- All ephemeral files (candidates, reviews, enrichment, accepted) live under `output/scratch/`, which is wiped at the start of each `--fetch-candidates` run.
- `SKILL_DIR/config.yaml` is NOT needed — the `org` field is already baked into each topic candidate

**Step 2 — spawn one subagent per category.** For each category listed in the CLASSIFICATION PLAN, spawn a subagent with this task:

```
Classify AI talk candidates for the "{category}" category. Read ALL candidate files listed below, apply the classification rules, and write the review file.

CANDIDATE FILES (read every file — each is ≤15 items):
{list all chunk files for this category, e.g.:
  - SKILL_DIR/output/scratch/candidates_topics_1.json
  - SKILL_DIR/output/scratch/candidates_topics_2.json
  - SKILL_DIR/output/scratch/candidates_topics_3.json
  - SKILL_DIR/output/scratch/candidates_topics_4.json}

REVIEW FILE: SKILL_DIR/output/scratch/review_{category}.json

CLASSIFICATION RULES:
{paste full CLASSIFY.md content here}

STATE ITEMS (for deduplication):
{paste state.json items array here, or "none" if empty}

OUTPUT FORMAT — write the review file as valid JSON:
{
  "source": "scratch/candidates_{category}",
  "candidates_reviewed": <total items across ALL files above>,
  "accepted": [
    {"id": "VIDEO_ID", "reason": "one sentence: who the speaker is, format, key topic"}
  ],
  "rejected": ["VIDEO_ID_1", "VIDEO_ID_2"]
}

RULES:
- Read ALL candidate files listed above. Do not skip any file.
- Every candidate must appear in either accepted or rejected.
- The "reason" field is required for each accepted item.
- candidates_reviewed must equal the total items across all files.
```

**Step 3 — wait and merge.** After all subagents complete, read each `output/scratch/review_{category}.json` file and merge into `SKILL_DIR/output/scratch/review.json`:
```json
{
  "accepted": [{"id": "VIDEO_ID_A", "reason": "..."}, {"id": "VIDEO_ID_B", "reason": "..."}],
  "rejected": ["VIDEO_ID_1", "VIDEO_ID_2", "VIDEO_ID_3"]
}
```

IDs in neither `accepted` nor `rejected` are left unmarked in state and will reappear on the next run.

**Phase 3 — prepare accepted items for enrichment:**
```bash
python3 SKILL_DIR/scripts/check_talks.py --prepare-accepted SKILL_DIR/output/scratch/review.json
```

This writes `SKILL_DIR/output/scratch/accepted.json` with only the accepted candidates plus their original metadata.

Now read `output/scratch/accepted.json` and generate an enrichment file. For each accepted video, write **only** the generated fields to `SKILL_DIR/output/scratch/enrichment.json`:
```json
[
  {
    "id": "VIDEO_ID_A",
    "description_clean": "...",
    "title_zh": "...",
    "description_zh": "..."
  }
]
```

Field guidelines:
- `description_clean`: a cleaned version of the video description in its original language. Base it only on available metadata (title, channel, description). Remove links, sponsor boilerplate, social handles, calls to action. Keep useful structure like chapter/timestamp breakdowns. If the source description is sparse, write a short conservative blurb rather than inventing details.
- `title_zh`: a concise, natural Chinese translation of the title (not literal word-for-word)
- `description_zh`: a Chinese translation of `description_clean`, written for a Chinese-speaking audience. Do not add information not present in the metadata.

**Important:** Use Chinese-style quotation marks `「」` instead of ASCII `"` inside Chinese text to avoid breaking JSON.

Then apply the enrichment:
```bash
python3 SKILL_DIR/scripts/check_talks.py --apply-enrichment SKILL_DIR/output/scratch/enrichment.json
```
This merges your generated fields into `accepted.json`. It will reject the enrichment if any accepted items are missing or have empty fields.

**Phase 4 — commit accepted videos:**

You MUST use `--commit-file` (not `--commit`). Only `--commit-file` publishes the enriched Chinese translations and cleaned descriptions.
```bash
python3 SKILL_DIR/scripts/check_talks.py --commit-file SKILL_DIR/output/scratch/accepted.json
```
This writes both `ai_talks.xml` (English) and `ai_talks_zh.xml` (Chinese titles and translated descriptions), updates `state.json`, sends a notification if configured, and pushes to the feeds repo if `AI_TALKS_FEEDS_REPO` is set.

Note: `--commit-file` will refuse to publish any item missing a `published_at` date (can happen with yt-dlp metadata fallback). If this occurs, re-run `--fetch-candidates` with `YOUTUBE_API_KEY` set and `backends.metadata: youtube_api`, or set `ytdlp_search.cookies_from_browser` in `config.yaml`, then retry.

Use `--dry-run` to preview without writing files or updating state:
```bash
python3 SKILL_DIR/scripts/check_talks.py --commit-file SKILL_DIR/output/scratch/accepted.json --dry-run
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
- `backends.search` — `auto`, `youtube_api`, or `yt_dlp`. Use `yt_dlp` to save quota on discovery searches.
- `backends.metadata` — `auto`, `youtube_api`, or `yt_dlp`. A good hybrid setup is `search: yt_dlp` plus `metadata: youtube_api`.
- `ytdlp_search.use_this_month_filter` — when `backends.search` uses yt-dlp, optionally apply a YouTube-side "This month" prefilter before local date filtering (default: `true`). Reduces stale results but may also reduce recall; local date filtering remains the authoritative cutoff either way.
- `ytdlp_search.cookies_from_browser` — browser to pull cookies from when a yt-dlp backend is used (`chrome`, `firefox`, or `safari`; default: `""`). Set this if yt-dlp hits YouTube bot-checks or if you want yt-dlp metadata fallback to fetch full descriptions.
- `notifications.backend` — `telegram`, `openclaw`, or `none`
- `notifications.openclaw.channel` / `notifications.openclaw.target` / `notifications.openclaw.account` — used when routing notifications through OpenClaw, including Feishu support

## Automated Daily Check

The fetch step can be scheduled unattended — `--fetch-candidates` only writes candidate files and never modifies state, so it's safe to run on a timer. The review/prepare/commit steps happen via the skill when you review.

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
2. **Heuristic pre-filter** (`--fetch-candidates`): Rejects titles containing "reaction", "summary", "explained", "breakdown", "解读", "总结", "面试", etc. Wipes `output/scratch/`, then writes survivors to grouped candidate files there
3. **Classification (you)**: Read `output/state.json` + the grouped candidate files in `output/scratch/`; decide which are genuine original talks, deduplicating same-event uploads both within candidates and against already-committed items. Write decisions to `output/scratch/review.json`
4. **Accepted-item enrichment (you)**: Run `--prepare-accepted`, write `output/scratch/enrichment.json` with `description_clean`, `title_zh`, `description_zh`, then run `--apply-enrichment`
5. **Commit** (`--commit-file accepted.json`): Builds feeds with a rolling 30-day RSS window, updates `state.json`, sends a notification if configured

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
- `scripts/check_talks.py` — main script: `--fetch-candidates`, `--prepare-accepted`, `--apply-enrichment`, `--commit-file`
- `config.yaml` — watchlist and settings (edit this to customize)
- `output/state.json` — persistent: seen video IDs, last_checked timestamp, rolling item list
- `output/ai_talks.xml` — persistent: auto-generated RSS 2.0 feed (English)
- `output/ai_talks_zh.xml` — persistent: auto-generated RSS 2.0 feed (Chinese titles and translated descriptions)
- `output/scratch/` — ephemeral per-run directory, wiped at the start of each `--fetch-candidates`:
  - `candidates.json` — full candidate dump
  - `candidates_people[_N].json` / `candidates_topics[_N].json` / `candidates_channels[_N].json` — chunked candidate files (≤15 items each)
  - `review.json` — merged classification decisions
  - `review_{category}.json` — per-subagent classification output
  - `enrichment.json` — LLM-generated fields (description_clean, title_zh, description_zh)
  - `accepted.json` — built by `--prepare-accepted`, enriched by `--apply-enrichment`, input to `--commit-file`

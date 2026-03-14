# AI Talks Monitor

A Claude Code skill that watches YouTube for new long-form original talks and interviews featuring AI thought leaders, then delivers them as an RSS feed.

**Live example feed:** [linzzzzzz.github.io/feeds/ai_talks.xml](https://linzzzzzz.github.io/feeds/ai_talks.xml)

---

## The problem

YouTube surfaces endless reactions, summaries, and explainers about AI leaders — but buries the original source material. Finding a new Sam Altman interview means scrolling past dozens of videos *about* the interview.

This tool solves that. It searches YouTube, applies heuristic filters, then lets Claude classify the survivors — keeping only genuine first-person talks and interviews.

## How it works

```
config.yaml
    │
    ▼
YouTube search (API key or yt-dlp fallback)
    │
    ▼
Heuristic pre-filter  ← rejects "reaction", "summary", "breakdown", CJK equivalents, etc.
    │
    ▼
candidates.json  ← Claude reads and classifies these
    │
    ▼
--commit ID ...  ← only accepted IDs
    │
    ▼
ai_talks.xml  +  optional Slack notification
```

The workflow is split into two phases so the fetch step can run unattended on a schedule, and Claude only enters the loop for the classification step.

## Installation

### 1. Install as a Claude Code skill

```bash
cp -r ai-talks-monitor ~/.claude/skills/ai-talks-monitor
```

Or clone directly:

```bash
git clone https://github.com/linzzzzzz/ai-talks-monitor ~/.claude/skills/ai-talks-monitor
```

### 2. Install Python dependencies

```bash
pip install requests pyyaml
```

### 3. Set environment variables

| Variable | Required | Purpose |
|----------|----------|---------|
| `YOUTUBE_API_KEY` | No | YouTube Data API v3. Free — get one from [Google Cloud Console](https://console.cloud.google.com). If absent, falls back to yt-dlp. |
| `YTDLP_COOKIES_FROM_BROWSER` | No | Browser name (`chrome`, `firefox`, `safari`). Enables yt-dlp to fetch full video descriptions without hitting bot-checks. |
| `TELEGRAM_BOT_TOKEN` | No | Telegram bot token. Get one from [@BotFather](https://t.me/botfather). Notifications are sent on `--commit` when both token and chat ID are set. |
| `TELEGRAM_CHAT_ID` | No | Telegram chat or channel ID to send notifications to. |
| `AI_TALKS_FEEDS_REPO` | No | Absolute path to a local git repo (e.g. `~/feeds`). If set, `--commit` copies `ai_talks.xml` there and runs `git push` automatically — keeping your GitHub Pages feed up to date. |

**yt-dlp fallback:** If `YOUTUBE_API_KEY` is not set, the script uses [yt-dlp](https://github.com/yt-dlp/yt-dlp) to search YouTube without an API key. Install it with `pip install yt-dlp`. Without `YTDLP_COOKIES_FROM_BROWSER`, video descriptions will be empty (YouTube requires login for per-video metadata).
You can optionally enable a coarse YouTube-side `This month` publish-date filter for yt-dlp fallback searches with `config.yaml`:

```yaml
ytdlp_search:
  use_this_month_filter: true
```

## Quick start

**Step 1 — fetch candidates:**

```bash
python3 ~/.claude/skills/ai-talks-monitor/scripts/check_talks.py --fetch-candidates
```

On first run, add `--lookback-days 30` to search further back:

```bash
python3 ~/.claude/skills/ai-talks-monitor/scripts/check_talks.py --fetch-candidates --lookback-days 30
```

**Step 2 — classify (Claude's job):**

In Claude Code, invoke the skill. Claude reads `candidates.json` and decides which videos are genuine original talks vs derivative content.

**Step 3 — commit accepted videos:**

```bash
python3 ~/.claude/skills/ai-talks-monitor/scripts/check_talks.py --commit VIDEO_ID_1 VIDEO_ID_2 ...
```

This writes `ai_talks.xml`, updates `state.json`, and posts to Slack if configured.

## Using with Claude Code

Once installed as a skill, you can interact with it conversationally:

> "Check for new AI talks"

> "Add Demis Hassabis to the watchlist"

> "Show me who's being monitored"

Claude handles the full workflow: fetching, classifying, and committing — guided by the instructions in `SKILL.md`.

## Configuration

Edit `config.yaml` to customize the watchlist and settings:

```yaml
thought_leaders:
  - name: "Sam Altman"
    search_query: '"Sam Altman" interview'
  - name: "Yann LeCun"
    search_query: '"Yann LeCun" interview'

# Optional topic searches (disabled by default — noisier than person searches)
topics:
  enabled: false
  searches:
    - name: "AI Safety"
      search_query: '"AI safety" interview OR talk OR podcast'
      min_duration_minutes: 30

min_duration_minutes: 20  # skip videos shorter than this
lookback_days: 7          # search window on first run
```

**Person watchlist:** Each person gets an exact YouTube search. Claude verifies the person is an actual participant, not just the subject of a reaction or news video.

**Topic searches:** Broader and noisier. Useful for catching talks from people you don't know yet, or conference keynotes. A higher `min_duration_minutes` helps cut noise.

**Bilingual subjects:** Add a second entry with the native name:
```yaml
- name: "Fei-Fei Li (Chinese)"
  search_query: '"李飞飞" 访谈'
```

## RSS feed

`--commit` writes a standard RSS 2.0 file (`ai_talks.xml`) with a rolling 30-day window.

**Serve it via GitHub Pages:**

```bash
mkdir ~/feeds && cd ~/feeds
git init && git remote add origin https://github.com/YOURNAME/feeds
cp /path/to/ai_talks.xml .
git add ai_talks.xml && git commit -m "feed"
git push -u origin main
# Enable Pages in repo Settings → Pages → Deploy from main branch
```

Your feed will be live at `https://YOURNAME.github.io/feeds/ai_talks.xml`.

**Add to TrendRadar:**

```yaml
rss:
  feeds:
    - id: "ai-talks"
      name: "AI Thought Leader Talks"
      url: "https://YOURNAME.github.io/feeds/ai_talks.xml"
      max_age_days: 30
      enabled: true
```

## Automated daily fetch

The `--fetch-candidates` step is safe to schedule unattended — it only writes `candidates.json` and never modifies state.

**macOS (launchd):**

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.openclaw.ai-talks-monitor</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>/Users/YOU/.claude/skills/ai-talks-monitor/scripts/check_talks.py</string>
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

Save to `~/Library/LaunchAgents/com.openclaw.ai-talks-monitor.plist`, then:

```bash
launchctl load ~/Library/LaunchAgents/com.openclaw.ai-talks-monitor.plist
```

**Linux (cron):**

```
0 9 * * * YOUTUBE_API_KEY=your_key python3 ~/.claude/skills/ai-talks-monitor/scripts/check_talks.py --fetch-candidates
```

## Files

| File | Purpose |
|------|---------|
| `scripts/check_talks.py` | Main script |
| `config.yaml` | Watchlist and settings |
| `SKILL.md` | Claude Code skill instructions |
| `candidates.json` | Auto-written by `--fetch-candidates`; read by Claude for classification |
| `state.json` | Auto-managed: seen video IDs, last_checked timestamp, rolling RSS items |
| `ai_talks.xml` | Auto-generated RSS 2.0 feed |

Delete `state.json` to trigger a full re-scan on the next run.

## CLI reference

```
--fetch-candidates       Search YouTube, apply heuristic filter, write candidates.json
--commit ID [ID ...]     Accept these video IDs, write RSS feed, update state

--dry-run                Preview without writing files, updating state, or sending notifications
--lookback-days N        Override last_checked; search N days back
```

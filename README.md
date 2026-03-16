# AI Talks Monitor

A Claude Code skill that watches YouTube for new long-form original talks and interviews featuring AI thought leaders, then delivers them as RSS feeds in English and Chinese.

**Live example feeds:**
- English: [linzzzzzz.github.io/feeds/ai_talks.xml](https://linzzzzzz.github.io/feeds/ai_talks.xml)
- Chinese: [linzzzzzz.github.io/feeds/ai_talks_zh.xml](https://linzzzzzz.github.io/feeds/ai_talks_zh.xml)

---

## The problem

YouTube surfaces endless reactions, summaries, and explainers about AI leaders — but buries the original source material. Finding a new Sam Altman interview means scrolling past dozens of videos *about* the interview.

This tool solves that. It searches YouTube, applies heuristic filters, then lets Claude classify the survivors — keeping only genuine first-person talks and interviews. For each accepted talk, Claude also generates a Chinese title and summary, making the feed useful to a bilingual audience.

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
output/candidates.json  ← Claude reads, classifies, and translates
    │
    ▼
output/accepted.json  ← Claude writes: accepted IDs + cleaned source descriptions + Chinese titles/descriptions + definitive rejects
    │
    ▼
--commit-file output/accepted.json
    │
    ▼
output/ai_talks.xml (English) + output/ai_talks_zh.xml (Chinese)  +  optional notification (Telegram or OpenClaw)
```

The workflow is split into phases so the fetch step can run unattended on a schedule, and Claude only enters the loop for classification and translation.

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
| `YOUTUBE_API_KEY` | Recommended | YouTube Data API v3. Free — get one from [Google Cloud Console](https://console.cloud.google.com). If absent, falls back to yt-dlp. |
| `TELEGRAM_BOT_TOKEN` | No | Telegram bot token. Only needed when `notifications.backend: "telegram"`. |
| `TELEGRAM_CHAT_ID` | No | Telegram chat or channel ID. Only needed when `notifications.backend: "telegram"`. |
| `AI_TALKS_FEEDS_REPO` | No | Absolute path to a local git repo (e.g. `~/feeds`). If set, both `ai_talks.xml` and `ai_talks_zh.xml` are copied there and pushed automatically after each commit — keeping a GitHub Pages feed up to date. |

If you already run OpenClaw and want to reuse its Telegram or Feishu channels, set `notifications.backend: "openclaw"` in `config.yaml` and configure `notifications.openclaw.channel/target` instead of using the raw Telegram env vars.

**yt-dlp fallback:** If `YOUTUBE_API_KEY` is not set, the script uses [yt-dlp](https://github.com/yt-dlp/yt-dlp) to search YouTube without an API key. Install it with `pip install yt-dlp`. To enable full video descriptions (requires authentication), set `ytdlp_search.cookies_from_browser: chrome` (or `firefox`/`safari`) in `config.yaml`.

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

In Claude Code, invoke the skill. Claude reads `output/candidates.json`, classifies each video (accept / definitive reject / uncertain), and writes `output/accepted.json` with a cleaned source-language description plus Chinese title/description for accepted videos.

**Step 3 — commit:**

```bash
python3 ~/.claude/skills/ai-talks-monitor/scripts/check_talks.py --commit-file ~/.claude/skills/ai-talks-monitor/output/accepted.json
```

This writes both `output/ai_talks.xml` (English) and `output/ai_talks_zh.xml` (Chinese), updates `output/state.json`, and sends a notification if configured.

## Using with Claude Code

Once installed as a skill, interact conversationally:

> "Check for new AI talks"

> "Add Demis Hassabis to the watchlist"

> "Show me who's being monitored"

Claude handles the full workflow: fetching, classifying, translating, and committing — guided by the instructions in `SKILL.md`.

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
lookback_days: 7          # rolling search window in days
```

Notification delivery is configured separately:

```yaml
notifications:
  backend: "openclaw"
  openclaw:
    binary: "openclaw"
    channel: "telegram"  # or "feishu"
    target: "123456789"  # Telegram chat ID, @username, or Feishu target like feishu:group:oc_xxx
    account: ""          # optional OpenClaw account id
```

Use `backend: "telegram"` to keep the current direct Telegram Bot API path, or `backend: "none"` to disable notifications.

**Person watchlist:** Each person gets an exact YouTube search. Claude verifies the person is an actual participant, not just the subject of a reaction or news video.

**Topic searches:** Broader and noisier. Useful for catching talks from people you don't know yet, or conference keynotes. A higher `min_duration_minutes` helps cut noise.

**Bilingual subjects:** Add a second entry with the native name:
```yaml
- name: "Fei-Fei Li (Chinese)"
  search_query: '"李飞飞" 访谈'
```

## RSS feeds

Each commit produces two standard RSS 2.0 files with a rolling 30-day window:

| Feed | File | Contents |
|------|------|----------|
| English | `output/ai_talks.xml` | Original titles and descriptions |
| Chinese | `output/ai_talks_zh.xml` | Claude-translated titles and cleaned descriptions |

### Serving via GitHub Pages

```bash
mkdir ~/feeds && cd ~/feeds
git init && git remote add origin https://github.com/YOURNAME/feeds
cp /path/to/output/ai_talks.xml /path/to/output/ai_talks_zh.xml .
git add ai_talks.xml ai_talks_zh.xml && git commit -m "feed"
git push -u origin main
# Enable Pages in repo Settings → Pages → Deploy from main branch
```

Your feeds will be live at:
- `https://YOURNAME.github.io/feeds/ai_talks.xml`
- `https://YOURNAME.github.io/feeds/ai_talks_zh.xml`

Set `AI_TALKS_FEEDS_REPO=~/feeds` to push automatically after each commit.

### Using with TrendRadar

[TrendRadar](https://github.com/linzzzzzz/trendradar) is an RSS aggregator and trend analysis tool. AI Talks Monitor integrates with it directly.

**Option A — local file URL** (if TrendRadar runs on the same machine):

```yaml
# In TrendRadar's config/config.yaml
rss:
  feeds:
    - id: "ai-talks"
      name: "AI Thought Leader Talks"
      url: "file:///Users/YOU/.claude/skills/ai-talks-monitor/output/ai_talks.xml"
      max_age_days: 30
      enabled: true
    - id: "ai-talks-zh"
      name: "AI大咖讲座精选"
      url: "file:///Users/YOU/.claude/skills/ai-talks-monitor/output/ai_talks_zh.xml"
      max_age_days: 30
      enabled: true
```

**Option B — GitHub Pages URL** (recommended for sharing or remote access):

```yaml
# In TrendRadar's config/config.yaml
rss:
  feeds:
    - id: "ai-talks"
      name: "AI Thought Leader Talks"
      url: "https://YOURNAME.github.io/feeds/ai_talks.xml"
      max_age_days: 30
      enabled: true
    - id: "ai-talks-zh"
      name: "AI大咖讲座精选"
      url: "https://YOURNAME.github.io/feeds/ai_talks_zh.xml"
      max_age_days: 30
      enabled: true
```

You can enable one or both feeds depending on your audience. The Chinese feed (`ai_talks_zh.xml`) uses Claude-generated translations and cleaned description translations, making it useful on its own for Chinese-speaking users.

## Automated daily fetch

The `--fetch-candidates` step is safe to schedule unattended — it only writes `output/candidates.json` and never modifies state or the RSS feed.

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
| `output/candidates.json` | Auto-written by `--fetch-candidates`; read by Claude for classification |
| `output/accepted.json` | Written by Claude; input to `--commit-file` |
| `output/state.json` | Auto-managed: seen video IDs (with 30-day expiry), last_checked timestamp, rolling RSS items |
| `output/ai_talks.xml` | Auto-generated English RSS 2.0 feed |
| `output/ai_talks_zh.xml` | Auto-generated Chinese RSS 2.0 feed |

Delete `output/state.json` to trigger a full re-scan on the next run.

## CLI reference

```
--fetch-candidates           Search YouTube, apply heuristic filter, write output/candidates.json
--commit-file FILE           Read output/accepted.json (written by Claude); write both RSS feeds, update state
--commit ID [ID ...]         Legacy: accept these video IDs, write English feed only, update state

--dry-run                    Preview without writing files or updating state
--lookback-days N            Override config lookback_days; search N days back
--limit N                    Process only the first N entries per category (useful for testing)
```

# AI Talks Monitor

An Agent Skill that automatically tracks first-person talks and interviews from top AI leaders on YouTube, filters out noise, and delivers them as bilingual RSS feeds (English + Chinese).

## Why this exists

There's too much AI content out there — reactions, summaries, hot takes — but not enough signal. The most valuable (and underrated) information source? Hearing directly from the people building AI: OpenAI, Anthropic, DeepMind, NVIDIA, Meta AI and others.

The problem is their talks are scattered across dozens of YouTube channels, making them nearly impossible to track manually. This skill solves that — an AI agent searches, classifies, and curates so you don't have to.

## Who is this for

- **AI enthusiasts** — keep up with the latest talks from AI leaders in your spare time
- **Content creators** — get first access to new interviews and talks
- Or anyone who wants to learn English by watching original AI talks

## What it covers

| Category | Examples |
|----------|----------|
| 🏢 People | Sam Altman, Dario Amodei, Jensen Huang, Demis Hassabis, Yann LeCun, Fei-Fei Li, Andrew Ng, and more |
| 🎙️ Channels | Zhang Xiaojun Podcast, Lex Fridman, Dwarkesh Patel, Y Combinator |
| 🔬 Orgs | Talks and podcasts from researchers from OpenAI, Anthropic, Google DeepMind, NVIDIA AI, Meta AI |

Fully customizable — add or remove people, channels, and orgs in `config.yaml`.

<table>
<tr>
<td align="center">
Feishu push notification<br>
<img src="_image/feishu.png" alt="Feishu push notification" width="400"/>
</td>
<td align="center">
RSS reading experience<br>
<img src="_image/netnewswire.png" alt="NetNewsWire reading experience" width="400"/>
</td>
</tr>
</table>

## Just subscribe (no setup needed)

Don't want to run anything? Subscribe to the curated RSS feeds directly. Updated daily around 10:10 AM Beijing time (UTC+8):

- **English:** [ai_talks.xml](https://linzzzzzz.github.io/feeds/ai_talks.xml)
- **Chinese:** [ai_talks_zh.xml](https://linzzzzzz.github.io/feeds/ai_talks_zh.xml)

Use any RSS reader — [NetNewsWire](https://netnewswire.com/) (free, macOS/iOS), [Inoreader](https://www.inoreader.com/) (free, web), or any other. You can also add these feeds to [TrendRadar](https://github.com/sansan0/TrendRadar) to get AI talks in your daily trending briefings.

## Run it yourself

Want to deploy in your OpenClaw and customize who you track? Run the skill with your own configuration.

### How it works

```
YouTube search → heuristic pre-filter → LLM classification → enrichment → RSS feeds
```

1. **Fetch** — searches YouTube for each watchlist entry, filters out reactions/summaries/clips
2. **Classify** — LLM subagents review candidates per category, accepting only genuine first-person talks
3. **Enrich** — translates titles and descriptions to Chinese for accepted talks
4. **Publish** — generates RSS feeds, updates state, sends notifications (Telegram/Feishu)

Runs every few hours automatically. The fetch step is safe to schedule unattended.

### Prerequisites

- **An LLM agent** — [OpenClaw](https://openclaw.com) (tested with MiniMax 2.5 and 2.7) or [Claude Code](https://claude.com/claude-code). The classification step (Step 2) uses an LLM agent to review candidates — this skill is not a standalone CLI tool.
- **YouTube Data API v3 key** — [get one free](https://console.cloud.google.com). The default config uses yt-dlp for search (no key needed) but the YouTube API for metadata enrichment. Without a key, metadata falls back to yt-dlp, which frequently triggers YouTube bot-detection, leading to incomplete data and failed feed generation.
- **Python 3.9+**

### Installation

**1. Install as a skill**

For OpenClaw:
```bash
git clone https://github.com/linzzzzzz/ai-talks-monitor-skill ~/.agents/skills/ai-talks-monitor
```

For Claude Code:
```bash
git clone https://github.com/linzzzzzz/ai-talks-monitor-skill ~/.claude/skills/ai-talks-monitor
```

**2. Install dependencies**

```bash
pip install requests pyyaml yt-dlp
```

**3. Set environment variables**

| Variable | Required | Purpose |
|----------|----------|---------|
| `YOUTUBE_API_KEY` | **Yes** | YouTube Data API v3 key ([get one free](https://console.cloud.google.com)). The default config uses yt-dlp for search (which works fine without a key) but the YouTube API for metadata enrichment — this hybrid approach saves API quota on discovery while still getting reliable metadata (publish dates, full descriptions). Without a key, metadata also falls back to yt-dlp, which frequently triggers YouTube bot-detection, leading to incomplete data and failed feed generation. |
| `AI_TALKS_FEEDS_REPO` | No | Path to a local git repo for auto-publishing feeds to GitHub Pages |
| `TELEGRAM_BOT_TOKEN` | No | For native Telegram notifications |
| `TELEGRAM_CHAT_ID` | No | Telegram chat/channel ID |

**4. Run it**

Just tell your agent:

> "Check for new AI talks"

Or run the steps manually:

```bash
# Step 1: Fetch candidates
python3 scripts/check_talks.py --fetch-candidates

# Step 2: Classify (done by the agent via SKILL.md)

# Step 3: Prepare accepted items
python3 scripts/check_talks.py --prepare-accepted output/scratch/review.json

# Step 4: Commit to feeds
python3 scripts/check_talks.py --commit-file output/scratch/accepted.json
```

On first run, add `--lookback-days 30` to search further back.

### Customization

Edit `config.yaml`:

```yaml
thought_leaders:
  - name: "Sam Altman"
    search_query: '"Sam Altman" interview'
  - name: "Your Person"
    search_query: '"Your Person" interview OR talk'

channels:
  - name: "Your Channel"
    channel_url: "https://www.youtube.com/@yourchannel"

orgs:
  enabled: true
  searches:
    - name: "Your Org"
      search_query: '"Your Org" researcher talk podcast'
      org: "Organization Name"
```

### Notifications

Notifications are sent automatically after each `--commit-file`. Three backends are available:

| Backend | Description |
|---------|-------------|
| `none` | No notifications (recommended to start with) |
| `native` | Built-in Telegram delivery — requires `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` env vars |
| `openclaw` | Delivers via OpenClaw to Telegram or Feishu |

```yaml
notifications:
  backend: "none"          # "none", "native" (Telegram), or "openclaw" (Telegram/Feishu)
  language: "zh"           # "zh" for Chinese titles in notifications, "original" for as-is
  include_excerpt: true    # include a description excerpt

  # Config for backend: "native"
  native:
    channel: "telegram"
    target: ""             # Telegram chat ID or channel ID

  # Config for backend: "openclaw"
  openclaw:
    channel: "feishu"      # "telegram" or "feishu"
    target: ""             # e.g. "feishu:group:oc_xxx" or Telegram chat ID
```

### Scheduling

Schedule the full flow to run daily with an OpenClaw cron job:

```bash
openclaw cron add \
  --name "AI Talks Monitor" \
  --cron "0 10 * * *" \
  --session isolated \
  --message "Let's use ai-talks-monitor skill to get AI talks." \
  --no-deliver
```

### TrendRadar integration

If you use [TrendRadar](https://github.com/sansan0/TrendRadar) for trending topics monitoring, you can add the AI talks feeds to your TrendRadar config to get AI talks included in your daily briefings:

```yaml
# In TrendRadar's config/config.yaml, under rss.feeds:
- id: "ai-talks"
  name: "AI Thought Leader Talks"
  url: "file:///path/to/ai-talks-monitor/output/ai_talks.xml"
  max_age_days: 30
  enabled: true
- id: "ai-talks-zh"
  name: "AI大咖讲座精选"
  url: "file:///path/to/ai-talks-monitor/output/ai_talks_zh.xml"
  max_age_days: 30
  enabled: true
```

## CLI reference

```
--fetch-candidates           Search YouTube, filter, write candidate files
--prepare-accepted FILE      Merge review into accepted.json draft
--apply-enrichment FILE      Merge LLM-generated fields into accepted.json
--commit-file FILE           Write RSS feeds, update state, notify
--dry-run                    Preview without writing
--lookback-days N            Override search window
--limit N                    Process first N entries per category (testing)
```

## Files

| File | Purpose |
|------|---------|
| `SKILL.md` | Agent instructions |
| `CLASSIFY.md` | Classification rules reference |
| `config.yaml` | Watchlist and settings |
| `scripts/check_talks.py` | Main script |
| `output/state.json` | Persistent state (seen IDs, RSS items) |
| `output/scratch/` | Per-run working files (candidates, reviews, enrichment) |
| `output/ai_talks.xml` | English RSS feed |
| `output/ai_talks_zh.xml` | Chinese RSS feed |
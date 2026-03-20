# AI Talks Monitor

An Agent Skill that automatically tracks first-person talks and interviews from top AI leaders on YouTube, filters out noise, and delivers them as bilingual RSS feeds (English + Chinese).

## Why this exists

There's too much AI content out there — reactions, summaries, hot takes — but not enough signal. The most valuable (and underrated) information source? Hearing directly from the people building AI: OpenAI, Anthropic, DeepMind, NVIDIA, Meta AI and others.

The problem is their talks are scattered across dozens of YouTube channels, making them nearly impossible to track manually. This skill solves that — an AI agent searches, classifies, and curates so you don't have to.

## Who is this for

- **AI enthusiasts** — keep up with the latest talks from AI leaders in your spare time
- **Content creators** — get first access to new interviews and talks
- Or anyone who wants to learn English by watching original AI talks

## Just subscribe (no setup needed)

Don't want to run anything? Subscribe to the curated RSS feeds directly:

- **English:** [ai_talks.xml](https://linzzzzzz.github.io/feeds/ai_talks.xml)
- **Chinese:** [ai_talks_zh.xml](https://linzzzzzz.github.io/feeds/ai_talks_zh.xml)

Use any RSS reader — [NetNewsWire](https://netnewswire.com/) (free, macOS/iOS), [Inoreader](https://www.inoreader.com/) (free, web), or any other.

## Run it yourself

Want to customize who you track? Run the skill with your own watchlist.

### What it covers (default config)

| Category | Examples |
|----------|----------|
| 🏢 People | Sam Altman, Dario Amodei, Jensen Huang, Demis Hassabis, Yann LeCun, Fei-Fei Li, Andrew Ng, and more |
| 🎙️ Channels | Zhang Xiaojun Podcast, Lex Fridman, Dwarkesh Patel, Y Combinator |
| 🔬 Orgs | OpenAI, Anthropic, Google DeepMind, NVIDIA AI, Meta AI talks |

Fully customizable — add or remove people, channels, and orgs in `config.yaml`.

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

### Serving feeds via GitHub Pages

```bash
mkdir ~/feeds && cd ~/feeds
git init && git remote add origin https://github.com/YOURNAME/feeds
# Enable Pages in repo Settings → Pages → Deploy from main branch
```

Set `AI_TALKS_FEEDS_REPO=~/feeds` and feeds auto-publish after each run.

### Notifications

```yaml
notifications:
  backend: "native"      # "native" (Telegram), "openclaw" (Telegram/Feishu), or "none"
  language: "zh"          # "zh" or "original"
  include_excerpt: true
```

### Scheduling

**macOS (launchd):** Save a plist to `~/Library/LaunchAgents/` that runs `--fetch-candidates` on a schedule.

**Linux (cron):**
```
0 */4 * * * YOUTUBE_API_KEY=your_key python3 ~/.claude/skills/ai-talks-monitor/scripts/check_talks.py --fetch-candidates
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
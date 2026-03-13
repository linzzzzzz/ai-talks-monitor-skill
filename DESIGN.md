# AI Talks Monitor — Purpose & Design

## Purpose

AI Talks Monitor is a YouTube surveillance tool that automatically discovers new long-form, original talks and interviews featuring AI thought leaders, then delivers findings to Slack or an RSS feed. It exists to solve a specific signal-to-noise problem: YouTube is flooded with derivative content (reactions, summaries, explainers, highlight clips) that buries the original source material. This tool filters that noise and surfaces only primary content.

## Goals

- Track specific people (e.g. Sam Altman, Yann LeCun) across any channel they appear on
- Optionally catch talks from unknown speakers via topic-based searches (AI safety, conference keynotes)
- Never notify about the same video twice
- Integrate with TrendRadar or any RSS feed reader via a standard RSS 2.0 output

## Architecture

The project has a single entry point:

```
check_talks.py      — two-phase workflow: fetch candidates → Claude reviews → commit accepted
```

The pipeline:

```
config.yaml
    |
    v
YouTube Data API v3 (search.list + videos.list)
    |
    v
Heuristic pre-filter (keyword blacklist on title)
    |
    v
candidates.json  ← written for Claude to review
    |
    v
[Claude reviews and selects IDs to accept]
    |
    v
--commit ID ...
    |
    v
Deduplication (state.json)
    |
    v
Output: ai_talks.xml  +  optional Slack webhook
```

## Core Components

### `check_talks.py`
The single script, with two mutually exclusive modes:

**Phase 1 — `--fetch-candidates`**:
1. Reads the watchlist from `config.yaml`
2. Determines the search window (`last_checked` from state, or `--lookback-days`)
3. Queries YouTube for each person/topic
4. Applies heuristic filters
5. Writes survivors to `candidates.json` for Claude to review
6. Does **not** modify state or write the RSS feed

**Phase 2 — `--commit ID [ID ...]`**:
1. Reads `candidates.json`
2. Accepts only the video IDs passed as arguments (chosen by Claude after reviewing candidates)
3. Writes `ai_talks.xml` — RSS 2.0 feed with a rolling 30-day window
4. Posts to Slack/Discord if `SLACK_WEBHOOK_URL` is set
5. Persists seen video IDs, `last_checked` timestamp, and the rolling item list to `state.json`

### `config.yaml`
User-facing configuration. Two search modes:

**Person watchlist** (always active):
```yaml
thought_leaders:
  - name: "Sam Altman"
    search_query: '"Sam Altman" interview'
```
Each person gets an exact YouTube search. Claude then verifies the person is an actual participant, not just a subject being discussed.

**Topic searches** (opt-in, `topics.enabled: true`):
```yaml
topics:
  enabled: false
  searches:
    - name: "AI Safety"
      search_query: '"AI safety" interview OR talk OR podcast'
      min_duration_minutes: 30
```
Broader and noisier than person searches. Higher `min_duration_minutes` is recommended to compensate.

### Filtering Pipeline

**Stage 1 — YouTube API filters** (no cost):
- `videoDuration=long` restricts to videos >20 minutes at the API level
- `publishedAfter` restricts to the search window
- `maxResults=20` per query
- `videos.list` is called for full descriptions (search snippets are truncated)

**Stage 2 — Heuristic pre-filter** (no cost):
- Rejects titles containing keywords like `reaction`, `summary`, `breakdown`, `#shorts`, `highlights`, and CJK equivalents (`解读`, `总结`, `面试`, etc.)
- Fast and free; catches the most obvious derivative content

**Stage 3 — Claude classification** (human-in-the-loop):
- Claude reads `candidates.json` and decides which videos to accept
- Person mode: is the named person a direct guest/participant, or just being discussed?
- Topic mode: is this a genuine first-person talk vs. third-party explainer/commentary?
- Low-confidence cases are rejected — better to miss a real talk than include a reaction video
- Claude passes accepted IDs to `--commit`

**Stage 4 — Deduplication**:
- Video IDs are stored in `state.json`
- Already-seen IDs are skipped in `--fetch-candidates` before candidates are written

## State Files

| File | Contents |
|------|----------|
| `state.json` | `seen_ids[]`, `last_checked`, `items[]` (last 30 days) |
| `candidates.json` | Written by `--fetch-candidates`; read by Claude for classification |
| `ai_talks.xml` | Generated RSS 2.0 feed; written by `--commit` |

State files are auto-managed. Delete `state.json` to trigger a full re-scan on next run, or use `--lookback-days` to override the search window without resetting state.

## Environment Variables

| Variable | Required | Purpose |
|----------|----------|---------|
| `YOUTUBE_API_KEY` | No | YouTube Data API v3; if absent, yt-dlp is used as fallback |
| `YTDLP_COOKIES_FROM_BROWSER` | No | Browser name (`chrome`, `firefox`, `safari`) for yt-dlp bot-check workaround |
| `TELEGRAM_BOT_TOKEN` | No | Telegram bot token — notifications sent on `--commit` when both token and chat ID are set |
| `TELEGRAM_CHAT_ID` | No | Telegram chat/channel ID to send notifications to |
| `AI_TALKS_FEEDS_REPO` | No | Absolute path to a local git repo (e.g. `~/feeds`). If set, `--commit` copies `ai_talks.xml` there and pushes automatically. |

## CLI Flags

```
--fetch-candidates       Search YouTube, apply heuristic filter, write candidates.json
--commit ID [ID ...]     Accept these video IDs, write RSS feed, update state

--dry-run                Preview without writing files, updating state, or sending notifications
--lookback-days N        Override last_checked; search N days back
```

## TrendRadar Integration

`check_talks.py --commit` produces a standard RSS 2.0 file. Add it to TrendRadar's config:

```yaml
rss:
  feeds:
    - id: "ai-talks"
      name: "AI Thought Leader Talks"
      url: "file:///path/to/ai-talks-monitor/ai_talks.xml"
      max_age_days: 30
      enabled: true
```

## Design Decisions

**Human-in-the-loop classification**: Rather than automated LLM classification via API, Claude reviews `candidates.json` directly in the conversation. This gives higher accuracy (Claude can reason about context, channel reputation, and video metadata together) with no API cost or key management.

**Two-phase design**: `--fetch-candidates` and `--commit` are separate commands so the fetch step can run unattended on a cron/launchd schedule. Claude only needs to be involved for the classification + commit step.

**Heuristic filter before classification**: The keyword blacklist catches 60-80% of derivative content at zero cost. This matters because YouTube searches return many reaction/summary videos, and each candidate reviewed by Claude takes context window space.

**Person-centric search design**: Searching `"Sam Altman" interview` rather than just `Sam Altman` significantly improves precision. Claude then catches the remaining mismatches (e.g. a debate show titled "Sam Altman Was Wrong About Everything").

**Full descriptions via `videos.list`**: The `search.list` API returns truncated snippets (~300 chars). The script makes a second call to `videos.list` to fetch full descriptions, which are written to the RSS feed and visible to Claude during classification.

**yt-dlp fallback when no API key**: If `YOUTUBE_API_KEY` is not set, `yt-dlp` is used to search YouTube without an API key. The search phase uses `--flat-playlist` (fast, no auth needed). If `YTDLP_COOKIES_FROM_BROWSER` is set, full descriptions are fetched afterward via individual `yt-dlp -J` calls with a 1.5s delay between requests; otherwise descriptions are left empty and a tip is printed. Date filtering is approximate — yt-dlp doesn't support `publishedAfter` at query time, so the script fetches 3× more results and filters by `upload_date` post-fetch.

**30-day rolling RSS window**: `state.json` stores the accumulated item list and trims entries older than 30 days on each run. This prevents unbounded growth while keeping the feed populated enough to be useful to a feed reader checking weekly.
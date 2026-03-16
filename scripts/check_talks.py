#!/usr/bin/env python3
"""
AI Talks Monitor — two-phase workflow:

  Phase 1 — --fetch-candidates:
    Search YouTube, apply heuristic filters, write candidates.json.

  Phase 2 — --commit ID [ID ...]:
    Accept specific video IDs (chosen by Claude after reviewing candidates),
    write ai_talks.xml RSS feed, update state, optionally notify Slack.

Outputs:
  - candidates.json  — written by --fetch-candidates for Claude to review
  - ai_talks.xml     — RSS 2.0 feed, written by --commit
  - Slack/Discord    — posted on --commit when SLACK_WEBHOOK_URL env var is set
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path

import requests
import yaml

SKILL_DIR = Path(__file__).parent.parent
CONFIG_FILE = SKILL_DIR / "config.yaml"
STATE_FILE = SKILL_DIR / "state.json"
RSS_FILE = SKILL_DIR / "ai_talks.xml"
RSS_FILE_ZH = SKILL_DIR / "ai_talks_zh.xml"
CANDIDATES_FILE = SKILL_DIR / "candidates.json"
ACCEPTED_FILE = SKILL_DIR / "accepted.json"

RSS_RETENTION_DAYS = 30
YOUTUBE_SEARCH_SP_THIS_MONTH = "EgIIBA%253D%253D"

DERIVATIVE_KEYWORDS = [
    "reaction", "reacts", "reacted", "summary", "summarized",
    "explained", "breakdown", "my thoughts", "review", "analysis",
    "clips", "highlights", "#shorts", "shorts", "compilation",
    "解读", "总结", "讲解", "分析", "解析", "精华", "剪辑", "面试",
]


def load_config() -> dict:
    with open(CONFIG_FILE) as f:
        return yaml.safe_load(f)


def load_state() -> dict:
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"seen_ids": {}, "last_checked": None, "items": []}


def load_seen_ids(state: dict) -> dict[str, str]:
    """Return seen_ids as {id: seen_at_iso} dict, migrating from legacy list format."""
    raw = state.get("seen_ids", [])
    if isinstance(raw, list):
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return {vid_id: now for vid_id in raw}
    return dict(raw)


def prune_seen_ids(seen_ids: dict[str, str], retention_days: int) -> dict[str, str]:
    """Remove entries older than retention_days."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    return {
        vid_id: seen_at
        for vid_id, seen_at in seen_ids.items()
        if datetime.fromisoformat(seen_at.replace("Z", "+00:00")) > cutoff
    }


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def is_derivative(title: str) -> bool:
    title_lower = title.lower()
    return any(kw.lower() in title_lower for kw in DERIVATIVE_KEYWORDS)


def parse_iso_duration_minutes(duration: str) -> int:
    hours = int(m.group(1)) if (m := re.search(r"(\d+)H", duration)) else 0
    mins = int(m.group(1)) if (m := re.search(r"(\d+)M", duration)) else 0
    secs = int(m.group(1)) if (m := re.search(r"(\d+)S", duration)) else 0
    return hours * 60 + mins + (1 if secs >= 30 else 0)


def upload_date_to_iso8601(upload_date: str | None) -> str | None:
    if not upload_date:
        return None
    return f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:]}T00:00:00Z"


def unix_timestamp_to_iso8601(timestamp: int | float | None) -> str | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def youtube_search_results_url(query: str, sp: str | None = None) -> str:
    url = f"https://www.youtube.com/results?search_query={urllib.parse.quote_plus(query)}"
    if sp:
        url += f"&sp={sp}"
    return url


def search_youtube(query: str, published_after: str, api_key: str) -> list[dict]:
    search_resp = requests.get(
        "https://www.googleapis.com/youtube/v3/search",
        params={
            "part": "snippet",
            "q": query,
            "type": "video",
            "videoDuration": "long",
            "publishedAfter": published_after,
            "maxResults": 20,
            "key": api_key,
        },
        timeout=15,
    )
    search_resp.raise_for_status()
    items = search_resp.json().get("items", [])
    if not items:
        return []

    video_ids = [item["id"]["videoId"] for item in items]
    details_resp = requests.get(
        "https://www.googleapis.com/youtube/v3/videos",
        params={"part": "contentDetails,snippet", "id": ",".join(video_ids), "key": api_key},
        timeout=15,
    )
    details_resp.raise_for_status()
    details = {v["id"]: v for v in details_resp.json().get("items", [])}

    results = []
    for item in items:
        vid_id = item["id"]["videoId"]
        detail = details.get(vid_id)
        if not detail:
            continue
        results.append({
            "id": vid_id,
            "title": item["snippet"]["title"],
            "channel": item["snippet"]["channelTitle"],
            "description": detail["snippet"]["description"],
            "published_at": item["snippet"]["publishedAt"],
            "published_at_precision": "exact",
            "duration_min": parse_iso_duration_minutes(detail["contentDetails"]["duration"]),
            "url": f"https://www.youtube.com/watch?v={vid_id}",
        })
    return results


def search_youtube_channel(channel_id: str, published_after: str, api_key: str) -> list[dict]:
    """Search a specific YouTube channel for recent long videos via the Data API."""
    search_resp = requests.get(
        "https://www.googleapis.com/youtube/v3/search",
        params={
            "part": "snippet",
            "channelId": channel_id,
            "type": "video",
            "videoDuration": "long",
            "order": "date",
            "publishedAfter": published_after,
            "maxResults": 20,
            "key": api_key,
        },
        timeout=15,
    )
    search_resp.raise_for_status()
    items = search_resp.json().get("items", [])
    if not items:
        return []

    video_ids = [item["id"]["videoId"] for item in items]
    details_resp = requests.get(
        "https://www.googleapis.com/youtube/v3/videos",
        params={"part": "contentDetails,snippet", "id": ",".join(video_ids), "key": api_key},
        timeout=15,
    )
    details_resp.raise_for_status()
    details = {v["id"]: v for v in details_resp.json().get("items", [])}

    results = []
    for item in items:
        vid_id = item["id"]["videoId"]
        detail = details.get(vid_id)
        if not detail:
            continue
        results.append({
            "id": vid_id,
            "title": item["snippet"]["title"],
            "channel": item["snippet"]["channelTitle"],
            "description": detail["snippet"]["description"],
            "published_at": item["snippet"]["publishedAt"],
            "published_at_precision": "exact",
            "duration_min": parse_iso_duration_minutes(detail["contentDetails"]["duration"]),
            "url": f"https://www.youtube.com/watch?v={vid_id}",
        })
    return results


def search_youtube_channel_ytdlp(channel_url: str, published_after: str, max_results: int = 30, cookies_browser: str = "") -> list[dict]:
    """Fetch recent videos from a YouTube channel using yt-dlp (no API key required)."""
    cutoff = datetime.fromisoformat(published_after.replace("Z", "+00:00")).date()
    cmd = [
        "yt-dlp", "--flat-playlist", "--dump-single-json",
        "--extractor-args", "youtubetab:approximate_date",
        "--playlist-end", str(max_results),
        f"{channel_url}/videos",
    ]
    if cookies_browser:
        cmd += ["--cookies-from-browser", cookies_browser]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=True)
    except FileNotFoundError:
        raise RuntimeError("yt-dlp not found. Install with: pip install yt-dlp")
    except subprocess.CalledProcessError as e:
        stderr = e.stderr or ""
        if "Sign in to confirm" in stderr or "bot" in stderr.lower():
            raise RuntimeError(
                "yt-dlp bot-check triggered. Set ytdlp_search.cookies_from_browser: chrome "
                "(or firefox/safari) in config.yaml to authenticate."
            )
        raise RuntimeError(f"yt-dlp failed: {stderr.strip()}")

    entries = json.loads(proc.stdout).get("entries", []) or []
    results = []
    for entry in entries:
        if not entry or not entry.get("id"):
            continue
        upload_date_str = entry.get("upload_date")
        if upload_date_str:
            upload_date = datetime.strptime(upload_date_str, "%Y%m%d").date()
            if upload_date < cutoff:
                continue
            published_at = upload_date_to_iso8601(upload_date_str)
            published_at_precision = "approximate"
        elif entry.get("timestamp") is not None:
            published_at = unix_timestamp_to_iso8601(entry.get("timestamp"))
            published_at_precision = "approximate"
            if published_at:
                upload_date = datetime.fromisoformat(published_at.replace("Z", "+00:00")).date()
                if upload_date < cutoff:
                    continue
        else:
            published_at = None
            published_at_precision = "unknown"

        results.append({
            "id": entry["id"],
            "title": entry.get("title", ""),
            "channel": entry.get("channel") or entry.get("uploader", ""),
            "description": entry.get("description") or "",
            "published_at": published_at,
            "published_at_precision": published_at_precision,
            "duration_min": round((entry.get("duration") or 0) / 60),
            "url": f"https://www.youtube.com/watch?v={entry['id']}",
        })

    return results


def search_youtube_ytdlp(
    query: str,
    published_after: str,
    max_results: int = 20,
    use_this_month_filter: bool = False,
    cookies_browser: str = "",
) -> list[dict]:
    """Fallback YouTube search using yt-dlp (no API key required).

    Uses --flat-playlist for a fast initial search, returning results without
    descriptions. Call enrich_ytdlp_descriptions() on the filtered candidates
    afterward to fetch full descriptions via individual yt-dlp -J calls.

    Note: date filtering is approximate — yt-dlp doesn't support publishedAfter
    at query time, so we fetch 3x results and filter by upload_date post-fetch.

    Set ytdlp_search.cookies_from_browser in config.yaml if YouTube returns
    a bot-check error.
    """
    cutoff = datetime.fromisoformat(published_after.replace("Z", "+00:00")).date()
    search_target = (
        youtube_search_results_url(query, YOUTUBE_SEARCH_SP_THIS_MONTH)
        if use_this_month_filter else f"ytsearch{max_results * 3}:{query}"
    )
    cmd = [
        "yt-dlp", "--flat-playlist", "--dump-single-json",
        "--extractor-args", "youtubetab:approximate_date",
        "--playlist-end", str(max_results * 3),
        search_target,
    ]
    if cookies_browser:
        cmd += ["--cookies-from-browser", cookies_browser]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=True)
    except FileNotFoundError:
        raise RuntimeError("yt-dlp not found. Install with: pip install yt-dlp")
    except subprocess.CalledProcessError as e:
        stderr = e.stderr or ""
        if "Sign in to confirm" in stderr or "bot" in stderr.lower():
            raise RuntimeError(
                "yt-dlp bot-check triggered. Set ytdlp_search.cookies_from_browser: chrome "
                "(or firefox/safari) in config.yaml to authenticate."
            )
        raise RuntimeError(f"yt-dlp failed: {stderr.strip()}")

    entries = json.loads(proc.stdout).get("entries", []) or []
    results = []
    for entry in entries:
        if not entry or not entry.get("id"):
            continue

        upload_date_str = entry.get("upload_date")  # YYYYMMDD or None
        if upload_date_str:
            upload_date = datetime.strptime(upload_date_str, "%Y%m%d").date()
            if upload_date < cutoff:
                continue
            published_at = upload_date_to_iso8601(upload_date_str)
            published_at_precision = "approximate"
        elif entry.get("timestamp") is not None:
            published_at = unix_timestamp_to_iso8601(entry.get("timestamp"))
            published_at_precision = "approximate"
            if published_at:
                upload_date = datetime.fromisoformat(published_at.replace("Z", "+00:00")).date()
                if upload_date < cutoff:
                    continue
        else:
            published_at = None
            published_at_precision = "unknown"

        results.append({
            "id": entry["id"],
            "title": entry.get("title", ""),
            "channel": entry.get("channel") or entry.get("uploader", ""),
            "description": entry.get("description") or "",
            "published_at": published_at,
            "published_at_precision": published_at_precision,
            "duration_min": round((entry.get("duration") or 0) / 60),
            "url": f"https://www.youtube.com/watch?v={entry['id']}",
        })

        if len(results) >= max_results:
            break

    return results


class YtdlpBotCheck(Exception):
    pass


def ytdlp_fetch_video_metadata(video_id: str, cookies_browser: str = "") -> dict:
    """Fetch full metadata for a single video using yt-dlp -J.

    Raises YtdlpBotCheck if YouTube returns a bot-check error.
    Returns an empty dict on other errors (e.g. video unavailable).
    """
    cmd = ["yt-dlp", "-J", f"https://www.youtube.com/watch?v={video_id}"]
    if cookies_browser:
        cmd += ["--cookies-from-browser", cookies_browser]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=True)
        info = json.loads(proc.stdout)
        published_at = (
            unix_timestamp_to_iso8601(info.get("timestamp"))
            or upload_date_to_iso8601(info.get("upload_date"))
        )
        metadata = {"description": info.get("description") or ""}
        if published_at:
            metadata["published_at"] = published_at
            metadata["published_at_precision"] = "exact"
        return metadata
    except subprocess.CalledProcessError as e:
        stderr = e.stderr or ""
        if "Sign in to confirm" in stderr or "bot" in stderr.lower():
            raise YtdlpBotCheck()
        return {}
    except (json.JSONDecodeError, subprocess.TimeoutExpired):
        return {}


def enrich_ytdlp_descriptions(candidates: list[dict], delay: float = 1.5, cookies_browser: str = "") -> None:
    """Fetch full metadata for candidates in-place, with a delay between requests.

    Stops early if YouTube triggers a bot-check and prints a tip.
    """
    print(f"\nFetching descriptions for {len(candidates)} candidate(s) (yt-dlp, {delay}s delay)...")
    for i, video in enumerate(candidates):
        if i > 0:
            time.sleep(delay)
        print(f"  [{i + 1}/{len(candidates)}] {video['title'][:70]}")
        try:
            metadata = ytdlp_fetch_video_metadata(video["id"], cookies_browser)
            if metadata.get("description"):
                video["description"] = metadata["description"]
            if metadata.get("published_at"):
                video["published_at"] = metadata["published_at"]
                video["published_at_precision"] = metadata["published_at_precision"]
        except YtdlpBotCheck:
            print(
                "  [bot-check] YouTube requires authentication for remaining descriptions.\n"
                "  Set ytdlp_search.cookies_from_browser: chrome (or firefox/safari) in config.yaml."
            )
            break


def collect_candidates(
    videos: list[dict],
    label: str,
    seen_ids: set,
    min_duration: int,
) -> list[dict]:
    """Apply heuristic filters and return candidates for Claude to review."""
    candidates = []
    for video in videos:
        if video["id"] in seen_ids:
            continue
        if video["duration_min"] < min_duration:
            print(f"  [too short: {video['duration_min']}min] {video['title']}")
            continue
        if is_derivative(video["title"]):
            print(f"  [heuristic filter] {video['title']}")
            continue
        video["label"] = label
        candidates.append(video)
        print(f"  [candidate] {video['title']} ({video['duration_min']}min)")
    return candidates


def send_telegram(bot_token: str, chat_id: str, talks: list[dict], dry_run: bool = False) -> None:
    for video in talks:
        text = (
            f"*New talk: {video['label']}*\n"
            f"*{video['title']}*\n"
            f"Channel: {video['channel']} · {video['duration_min']} min\n"
            f"{video['url']}"
        )
        if dry_run:
            text = f"_\\[DRY RUN\\]_\n{text}"
        requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        ).raise_for_status()


def youtube_ts_to_rfc2822(iso_ts: str) -> str:
    dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    return format_datetime(dt)


def build_rss(items: list[dict]) -> str:
    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")

    ET.SubElement(channel, "title").text = "AI Thought Leader Talks (YouTube)"
    ET.SubElement(channel, "link").text = "https://www.youtube.com"
    ET.SubElement(channel, "description").text = (
        "Original long-form talks and interviews with AI thought leaders, filtered by Claude."
    )
    ET.SubElement(channel, "lastBuildDate").text = format_datetime(datetime.now(timezone.utc))

    for item_data in items:
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = f"[{item_data['label']}] {item_data['title']}"
        ET.SubElement(item, "link").text = item_data["url"]
        ET.SubElement(item, "guid", isPermaLink="false").text = item_data["id"]
        ET.SubElement(item, "pubDate").text = youtube_ts_to_rfc2822(item_data["published_at"])
        ET.SubElement(item, "author").text = item_data["channel"]
        description = item_data.get("description_clean") or item_data.get("description", "")
        ET.SubElement(item, "description").text = (
            f"{item_data['channel']} · {item_data['duration_min']} min\n\n"
            f"{description}"
        )

    ET.indent(rss, space="  ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(rss, encoding="unicode")


def build_rss_zh(items: list[dict]) -> str:
    """Build a Chinese RSS feed using title_zh and description_zh fields where available."""
    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")

    ET.SubElement(channel, "title").text = "AI大咖讲座精选 (YouTube)"
    ET.SubElement(channel, "link").text = "https://www.youtube.com"
    ET.SubElement(channel, "description").text = "精选AI思想领袖的长篇访谈与演讲，由Claude筛选整理。"
    ET.SubElement(channel, "lastBuildDate").text = format_datetime(datetime.now(timezone.utc))

    for item_data in items:
        item = ET.SubElement(channel, "item")
        title = item_data.get("title_zh") or item_data["title"]
        ET.SubElement(item, "title").text = f"[{item_data['label']}] {title}"
        ET.SubElement(item, "link").text = item_data["url"]
        ET.SubElement(item, "guid", isPermaLink="false").text = item_data["id"]
        ET.SubElement(item, "pubDate").text = youtube_ts_to_rfc2822(item_data["published_at"])
        ET.SubElement(item, "author").text = item_data["channel"]
        summary = (
            item_data.get("description_zh")
            or item_data.get("description_clean")
            or item_data.get("description", "")
        )
        ET.SubElement(item, "description").text = (
            f"{item_data['channel']} · {item_data['duration_min']} 分钟\n\n{summary}"
        )

    ET.indent(rss, space="  ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(rss, encoding="unicode")


def cmd_fetch_candidates(args) -> None:
    """Search YouTube, apply heuristic filters, write candidates.json."""
    config = load_config()
    state = load_state()
    ytdlp_search_config = config.get("ytdlp_search", {})
    use_this_month_filter = ytdlp_search_config.get("use_this_month_filter", False)

    api_key = os.environ.get("YOUTUBE_API_KEY", "")
    ytdlp_enabled = ytdlp_search_config.get("enabled", False)
    cookies_browser = ytdlp_search_config.get("cookies_from_browser") or ""

    if api_key:
        def do_search(query: str, published_after: str) -> list[dict]:
            return search_youtube(query, published_after, api_key)
    elif ytdlp_enabled:
        print("No YOUTUBE_API_KEY set — using yt-dlp fallback (date filtering approximate).\n")
        def do_search(query: str, published_after: str) -> list[dict]:
            return search_youtube_ytdlp(
                query,
                published_after,
                use_this_month_filter=use_this_month_filter,
                cookies_browser=cookies_browser,
            )
    else:
        print(
            "No YOUTUBE_API_KEY set and yt-dlp fallback is disabled.\n"
            "Set YOUTUBE_API_KEY, or set ytdlp_search.enabled: true in config.yaml to use yt-dlp."
        )
        return

    min_duration = config.get("min_duration_minutes", 20)
    lookback_days = args.lookback_days or config.get("lookback_days", 5)

    dt = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    published_after = dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    print(f"Searching for videos published after {published_after} ({lookback_days}d rolling window)\n")

    seen_ids: set[str] = set(load_seen_ids(state).keys())
    all_candidates: list[dict] = []
    limit = args.limit  # None means no limit

    ytdlp_delay = 0.5  # seconds between yt-dlp search calls to avoid bot-checks

    for leader in config.get("thought_leaders", [])[:limit]:
        print(f"Person: {leader['name']}...")
        try:
            videos = do_search(leader["search_query"], published_after)
        except (requests.exceptions.RequestException, RuntimeError) as e:
            print(f"  Search error: {e}")
            continue
        all_candidates.extend(
            collect_candidates(videos, leader["name"], seen_ids, min_duration)
        )
        if not api_key:
            time.sleep(ytdlp_delay)

    topics_config = config.get("topics", {})
    if topics_config.get("enabled", False):
        for topic in topics_config.get("searches", [])[:limit]:
            topic_name = topic["name"]
            topic_min = topic.get("min_duration_minutes", min_duration)
            print(f"Topic: {topic_name}...")
            try:
                videos = do_search(topic["search_query"], published_after)
            except (requests.exceptions.RequestException, RuntimeError) as e:
                print(f"  Search error: {e}")
                continue
            all_candidates.extend(
                collect_candidates(videos, f"Topic: {topic_name}", seen_ids, topic_min)
            )
            if not api_key:
                time.sleep(ytdlp_delay)

    channels_config = config.get("channels", {})
    if channels_config.get("enabled", False):
        for channel in channels_config.get("list", [])[:limit]:
            channel_name = channel["name"]
            print(f"Channel: {channel_name}...")
            try:
                if api_key and channel.get("channel_id"):
                    videos = search_youtube_channel(channel["channel_id"], published_after, api_key)
                elif channel.get("url") and ytdlp_enabled:
                    videos = search_youtube_channel_ytdlp(channel["url"], published_after, cookies_browser=cookies_browser)
                    time.sleep(ytdlp_delay)
                else:
                    reason = "no url configured" if not channel.get("url") else "yt-dlp fallback disabled (set ytdlp_search.enabled: true)"
                    print(f"  Skipping: {reason}")
                    continue
            except (requests.exceptions.RequestException, RuntimeError) as e:
                print(f"  Search error: {e}")
                continue
            all_candidates.extend(
                collect_candidates(videos, f"Channel: {channel_name}", seen_ids, min_duration)
            )

    # Backfill exact metadata for yt-dlp candidates when non-flat extraction is available.
    needs_enrichment = [
        c for c in all_candidates
        if not c.get("description") or c.get("published_at_precision") != "exact"
    ]
    if needs_enrichment:
        enrich_ytdlp_descriptions(needs_enrichment, cookies_browser=cookies_browser)

    with open(CANDIDATES_FILE, "w") as f:
        json.dump(all_candidates, f, indent=2)

    print(f"\n{len(all_candidates)} candidate(s) written to {CANDIDATES_FILE}")


def push_to_feeds_repo(rss_files: list[Path], feeds_repo: Path, dry_run: bool = False) -> None:
    """Copy RSS files into the feeds repo and push to origin."""
    if dry_run:
        for f in rss_files:
            print(f"[DRY RUN] Would copy {f} → {feeds_repo / f.name} and push")
        return
    for f in rss_files:
        shutil.copy(f, feeds_repo / f.name)
    try:
        for f in rss_files:
            subprocess.run(["git", "-C", str(feeds_repo), "add", f.name], check=True)
        result = subprocess.run(
            ["git", "-C", str(feeds_repo), "commit", "-m", "update ai_talks feed"],
            capture_output=True, text=True,
        )
        if result.returncode != 0 and "nothing to commit" in result.stdout + result.stderr:
            print("Feeds repo: nothing new to commit.")
            return
        result.check_returncode()
        subprocess.run(["git", "-C", str(feeds_repo), "push"], check=True)
        print(f"Pushed {[f.name for f in rss_files]} to {feeds_repo}")
    except subprocess.CalledProcessError as e:
        print(f"Feeds repo push failed: {e}")


def cmd_commit(args) -> None:
    """Accept specific video IDs, write RSS feed, update state."""
    if not CANDIDATES_FILE.exists():
        print("No candidates.json found. Run --fetch-candidates first.")
        return

    with open(CANDIDATES_FILE) as f:
        candidates = json.load(f)

    accepted_ids = set(args.commit)
    accepted = [v for v in candidates if v["id"] in accepted_ids]

    if not accepted:
        print("None of the provided IDs matched candidates.json.")
        return

    missing_dates = [v for v in accepted if not v.get("published_at")]
    if missing_dates:
        print("The following accepted items are missing published_at and cannot be added to RSS:")
        for video in missing_dates:
            print(f"  {video['id']}: {video['title']}")
        print("Re-run --fetch-candidates with YOUTUBE_API_KEY set, or set ytdlp_search.cookies_from_browser in config.yaml to backfill dates.")
        return

    state = load_state()
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    tg_chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")

    cutoff = datetime.now(timezone.utc) - timedelta(days=RSS_RETENTION_DAYS)
    existing_items = [
        i for i in state.get("items", [])
        if i.get("published_at")
        and datetime.fromisoformat(i["published_at"].replace("Z", "+00:00")) > cutoff
    ]
    all_items = accepted + existing_items
    rss_content = build_rss(all_items)

    if args.dry_run:
        print("\n--- RSS preview (first 1000 chars) ---")
        print(rss_content[:1000] + ("..." if len(rss_content) > 1000 else ""))
        if tg_token and tg_chat_id:
            send_telegram(tg_token, tg_chat_id, accepted, dry_run=True)
        else:
            print("(No TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID set)")
        print(f"\n[DRY RUN] Would write {len(accepted)} new item(s) to {RSS_FILE}")
        feeds_repo = os.environ.get("AI_TALKS_FEEDS_REPO", "")
        if feeds_repo:
            push_to_feeds_repo([RSS_FILE], Path(feeds_repo), dry_run=True)
        return

    RSS_FILE.write_text(rss_content, encoding="utf-8")
    print(f"Wrote {len(all_items)} item(s) to {RSS_FILE}")

    if tg_token and tg_chat_id:
        send_telegram(tg_token, tg_chat_id, accepted)
    else:
        print("(No TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID set — printing only)")
        for v in accepted:
            print(f"  {v['label']}: {v['title']}\n  {v['url']}\n")

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    seen_dict = load_seen_ids(state)
    for v in accepted:
        seen_dict[v["id"]] = now
    state["seen_ids"] = prune_seen_ids(seen_dict, RSS_RETENTION_DAYS)
    state["last_checked"] = now
    state["items"] = all_items
    save_state(state)
    print("State updated.")

    feeds_repo = os.environ.get("AI_TALKS_FEEDS_REPO", "")
    if feeds_repo:
        push_to_feeds_repo([RSS_FILE], Path(feeds_repo))


def cmd_commit_file(args) -> None:
    """Read accepted.json (with description_clean/title_zh/description_zh), write both English and Chinese RSS feeds."""
    accepted_file = Path(args.commit_file)
    if not accepted_file.exists():
        print(f"File not found: {accepted_file}")
        return

    with open(accepted_file) as f:
        raw = json.load(f)

    # Support both new format {"accepted": [...], "rejected": [...]} and legacy list format.
    if isinstance(raw, list):
        accepted_entries = raw  # legacy list shape: [{id, description_clean, title_zh, description_zh}, ...]
        rejected_ids: set[str] = set()
    else:
        accepted_entries = raw.get("accepted", [])
        rejected_ids = set(raw.get("rejected", []))

    if not CANDIDATES_FILE.exists():
        print("No candidates.json found. Run --fetch-candidates first.")
        return

    with open(CANDIDATES_FILE) as f:
        candidates = json.load(f)

    candidates_by_id = {v["id"]: v for v in candidates}
    accepted = []
    for entry in accepted_entries:
        vid = candidates_by_id.get(entry["id"])
        if not vid:
            print(f"  Warning: ID {entry['id']} not found in candidates.json, skipping.")
            continue
        video = dict(vid)
        if entry.get("description_clean"):
            video["description_clean"] = entry["description_clean"]
        if entry.get("title_zh"):
            video["title_zh"] = entry["title_zh"]
        if entry.get("description_zh"):
            video["description_zh"] = entry["description_zh"]
        accepted.append(video)

    if not accepted:
        if rejected_ids:
            # Still update state to mark rejected IDs as seen so they don't resurface.
            state = load_state()
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            seen_dict = load_seen_ids(state)
            for vid_id in rejected_ids:
                seen_dict[vid_id] = now
            state["seen_ids"] = prune_seen_ids(seen_dict, RSS_RETENTION_DAYS)
            state["last_checked"] = now
            save_state(state)
            print(f"No new talks accepted. {len(rejected_ids)} rejected IDs recorded in state.")
        else:
            print("No valid IDs matched candidates.json.")
        return

    missing_dates = [v for v in accepted if not v.get("published_at")]
    if missing_dates:
        print("The following accepted items are missing published_at and cannot be added to RSS:")
        for video in missing_dates:
            print(f"  {video['id']}: {video['title']}")
        print("Re-run --fetch-candidates with YOUTUBE_API_KEY set, or set ytdlp_search.cookies_from_browser in config.yaml to backfill dates.")
        return

    state = load_state()
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    tg_chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")

    cutoff = datetime.now(timezone.utc) - timedelta(days=RSS_RETENTION_DAYS)
    existing_items = [
        i for i in state.get("items", [])
        if i.get("published_at")
        and datetime.fromisoformat(i["published_at"].replace("Z", "+00:00")) > cutoff
    ]
    all_items = accepted + existing_items
    rss_content = build_rss(all_items)
    rss_zh_content = build_rss_zh(all_items)

    if args.dry_run:
        print("\n--- RSS (English) preview ---")
        print(rss_content[:500] + "...")
        print("\n--- RSS (Chinese) preview ---")
        print(rss_zh_content[:500] + "...")
        if tg_token and tg_chat_id:
            send_telegram(tg_token, tg_chat_id, accepted, dry_run=True)
        else:
            print("(No TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID set)")
        print(f"\n[DRY RUN] Would write {len(accepted)} new item(s) to {RSS_FILE} and {RSS_FILE_ZH}")
        feeds_repo = os.environ.get("AI_TALKS_FEEDS_REPO", "")
        if feeds_repo:
            push_to_feeds_repo([RSS_FILE, RSS_FILE_ZH], Path(feeds_repo), dry_run=True)
        return

    RSS_FILE.write_text(rss_content, encoding="utf-8")
    RSS_FILE_ZH.write_text(rss_zh_content, encoding="utf-8")
    print(f"Wrote {len(all_items)} item(s) to {RSS_FILE} and {RSS_FILE_ZH}")

    if tg_token and tg_chat_id:
        send_telegram(tg_token, tg_chat_id, accepted)
    else:
        print("(No TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID set — printing only)")
        for v in accepted:
            print(f"  {v['label']}: {v.get('title_zh') or v['title']}\n  {v['url']}\n")

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    seen_dict = load_seen_ids(state)
    for v in accepted:
        seen_dict[v["id"]] = now
    for vid_id in rejected_ids:  # definitive rejects — won't resurface
        seen_dict[vid_id] = now
    # IDs in neither accepted nor rejected_ids are left unmarked and will resurface
    # next run (useful for candidates rejected due to missing description).
    state["seen_ids"] = prune_seen_ids(seen_dict, RSS_RETENTION_DAYS)
    state["last_checked"] = now
    state["items"] = all_items
    save_state(state)
    print("State updated.")

    feeds_repo = os.environ.get("AI_TALKS_FEEDS_REPO", "")
    if feeds_repo:
        push_to_feeds_repo([RSS_FILE, RSS_FILE_ZH], Path(feeds_repo))


def main() -> None:
    parser = argparse.ArgumentParser(description="AI Talks Monitor")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview without writing files or updating state; Telegram still fires (prefixed [DRY RUN])")
    parser.add_argument("--lookback-days", type=int, default=None,
                        help="How many days back to search (overrides config's lookback_days)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Process only the first N entries per category (useful for testing)")

    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--fetch-candidates", action="store_true",
                      help="Search YouTube, apply heuristic filter, write candidates.json")
    mode.add_argument("--commit-file", metavar="FILE",
                      help="Read accepted.json (with description_clean/title_zh/description_zh) and write both English and Chinese RSS feeds")
    mode.add_argument("--commit", nargs="+", metavar="ID",
                      help="Accept these video IDs from candidates.json, write English RSS only (no Chinese feed)")

    args = parser.parse_args()

    if args.fetch_candidates:
        cmd_fetch_candidates(args)
    elif args.commit_file:
        cmd_commit_file(args)
    else:
        cmd_commit(args)


if __name__ == "__main__":
    main()

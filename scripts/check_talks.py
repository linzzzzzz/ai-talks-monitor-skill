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

import argparse
import json
import os
import re
import shutil
import subprocess
import time
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
CANDIDATES_FILE = SKILL_DIR / "candidates.json"

RSS_RETENTION_DAYS = 30

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
    return {"seen_ids": [], "last_checked": None, "items": []}


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


def search_youtube(query: str, published_after: str, api_key: str) -> list[dict]:
    search_resp = requests.get(
        "https://www.googleapis.com/youtube/v3/search",
        params={
            "part": "snippet",
            "q": query,
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
            "duration_min": parse_iso_duration_minutes(detail["contentDetails"]["duration"]),
            "url": f"https://www.youtube.com/watch?v={vid_id}",
        })
    return results


def search_youtube_ytdlp(query: str, published_after: str, max_results: int = 20) -> list[dict]:
    """Fallback YouTube search using yt-dlp (no API key required).

    Uses --flat-playlist for a fast initial search, returning results without
    descriptions. Call enrich_ytdlp_descriptions() on the filtered candidates
    afterward to fetch full descriptions via individual yt-dlp -J calls.

    Note: date filtering is approximate — yt-dlp doesn't support publishedAfter
    at query time, so we fetch 3x results and filter by upload_date post-fetch.

    Set YTDLP_COOKIES_FROM_BROWSER=chrome (or firefox/safari) if YouTube returns
    a bot-check error.
    """
    cutoff = datetime.fromisoformat(published_after.replace("Z", "+00:00")).date()
    cmd = [
        "yt-dlp", "--flat-playlist", "--dump-single-json",
        f"ytsearch{max_results * 3}:{query}",
    ]
    cookies_browser = os.environ.get("YTDLP_COOKIES_FROM_BROWSER", "")
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
                "yt-dlp bot-check triggered. Set YTDLP_COOKIES_FROM_BROWSER=chrome "
                "(or firefox/safari) to authenticate."
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
            published_at = (
                f"{upload_date_str[:4]}-{upload_date_str[4:6]}-{upload_date_str[6:]}T00:00:00Z"
            )
        else:
            published_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        results.append({
            "id": entry["id"],
            "title": entry.get("title", ""),
            "channel": entry.get("channel") or entry.get("uploader", ""),
            "description": entry.get("description") or "",
            "published_at": published_at,
            "duration_min": round((entry.get("duration") or 0) / 60),
            "url": f"https://www.youtube.com/watch?v={entry['id']}",
        })

        if len(results) >= max_results:
            break

    return results


class YtdlpBotCheck(Exception):
    pass


def ytdlp_fetch_description(video_id: str, cookies_browser: str = "") -> str:
    """Fetch full description for a single video using yt-dlp -J.

    Raises YtdlpBotCheck if YouTube returns a bot-check error.
    Returns empty string on other errors (e.g. video unavailable).
    """
    cmd = ["yt-dlp", "-J", f"https://www.youtube.com/watch?v={video_id}"]
    if cookies_browser:
        cmd += ["--cookies-from-browser", cookies_browser]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=True)
        return json.loads(proc.stdout).get("description") or ""
    except subprocess.CalledProcessError as e:
        stderr = e.stderr or ""
        if "Sign in to confirm" in stderr or "bot" in stderr.lower():
            raise YtdlpBotCheck()
        return ""
    except (json.JSONDecodeError, subprocess.TimeoutExpired):
        return ""


def enrich_ytdlp_descriptions(candidates: list[dict], delay: float = 1.5) -> None:
    """Fetch full descriptions for candidates in-place, with a delay between requests.

    Stops early if YouTube triggers a bot-check and prints a tip.
    """
    cookies_browser = os.environ.get("YTDLP_COOKIES_FROM_BROWSER", "")
    print(f"\nFetching descriptions for {len(candidates)} candidate(s) (yt-dlp, {delay}s delay)...")
    for i, video in enumerate(candidates):
        if i > 0:
            time.sleep(delay)
        print(f"  [{i + 1}/{len(candidates)}] {video['title'][:70]}")
        try:
            video["description"] = ytdlp_fetch_description(video["id"], cookies_browser)
        except YtdlpBotCheck:
            print(
                "  [bot-check] YouTube requires authentication for remaining descriptions.\n"
                "  Set YTDLP_COOKIES_FROM_BROWSER=chrome (or firefox/safari) to enable full descriptions."
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
        ET.SubElement(item, "description").text = (
            f"{item_data['channel']} · {item_data['duration_min']} min\n\n"
            f"{item_data['description']}"
        )

    ET.indent(rss, space="  ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(rss, encoding="unicode")


def cmd_fetch_candidates(args) -> None:
    """Search YouTube, apply heuristic filters, write candidates.json."""
    config = load_config()
    state = load_state()

    api_key = os.environ.get("YOUTUBE_API_KEY", "")
    if api_key:
        def do_search(query: str, published_after: str) -> list[dict]:
            return search_youtube(query, published_after, api_key)
    else:
        print("No YOUTUBE_API_KEY set — using yt-dlp fallback (date filtering approximate).\n")
        def do_search(query: str, published_after: str) -> list[dict]:
            return search_youtube_ytdlp(query, published_after)

    min_duration = config.get("min_duration_minutes", 20)
    lookback_days = args.lookback_days or config.get("lookback_days", 5)

    dt = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    published_after = dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    print(f"Searching for videos published after {published_after} ({lookback_days}d rolling window)\n")

    seen_ids: set[str] = set(state.get("seen_ids", []))
    all_candidates: list[dict] = []

    for leader in config.get("thought_leaders", []):
        print(f"Person: {leader['name']}...")
        try:
            videos = do_search(leader["search_query"], published_after)
        except (requests.exceptions.RequestException, RuntimeError) as e:
            print(f"  Search error: {e}")
            continue
        all_candidates.extend(
            collect_candidates(videos, leader["name"], seen_ids, min_duration)
        )

    topics_config = config.get("topics", {})
    if topics_config.get("enabled", False):
        for topic in topics_config.get("searches", []):
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

    if not api_key and all_candidates:
        enrich_ytdlp_descriptions(all_candidates)

    with open(CANDIDATES_FILE, "w") as f:
        json.dump(all_candidates, f, indent=2)

    print(f"\n{len(all_candidates)} candidate(s) written to {CANDIDATES_FILE}")


def push_to_feeds_repo(rss_file: Path, feeds_repo: Path, dry_run: bool = False) -> None:
    """Copy ai_talks.xml into the feeds repo and push to origin."""
    dest = feeds_repo / rss_file.name
    if dry_run:
        print(f"[DRY RUN] Would copy {rss_file} → {dest} and push")
        return
    shutil.copy(rss_file, dest)
    try:
        subprocess.run(["git", "-C", str(feeds_repo), "add", rss_file.name], check=True)
        result = subprocess.run(
            ["git", "-C", str(feeds_repo), "commit", "-m", "update ai_talks feed"],
            capture_output=True, text=True,
        )
        if result.returncode != 0 and "nothing to commit" in result.stdout + result.stderr:
            print("Feeds repo: nothing new to commit.")
            return
        result.check_returncode()
        subprocess.run(["git", "-C", str(feeds_repo), "push"], check=True)
        print(f"Pushed {rss_file.name} to {feeds_repo}")
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

    state = load_state()
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    tg_chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")

    cutoff = datetime.now(timezone.utc) - timedelta(days=RSS_RETENTION_DAYS)
    existing_items = [
        i for i in state.get("items", [])
        if datetime.fromisoformat(i["published_at"].replace("Z", "+00:00")) > cutoff
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
            push_to_feeds_repo(RSS_FILE, Path(feeds_repo), dry_run=True)
        return

    RSS_FILE.write_text(rss_content, encoding="utf-8")
    print(f"Wrote {len(all_items)} item(s) to {RSS_FILE}")

    if tg_token and tg_chat_id:
        send_telegram(tg_token, tg_chat_id, accepted)
    else:
        print("(No TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID set — printing only)")
        for v in accepted:
            print(f"  {v['label']}: {v['title']}\n  {v['url']}\n")

    seen_ids = set(state.get("seen_ids", [])) | {v["id"] for v in accepted}
    state["seen_ids"] = list(seen_ids)
    state["last_checked"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    state["items"] = all_items
    save_state(state)
    print("State updated.")

    feeds_repo = os.environ.get("AI_TALKS_FEEDS_REPO", "")
    if feeds_repo:
        push_to_feeds_repo(RSS_FILE, Path(feeds_repo))


def main() -> None:
    parser = argparse.ArgumentParser(description="AI Talks Monitor")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview without writing files or updating state; Telegram still fires (prefixed [DRY RUN])")
    parser.add_argument("--lookback-days", type=int, default=None,
                        help="How many days back to search (overrides state's last_checked)")

    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--fetch-candidates", action="store_true",
                      help="Search YouTube, apply heuristic filter, write candidates.json")
    mode.add_argument("--commit", nargs="+", metavar="ID",
                      help="Accept these video IDs from candidates.json, write RSS, update state")

    args = parser.parse_args()

    if args.fetch_candidates:
        cmd_fetch_candidates(args)
    else:
        cmd_commit(args)


if __name__ == "__main__":
    main()
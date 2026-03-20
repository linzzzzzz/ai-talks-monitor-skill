#!/usr/bin/env python3
"""
AI Talks Monitor — review workflow:

  Phase 1 — --fetch-candidates:
    Search YouTube, apply heuristic filters, write grouped candidate files.

  Phase 2 — review decisions:
    The agent writes review.json with accepted/rejected IDs only.

  Phase 3 — --prepare-accepted review.json:
    Build accepted.json as an enrichment draft for accepted items only.

  Phase 4 — --commit-file accepted.json:
    Publish accepted items, write RSS feeds, update state, notify subscribers.

Persistent outputs (output/):
  - state.json, ai_talks.xml, ai_talks_zh.xml

Ephemeral outputs (output/scratch/ — wiped each --fetch-candidates run):
  - candidates.json, candidates_people[_N].json, candidates_orgs[_N].json, candidates_channels[_N].json
  - review.json, review_{category}.json, enrichment.json, accepted.json
"""

from __future__ import annotations

import argparse
import html
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
OUTPUT_DIR = SKILL_DIR / "output"
SCRATCH_DIR = OUTPUT_DIR / "scratch"
STATE_FILE = OUTPUT_DIR / "state.json"
RSS_FILE = OUTPUT_DIR / "ai_talks.xml"
RSS_FILE_ZH = OUTPUT_DIR / "ai_talks_zh.xml"
CANDIDATES_FILE = SCRATCH_DIR / "candidates.json"
PEOPLE_CANDIDATES_FILE = SCRATCH_DIR / "candidates_people.json"
ORG_CANDIDATES_FILE = SCRATCH_DIR / "candidates_orgs.json"
CHANNEL_CANDIDATES_FILE = SCRATCH_DIR / "candidates_channels.json"
REVIEW_FILE = SCRATCH_DIR / "review.json"
ACCEPTED_FILE = SCRATCH_DIR / "accepted.json"

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


OUTPUT_DIR.mkdir(exist_ok=True)
SCRATCH_DIR.mkdir(exist_ok=True)


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


def write_json(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def is_derivative(title: str) -> bool:
    title_lower = title.lower()
    return any(kw.lower() in title_lower for kw in DERIVATIVE_KEYWORDS)


def is_person_label(label: str) -> bool:
    return not label.startswith("Org: ") and not label.startswith("Channel: ")


def mentions_person(video: dict, person_name: str) -> bool:
    haystack = " ".join([
        html.unescape(video.get("title", "")),
        html.unescape(video.get("description", "")),
    ]).lower()
    return person_name.lower() in haystack


def strip_description_noise(text: str) -> str:
    """Remove URLs, hashtag blocks, and boilerplate from a YouTube description."""
    text = re.sub(r'https?://\S+', '', text)
    text = re.sub(r'(?:#\S+[\s]*){3,}', '', text)
    text = re.sub(
        r'(?i)^[\s]*(?:subscribe to|follow us on|like our|join the conversation'
        r'|got a story|send us an email|your voice matters).*$',
        '', text, flags=re.MULTILINE,
    )
    text = re.sub(r'^-{3,}\s*$', '', text, flags=re.MULTILINE)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


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


def fetch_youtube_video_details(video_ids: list[str], api_key: str) -> dict[str, dict]:
    """Fetch full metadata for YouTube video IDs via videos.list."""
    details: dict[str, dict] = {}
    for start in range(0, len(video_ids), 50):
        batch_ids = video_ids[start:start + 50]
        if not batch_ids:
            continue
        details_resp = requests.get(
            "https://www.googleapis.com/youtube/v3/videos",
            params={"part": "contentDetails,snippet", "id": ",".join(batch_ids), "key": api_key},
            timeout=15,
        )
        details_resp.raise_for_status()
        for item in details_resp.json().get("items", []):
            details[item["id"]] = item
    return details


def enrich_with_youtube_api_metadata(candidates: list[dict], api_key: str) -> None:
    """Backfill exact metadata for candidates in-place using videos.list."""
    video_ids = [video["id"] for video in candidates if video.get("id")]
    if not video_ids:
        return

    print(f"\nFetching metadata for {len(video_ids)} candidate(s) via YouTube Data API...")
    details = fetch_youtube_video_details(video_ids, api_key)
    for video in candidates:
        detail = details.get(video["id"])
        if not detail:
            continue
        snippet = detail.get("snippet", {})
        content = detail.get("contentDetails", {})
        if snippet.get("title"):
            video["title"] = snippet["title"]
        if snippet.get("channelTitle"):
            video["channel"] = snippet["channelTitle"]
        if "description" in snippet:
            video["description"] = snippet.get("description") or ""
        if snippet.get("publishedAt"):
            video["published_at"] = snippet["publishedAt"]
            video["published_at_precision"] = "exact"
        if content.get("duration"):
            video["duration_min"] = parse_iso_duration_minutes(content["duration"])


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
        if is_person_label(label) and not mentions_person(video, label):
            print(f"  [person mismatch] {video['title']}")
            continue
        video["label"] = label
        candidates.append(video)
        print(f"  [candidate] {video['title']} ({video['duration_min']}min)")
    return candidates


def build_accepted_draft(candidates: list[dict], accepted_entries: list[dict], rejected_ids: list[str]) -> dict:
    """Build accepted.json draft from review decisions.

    accepted_entries: list of {id, reason} dicts (new format) or plain ID strings (legacy).
    """
    candidates_by_id = {v["id"]: v for v in candidates}
    accepted = []
    for entry in accepted_entries:
        if isinstance(entry, dict):
            vid_id = entry.get("id", "")
            reason = entry.get("reason", "")
        else:
            vid_id = entry
            reason = ""
        video = candidates_by_id.get(vid_id)
        if not video:
            continue
        accepted.append({
            "id": video["id"],
            "label": video.get("label", ""),
            "title": video.get("title", ""),
            "channel": video.get("channel", ""),
            "description": strip_description_noise(video.get("description", "")),
            "published_at": video.get("published_at"),
            "duration_min": video.get("duration_min"),
            "url": video.get("url", ""),
            "accept_reason": reason,
            "description_clean": "",
            "title_zh": "",
            "description_zh": "",
        })
    return {"accepted": accepted, "rejected": rejected_ids}


def display_source(video: dict) -> str:
    channel = video.get("channel")
    if channel:
        return channel
    label = video.get("label", "")
    if isinstance(label, str) and label.startswith("Channel: "):
        return label.removeprefix("Channel: ")
    return ""


def build_notification_chunks(
    talks: list[dict],
    dry_run: bool = False,
    max_chars: int = 3500,
    language: str = "original",
    include_excerpt: bool = False,
) -> list[str]:
    max_chars = 3500
    header = f"AI Talks Monitor: {len(talks)} new talk(s)"
    if dry_run:
        header = f"[DRY RUN]\n{header}"

    chunks = []
    current = header

    for index, video in enumerate(talks, start=1):
        source = display_source(video)
        title = video.get("title_zh") if language == "zh" else None
        meta = f"{source} · {video['duration_min']} min" if source else f"{video['duration_min']} min"
        excerpt = ""
        if include_excerpt:
            excerpt = notification_excerpt(video, language=language)
        excerpt_block = f"\n{excerpt}" if excerpt else ""
        divider = "\n\n──────────\n" if index > 1 else "\n\n"
        item_block = (
            f"{divider}{index}. [{video['label']}] {title or video['title']}\n"
            f"{meta}\n"
            f"{excerpt_block}"
            f"\n\n{video['url']}"
        )
        if len(current) + len(item_block) > max_chars:
            chunks.append(current)
            current = (
                f"{header} (cont.)\n\n{index}. [{video['label']}] {title or video['title']}\n"
                f"{meta}{excerpt_block}\n\n{video['url']}"
            )
        else:
            current += item_block

    chunks.append(current)
    return chunks


def send_telegram(
    bot_token: str,
    chat_id: str,
    talks: list[dict],
    dry_run: bool = False,
    language: str = "original",
    include_excerpt: bool = False,
) -> None:
    for text in build_notification_chunks(
        talks,
        dry_run=dry_run,
        language=language,
        include_excerpt=include_excerpt,
    ):
        requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "link_preview_options": {"is_disabled": True},
            },
            timeout=10,
        ).raise_for_status()


def send_openclaw(
    binary: str,
    channel: str,
    target: str,
    talks: list[dict],
    account: str = "",
    dry_run: bool = False,
    language: str = "original",
    include_excerpt: bool = False,
) -> None:
    if not shutil.which(binary):
        raise RuntimeError(f"OpenClaw binary not found on PATH: {binary}")

    for text in build_notification_chunks(
        talks,
        dry_run=dry_run,
        language=language,
        include_excerpt=include_excerpt,
    ):
        cmd = [
            binary, "message", "send",
            "--channel", channel,
            "--target", target,
            "--message", text,
        ]
        if account:
            cmd += ["--account", account]
        subprocess.run(cmd, check=True)


def notify_talks(config: dict, talks: list[dict], dry_run: bool = False) -> None:
    if not talks:
        return

    notifications = config.get("notifications", {})
    backend = notifications.get("backend", "native")
    language = notifications.get("language", "original")
    include_excerpt = notifications.get("include_excerpt", False)

    if backend == "none":
        print("(Notifications disabled)")
        return

    if backend in {"native", "telegram"}:
        native_config = notifications.get("native", {})
        channel = (native_config.get("channel") or "telegram").strip() or "telegram"
        target = (native_config.get("target") or "").strip()
        if channel != "telegram":
            print(f"(Unsupported native notification channel: {channel})")
            return
        tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        tg_chat_id = target or os.environ.get("TELEGRAM_CHAT_ID", "")
        if tg_token and tg_chat_id:
            send_telegram(
                tg_token,
                tg_chat_id,
                talks,
                dry_run=dry_run,
                language=language,
                include_excerpt=include_excerpt,
            )
        else:
            print("(No TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID set)")
        return

    if backend == "openclaw":
        openclaw_config = notifications.get("openclaw", {})
        channel = (openclaw_config.get("channel") or "").strip()
        target = (openclaw_config.get("target") or "").strip()
        binary = (openclaw_config.get("binary") or "openclaw").strip() or "openclaw"
        account = (openclaw_config.get("account") or "").strip()
        if not channel or not target:
            print("(OpenClaw notifications configured, but notifications.openclaw.channel/target is missing)")
            return
        send_openclaw(
            binary,
            channel,
            target,
            talks,
            account=account,
            dry_run=dry_run,
            language=language,
            include_excerpt=include_excerpt,
        )
        return

    print(f"(Unknown notifications.backend: {backend})")


def notification_excerpt(video: dict, language: str = "original", max_chars: int = 300) -> str:
    text = (
        video.get("description_zh")
        if language == "zh"
        else video.get("description_clean") or video.get("description")
    ) or ""
    if not text:
        return ""

    lines = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if re.match(r"^\d{1,2}:\d{2}(?::\d{2})?\s*[-–—]", line):
            continue
        if re.search(r"https?://|www\.", line):
            continue
        if line.startswith("#"):
            continue
        lines.append(line)
        if len(" ".join(lines)) >= max_chars:
            break

    compact = re.sub(r"\s+", " ", " ".join(lines)).strip()
    return compact


def print_accepted_items(talks: list[dict], prefer_zh: bool = False) -> None:
    for video in talks:
        title = video.get("title_zh") if prefer_zh else None
        print(f"  {video['label']}: {title or video['title']}\n  {video['url']}\n")


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
        source = display_source(item_data)
        ET.SubElement(item, "title").text = f"[{item_data['label']}] {item_data['title']}"
        ET.SubElement(item, "link").text = item_data["url"]
        ET.SubElement(item, "guid", isPermaLink="false").text = item_data["id"]
        ET.SubElement(item, "pubDate").text = youtube_ts_to_rfc2822(item_data["published_at"])
        ET.SubElement(item, "author").text = source
        description = item_data.get("description_clean") or item_data.get("description", "")
        meta = f"{source} · {item_data['duration_min']} min" if source else f"{item_data['duration_min']} min"
        thumb = f'<img src="https://img.youtube.com/vi/{item_data["id"]}/hqdefault.jpg" width="480" /><br/>'
        ET.SubElement(item, "description").text = (
            f"{thumb}{meta}<br/><br/>"
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
        source = display_source(item_data)
        title = item_data.get("title_zh") or item_data["title"]
        ET.SubElement(item, "title").text = f"[{item_data['label']}] {title}"
        ET.SubElement(item, "link").text = item_data["url"]
        ET.SubElement(item, "guid", isPermaLink="false").text = item_data["id"]
        ET.SubElement(item, "pubDate").text = youtube_ts_to_rfc2822(item_data["published_at"])
        ET.SubElement(item, "author").text = source
        summary = (
            item_data.get("description_zh")
            or item_data.get("description_clean")
            or item_data.get("description", "")
        )
        meta = f"{source} · {item_data['duration_min']} 分钟" if source else f"{item_data['duration_min']} 分钟"
        thumb = f'<img src="https://img.youtube.com/vi/{item_data["id"]}/hqdefault.jpg" width="480" /><br/>'
        ET.SubElement(item, "description").text = (
            f"{thumb}{meta}<br/><br/>{summary}"
        )

    ET.indent(rss, space="  ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(rss, encoding="unicode")


def cmd_fetch_candidates(args) -> None:
    """Search YouTube, apply heuristic filters, write grouped candidate files."""
    # Wipe scratch dir to prevent stale files from previous runs.
    if SCRATCH_DIR.exists():
        shutil.rmtree(SCRATCH_DIR)
    SCRATCH_DIR.mkdir(exist_ok=True)

    config = load_config()
    state = load_state()
    ytdlp_search_config = config.get("ytdlp_search", {})
    backend_config = config.get("backends", {})
    use_this_month_filter = ytdlp_search_config.get("use_this_month_filter", False)

    api_key = os.environ.get("YOUTUBE_API_KEY", "")
    cookies_browser = ytdlp_search_config.get("cookies_from_browser") or ""
    search_backend = backend_config.get("search", "auto")
    metadata_backend = backend_config.get("metadata", "auto")

    if search_backend == "auto":
        resolved_search_backend = "youtube_api" if api_key else "yt_dlp"
    else:
        resolved_search_backend = search_backend

    if resolved_search_backend == "youtube_api" and not api_key:
        print(
            "Config requests backends.search: youtube_api but YOUTUBE_API_KEY is not set.\n"
            "Set YOUTUBE_API_KEY or switch backends.search to yt_dlp/auto."
        )
        return

    if resolved_search_backend == "youtube_api":
        def do_search(query: str, published_after: str) -> list[dict]:
            return search_youtube(query, published_after, api_key)
    elif resolved_search_backend == "yt_dlp":
        print("Using yt-dlp for search discovery (date filtering approximate).\n")

        def do_search(query: str, published_after: str) -> list[dict]:
            return search_youtube_ytdlp(
                query,
                published_after,
                use_this_month_filter=use_this_month_filter,
                cookies_browser=cookies_browser,
            )
    else:
        print(f"Unknown backends.search value: {resolved_search_backend}")
        return

    if metadata_backend == "auto":
        resolved_metadata_backend = "youtube_api" if api_key else "yt_dlp"
    else:
        resolved_metadata_backend = metadata_backend

    if resolved_metadata_backend == "youtube_api" and not api_key:
        print(
            "Config requests backends.metadata: youtube_api but YOUTUBE_API_KEY is not set.\n"
            "Set YOUTUBE_API_KEY or switch backends.metadata to yt_dlp/auto."
        )
        return

    min_duration = config.get("min_duration_minutes", 20)
    lookback_days = args.lookback_days or config.get("lookback_days", 5)

    dt = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    published_after = dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    print(f"Searching for videos published after {published_after} ({lookback_days}d rolling window)\n")

    seen_ids: set[str] = set(load_seen_ids(state).keys())
    person_candidates: list[dict] = []
    org_candidates: list[dict] = []
    channel_candidates: list[dict] = []
    limit = args.limit  # None means no limit

    ytdlp_delay = 0.5  # seconds between yt-dlp search calls to avoid bot-checks

    for leader in config.get("thought_leaders", [])[:limit]:
        print(f"Person: {leader['name']}...")
        try:
            videos = do_search(leader["search_query"], published_after)
        except (requests.exceptions.RequestException, RuntimeError) as e:
            print(f"  Search error: {e}")
            continue
        person_candidates.extend(
            collect_candidates(videos, leader["name"], seen_ids, min_duration)
        )
        if resolved_search_backend == "yt_dlp":
            time.sleep(ytdlp_delay)

    orgs_config = config.get("orgs", {})
    if orgs_config.get("enabled", False):
        for org_entry in orgs_config.get("searches", [])[:limit]:
            org_name = org_entry["name"]
            org_min = org_entry.get("min_duration_minutes", min_duration)
            print(f"Org: {org_name}...")
            try:
                videos = do_search(org_entry["search_query"], published_after)
            except (requests.exceptions.RequestException, RuntimeError) as e:
                print(f"  Search error: {e}")
                continue
            new_candidates = collect_candidates(videos, f"Org: {org_name}", seen_ids, org_min)
            candidate_org = org_entry.get("org", "")
            for c in new_candidates:
                c["org"] = candidate_org
            org_candidates.extend(new_candidates)
            if resolved_search_backend == "yt_dlp":
                time.sleep(ytdlp_delay)

    channels_config = config.get("channels", {})
    if channels_config.get("enabled", False):
        for channel in channels_config.get("list", [])[:limit]:
            channel_name = channel["name"]
            print(f"Channel: {channel_name}...")
            try:
                if resolved_search_backend == "youtube_api" and channel.get("channel_id"):
                    videos = search_youtube_channel(channel["channel_id"], published_after, api_key)
                elif resolved_search_backend == "yt_dlp" and channel.get("url"):
                    videos = search_youtube_channel_ytdlp(channel["url"], published_after, cookies_browser=cookies_browser)
                    time.sleep(ytdlp_delay)
                else:
                    if resolved_search_backend == "youtube_api":
                        reason = "no channel_id configured"
                    else:
                        reason = "no url configured"
                    print(f"  Skipping: {reason}")
                    continue
            except (requests.exceptions.RequestException, RuntimeError) as e:
                print(f"  Search error: {e}")
                continue
            channel_candidates.extend(
                collect_candidates(videos, f"Channel: {channel_name}", seen_ids, min_duration)
            )

    all_candidates = person_candidates + org_candidates + channel_candidates

    # Backfill exact metadata for yt-dlp candidates when non-flat extraction is available.
    needs_enrichment = [
        c for c in all_candidates
        if not c.get("description") or c.get("published_at_precision") != "exact"
    ]
    if needs_enrichment:
        if resolved_metadata_backend == "youtube_api":
            try:
                enrich_with_youtube_api_metadata(needs_enrichment, api_key)
            except requests.exceptions.RequestException as e:
                print(f"Metadata enrichment error: {e}")
        elif resolved_metadata_backend == "yt_dlp":
            enrich_ytdlp_descriptions(needs_enrichment, cookies_browser=cookies_browser)
        else:
            print(f"Unknown backends.metadata value: {resolved_metadata_backend}")

    # Post-enrichment date filter: yt-dlp flat search may return videos without
    # dates that slip through the initial cutoff. After metadata enrichment fills
    # in exact dates, re-apply the cutoff to remove stale videos.
    cutoff_date = datetime.fromisoformat(published_after.replace("Z", "+00:00")).date()
    before_count = len(all_candidates)
    all_candidates = [
        c for c in all_candidates
        if not c.get("published_at")
        or datetime.fromisoformat(c["published_at"].replace("Z", "+00:00")).date() >= cutoff_date
    ]
    person_candidates = [c for c in all_candidates if is_person_label(c.get("label", ""))]
    org_candidates = [c for c in all_candidates if c.get("label", "").startswith("Org: ")]
    channel_candidates = [c for c in all_candidates if c.get("label", "").startswith("Channel: ")]
    if before_count != len(all_candidates):
        print(f"  Post-enrichment date filter removed {before_count - len(all_candidates)} stale candidate(s)")

    write_json(CANDIDATES_FILE, all_candidates)

    # Split each category into chunk files of max CHUNK_SIZE items so
    # every file fits in a single agent Read call (~200 lines).
    CHUNK_SIZE = 15

    def _slim_for_review(video: dict, max_desc: int = 500) -> dict:
        """Strip a candidate to only the fields the model needs for classification."""
        desc = video.get("description", "")
        if len(desc) > max_desc:
            desc = desc[:max_desc] + "..."
        result = {
            "id": video["id"],
            "title": video.get("title", ""),
            "channel": video.get("channel", ""),
            "description": desc,
            "label": video.get("label", ""),
        }
        if video.get("org"):
            result["org"] = video["org"]
        return result

    def _write_chunks(base_name: str, items: list[dict]) -> list[Path]:
        """Write items into chunk files; return list of paths written."""
        if not items:
            return []
        paths: list[Path] = []
        for i in range(0, len(items), CHUNK_SIZE):
            chunk = [_slim_for_review(v) for v in items[i : i + CHUNK_SIZE]]
            suffix = f"_{i // CHUNK_SIZE + 1}" if len(items) > CHUNK_SIZE else ""
            path = SCRATCH_DIR / f"{base_name}{suffix}.json"
            write_json(path, chunk)
            paths.append(path)
        return paths

    people_files = _write_chunks("candidates_people", person_candidates)
    org_files = _write_chunks("candidates_orgs", org_candidates)
    channel_files = _write_chunks("candidates_channels", channel_candidates)

    print(f"\n{len(all_candidates)} candidate(s) written:")

    # Print classification plan: one subagent per category.
    print("\n--- CLASSIFICATION PLAN ---")
    print("Spawn up to 3 subagents (one per category) to classify in parallel.\n")
    categories = [
        ("people", people_files, person_candidates),
        ("orgs", org_files, org_candidates),
        ("channels", channel_files, channel_candidates),
    ]
    for label, files, items in categories:
        if not files:
            continue
        print(f"  Subagent '{label}': {len(items)} items across {len(files)} file(s)")
        for path in files:
            chunk_items = json.loads(path.read_text())
            rel = path.relative_to(SKILL_DIR)
            print(f"    - {rel} ({len(chunk_items)} items)")
        print(f"    → write output/scratch/review_{label}.json\n")

    print(f"Reference files to inline in each subagent task:")
    print(f"  {SKILL_DIR / 'CLASSIFY.md'}")
    print(f"  {STATE_FILE}")

    print(f"\nAfter all subagents complete, merge review files into output/scratch/review.json, then run --prepare-accepted.")
    print("--- END CLASSIFICATION PLAN ---")


def cmd_prepare_accepted(args) -> None:
    """Read review.json with accepted/rejected IDs and write accepted.json draft."""
    review_file = Path(args.prepare_accepted)
    if not review_file.exists():
        print(f"File not found: {review_file}")
        return
    if not CANDIDATES_FILE.exists():
        print("No candidates.json found. Run --fetch-candidates first.")
        return

    review = json.loads(review_file.read_text())
    raw_accepted = review.get("accepted", []) if isinstance(review, dict) else []
    rejected_ids = review.get("rejected", []) if isinstance(review, dict) else []
    uncertain_ids = review.get("uncertain", []) if isinstance(review, dict) else []

    # Normalise: support both new [{id, reason}] format and legacy ["ID"] format.
    accepted_entries = [
        entry if isinstance(entry, dict) else {"id": entry, "reason": ""}
        for entry in raw_accepted
    ]

    if not accepted_entries and not rejected_ids and not uncertain_ids:
        print("No accepted, rejected, or uncertain IDs found in review file.")
        return

    candidates = json.loads(CANDIDATES_FILE.read_text())

    # --- Coverage check: every candidate must be accounted for ---
    reviewed_ids = {
        (e["id"] if isinstance(e, dict) else e) for e in raw_accepted
    } | set(rejected_ids) | set(uncertain_ids)
    candidate_ids = {c["id"] for c in candidates}
    missing = candidate_ids - reviewed_ids
    if missing:
        print(
            f"ERROR: {len(missing)} candidate(s) not classified. "
            f"Every candidate must appear in accepted, rejected, or uncertain.\n"
            f"Missing IDs:\n"
        )
        for mid in sorted(missing):
            title = next((c["title"] for c in candidates if c["id"] == mid), "?")
            print(f"  {mid}: {title[:80]}")
        print(
            f"\nGo back and classify the missing candidates, "
            f"then update {review_file.name} and re-run --prepare-accepted."
        )
        return

    draft = build_accepted_draft(candidates, accepted_entries, rejected_ids)
    write_json(ACCEPTED_FILE, draft)
    summary = (
        f"Wrote accepted draft to {ACCEPTED_FILE}\n"
        f"  accepted: {len(draft['accepted'])}\n"
        f"  rejected: {len(draft['rejected'])}"
    )
    if uncertain_ids:
        summary += f"\n  uncertain: {len(uncertain_ids)} (will resurface next run)"
    print(summary)


ENRICHMENT_FILE = SCRATCH_DIR / "enrichment.json"


def cmd_apply_enrichment(args) -> None:
    """Merge enrichment.json into accepted.json, filling description_clean/title_zh/description_zh."""
    enrichment_file = Path(args.apply_enrichment)
    if not enrichment_file.exists():
        print(f"File not found: {enrichment_file}")
        return
    if not ACCEPTED_FILE.exists():
        print("No accepted.json found. Run --prepare-accepted first.")
        return

    accepted_data = json.loads(ACCEPTED_FILE.read_text())
    enrichments = json.loads(enrichment_file.read_text())

    # Build lookup by ID.
    if isinstance(enrichments, list):
        enrich_by_id = {e["id"]: e for e in enrichments}
    elif isinstance(enrichments, dict) and "items" in enrichments:
        enrich_by_id = {e["id"]: e for e in enrichments["items"]}
    else:
        print("Unexpected enrichment format. Expected a list or {items: [...]}.")
        return

    accepted_items = accepted_data.get("accepted", [])
    accepted_ids = {item["id"] for item in accepted_items}

    # Check all accepted items have enrichment.
    missing = accepted_ids - set(enrich_by_id.keys())
    if missing:
        print(
            f"ERROR: {len(missing)} accepted item(s) missing from enrichment file:\n"
        )
        for mid in sorted(missing):
            title = next((a["title"] for a in accepted_items if a["id"] == mid), "?")
            print(f"  {mid}: {title[:80]}")
        print(f"\nAdd the missing items to {enrichment_file.name} and re-run.")
        return

    # Check required fields.
    required_fields = ["description_clean", "title_zh", "description_zh"]
    for item_id, enrich in enrich_by_id.items():
        if item_id not in accepted_ids:
            continue
        empty = [f for f in required_fields if not enrich.get(f, "").strip()]
        if empty:
            print(f"ERROR: Item {item_id} missing fields: {', '.join(empty)}")
            return

    # Merge.
    updated = 0
    for item in accepted_items:
        enrich = enrich_by_id.get(item["id"])
        if enrich:
            for field in required_fields:
                item[field] = enrich[field]
            updated += 1

    write_json(ACCEPTED_FILE, accepted_data)
    print(
        f"Enrichment applied to {ACCEPTED_FILE}\n"
        f"  {updated} item(s) updated with description_clean, title_zh, description_zh"
    )


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
    config = load_config()
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
        notify_talks(config, accepted, dry_run=True)
        print(f"\n[DRY RUN] Would write {len(accepted)} new item(s) to {RSS_FILE}")
        feeds_repo = os.environ.get("AI_TALKS_FEEDS_REPO", "")
        if feeds_repo:
            push_to_feeds_repo([RSS_FILE], Path(feeds_repo), dry_run=True)
        return

    RSS_FILE.write_text(rss_content, encoding="utf-8")
    print(f"Wrote {len(all_items)} item(s) to {RSS_FILE}")

    backend = config.get("notifications", {}).get("backend", "telegram")
    if backend == "none":
        print("(Notifications disabled — printing only)")
        print_accepted_items(accepted)
    else:
        notify_talks(config, accepted)
        if backend in {"native", "telegram"} and not (
            os.environ.get("TELEGRAM_BOT_TOKEN", "") and os.environ.get("TELEGRAM_CHAT_ID", "")
        ):
            print("(No TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID set — printing only)")
            print_accepted_items(accepted)
        elif backend not in {"native", "telegram", "openclaw"}:
            print_accepted_items(accepted)

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
    """Read accepted.json draft/final file and write both English and Chinese RSS feeds."""
    config = load_config()
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
        notify_talks(config, accepted, dry_run=True)
        print(f"\n[DRY RUN] Would write {len(accepted)} new item(s) to {RSS_FILE} and {RSS_FILE_ZH}")
        feeds_repo = os.environ.get("AI_TALKS_FEEDS_REPO", "")
        if feeds_repo:
            push_to_feeds_repo([RSS_FILE, RSS_FILE_ZH], Path(feeds_repo), dry_run=True)
        return

    RSS_FILE.write_text(rss_content, encoding="utf-8")
    RSS_FILE_ZH.write_text(rss_zh_content, encoding="utf-8")
    print(f"Wrote {len(all_items)} item(s) to {RSS_FILE} and {RSS_FILE_ZH}")

    backend = config.get("notifications", {}).get("backend", "telegram")
    if backend == "none":
        print("(Notifications disabled — printing only)")
        print_accepted_items(accepted, prefer_zh=True)
    else:
        notify_talks(config, accepted)
        if backend in {"native", "telegram"} and not (
            os.environ.get("TELEGRAM_BOT_TOKEN", "") and os.environ.get("TELEGRAM_CHAT_ID", "")
        ):
            print("(No TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID set — printing only)")
            print_accepted_items(accepted, prefer_zh=True)
        elif backend not in {"native", "telegram", "openclaw"}:
            print_accepted_items(accepted, prefer_zh=True)

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
                      help="Search YouTube, apply heuristic filter, write grouped candidate files")
    mode.add_argument("--prepare-accepted", metavar="FILE",
                      help="Read review.json (accepted/rejected IDs only) and write accepted.json draft")
    mode.add_argument("--apply-enrichment", metavar="FILE",
                      help="Merge enrichment.json into accepted.json (fills description_clean, title_zh, description_zh)")
    mode.add_argument("--commit-file", metavar="FILE",
                      help="Read accepted.json (draft/final) and write both English and Chinese RSS feeds")
    mode.add_argument("--commit", nargs="+", metavar="ID",
                      help="Accept these video IDs from candidates.json, write English RSS only (no Chinese feed)")

    args = parser.parse_args()

    if args.fetch_candidates:
        cmd_fetch_candidates(args)
    elif args.prepare_accepted:
        cmd_prepare_accepted(args)
    elif args.apply_enrichment:
        cmd_apply_enrichment(args)
    elif args.commit_file:
        cmd_commit_file(args)
    else:
        cmd_commit(args)


if __name__ == "__main__":
    main()

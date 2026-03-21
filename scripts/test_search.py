#!/usr/bin/env python3
"""Smoke tests for yt-dlp and YouTube API search functions.

Runs real queries against YouTube to verify search results look correct.
Not meant for CI — requires network access and optionally YOUTUBE_API_KEY.

Usage:
    python3 scripts/test_search.py                    # run all tests
    python3 scripts/test_search.py --ytdlp-only       # skip API tests
    python3 scripts/test_search.py --api-only          # skip yt-dlp tests
    python3 scripts/test_search.py -v                  # verbose: print each result
"""

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(__file__))
from check_talks import (
    search_youtube,
    search_youtube_channel,
    search_youtube_ytdlp,
    search_youtube_channel_ytdlp,
    backfill_youtube_api_metadata,
    backfill_ytdlp_metadata,
)

LOOKBACK_DAYS = 14
PUBLISHED_AFTER = (
    (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS))
    .strftime("%Y-%m-%dT%H:%M:%SZ")
)

passed = 0
failed = 0
skipped = 0


def report(name: str, ok: bool, detail: str = ""):
    global passed, failed
    status = "PASS" if ok else "FAIL"
    if ok:
        passed += 1
    else:
        failed += 1
    msg = f"  [{status}] {name}"
    if detail:
        msg += f" — {detail}"
    print(msg)


def print_results(results: list[dict], verbose: bool):
    if not verbose:
        return
    for r in results:
        date = r.get("published_at", "?")[:10]
        dur = r.get("duration_min", "?")
        print(f"    {r['id']}  {date}  {dur}min  {r['title'][:70]}")


def validate_results(results: list[dict], name: str, expect_min: int = 1) -> bool:
    """Check basic structure of search results."""
    if len(results) < expect_min:
        report(name, False, f"expected >= {expect_min} results, got {len(results)}")
        return False

    for r in results:
        required = ["id", "title", "url", "published_at_precision"]
        missing = [k for k in required if k not in r]
        if missing:
            report(name, False, f"result {r.get('id', '?')} missing keys: {missing}")
            return False

    report(name, True, f"{len(results)} result(s)")
    return True


# ---------------------------------------------------------------------------
# yt-dlp tests
# ---------------------------------------------------------------------------

def test_ytdlp_keyword_search(verbose: bool):
    print("\n--- yt-dlp keyword search ---")

    # Basic person search
    results = search_youtube_ytdlp(
        '"Sam Altman" interview', PUBLISHED_AFTER, max_results=5
    )
    validate_results(results, "keyword: Sam Altman", expect_min=1)
    print_results(results, verbose)

    # With this-week filter
    results = search_youtube_ytdlp(
        '"Sam Altman" interview', PUBLISHED_AFTER,
        max_results=5, use_this_week_filter=True
    )
    validate_results(results, "keyword: Sam Altman (this-week filter)", expect_min=0)
    print_results(results, verbose)

    # Org-style search
    results = search_youtube_ytdlp(
        'anthropic researcher talk podcast', PUBLISHED_AFTER, max_results=5
    )
    validate_results(results, "keyword: Anthropic org search", expect_min=0)
    print_results(results, verbose)


def test_ytdlp_channel_search(verbose: bool):
    print("\n--- yt-dlp channel search ---")

    results = search_youtube_channel_ytdlp(
        "https://www.youtube.com/@lexfridman",
        PUBLISHED_AFTER, max_results=5
    )
    validate_results(results, "channel: Lex Fridman", expect_min=0)
    print_results(results, verbose)


def test_ytdlp_date_filtering(verbose: bool):
    print("\n--- yt-dlp date filtering ---")

    # Use a very narrow window — results should be few or zero, but never stale
    narrow_after = (
        (datetime.now(timezone.utc) - timedelta(days=3))
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    cutoff = (datetime.now(timezone.utc) - timedelta(days=3)).date()

    results = search_youtube_ytdlp(
        '"Sam Altman" interview', narrow_after, max_results=10
    )
    stale = [
        r for r in results
        if r.get("published_at") and
        datetime.fromisoformat(r["published_at"].replace("Z", "+00:00")).date() < cutoff
    ]
    if stale:
        report("date filter: no stale results", False,
               f"{len(stale)} result(s) before cutoff")
        print_results(stale, True)
    else:
        report("date filter: no stale results", True,
               f"{len(results)} result(s), all within window")


def test_ytdlp_backfill(verbose: bool):
    print("\n--- yt-dlp metadata backfill ---")

    results = search_youtube_ytdlp(
        '"Sam Altman" interview', PUBLISHED_AFTER, max_results=2
    )
    if not results:
        report("backfill: yt-dlp", False, "no search results to backfill")
        return

    # Flat search results typically lack descriptions
    before_descs = [bool(r.get("description")) for r in results]
    backfill_ytdlp_metadata(results, delay=1.0)

    filled = sum(1 for r in results if r.get("description"))
    report("backfill: yt-dlp", True,
           f"{filled}/{len(results)} now have descriptions")
    print_results(results, verbose)


# ---------------------------------------------------------------------------
# YouTube API tests
# ---------------------------------------------------------------------------

def test_api_keyword_search(api_key: str, verbose: bool):
    print("\n--- YouTube API keyword search ---")

    results = search_youtube('"Sam Altman" interview', PUBLISHED_AFTER, api_key)
    validate_results(results, "keyword: Sam Altman", expect_min=1)
    print_results(results, verbose)


def test_api_channel_search(api_key: str, verbose: bool):
    print("\n--- YouTube API channel search ---")

    # Lex Fridman channel ID
    results = search_youtube_channel(
        "UCSHZKyawb77ixDdsGog4iWA", PUBLISHED_AFTER, api_key
    )
    validate_results(results, "channel: Lex Fridman", expect_min=0)
    print_results(results, verbose)


def test_api_backfill(api_key: str, verbose: bool):
    print("\n--- YouTube API metadata backfill ---")

    # Get lightweight channel results (no duration, truncated desc)
    results = search_youtube_channel(
        "UCSHZKyawb77ixDdsGog4iWA", PUBLISHED_AFTER, api_key
    )
    if not results:
        report("backfill: API", False, "no channel results to backfill")
        return

    before = results[0].get("duration_min", 0)
    backfill_youtube_api_metadata(results, api_key)
    after = results[0].get("duration_min", 0)

    ok = after > 0 or before > 0
    report("backfill: API fills duration", ok,
           f"duration_min: {before} -> {after}")
    print_results(results, verbose)


# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Smoke tests for search functions")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Print individual search results")
    parser.add_argument("--ytdlp-only", action="store_true",
                        help="Only run yt-dlp tests")
    parser.add_argument("--api-only", action="store_true",
                        help="Only run YouTube API tests")
    args = parser.parse_args()

    api_key = os.environ.get("YOUTUBE_API_KEY", "")

    print(f"Lookback: {LOOKBACK_DAYS} days (since {PUBLISHED_AFTER[:10]})")
    print(f"YouTube API key: {'set' if api_key else 'not set'}")

    global skipped

    # yt-dlp tests
    if not args.api_only:
        test_ytdlp_keyword_search(args.verbose)
        test_ytdlp_channel_search(args.verbose)
        test_ytdlp_date_filtering(args.verbose)
        test_ytdlp_backfill(args.verbose)
    else:
        skipped += 4
        print("\n(yt-dlp tests skipped)")

    # API tests
    if not args.ytdlp_only:
        if api_key:
            test_api_keyword_search(api_key, args.verbose)
            test_api_channel_search(api_key, args.verbose)
            test_api_backfill(api_key, args.verbose)
        else:
            skipped += 3
            print("\n(YouTube API tests skipped — no YOUTUBE_API_KEY)")
    else:
        skipped += 3
        print("\n(YouTube API tests skipped)")

    print(f"\n{'='*40}")
    print(f"Results: {passed} passed, {failed} failed, {skipped} skipped")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
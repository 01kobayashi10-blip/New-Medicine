#!/usr/bin/env python3
"""Fetch Mix Online RSS, filter titles containing 「発売」, update state and HTML when new items appear."""

from __future__ import annotations

import html
import json
import os
import socket
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, urljoin

import feedparser

ROOT = Path(__file__).resolve().parents[1]
DATA_PATH = ROOT / "data" / "processed_items.json"
REPORT_PATH = ROOT / "reports" / "latest.html"
NOTIFY_LATEST_PATH = ROOT / "reports" / "notify_latest.json"

DEFAULT_RSS = (
    "https://www.mixonline.jp/DesktopModules/MixOnline_Rss/MixOnlinerss.aspx?rssmode=3"
)

# RSS の link が / で始まる相対URLのとき、file:// で開いた HTML からも辿れるよう絶対化する基準
LINK_BASE = "https://www.mixonline.jp/"


def canonical_item_id(raw: str) -> str:
    """guid / link を保存・比較用の安定キーに正規化（相対パスはミクス本番オリジンに結合）。"""
    raw = (raw or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    if parsed.scheme in ("http", "https"):
        return raw
    # tag:, urn: など http 以外のスキームはそのまま
    if parsed.scheme:
        return raw
    return urljoin(LINK_BASE, raw)


def stable_id(entry: feedparser.FeedParserDict) -> str:
    guid = (entry.get("id") or entry.get("guid") or "").strip()
    candidate = guid or (entry.get("link") or "").strip()
    return canonical_item_id(candidate) if candidate else ""


def fetch_feed(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "NewMedicineRSSBot/1.0 (+https://github.com/actions)",
            "Accept": "application/rss+xml, application/xml, text/xml, */*",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def load_processed() -> set[str]:
    if not DATA_PATH.is_file():
        return set()
    with open(DATA_PATH, encoding="utf-8") as f:
        data = json.load(f)
    ids = data.get("processed_ids") or []
    return {canonical_item_id(str(x)) for x in ids if str(x).strip()}


def save_processed(ids: set[str]) -> None:
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"processed_ids": sorted(ids)}
    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")


def build_html(
    items: list[tuple[str, str, str]],
    new_count: int,
    meta_line: str,
) -> str:
    """items: (title, link, published)"""
    lis = []
    for title, link, published in items:
        safe_title = html.escape(title, quote=True)
        safe_link = html.escape(link, quote=True)
        pub = html.escape(published, quote=True) if published else ""
        meta = f' <span style="color:#666">({pub})</span>' if pub else ""
        lis.append(
            f'<li><a href="{safe_link}" rel="noopener noreferrer">{safe_title}</a>{meta}</li>'
        )
    body_list = "\n    ".join(lis) if lis else "<li>（該当なし）</li>"
    new_note = (
        f"<p><strong>本実行で新規検出: {new_count} 件</strong></p>"
        if new_count
        else "<p>本実行での新規検出はありませんでした。</p>"
    )
    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>ミクスOnline「発売」記事スナップショット</title>
  <style>
    body {{ font-family: system-ui, sans-serif; max-width: 48rem; margin: 2rem auto; padding: 0 1rem; }}
    h1 {{ font-size: 1.25rem; }}
    ul {{ padding-left: 1.2rem; }}
    li {{ margin: 0.5rem 0; }}
    .meta {{ color: #555; font-size: 0.875rem; margin-bottom: 1.5rem; }}
    .note {{ font-size: 0.8rem; color: #666; margin-top: 2rem; }}
  </style>
</head>
<body>
  <h1>ミクスOnline RSS — タイトルに「発売」を含む記事</h1>
  <p class="meta">{html.escape(meta_line, quote=True)}</p>
  {new_note}
  <ul>
    {body_list}
  </ul>
  <p class="note">出典: ミクスOnline 公式 RSS。診療判断は医療専門家の指示に従ってください。</p>
</body>
</html>
"""


def append_github_output(**pairs: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        for k, v in pairs.items():
            f.write(f"{k}={v}\n")


def write_notify_latest(rows: list[tuple[str, str, str]], top_n: int) -> None:
    """メール/Slack 用: RSS 上の「発売」記事の先頭 top_n 件（通常は新しい順）。"""
    slice_rows = rows[: max(0, top_n)]
    items = [
        {"title": t, "link": u, "published": p} for t, u, p in slice_rows
    ]
    NOTIFY_LATEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(NOTIFY_LATEST_PATH, "w", encoding="utf-8") as f:
        json.dump({"items": items}, f, ensure_ascii=False, indent=2)
        f.write("\n")


def main() -> int:
    rss_url = os.environ.get("RSS_URL", DEFAULT_RSS)
    try:
        raw = fetch_feed(rss_url)
    except urllib.error.HTTPError as e:
        print(f"ERROR: HTTP {e.code} fetching RSS", file=sys.stderr)
        return 1
    except urllib.error.URLError as e:
        print(f"ERROR: fetch failed: {e}", file=sys.stderr)
        return 1
    except TimeoutError:
        print("ERROR: request timed out", file=sys.stderr)
        return 1
    except socket.timeout:
        print("ERROR: socket timed out", file=sys.stderr)
        return 1

    feed = feedparser.parse(raw)
    if feed.bozo and not feed.entries:
        print("ERROR: could not parse RSS", file=sys.stderr)
        return 1

    total = len(feed.entries)
    matched: list[feedparser.FeedParserDict] = []
    for entry in feed.entries:
        title = entry.get("title") or ""
        if "発売" in title:
            sid = stable_id(entry)
            if sid:
                matched.append(entry)

    processed = load_processed()
    new_entries = [e for e in matched if stable_id(e) not in processed]

    print(f"rss_total_entries={total}")
    print(f"matched_hatsubai={len(matched)}")
    print(f"new_unprocessed={len(new_entries)}")

    new_count = len(new_entries)
    append_github_output(
        new_count=str(new_count),
        needs_commit="true" if new_count else "false",
    )

    if not matched:
        write_notify_latest([], int(os.environ.get("EMAIL_TOP_N", "5")))
        print("No 発売 items in current RSS; leaving reports and state unchanged.")
        return 0

    rows: list[tuple[str, str, str]] = []
    for e in matched:
        title = e.get("title") or ""
        link = canonical_item_id((e.get("link") or "").strip())
        published = ""
        if e.get("published"):
            published = e["published"]
        elif e.get("updated"):
            published = e["updated"]
        rows.append((title, link, published))

    top_n = int(os.environ.get("EMAIL_TOP_N", "5"))
    write_notify_latest(rows, top_n)

    if new_entries:
        updated = processed | {stable_id(e) for e in new_entries}
        save_processed(updated)

    now = datetime.now(timezone.utc)
    if new_count:
        meta_line = (
            f"生成: {now.strftime('%Y-%m-%dT%H:%M:%SZ')} / 本バッチの新規件数: {new_count}"
        )
    else:
        # 日付を入れると毎日ファイルが変わり空コミットが増えるため、新規なし時は固定文
        meta_line = "RSSスナップショット（新規なし） / 記事リンクは https の絶対URL"

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write(build_html(rows, new_count, meta_line))

    if new_count:
        print(f"Wrote {REPORT_PATH.relative_to(ROOT)} and updated state ({new_count} new).")
    else:
        print(
            f"Wrote {REPORT_PATH.relative_to(ROOT)} (absolute links / snapshot, 0 new)."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

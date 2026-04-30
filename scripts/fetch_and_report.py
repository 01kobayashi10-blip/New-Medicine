#!/usr/bin/env python3
"""Fetch Mix Online RSS, filter titles for 実発売記事, update state and HTML when new items appear."""

from __future__ import annotations

import calendar
import html
import json
import os
import socket
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse, urljoin

import feedparser

ROOT = Path(__file__).resolve().parents[1]
DATA_PATH = ROOT / "data" / "processed_items.json"
REPORT_PATH = ROOT / "reports" / "latest.html"
NOTIFY_LATEST_PATH = ROOT / "reports" / "notify_latest.json"
GENERATE_QUEUE_PATH = ROOT / "reports" / "generate_queue.json"

# rssmode=3 は新薬寄りだが件数が少なく「発売」見出しがすぐ流れる。=1 と併用してカバーする。
DEFAULT_RSS_FEED_URLS = (
    "https://www.mixonline.jp/DesktopModules/MixOnline_Rss/MixOnlinerss.aspx?rssmode=3,"
    "https://www.mixonline.jp/DesktopModules/MixOnline_Rss/MixOnlinerss.aspx?rssmode=1"
)

# RSS の link が / で始まる相対URLのとき、file:// で開いた HTML からも辿れるよう絶対化する基準
LINK_BASE = "https://www.mixonline.jp/"


def _parse_exclude_substrings(raw: str) -> list[str]:
    out: list[str] = []
    for part in raw.split(","):
        s = part.strip()
        if s:
            out.append(s)
    return out


def title_matches_hatsubai(
    title: str,
    *,
    require_any: list[str],
    base_substring: str,
    exclude_substrings: list[str],
) -> bool:
    """実発売の見出しに寄せる。require_any が空なら base のみ（除外は常に適用）。"""
    if require_any:
        if not any(part in title for part in require_any):
            return False
    else:
        if base_substring not in title:
            return False
    for ex in exclude_substrings:
        if ex in title:
            return False
    return True


def load_hatsubai_require_any() -> list[str]:
    """タイトルに含まれるべき実発売シグネチャ（いずれか一致）。空リストなら base のみでマッチ。"""
    if "HATSUBAI_REQUIRE_ANY" in os.environ:
        return _parse_exclude_substrings(os.environ.get("HATSUBAI_REQUIRE_ANY", ""))
    legacy = os.environ.get("HATSUBAI_REQUIRE_SUBSTRING")
    if legacy is not None:
        s = legacy.strip()
        return [s] if s else []
    return ["を発売", "に発売"]


def load_hatsubai_filter_from_environ() -> tuple[list[str], str, list[str]]:
    """(require_any, base, excludes)。除外既定に「発売は「」を含め発売スケジュール見出しを落とす。"""
    require_any = load_hatsubai_require_any()
    base = os.environ.get("HATSUBAI_BASE_SUBSTRING", "発売").strip() or "発売"
    raw_ex = os.environ.get(
        "HATSUBAI_EXCLUDE_SUBSTRINGS", "発売予定,発売を予定,発売は「"
    )
    return require_any, base, _parse_exclude_substrings(raw_ex)


def load_feed_urls() -> list[str]:
    """RSS_FEED_URLS（カンマ区切り）優先。未設定なら RSS_URL の1本。それもなければ既定の2本。"""
    multi = os.environ.get("RSS_FEED_URLS", "").strip()
    if multi:
        return [u.strip() for u in multi.split(",") if u.strip()]
    single = os.environ.get("RSS_URL", "").strip()
    if single:
        return [single]
    return [u.strip() for u in DEFAULT_RSS_FEED_URLS.split(",") if u.strip()]


def entry_published_unix(entry: feedparser.FeedParserDict) -> float:
    """RSS 項目の並び替え用タイムスタンプ（大きいほど新しい）。欠損時は 0。"""
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    if t:
        try:
            return float(calendar.timegm(t))
        except (TypeError, ValueError):
            pass
    for key in ("published", "updated"):
        s = (entry.get(key) or "").strip()
        if not s:
            continue
        try:
            return parsedate_to_datetime(s).timestamp()
        except (TypeError, ValueError):
            continue
    return 0.0


def sort_entries_by_published_desc(entries: list[feedparser.FeedParserDict]) -> None:
    """公開日時の降順（同一時刻は安定のため stable_id で二次ソート）。"""
    entries.sort(
        key=lambda e: (entry_published_unix(e), stable_id(e)),
        reverse=True,
    )


def merge_rss_entries(urls: list[str]) -> tuple[list[feedparser.FeedParserDict], int]:
    """複数 RSS を取得し stable_id で重複除去（先勝ち）。戻り値は (merged, raw_entry_sum)。"""
    merged: list[feedparser.FeedParserDict] = []
    seen: set[str] = set()
    raw_total = 0
    for url in urls:
        raw = fetch_feed(url)
        feed = feedparser.parse(raw)
        if feed.bozo and not feed.entries:
            raise ValueError(f"could not parse RSS: {url}")
        raw_total += len(feed.entries)
        for entry in feed.entries:
            sid = stable_id(entry)
            if sid and sid not in seen:
                seen.add(sid)
                merged.append(entry)
    return merged, raw_total


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
  <title>ミクスOnline 実発売記事スナップショット</title>
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
  <h1>ミクスOnline RSS — 実発売として抽出した記事</h1>
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


def _dispatch_queue_infographic_without_new() -> bool:
    """GitHub Actions の手動実行で、新規0件でも図解用キューに先頭記事を載せるか。"""
    if os.environ.get("GITHUB_EVENT_NAME", "").strip() != "workflow_dispatch":
        return False
    v = os.environ.get("DISPATCH_QUEUE_INFOGRAPHIC_WITHOUT_NEW", "true").strip().lower()
    return v in ("true", "1", "yes", "")


def write_generate_queue(new_entries: list[feedparser.FeedParserDict]) -> None:
    """図解生成用: 本実行で新規の RSS のみ書く。新規なしのときはファイルを消してコミットノイズを防ぐ。"""
    if not new_entries:
        if GENERATE_QUEUE_PATH.is_file():
            GENERATE_QUEUE_PATH.unlink()
        return
    items: list[dict[str, str]] = []
    for e in new_entries:
        sid = stable_id(e)
        if not sid:
            continue
        title = e.get("title") or ""
        link = canonical_item_id((e.get("link") or "").strip())
        published = ""
        if e.get("published"):
            published = e["published"]
        elif e.get("updated"):
            published = e["updated"]
        items.append(
            {"stable_id": sid, "title": title, "link": link, "published": published}
        )
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "items": items,
    }
    GENERATE_QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(GENERATE_QUEUE_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")


def write_notify_latest(rows: list[tuple[str, str, str]], top_n: int) -> None:
    """メール/Slack 用: フィルタ通過後の記事の先頭 top_n 件（通常は新しい順）。"""
    slice_rows = rows[: max(0, top_n)]
    items = [
        {"title": t, "link": u, "published": p} for t, u, p in slice_rows
    ]
    NOTIFY_LATEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(NOTIFY_LATEST_PATH, "w", encoding="utf-8") as f:
        json.dump({"items": items}, f, ensure_ascii=False, indent=2)
        f.write("\n")


def main() -> int:
    urls = load_feed_urls()
    try:
        merged_entries, raw_entry_sum = merge_rss_entries(urls)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
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

    require_any, base, excludes = load_hatsubai_filter_from_environ()
    total = len(merged_entries)
    matched: list[feedparser.FeedParserDict] = []
    for entry in merged_entries:
        title = entry.get("title") or ""
        if title_matches_hatsubai(
            title,
            require_any=require_any,
            base_substring=base,
            exclude_substrings=excludes,
        ):
            sid = stable_id(entry)
            if sid:
                matched.append(entry)

    sort_entries_by_published_desc(matched)

    processed = load_processed()
    new_entries = [e for e in matched if stable_id(e) not in processed]

    print(f"rss_feed_urls={len(urls)}")
    print(f"rss_raw_entries={raw_entry_sum}")
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
        write_generate_queue([])
        print("No matched hatsubai items in current RSS; leaving reports and state unchanged.")
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

    queue_for_generate = new_entries
    if not new_entries and _dispatch_queue_infographic_without_new() and matched:
        queue_for_generate = matched[:1]
        print(
            "workflow_dispatch: RSS 上のフィルタ通過記事の先頭1件を図解キューに載せます（新規0件・processed は更新しません）。"
        )
    write_generate_queue(queue_for_generate)

    if new_count:
        print(f"Wrote {REPORT_PATH.relative_to(ROOT)} and updated state ({new_count} new).")
    else:
        print(
            f"Wrote {REPORT_PATH.relative_to(ROOT)} (absolute links / snapshot, 0 new)."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

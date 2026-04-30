#!/usr/bin/env python3
"""Slack Incoming Webhook に発売記事レポートの通知を送る（GitHub Actions 用）。"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NOTIFY_LATEST_PATH = ROOT / "reports" / "notify_latest.json"


def load_latest_items() -> list[dict[str, str]]:
    if not NOTIFY_LATEST_PATH.is_file():
        return []
    try:
        with open(NOTIFY_LATEST_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    items = data.get("items")
    if not isinstance(items, list):
        return []
    out: list[dict[str, str]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        title = str(it.get("title") or "").strip()
        link = str(it.get("link") or "").strip()
        if not link:
            continue
        pub = str(it.get("published") or "").strip()
        out.append({"title": title or link, "link": link, "published": pub})
    return out


def format_latest_for_slack(items: list[dict[str, str]]) -> str:
    if not items:
        return "（このRSS取得では実発売フィルタ該当なし）"
    lines: list[str] = []
    for it in items[:5]:
        pub = f" {it['published']}" if it.get("published") else ""
        lines.append(f"・{it['title']}{pub}\n  {it['link']}")
    return "\n".join(lines)


def main() -> int:
    url = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    if not url:
        print("SLACK_WEBHOOK_URL is not set; skipping Slack.")
        return 0

    n = os.environ.get("NEW_COUNT", "?")
    link = os.environ.get("LINK", "").strip()
    ev = os.environ.get("GITHUB_EVENT_NAME", "")
    if ev == "workflow_dispatch":
        mix = format_latest_for_slack(load_latest_items())
        text = (
            f"【手動実行】Mix Online 発売記事\n"
            f"本RSS取得時点の新規件数: {n} 件\n\n"
            f"最新の新薬はこちら（ミクス直リンク）\n{mix}\n\n"
            f"HTMLスナップショット: {link or '（リンクなし）'}"
        )
    else:
        text = f"Mix Online 発売記事: {n} 件の新規を検出しました。\n{link}"

    data = json.dumps({"text": text}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
    except urllib.error.HTTPError as e:
        print(f"::warning::Slack POST failed (workflow continues): HTTP {e.code}")
    except urllib.error.URLError as e:
        print(f"::warning::Slack POST failed (workflow continues): {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

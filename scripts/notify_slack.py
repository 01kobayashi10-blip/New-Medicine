#!/usr/bin/env python3
"""Slack Incoming Webhook に発売記事レポートの通知を送る（GitHub Actions 用）。"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request


def main() -> int:
    url = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    if not url:
        print("SLACK_WEBHOOK_URL is not set; skipping Slack.")
        return 0

    n = os.environ.get("NEW_COUNT", "?")
    link = os.environ.get("LINK", "").strip()
    ev = os.environ.get("GITHUB_EVENT_NAME", "")
    if ev == "workflow_dispatch":
        text = f"【手動実行】Mix Online 発売記事\n本RSS取得時点の新規件数: {n} 件\n{link}"
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

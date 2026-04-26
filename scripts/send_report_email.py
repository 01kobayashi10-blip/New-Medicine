#!/usr/bin/env python3
"""Mix Online 発売記事レポートのメール通知。GitHub Actions から SMTP で送信する想定。"""

from __future__ import annotations

import json
import os
import smtplib
import sys
from email.message import EmailMessage
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
        out.append(
            {
                "title": title or link,
                "link": link,
                "published": str(it.get("published") or "").strip(),
            }
        )
    return out


def format_latest_hatsubai_block(items: list[dict[str, str]]) -> str:
    if not items:
        return "（このRSS取得では、タイトルに「発売」を含む記事はありませんでした。）"
    lines: list[str] = []
    for it in items:
        title = it["title"]
        link = it["link"]
        pub = it.get("published") or ""
        suffix = f" ({pub})" if pub else ""
        lines.append(f"- {title}{suffix}")
        lines.append(f"  {link}")
    return "\n".join(lines)


def main() -> int:
    host = os.environ.get("SMTP_HOST", "").strip()
    user = os.environ.get("SMTP_USER", "").strip()
    password = os.environ.get("SMTP_PASSWORD", "").strip()
    mail_to = os.environ.get("MAIL_TO", "").strip()

    if not (host and user and password and mail_to):
        print(
            "SMTP_HOST, SMTP_USER, SMTP_PASSWORD, MAIL_TO のいずれか未設定のためメールをスキップします。"
        )
        return 0

    raw_port = os.environ.get("SMTP_PORT", "").strip()
    try:
        port = int(raw_port) if raw_port else 587
    except ValueError:
        print(f"::warning::SMTP_PORT が不正です: {raw_port!r}。587 を使います。")
        port = 587

    mail_from = os.environ.get("MAIL_FROM", "").strip() or user
    new_count = os.environ.get("NEW_COUNT", "?")
    link = os.environ.get("LINK", "").strip()
    default_subject = "Mix Online 発売記事: 新規検出"
    subject = os.environ.get("MAIL_SUBJECT", default_subject).strip()
    ev = os.environ.get("GITHUB_EVENT_NAME", "")

    if ev == "workflow_dispatch":
        if subject == default_subject:
            subject = "【手動実行】Mix Online 発売記事レポート"
        latest_block = format_latest_hatsubai_block(load_latest_items())
        lines = [
            "【手動実行】このメールは GitHub Actions の手動実行に基づき送付しています。",
            "",
            f"本RSS取得時点の新規件数: {new_count} 件",
            "",
            "最新の新薬はこちら",
            "",
            latest_block,
        ]
    else:
        lines = [
            f"Mix Online 発売記事: {new_count} 件の新規を検出しました。",
            "",
            link if link else "（リンクなし）",
        ]
    body = "\n".join(lines)

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = mail_from
    msg["To"] = mail_to
    msg.set_content(body, charset="utf-8")

    try:
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()
            smtp.login(user, password)
            smtp.send_message(msg)
    except OSError as e:
        print(f"::warning::メール送信に失敗しました（ワークフローは続行）: {e}")
        return 0

    print("メール通知を送信しました。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

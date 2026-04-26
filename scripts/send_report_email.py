#!/usr/bin/env python3
"""Mix Online 発売記事レポートのメール通知。GitHub Actions から SMTP で送信する想定。

任意の環境変数 INFOGRAPHIC_HTML_PATH:
  図解などの .html ファイルへのパス（相対はカレントディレクトリ、なければリポジトリルートから解決）。
  ファイルが存在し、かつ INFOGRAPHIC_ATTACH が無効でないとき multipart/mixed で添付。
任意: INFOGRAPHIC_ATTACHMENT_NAME — 添付ファイル名（未設定時は実ファイルの basename）
任意: INFOGRAPHIC_URL — 図解 HTML をブラウザで開く URL。設定時は平文本文の末尾に「ミクス記事の URL と同様」2行で追記する。
任意: INFOGRAPHIC_ATTACH — "0" / "false" / "no" / "off" のいずれかならファイル添付しない（URL のみにしたいとき）。未設定時はパスが有効なら添付する。
"""

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


def resolve_infographic_path(raw: str) -> Path | None:
    """INFOGRAPHIC_HTML_PATH を解決。見つからなければ None（警告のみ）。"""
    raw = raw.strip()
    if not raw:
        return None
    first = Path(raw).expanduser()
    candidates = [first]
    if not first.is_absolute():
        candidates.append(ROOT / raw)
    for p in candidates:
        if p.is_file():
            return p
    print(
        f"::warning::INFOGRAPHIC_HTML_PATH が見つかりません: {raw!r}。添付なしで送信します。"
    )
    return None


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

    infographic_url = os.environ.get("INFOGRAPHIC_URL", "").strip()
    if infographic_url:
        lines.extend(["", "図解まとめ（HTML・ブラウザで開く）:", infographic_url])

    body = "\n".join(lines)

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = mail_from
    msg["To"] = mail_to
    msg.set_content(body, charset="utf-8")

    attach_raw = os.environ.get("INFOGRAPHIC_HTML_PATH", "").strip()
    attach_opt = os.environ.get("INFOGRAPHIC_ATTACH", "1").strip().lower()
    skip_attach = attach_opt in ("0", "false", "no", "off")
    infographic_path = (
        None
        if skip_attach
        else (resolve_infographic_path(attach_raw) if attach_raw else None)
    )
    if skip_attach and attach_raw:
        print("INFOGRAPHIC_ATTACH が無効のため HTML ファイル添付はスキップします。")
    if infographic_path is not None:
        display_name = os.environ.get("INFOGRAPHIC_ATTACHMENT_NAME", "").strip()
        filename = display_name or infographic_path.name or "infographic.html"
        data = infographic_path.read_bytes()
        msg.add_attachment(
            data,
            maintype="text",
            subtype="html",
            filename=filename,
        )
        print(f"HTML 添付: {infographic_path} → {filename}")

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

#!/usr/bin/env python3
"""Mix Online 発売記事レポートのメール通知。GitHub Actions から SMTP で送信する想定。"""

from __future__ import annotations

import os
import smtplib
import sys
from email.message import EmailMessage


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
    subject = os.environ.get("MAIL_SUBJECT", "Mix Online 発売記事: 新規検出").strip()

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

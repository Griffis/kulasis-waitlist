from __future__ import annotations

import requests


def send_discord(webhook_url: str, content: str) -> None:
    """Discord Webhook へメッセージを送信する。"""
    if not webhook_url:
        print("[notify] Webhook URLが未設定のため送信をスキップしました")
        return

    payload = {"content": content}
    response = requests.post(webhook_url, json=payload, timeout=10)
    response.raise_for_status()
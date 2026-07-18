"""Slack Events API 経由の単発メッセージ処理エントリポイント。

GitHub Actions ワークフロー (on: repository_dispatch) から stdin で Slack event の
client_payload (= Slack Events API の outer body の event フィールド or 全体) を受け取り、
cron 経路と同じ process_one を呼ぶ。cron/イベント両経路で処理ロジックは完全に共通。
"""
from __future__ import annotations

import json
import sys

from google import genai
from slack_sdk import WebClient

from slacken.main import GOOGLE_API_KEY, SLACK_TOKEN, process_one


def _extract_event(payload: dict) -> dict:
    """Slack Events outer body ({event: {...}}) と event 単体のどちらでも受け付ける"""
    if isinstance(payload.get("event"), dict):
        return payload["event"]
    return payload


def normalize_event(payload: dict) -> tuple[str, dict] | None:
    """Slack event を (channel_id, process_one 用 msg dict) に正規化。
    対象外イベントは None"""
    event = _extract_event(payload)
    if event.get("type") != "message":
        return None
    subtype = event.get("subtype")

    # 編集イベントは inner `message` を対象メッセージとして扱う
    if subtype == "message_changed":
        inner = event.get("message") or {}
        msg = dict(inner)
        # edited.ts は inner.message.edited.ts に入る想定
        # channel は outer event 側
    elif subtype in ("channel_join", "channel_leave", "message_deleted", "bot_message"):
        return None
    else:
        msg = dict(event)

    if msg.get("bot_id"):
        return None
    ts = msg.get("ts")
    if not ts:
        return None
    channel = event.get("channel") or msg.get("channel")
    if not channel:
        return None
    return channel, msg


def main() -> None:
    """stdin からJSONを読み1件処理する"""
    raw = sys.stdin.read()
    if not raw.strip():
        print("event: empty stdin, nothing to do", file=sys.stderr)
        return
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"event: invalid JSON on stdin: {e}", file=sys.stderr)
        sys.exit(1)

    normalized = normalize_event(payload)
    if normalized is None:
        print("event: skipped (not a processable message)", file=sys.stderr)
        return
    channel_id, msg = normalized

    slack = WebClient(token=SLACK_TOKEN)
    gemini_client = genai.Client(api_key=GOOGLE_API_KEY)
    process_one(slack, gemini_client, channel_id, msg)


if __name__ == "__main__":
    main()

"""Notion食事記録をもとに Gemini で食事アドバイスを生成し Slack に投稿"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from zoneinfo import ZoneInfo

import requests
from google import genai
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

SLACK_TOKEN = os.environ["SLACK_BOT_TOKEN"]
NOTION_TOKEN = os.environ["NOTION_TOKEN"]
GOOGLE_API_KEY = os.environ["GOOGLE_API_KEY"]
NOTION_DATABASE_ID = os.environ["NOTION_DATABASE_ID"]
SLACK_CHANNEL = os.environ.get("SLACK_CHANNEL", "food")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
LOOKBACK_DAYS = int(os.environ.get("ADVICE_LOOKBACK_DAYS", "14"))

JST = ZoneInfo("Asia/Tokyo")
NOTION_API = "https://api.notion.com/v1"
NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

MEAL_CHOICES = ("朝食", "昼食", "夕食")


def fetch_recent_records(days: int) -> list[dict]:
    """Notion DB から指定日数分の食事記録を取得 (日付昇順)"""
    since = (dt.datetime.now(tz=JST).date() - dt.timedelta(days=days)).isoformat()
    records: list[dict] = []
    cursor: str | None = None
    while True:
        body = {
            "filter": {"property": "日付", "date": {"on_or_after": since}},
            "sorts": [{"property": "日付", "direction": "ascending"}],
            "page_size": 100,
        }
        if cursor:
            body["start_cursor"] = cursor
        r = requests.post(
            f"{NOTION_API}/databases/{NOTION_DATABASE_ID}/query",
            headers=NOTION_HEADERS,
            json=body,
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        for page in data.get("results", []):
            props = page.get("properties", {})
            date_prop = props.get("日付", {}).get("date") or {}
            meal_prop = props.get("区分", {}).get("select") or {}
            content_prop = props.get("内容", {}).get("rich_text") or []
            content = "".join(rt.get("plain_text", "") for rt in content_prop)
            if not date_prop.get("start"):
                continue
            records.append(
                {
                    "date": date_prop["start"],
                    "meal": meal_prop.get("name", ""),
                    "content": content.strip(),
                }
            )
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return records


def format_records(records: list[dict]) -> str:
    """プロンプト用に食事記録をテキスト化"""
    if not records:
        return "(記録なし)"
    lines = []
    for r in records:
        lines.append(f"- {r['date']} {r['meal']}: {r['content'] or '(内容不明)'}")
    return "\n".join(lines)


ADVICE_PROMPT_TMPL = (
    "あなたは食事アドバイザーです。以下は直近{days}日間の食事記録です。\n\n"
    "{records}\n\n"
    "この履歴をふまえて、これから食べる「本日の{meal}」への提案を、"
    "栄養バランス・不足しがちな食材・食べ過ぎている傾向などの観点で考えてください。\n\n"
    "出力条件:\n"
    "- Slackに投稿するので300文字以内、簡潔に\n"
    "- 具体的なメニュー案を1〜2個含める\n"
    "- 前置き・自己紹介・締めの挨拶は不要、本文のみ\n"
    "- Markdownの見出しやコードブロックは使わない (箇条書きは可)\n"
)


def generate_advice(client: genai.Client, meal: str, records: list[dict]) -> str:
    prompt = ADVICE_PROMPT_TMPL.format(
        days=LOOKBACK_DAYS, records=format_records(records), meal=meal
    )
    resp = client.models.generate_content(model=GEMINI_MODEL, contents=[prompt])
    return (resp.text or "").strip()


def post_to_slack(slack: WebClient, channel: str, meal: str, advice: str) -> None:
    text = f":knife_fork_plate: *本日の{meal}アドバイス*\n{advice}"
    try:
        slack.chat_postMessage(channel=channel, text=text)
    except SlackApiError as e:
        print(f"slack post error: {e}", file=sys.stderr)
        raise


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--meal", required=True, choices=MEAL_CHOICES)
    args = parser.parse_args()

    slack = WebClient(token=SLACK_TOKEN)
    gemini_client = genai.Client(api_key=GOOGLE_API_KEY)

    records = fetch_recent_records(LOOKBACK_DAYS)
    print(f"fetched {len(records)} records for last {LOOKBACK_DAYS} days")
    advice = generate_advice(gemini_client, args.meal, records)
    if not advice:
        print("no advice generated, skipping post", file=sys.stderr)
        return
    post_to_slack(slack, SLACK_CHANNEL, args.meal, advice)
    print(f"posted {args.meal} advice to #{SLACK_CHANNEL}")


if __name__ == "__main__":
    main()

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
# 健康診断結果の情報整理ページ (背景コンテキスト)
HEALTH_CONTEXT_PAGE_ID = os.environ.get(
    "HEALTH_CONTEXT_PAGE_ID", "39b29ec6bc9b80aabccbc02eac1b6dbd"
)

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


def fetch_page_text(page_id: str) -> str:
    """Notionページの本文をブロック走査で平文化 (再帰、主要ブロック型に対応)"""
    # よく使うブロックのうち rich_text を持つもの
    RICH_TEXT_TYPES = {
        "paragraph",
        "heading_1",
        "heading_2",
        "heading_3",
        "bulleted_list_item",
        "numbered_list_item",
        "to_do",
        "toggle",
        "quote",
        "callout",
        "code",
    }
    lines: list[str] = []

    def walk(block_id: str, depth: int = 0) -> None:
        cursor: str | None = None
        while True:
            params = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            r = requests.get(
                f"{NOTION_API}/blocks/{block_id}/children",
                headers=NOTION_HEADERS,
                params=params,
                timeout=30,
            )
            if not r.ok:
                print(
                    f"notion page fetch error: {r.status_code} {r.text}",
                    file=sys.stderr,
                )
                return
            data = r.json()
            for b in data.get("results", []):
                btype = b.get("type")
                body = b.get(btype, {}) if btype else {}
                if btype == "table_row":
                    # table_row は rich_text ではなく cells にセル単位のテキストを持つ
                    text = " | ".join(
                        "".join(rt.get("plain_text", "") for rt in cell)
                        for cell in body.get("cells", [])
                    )
                    if text.strip():
                        lines.append("  " * depth + text)
                elif btype in RICH_TEXT_TYPES:
                    text = "".join(
                        rt.get("plain_text", "") for rt in body.get("rich_text", [])
                    )
                    if text.strip():
                        lines.append("  " * depth + text)
                if b.get("has_children"):
                    walk(b["id"], depth + 1)
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")

    walk(page_id)
    return "\n".join(lines)


def format_records(records: list[dict]) -> str:
    """プロンプト用に食事記録をテキスト化"""
    if not records:
        return "(記録なし)"
    lines = []
    for r in records:
        lines.append(f"- {r['date']} {r['meal']}: {r['content'] or '(内容不明)'}")
    return "\n".join(lines)


ADVICE_PROMPT_TMPL = (
    "あなたはこの人専属の食事アドバイザーです。\n\n"
    "【重要な背景: 健康診断結果の情報整理】\n"
    "{health_context}\n\n"
    "【直近{days}日間の食事記録】\n"
    "{records}\n\n"
    "上記の健康状態と食事履歴をふまえて、これから食べる「本日の{meal}」への提案を、"
    "健康診断で指摘されている項目の改善・栄養バランス・不足しがちな食材・"
    "食べ過ぎている傾向などの観点で考えてください。\n\n"
    "出力条件:\n"
    "- Slackに投稿するので300文字以内、簡潔に\n"
    "- 具体的なメニュー案を1〜2個含める\n"
    "- 健康診断結果と関連する提案は理由を短く添える\n"
    "- 前置き・自己紹介・締めの挨拶は不要、本文のみ\n"
    "- Markdownの見出しやコードブロックは使わない (箇条書きは可)\n"
)


def generate_advice(
    client: genai.Client, meal: str, records: list[dict], health_context: str
) -> str:
    prompt = ADVICE_PROMPT_TMPL.format(
        health_context=health_context or "(取得できず)",
        days=LOOKBACK_DAYS,
        records=format_records(records),
        meal=meal,
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
    health_context = fetch_page_text(HEALTH_CONTEXT_PAGE_ID)
    print(f"fetched health context: {len(health_context)} chars")
    advice = generate_advice(gemini_client, args.meal, records, health_context)
    if not advice:
        print("no advice generated, skipping post", file=sys.stderr)
        return
    post_to_slack(slack, SLACK_CHANNEL, args.meal, advice)
    print(f"posted {args.meal} advice to #{SLACK_CHANNEL}")


if __name__ == "__main__":
    main()

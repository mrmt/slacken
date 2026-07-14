"""Slack #food → Notion 食事記録DB 同期スクリプト"""
from __future__ import annotations

import datetime as dt
import os
import re
import sys
from zoneinfo import ZoneInfo

import requests
from google import genai
from google.genai import types as genai_types
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

SLACK_TOKEN = os.environ["SLACK_BOT_TOKEN"]
NOTION_TOKEN = os.environ["NOTION_TOKEN"]
GOOGLE_API_KEY = os.environ["GOOGLE_API_KEY"]
NOTION_DATABASE_ID = os.environ["NOTION_DATABASE_ID"]
SLACK_CHANNEL = os.environ.get("SLACK_CHANNEL", "food")
LOOKBACK_HOURS = int(os.environ.get("LOOKBACK_HOURS", "168"))  # 7日間
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

JST = ZoneInfo("Asia/Tokyo")
NOTION_API = "https://api.notion.com/v1"
NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

# 区分判定キーワード
MEAL_KEYWORDS = {
    "朝食": ["朝飯", "朝は", "朝食", "朝ごはん", "朝御飯", "朝メシ", "モーニング", "ブレックファスト", "ブレックファースト"],
    "昼食": ["昼飯", "昼は", "昼食", "昼ごはん", "昼メシ", "ランチ", "お昼"],
    "夕食": ["晩飯", "晩は", "夜は", "夕食", "夕飯", "夜ごはん", "晩ごはん", "晩御飯", "晩メシ", "ディナー"],
}

# 日付指定パターン (例: 7/14, 7月14日)
DATE_RE = re.compile(r"(\d{1,2})[/月](\d{1,2})")


def classify_meal(text: str, hour: int) -> str:
    """本文キーワード優先、なければ時間帯で区分判定"""
    for meal, kws in MEAL_KEYWORDS.items():
        if any(kw in text for kw in kws):
            return meal
    if 4 <= hour < 10:
        return "朝食"
    if 10 <= hour < 15:
        return "昼食"
    if 17 <= hour < 22:
        return "夕食"
    return "間食"


def extract_date(text: str, ts_dt: dt.datetime) -> dt.date:
    """本文の日付指定を優先、なければ投稿時刻の日付"""
    m = DATE_RE.search(text)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        try:
            candidate = dt.date(ts_dt.year, month, day)
            # 未来日は前年扱い (年末→年始の投稿対策)
            if candidate > ts_dt.date() + dt.timedelta(days=1):
                candidate = dt.date(ts_dt.year - 1, month, day)
            return candidate
        except ValueError:
            pass
    return ts_dt.date()


def resolve_channel_id(slack: WebClient, name: str) -> str:
    """チャンネル名 → ID解決 (privateチャンネル含む)"""
    if name.startswith("C") or name.startswith("G"):
        return name  # 既にID
    cursor = None
    while True:
        resp = slack.conversations_list(
            types="public_channel,private_channel", limit=200, cursor=cursor
        )
        for ch in resp["channels"]:
            if ch["name"] == name:
                return ch["id"]
        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
    raise RuntimeError(f"channel not found: {name}")


def download_slack_file(url: str) -> tuple[bytes, str]:
    """Slack添付ファイルをBot Token付きでDL"""
    r = requests.get(url, headers={"Authorization": f"Bearer {SLACK_TOKEN}"}, timeout=30)
    r.raise_for_status()
    return r.content, r.headers.get("Content-Type", "image/jpeg").split(";")[0]


PROMPT = (
    "この画像は食事の記録です。写っているメニュー、注文、料理内容を"
    "箇条書きで日本語で列挙してください。"
    "文字が写っていれば正確に書き起こし、"
    "料理の写真なら見た目から料理名を推定してください。"
    "前置きや説明は不要、箇条書きだけ返してください。"
)


def analyze_image(client: genai.Client, image_bytes: bytes, media_type: str) -> str:
    """Gemini で画像から食事内容を抽出"""
    resp = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=[
            genai_types.Part.from_bytes(data=image_bytes, mime_type=media_type),
            PROMPT,
        ],
    )
    return (resp.text or "").strip()


def notion_find_by_permalink(permalink: str) -> str | None:
    """permalinkでNotion既存レコード検索"""
    r = requests.post(
        f"{NOTION_API}/databases/{NOTION_DATABASE_ID}/query",
        headers=NOTION_HEADERS,
        json={
            "filter": {"property": "Slack投稿元", "url": {"equals": permalink}},
            "page_size": 1,
        },
        timeout=30,
    )
    r.raise_for_status()
    results = r.json().get("results", [])
    return results[0]["id"] if results else None


def notion_upsert(
    permalink: str, date: dt.date, meal: str, content: str, source: str
) -> None:
    page_id = notion_find_by_permalink(permalink)
    title = f"{date.isoformat()} {meal}"
    properties = {
        "名前": {"title": [{"text": {"content": title}}]},
        "日付": {"date": {"start": date.isoformat()}},
        "区分": {"select": {"name": meal}},
        "内容": {"rich_text": [{"text": {"content": content[:2000]}}]},
        "Slack投稿元": {"url": permalink},
        "取込元": {"select": {"name": source}},
    }
    if page_id:
        r = requests.patch(
            f"{NOTION_API}/pages/{page_id}",
            headers=NOTION_HEADERS,
            json={"properties": properties},
            timeout=30,
        )
    else:
        r = requests.post(
            f"{NOTION_API}/pages",
            headers=NOTION_HEADERS,
            json={
                "parent": {"database_id": NOTION_DATABASE_ID},
                "properties": properties,
            },
            timeout=30,
        )
    if not r.ok:
        print(f"notion error: {r.status_code} {r.text}", file=sys.stderr)
        r.raise_for_status()


def process_message(
    slack: WebClient, gemini_client: genai.Client, channel_id: str, msg: dict
) -> None:
    # bot自身・システムメッセージは無視
    if msg.get("subtype") in ("channel_join", "channel_leave", "bot_message"):
        return
    if msg.get("bot_id"):
        return

    ts_dt = dt.datetime.fromtimestamp(float(msg["ts"]), tz=JST)
    text = (msg.get("text") or "").strip()

    # 画像解析
    image_texts: list[str] = []
    for f in msg.get("files") or []:
        mimetype = f.get("mimetype", "")
        if not mimetype.startswith("image/"):
            continue
        url = f.get("url_private")
        if not url:
            continue
        try:
            body, mt = download_slack_file(url)
            image_texts.append(analyze_image(gemini_client, body, mt or mimetype))
        except Exception as e:
            print(f"image analyze failed for {url}: {e}", file=sys.stderr)

    if not text and not image_texts:
        return  # 空投稿は無視

    if text and image_texts:
        source = "テキスト+画像"
        content = text + "\n\n[画像解析]\n" + "\n\n".join(image_texts)
    elif image_texts:
        source = "画像解析"
        content = "\n\n".join(image_texts)
    else:
        source = "テキスト"
        content = text

    date = extract_date(text, ts_dt)
    meal = classify_meal(text, ts_dt.hour)

    permalink = slack.chat_getPermalink(channel=channel_id, message_ts=msg["ts"])[
        "permalink"
    ]
    notion_upsert(permalink, date, meal, content, source)
    print(f"synced: {date} {meal} ({source}) {permalink}")


def main() -> None:
    slack = WebClient(token=SLACK_TOKEN)
    gemini_client = genai.Client(api_key=GOOGLE_API_KEY)
    channel_id = resolve_channel_id(slack, SLACK_CHANNEL)

    oldest = (dt.datetime.now(tz=JST) - dt.timedelta(hours=LOOKBACK_HOURS)).timestamp()
    cursor: str | None = None
    total = 0

    while True:
        try:
            resp = slack.conversations_history(
                channel=channel_id, oldest=str(oldest), limit=100, cursor=cursor
            )
        except SlackApiError as e:
            print(f"slack error: {e}", file=sys.stderr)
            sys.exit(1)

        for msg in resp.get("messages", []):
            try:
                process_message(slack, gemini_client, channel_id, msg)
                total += 1
            except Exception as e:
                print(f"failed to process ts={msg.get('ts')}: {e}", file=sys.stderr)

        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break

    print(f"done. processed {total} messages")


if __name__ == "__main__":
    main()

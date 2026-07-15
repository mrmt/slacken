"""Slack #food → Notion 食事記録DB 同期スクリプト"""
from __future__ import annotations

import datetime as dt
import os
import random
import re
import sys
import time
from zoneinfo import ZoneInfo

import requests
from google import genai
from google.genai import types as genai_types
from google.genai import errors as genai_errors
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from slacken.advisor import (
    HEALTH_CONTEXT_PAGE_ID,
    LOOKBACK_DAYS as ADVICE_LOOKBACK_DAYS,
    fetch_page_text,
    fetch_recent_records,
    format_records,
)

SLACK_TOKEN = os.environ["SLACK_BOT_TOKEN"]
NOTION_TOKEN = os.environ["NOTION_TOKEN"]
GOOGLE_API_KEY = os.environ["GOOGLE_API_KEY"]
NOTION_DATABASE_ID = os.environ["NOTION_DATABASE_ID"]
SLACK_CHANNEL = os.environ.get("SLACK_CHANNEL", "food")
LOOKBACK_HOURS = int(os.environ.get("LOOKBACK_HOURS", "168"))  # 7日間
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")

JST = ZoneInfo("Asia/Tokyo")
NOTION_API = "https://api.notion.com/v1"
NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

# 区分判定キーワード
MEAL_KEYWORDS = {
    "朝食": ["朝飯", "朝は", "朝食", "朝ごはん", "朝御飯", "朝メシ", "あさは", "あさめし", "あさごはん", "モーニング", "ブレックファスト", "ブレックファースト"],
    "昼食": ["昼飯", "昼は", "ひるは", "ひるに", "ひる", "昼食", "昼ごはん", "昼メシ", "ランチ", "お昼"],
    "夕食": ["晩飯", "晩は", "夜は", "夕食", "夕飯", "夜ごはん", "晩ごはん", "晩御飯", "晩メシ", "ばんめし", "よるごはん", "ばんごはん", "ディナー"],
}

# 日付指定パターン (例: 7/14, 7月14日)
DATE_RE = re.compile(r"(\d{1,2})[/月](\d{1,2})日?")

# 相対日付キーワード (投稿日からの日数オフセット)
RELATIVE_DATE_KEYWORDS: list[tuple[int, tuple[str, ...]]] = [
    (-2, ("一昨日", "おととい", "おとつい")),
    (-1, ("昨日", "きのう", "ゆうべ", "夕べ", "昨夜", "昨晩")),
    (0, ("今日", "本日", "けさ", "今朝", "今晩", "今夜")),
]


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
            # 遠い未来日 (半年超) は前年扱い (年末→年始の投稿対策)
            if candidate > ts_dt.date() + dt.timedelta(days=180):
                candidate = dt.date(ts_dt.year - 1, month, day)
            return candidate
        except ValueError:
            pass
    for offset, kws in RELATIVE_DATE_KEYWORDS:
        if any(kw in text for kw in kws):
            return ts_dt.date() + dt.timedelta(days=offset)
    return ts_dt.date()


def has_date_expression(text: str) -> bool:
    """日付表現が含まれる可能性を軽く判定 (Gemini呼び出しの前段フィルタ)"""
    if DATE_RE.search(text):
        return True
    for _, kws in RELATIVE_DATE_KEYWORDS:
        if any(kw in text for kw in kws):
            return True
    return False


STRIP_DATE_PROMPT = (
    "以下は食事に関するSlack投稿の本文です。"
    "「昨日」「今日」「一昨日」「今朝」「ゆうべ」等の相対日付表現、"
    "および「7/14」「8月3日」等の日付指定を、それに付随する助詞ごと自然に取り除き、"
    "食べたものの記述だけを残した文を返してください。"
    "余計な整形・要約・解説はせず、元の文体を保ったまま最小限の削除のみ行ってください。"
    "前置きや説明は付けず、修正後の本文だけを返してください。\n\n"
    "本文:\n"
)


def strip_date_via_gemini(client: genai.Client, text: str) -> str:
    """Geminiで日付表現を除去。失敗時は原文を返す (同期は落とさない)"""
    if not has_date_expression(text):
        return text
    max_attempts = 5
    for attempt in range(max_attempts):
        try:
            resp = client.models.generate_content(
                model=GEMINI_MODEL, contents=[STRIP_DATE_PROMPT + text]
            )
            cleaned = (resp.text or "").strip()
            return cleaned or text
        except genai_errors.APIError as e:
            code = getattr(e, "code", None)
            if code in (429, 500, 503) and attempt < max_attempts - 1:
                wait = min(2**attempt, 30) + random.random()
                print(f"gemini strip {code}, retry in {wait:.1f}s", file=sys.stderr)
                time.sleep(wait)
                continue
            print(f"gemini strip failed: {e}", file=sys.stderr)
            return text
    return text


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
    """Gemini で画像から食事内容を抽出。一時エラー時は指数バックオフでリトライ"""
    max_attempts = 8
    for attempt in range(max_attempts):
        try:
            resp = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=[
                    genai_types.Part.from_bytes(data=image_bytes, mime_type=media_type),
                    PROMPT,
                ],
            )
            return (resp.text or "").strip()
        except genai_errors.APIError as e:
            # 503/429/500 系は一時エラーとしてリトライ (最大 ~2分待機)
            code = getattr(e, "code", None)
            if code in (429, 500, 503) and attempt < max_attempts - 1:
                wait = min(2**attempt, 60) + random.random()
                print(f"gemini {code}, retry in {wait:.1f}s (attempt {attempt+1}/{max_attempts})", file=sys.stderr)
                time.sleep(wait)
                continue
            raise


def notion_upload_file(image_bytes: bytes, filename: str, mime: str) -> str | None:
    """Notion File Upload API で画像をアップロードし file_upload id を返す。失敗時 None"""
    try:
        r = requests.post(
            f"{NOTION_API}/file_uploads",
            headers=NOTION_HEADERS,
            json={"filename": filename, "content_type": mime},
            timeout=30,
        )
        r.raise_for_status()
        info = r.json()
        upload_url = info["upload_url"]
        file_upload_id = info["id"]
        # 認証は同じ Bearer、multipart/form-data で送信 (Content-Type は自動設定させる)
        headers = {
            "Authorization": NOTION_HEADERS["Authorization"],
            "Notion-Version": NOTION_HEADERS["Notion-Version"],
        }
        r2 = requests.post(
            upload_url,
            headers=headers,
            files={"file": (filename, image_bytes, mime)},
            timeout=60,
        )
        if not r2.ok:
            print(f"notion upload send error: {r2.status_code} {r2.text}", file=sys.stderr)
            return None
        return file_upload_id
    except Exception as e:
        print(f"notion upload failed for {filename}: {e}", file=sys.stderr)
        return None


def notion_find_by_permalink(permalink: str) -> tuple[str, str] | None:
    """permalinkでNotion既存レコード検索。(page_id, 既存のSlack最終編集ts) を返す"""
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
    if not results:
        return None
    page = results[0]
    prop = page.get("properties", {}).get("Slack最終編集", {})
    rich = prop.get("rich_text") or []
    stored_ts = rich[0].get("plain_text", "") if rich else ""
    return page["id"], stored_ts


def notion_upsert(
    permalink: str,
    date: dt.date,
    meal: str,
    content: str,
    source: str,
    slack_last_edit_ts: str,
    photo_uploads: list[tuple[str, str]] | None = None,
) -> str:
    """Notionへupsert。'created' / 'updated' / 'skipped' を返す。
    photo_uploads は (file_upload_id, filename) のリスト"""
    found = notion_find_by_permalink(permalink)
    page_id = found[0] if found else None
    stored_ts = found[1] if found else ""
    # Slack側の最終編集tsが前回同期時と同じなら、Notion手動編集を保護してスキップ
    if page_id and stored_ts == slack_last_edit_ts:
        return "skipped"
    title = f"{date.isoformat()} {meal}"
    properties = {
        "名前": {"title": [{"text": {"content": title}}]},
        "日付": {"date": {"start": date.isoformat()}},
        "区分": {"select": {"name": meal}},
        "内容": {"rich_text": [{"text": {"content": content[:2000]}}]},
        "Slack投稿元": {"url": permalink},
        "取込元": {"select": {"name": source}},
        "Slack最終編集": {"rich_text": [{"text": {"content": slack_last_edit_ts}}]},
    }
    if photo_uploads:
        properties["写真"] = {
            "files": [
                {"type": "file_upload", "file_upload": {"id": fid}, "name": name}
                for fid, name in photo_uploads
            ]
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
    return "updated" if page_id else "created"


EVAL_PROMPT_TMPL = (
    "あなたはこの人専属の食事アドバイザーです。\n\n"
    "【重要な背景: 健康診断結果の情報整理】\n"
    "{health_context}\n\n"
    "【直近{days}日間の食事記録 (今回分を含む)】\n"
    "{records}\n\n"
    "【今回投稿された食事】\n"
    "- 日付: {date}\n"
    "- 区分: {meal}\n"
    "- 内容: {content}\n\n"
    "今回の食事について、健康診断で指摘されている項目・直近の食事傾向をふまえ、"
    "良い点と気になる点を短く評価してください。\n\n"
    "出力条件:\n"
    "- Slackに投稿するので250文字以内、簡潔に\n"
    "- 前置き・自己紹介・締めの挨拶は不要、本文のみ\n"
    "- Markdownの見出しやコードブロックは使わない (箇条書きは可)\n"
)


def evaluate_and_post(
    slack: WebClient,
    gemini_client: genai.Client,
    channel_id: str,
    thread_ts: str,
    date: dt.date,
    meal: str,
    content: str,
) -> None:
    """新規登録された食事について評価コメントを生成し、元投稿にスレッド返信"""
    try:
        records = fetch_recent_records(ADVICE_LOOKBACK_DAYS)
        health_context = fetch_page_text(HEALTH_CONTEXT_PAGE_ID)
        prompt = EVAL_PROMPT_TMPL.format(
            health_context=health_context or "(取得できず)",
            days=ADVICE_LOOKBACK_DAYS,
            records=format_records(records),
            date=date.isoformat(),
            meal=meal,
            content=content[:1000],
        )
        resp = gemini_client.models.generate_content(
            model=GEMINI_MODEL, contents=[prompt]
        )
        evaluation = (resp.text or "").strip()
        if not evaluation:
            return
        slack.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text=f":memo: *{meal}評価*\n{evaluation}",
        )
    except Exception as e:
        print(f"evaluation post failed: {e}", file=sys.stderr)


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

    # 画像解析 + Notion へアップロード
    image_texts: list[str] = []
    photo_uploads: list[tuple[str, str]] = []
    for f in msg.get("files") or []:
        mimetype = f.get("mimetype", "")
        if not mimetype.startswith("image/"):
            continue
        url = f.get("url_private")
        if not url:
            continue
        try:
            body, mt = download_slack_file(url)
            effective_mime = mt or mimetype
            image_texts.append(analyze_image(gemini_client, body, effective_mime))
            filename = f.get("name") or "photo"
            uploaded = notion_upload_file(body, filename, effective_mime)
            if uploaded:
                photo_uploads.append((uploaded, filename))
        except Exception as e:
            print(f"image analyze failed for {url}: {e}", file=sys.stderr)

    if not text and not image_texts:
        return  # 空投稿は無視

    # 判定は生テキストで行い、Notion保存用は Gemini で日付表現を除去
    date = extract_date(text, ts_dt)
    meal = classify_meal(text, ts_dt.hour)
    cleaned_text = strip_date_via_gemini(gemini_client, text) if text else ""

    if cleaned_text and image_texts:
        source = "テキスト+画像"
        content = cleaned_text + "\n\n[画像解析]\n" + "\n\n".join(image_texts)
    elif image_texts:
        source = "画像解析"
        content = "\n\n".join(image_texts)
    elif cleaned_text:
        source = "テキスト"
        content = cleaned_text
    else:
        return  # 除去後に本文空・画像なしなら記録しない

    permalink = slack.chat_getPermalink(channel=channel_id, message_ts=msg["ts"])[
        "permalink"
    ]
    # Slack側の最終編集ts (編集済なら edited.ts、なければ投稿ts)
    slack_last_edit_ts = (msg.get("edited") or {}).get("ts") or msg["ts"]
    result = notion_upsert(
        permalink, date, meal, content, source, slack_last_edit_ts, photo_uploads
    )
    print(f"{result}: {date} {meal} ({source}) {permalink}")
    # 新規登録時のみ評価コメントを元投稿にスレッド返信
    if result == "created":
        evaluate_and_post(
            slack, gemini_client, channel_id, msg["ts"], date, meal, content
        )


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

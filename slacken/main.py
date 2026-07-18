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
GEMINI_IMAGE_MODEL = os.environ.get("GEMINI_IMAGE_MODEL", "gemini-2.5-flash-image")

# 評価イラストの画風 (毎回ランダムに1つ選ぶ)
EVAL_IMAGE_STYLES = [
    "水彩画風、淡い色調、やわらかい筆致",
    "レトロなドット絵、16bit時代のRPG風、シンプルなパレット",
    "ゆるいイラスト、手描き風、パステルカラー、フラットデザイン",
    "写実的な絵画、油彩画風、豊かな陰影",
]

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
    "間食": ["間食", "おやつ"],
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


SUMMARIZE_PROMPT = (
    "以下は食事内容の記述です。日本語で20文字未満に要約してください。"
    "料理名を優先的に含め、句読点や記号は極力使わず、体言止めで簡潔にしてください。"
    "前置き・引用符・説明は不要、要約文だけを返してください。\n\n"
    "本文:\n"
)


def summarize_content(client: genai.Client, text: str) -> str:
    """Geminiで食事内容を20文字未満に要約。失敗時は先頭20文字を返す (同期は落とさない)"""
    fallback = text.strip().splitlines()[0][:19] if text.strip() else ""
    max_attempts = 5
    for attempt in range(max_attempts):
        try:
            resp = client.models.generate_content(
                model=GEMINI_MODEL, contents=[SUMMARIZE_PROMPT + text]
            )
            summary = (resp.text or "").strip().strip("「」\"'`")
            summary = summary.splitlines()[0] if summary else ""
            if summary:
                return summary[:19]
            return fallback
        except genai_errors.APIError as e:
            code = getattr(e, "code", None)
            if code in (429, 500, 503) and attempt < max_attempts - 1:
                wait = min(2**attempt, 30) + random.random()
                print(f"gemini summarize {code}, retry in {wait:.1f}s", file=sys.stderr)
                time.sleep(wait)
                continue
            print(f"gemini summarize failed: {e}", file=sys.stderr)
            return fallback
    return fallback


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


def _archive_notion_page(page_id: str) -> None:
    """Notionページをアーカイブ (重複レコード掃除用)"""
    try:
        r = requests.patch(
            f"{NOTION_API}/pages/{page_id}",
            headers=NOTION_HEADERS,
            json={"archived": True},
            timeout=30,
        )
        r.raise_for_status()
        print(f"archived duplicate page: {page_id}", file=sys.stderr)
    except Exception as e:
        print(f"archive failed for {page_id}: {e}", file=sys.stderr)


def notion_find_by_permalink(permalink: str) -> tuple[str, str] | None:
    """permalinkでNotion既存レコード検索。(page_id, 既存のSlack最終編集ts) を返す。
    同一permalinkが複数存在した場合 (並行実行race等) は最古1件を残し他はアーカイブする"""
    r = requests.post(
        f"{NOTION_API}/databases/{NOTION_DATABASE_ID}/query",
        headers=NOTION_HEADERS,
        json={
            # スレッド化前後で ?thread_ts=... が付いた URL も既存レコードとして扱うため contains で検索
            "filter": {"property": "Slack投稿元", "url": {"contains": permalink}},
            "sorts": [{"timestamp": "created_time", "direction": "ascending"}],
            "page_size": 10,
        },
        timeout=30,
    )
    r.raise_for_status()
    results = r.json().get("results", [])
    if not results:
        return None
    page = results[0]
    for dup in results[1:]:
        _archive_notion_page(dup["id"])
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
    title: str,
    photo_uploads: list[tuple[str, str]] | None = None,
) -> tuple[str, str | None, str | None]:
    """Notionへupsert。('created'|'updated'|'skipped', page_url or None, page_id or None) を返す。
    photo_uploads は (file_upload_id, filename) のリスト"""
    found = notion_find_by_permalink(permalink)
    page_id = found[0] if found else None
    stored_ts = found[1] if found else ""
    # Slack側の最終編集tsが前回同期時と同じなら、Notion手動編集を保護してスキップ
    if page_id and stored_ts == slack_last_edit_ts:
        return "skipped", None, page_id
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
    body = r.json()
    page_url = body.get("url")
    new_page_id = body.get("id") or page_id
    return ("updated" if page_id else "created"), page_url, new_page_id


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


def generate_evaluation_image(
    client: genai.Client, evaluation: str, meal: str
) -> tuple[bytes, str, str] | None:
    """評価テキストを可視化したイラストを Gemini で生成。(bytes, mime, style) or None。
    画風は EVAL_IMAGE_STYLES からランダムに選択。画像内に文字は入れない"""
    style = random.choice(EVAL_IMAGE_STYLES)
    prompt = (
        f"以下の食事に対する評価コメントを、比喩的・情緒的に表現するイラストを1枚生成してください。\n"
        f"画風: {style}\n"
        f"重要な制約: 画像内には日本語・英語問わず一切の文字・数字・記号・ラベルを描画しないこと。\n"
        f"料理そのものを描くのではなく、評価の雰囲気 (ポジティブ/ネガティブ、ヘルシー/リッチ 等) を"
        f"色彩・構図・モチーフで象徴的に表現してください。\n\n"
        f"食事区分: {meal}\n"
        f"評価コメント:\n{evaluation[:800]}"
    )
    try:
        resp = client.models.generate_content(
            model=GEMINI_IMAGE_MODEL,
            contents=[prompt],
        )
    except Exception as e:
        print(f"gemini image gen failed: {e}", file=sys.stderr)
        return None
    for cand in resp.candidates or []:
        parts = getattr(cand.content, "parts", None) or []
        for p in parts:
            inline = getattr(p, "inline_data", None)
            if inline and getattr(inline, "data", None):
                return inline.data, (inline.mime_type or "image/png"), style
    print("gemini image gen: no inline_data in response", file=sys.stderr)
    return None


def notion_update_evaluation(
    page_id: str, evaluation: str, image: tuple[bytes, str] | None = None
) -> None:
    """Notionページ本文に評価結果を段落ブロックで追記する。
    image が指定されていれば Notion File Upload 経由で画像ブロックも追記。
    Notion rich_text の1要素あたり2000文字上限に合わせて分割"""
    chunks = [evaluation[i : i + 1900] for i in range(0, len(evaluation), 1900)] or [""]
    children: list[dict] = [
        {
            "object": "block",
            "type": "paragraph",
            "paragraph": {
                "rich_text": [{"type": "text", "text": {"content": chunk}}]
            },
        }
        for chunk in chunks
    ]
    if image is not None:
        img_bytes, img_mime = image
        ext = "png" if "png" in img_mime else ("jpg" if "jpeg" in img_mime else "img")
        upload_id = notion_upload_file(img_bytes, f"evaluation.{ext}", img_mime)
        if upload_id:
            children.append(
                {
                    "object": "block",
                    "type": "image",
                    "image": {
                        "type": "file_upload",
                        "file_upload": {"id": upload_id},
                    },
                }
            )
    r = requests.patch(
        f"{NOTION_API}/blocks/{page_id}/children",
        headers=NOTION_HEADERS,
        json={"children": children},
        timeout=30,
    )
    if not r.ok:
        print(f"notion evaluation append failed: {r.status_code} {r.text}", file=sys.stderr)
        r.raise_for_status()


def evaluate_and_post(
    slack: WebClient,
    gemini_client: genai.Client,
    channel_id: str,
    thread_ts: str,
    date: dt.date,
    meal: str,
    content: str,
    page_id: str | None,
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
        image = generate_evaluation_image(gemini_client, evaluation, meal)
        slack_text = f":memo: *{meal}評価*\n{evaluation}"
        posted = False
        if image is not None:
            img_bytes, img_mime, _style = image
            ext = "png" if "png" in img_mime else ("jpg" if "jpeg" in img_mime else "img")
            try:
                slack.files_upload_v2(
                    channel=channel_id,
                    thread_ts=thread_ts,
                    initial_comment=slack_text,
                    content=img_bytes,
                    filename=f"evaluation.{ext}",
                    title=f"{meal}評価イラスト",
                )
                posted = True
            except SlackApiError as e:
                print(f"slack files_upload_v2 failed: {e}", file=sys.stderr)
        if not posted:
            slack.chat_postMessage(
                channel=channel_id, thread_ts=thread_ts, text=slack_text
            )
        if page_id:
            try:
                notion_update_evaluation(
                    page_id,
                    evaluation,
                    image=(image[0], image[1]) if image is not None else None,
                )
            except Exception as e:
                print(f"notion evaluation write failed: {e}", file=sys.stderr)
    except Exception as e:
        print(f"evaluation post failed: {e}", file=sys.stderr)


def process_one(
    slack: WebClient, gemini_client: genai.Client, channel_id: str, msg: dict
) -> None:
    """1件のSlackメッセージをNotionへupsertし、必要に応じてスレッドに評価返信する。
    cron (conversations.history) 経路と Events API 経路の両方から呼ばれる想定。"""
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

    raw_permalink = slack.chat_getPermalink(channel=channel_id, message_ts=msg["ts"])[
        "permalink"
    ]
    # スレッド化後に thread_ts/cid クエリが付与されると別URL扱いで重複作成されるため正規化
    permalink = raw_permalink.split("?", 1)[0]
    # Slack側の最終編集ts (編集済なら edited.ts、なければ投稿ts)
    slack_last_edit_ts = (msg.get("edited") or {}).get("ts") or msg["ts"]
    title = summarize_content(gemini_client, content)
    result, page_url, page_id = notion_upsert(
        permalink, date, meal, content, source, slack_last_edit_ts, title, photo_uploads
    )
    print(f"{result}: {date} {meal} ({source}) {permalink} -> {page_url}")
    # 新規登録時のみ Notion ページURLと評価コメントを元投稿にスレッド返信
    if result == "created":
        if page_url:
            try:
                slack.chat_postMessage(
                    channel=channel_id,
                    thread_ts=msg["ts"],
                    text=f":card_index: Notionに登録しました: {page_url}",
                    unfurl_links=False,
                )
            except Exception as e:
                print(f"notion url post failed: {e}", file=sys.stderr)
        evaluate_and_post(
            slack, gemini_client, channel_id, msg["ts"], date, meal, content, page_id
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
                process_one(slack, gemini_client, channel_id, msg)
                total += 1
            except Exception as e:
                print(f"failed to process ts={msg.get('ts')}: {e}", file=sys.stderr)

        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break

    print(f"done. processed {total} messages")


if __name__ == "__main__":
    main()

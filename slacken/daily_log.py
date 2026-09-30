"""Slack (shakuji) 自分用チャネル → Notion 日誌DB 蓄積スクリプト"""
from __future__ import annotations

import datetime as dt
import os
import sys
import time
from collections import defaultdict
from zoneinfo import ZoneInfo

import requests
from slack_sdk import WebClient

JST = ZoneInfo("Asia/Tokyo")
NOTION_API = "https://api.notion.com/v1"
TEXT_LIMIT = 1900  # Notion rich_text 1要素あたり2000文字上限に余裕を持たせる


def split_text(text: str, limit: int = TEXT_LIMIT) -> list[str]:
    return [text[i : i + limit] for i in range(0, len(text), limit)] or [""]


def jst_datetime(ts: str) -> dt.datetime:
    return dt.datetime.fromtimestamp(float(ts), JST)


def group_by_date(messages: list[dict]) -> dict[str, list[dict]]:
    """JST の投稿日 (YYYY-MM-DD) ごとに ts 昇順でグルーピング"""
    groups: dict[str, list[dict]] = defaultdict(list)
    for m in sorted(messages, key=lambda m: float(m["ts"])):
        groups[jst_datetime(m["ts"]).strftime("%Y-%m-%d")].append(m)
    return dict(groups)


def is_target_message(msg: dict) -> bool:
    """bot 投稿・join 等の subtype 付き・空本文を除外 (通常投稿と file_share は対象)"""
    if msg.get("bot_id") or msg.get("subtype") not in (None, "file_share"):
        return False
    return bool((msg.get("text") or "").strip())


def normalize_permalink(url: str) -> str:
    return url.split("?", 1)[0]


def build_blocks(msg: dict, permalink: str) -> list[dict]:
    """`HH:MM 本文` の段落ブロック。HH:MM に permalink を付与"""
    hhmm = jst_datetime(msg["ts"]).strftime("%H:%M")
    chunks = split_text(msg["text"].strip())
    blocks = []
    for i, chunk in enumerate(chunks):
        rich_text = []
        if i == 0:
            rich_text.append(
                {"type": "text", "text": {"content": hhmm, "link": {"url": permalink}}}
            )
            chunk = f" {chunk}"
        rich_text.append({"type": "text", "text": {"content": chunk}})
        blocks.append(
            {"object": "block", "type": "paragraph", "paragraph": {"rich_text": rich_text}}
        )
    return blocks


def extract_links(blocks: list[dict]) -> set[str]:
    """既存ブロックから permalink (rich_text の link) を集める"""
    links: set[str] = set()
    for b in blocks:
        for rt in (b.get(b.get("type", ""), {}) or {}).get("rich_text", []):
            link = (rt.get("text") or {}).get("link")
            if link:
                links.add(normalize_permalink(link["url"]))
    return links


class Notion:
    def __init__(self, token: str, database_id: str):
        self.database_id = database_id
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Notion-Version": "2022-06-28",
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, **kw) -> dict:
        r = requests.request(method, f"{NOTION_API}{path}", headers=self.headers, timeout=30, **kw)
        if not r.ok:
            print(f"notion {method} {path} failed: {r.status_code} {r.text}", file=sys.stderr)
            r.raise_for_status()
        return r.json()

    def find_or_create_day_page(self, day: str) -> str:
        res = self._request(
            "POST",
            f"/databases/{self.database_id}/query",
            json={"filter": {"property": "date", "date": {"equals": day}}, "page_size": 1},
        )
        if res["results"]:
            return res["results"][0]["id"]
        page = self._request(
            "POST",
            "/pages",
            json={
                "parent": {"database_id": self.database_id},
                "properties": {
                    "title": {"title": [{"type": "text", "text": {"content": day}}]},
                    "date": {"date": {"start": day}},
                },
            },
        )
        return page["id"]

    def list_children(self, page_id: str) -> list[dict]:
        blocks: list[dict] = []
        cursor = None
        while True:
            params = {"page_size": 100, **({"start_cursor": cursor} if cursor else {})}
            res = self._request("GET", f"/blocks/{page_id}/children", params=params)
            blocks.extend(res["results"])
            if not res.get("has_more"):
                return blocks
            cursor = res["next_cursor"]

    def append_blocks(self, page_id: str, blocks: list[dict]) -> None:
        for i in range(0, len(blocks), 100):
            self._request(
                "PATCH", f"/blocks/{page_id}/children", json={"children": blocks[i : i + 100]}
            )


def fetch_messages(slack: WebClient, channel: str, lookback_hours: int) -> list[dict]:
    oldest = time.time() - lookback_hours * 3600
    messages: list[dict] = []
    cursor = None
    while True:
        res = slack.conversations_history(
            channel=channel, oldest=str(oldest), limit=100, cursor=cursor
        )
        messages.extend(res["messages"])
        cursor = (res.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            return messages


def main() -> None:
    slack = WebClient(token=os.environ["DAILY_LOG_SLACK_TOKEN"])
    notion = Notion(os.environ["NOTION_TOKEN"], os.environ["DAILY_LOG_DATABASE_ID"])
    channel = os.environ.get("DAILY_LOG_CHANNEL_ID", "C0C4WUKF47K")
    lookback_hours = int(os.environ.get("LOOKBACK_HOURS", "48"))

    messages = [m for m in fetch_messages(slack, channel, lookback_hours) if is_target_message(m)]
    added = skipped = 0
    for day, msgs in group_by_date(messages).items():
        page_id = notion.find_or_create_day_page(day)
        existing = extract_links(notion.list_children(page_id))
        blocks: list[dict] = []
        for m in msgs:
            permalink = normalize_permalink(
                slack.chat_getPermalink(channel=channel, message_ts=m["ts"])["permalink"]
            )
            if permalink in existing:
                skipped += 1
                continue
            blocks.extend(build_blocks(m, permalink))
            added += 1
        if blocks:
            notion.append_blocks(page_id, blocks)
    print(f"done: added={added} skipped={skipped}")


if __name__ == "__main__":
    main()

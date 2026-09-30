from slacken.daily_log import (
    build_blocks,
    extract_links,
    group_by_date,
    is_target_message,
    normalize_permalink,
    split_text,
)

# 2026-09-30 15:30:00 UTC = 2026-10-01 00:30 JST
TS_JST_NEXT_DAY = "1790782200.000100"
# 2026-09-30 05:00:00 UTC = 2026-09-30 14:00 JST
TS_JST_DAY = "1790744400.000100"


def test_group_by_date_jst境界():
    msgs = [{"ts": TS_JST_NEXT_DAY, "text": "b"}, {"ts": TS_JST_DAY, "text": "a"}]
    g = group_by_date(msgs)
    assert list(g) == ["2026-09-30", "2026-10-01"]


def test_is_target_message():
    assert is_target_message({"text": "hi", "ts": "1"})
    assert is_target_message({"text": "hi", "subtype": "file_share"})
    assert not is_target_message({"text": "hi", "bot_id": "B1"})
    assert not is_target_message({"text": "joined", "subtype": "channel_join"})
    assert not is_target_message({"text": "  "})


def test_split_text():
    assert split_text("") == [""]
    assert [len(c) for c in split_text("a" * 4000)] == [1900, 1900, 200]


def test_build_blocks_と_extract_links往復():
    url = "https://shakuji.slack.com/archives/C0C4WUKF47K/p1"
    blocks = build_blocks({"ts": TS_JST_DAY, "text": "メモ"}, url)
    assert blocks[0]["paragraph"]["rich_text"][0]["text"]["content"] == "14:00"
    assert extract_links(blocks) == {url}


def test_normalize_permalink():
    assert normalize_permalink("https://x/p1?thread_ts=1") == "https://x/p1"

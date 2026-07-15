"""slacken.main の純粋関数に対するテスト (ネットワーク・外部API呼び出しはしない)"""
import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from slacken.main import (
    classify_meal,
    extract_date,
    has_date_expression,
)

JST = ZoneInfo("Asia/Tokyo")


class TestClassifyMeal:
    @pytest.mark.parametrize(
        "text,hour,expected",
        [
            # キーワード優先
            ("朝はパン", 15, "朝食"),
            ("ランチはラーメン", 20, "昼食"),
            ("夜はカレー", 8, "夕食"),
            ("モーニング", 22, "朝食"),
            ("ディナー", 6, "夕食"),
            # キーワードなし → 時間帯フォールバック
            ("カレー", 6, "朝食"),
            ("カレー", 12, "昼食"),
            ("カレー", 19, "夕食"),
            ("カレー", 15, "間食"),
            ("カレー", 23, "間食"),
            ("カレー", 3, "間食"),
        ],
    )
    def test_classify(self, text, hour, expected):
        assert classify_meal(text, hour) == expected


class TestExtractDate:
    def test_no_hint_returns_post_date(self):
        ts = dt.datetime(2026, 7, 15, 12, 0, tzinfo=JST)
        assert extract_date("ラーメン", ts) == dt.date(2026, 7, 15)

    def test_slash_date(self):
        ts = dt.datetime(2026, 7, 15, 12, 0, tzinfo=JST)
        assert extract_date("7/10 昼はカレー", ts) == dt.date(2026, 7, 10)

    def test_kanji_date(self):
        ts = dt.datetime(2026, 7, 15, 12, 0, tzinfo=JST)
        assert extract_date("8月3日 焼肉", ts) == dt.date(2026, 8, 3)

    def test_future_date_treated_as_prev_year(self):
        # 1月に「12/30」と書いた場合は前年扱い
        ts = dt.datetime(2026, 1, 5, 12, 0, tzinfo=JST)
        assert extract_date("12/30 蕎麦", ts) == dt.date(2025, 12, 30)

    def test_invalid_date_falls_through_to_keyword(self):
        # 2/30 は無効日付 → 相対キーワード評価へ
        ts = dt.datetime(2026, 7, 15, 12, 0, tzinfo=JST)
        assert extract_date("2/30 昨日カレー", ts) == dt.date(2026, 7, 14)

    @pytest.mark.parametrize(
        "text,offset",
        [
            ("今日ラーメン", 0),
            ("本日サラダ", 0),
            ("今朝パン", 0),
            ("昨日カレー", -1),
            ("きのう寿司", -1),
            ("ゆうべ天ぷら", -1),
            ("昨夜焼肉", -1),
            ("一昨日ステーキ", -2),
            ("おとというどん", -2),
        ],
    )
    def test_relative_keywords(self, text, offset):
        ts = dt.datetime(2026, 7, 15, 12, 0, tzinfo=JST)
        assert extract_date(text, ts) == dt.date(2026, 7, 15) + dt.timedelta(days=offset)

    def test_slash_date_takes_priority_over_relative(self):
        # M/D が先に評価される
        ts = dt.datetime(2026, 7, 15, 12, 0, tzinfo=JST)
        assert extract_date("7/10 昨日食べた", ts) == dt.date(2026, 7, 10)


class TestHasDateExpression:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("ラーメン", False),
            ("カレー美味しかった", False),
            ("昨日カレー", True),
            ("7/14 昼", True),
            ("8月3日 焼肉", True),
            ("今朝パン", True),
            ("ゆうべ寿司", True),
            ("一昨日の晩", True),
        ],
    )
    def test_detection(self, text, expected):
        assert has_date_expression(text) is expected

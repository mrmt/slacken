"""slacken.main の純粋関数に対するテスト (ネットワーク・外部API呼び出しはしない)"""
import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from slacken.main import (
    classify_meal,
    extract_date,
    has_date_expression,
    parse_evaluation,
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
            ("間食にチョコ", 12, "間食"),
            ("おやつタイム", 12, "間食"),
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


class TestParseEvaluation:
    def test_standard_format(self):
        raw = "点数: 82\n\n野菜が豊富で良好。塩分がやや高め。"
        score, body = parse_evaluation(raw)
        assert score == 82
        assert body == "野菜が豊富で良好。塩分がやや高め。"

    def test_fullwidth_colon(self):
        raw = "点数：73\n\n本文"
        score, body = parse_evaluation(raw)
        assert score == 73
        assert body == "本文"

    def test_no_blank_line(self):
        raw = "点数: 60\n本文だけ"
        score, body = parse_evaluation(raw)
        assert score == 60
        assert body == "本文だけ"

    def test_no_score_line(self):
        raw = "とても良い食事です。"
        score, body = parse_evaluation(raw)
        assert score is None
        assert body == "とても良い食事です。"

    def test_out_of_range(self):
        raw = "点数: 150\n\n本文"
        score, body = parse_evaluation(raw)
        assert score is None
        # 範囲外は本文として原文まるごと
        assert "点数: 150" in body

    def test_zero(self):
        raw = "点数: 0\n\n全然だめ"
        score, body = parse_evaluation(raw)
        assert score == 0
        assert body == "全然だめ"

    def test_hundred(self):
        raw = "点数: 100\n\n完璧"
        score, body = parse_evaluation(raw)
        assert score == 100
        assert body == "完璧"

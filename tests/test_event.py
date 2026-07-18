from slacken.event import normalize_event


class TestNormalizeEvent:
    def test_plain_message(self):
        e = {"type": "message", "channel": "C1", "ts": "1.2", "text": "ラーメン", "user": "U1"}
        assert normalize_event(e) == ("C1", e)

    def test_outer_body_shape(self):
        outer = {"event": {"type": "message", "channel": "C1", "ts": "1.2", "text": "x", "user": "U1"}}
        result = normalize_event(outer)
        assert result is not None
        ch, msg = result
        assert ch == "C1"
        assert msg["ts"] == "1.2"

    def test_bot_message_skipped(self):
        e = {"type": "message", "subtype": "bot_message", "channel": "C1", "ts": "1.2"}
        assert normalize_event(e) is None

    def test_bot_id_skipped(self):
        e = {"type": "message", "channel": "C1", "ts": "1.2", "bot_id": "B123"}
        assert normalize_event(e) is None

    def test_channel_join_skipped(self):
        e = {"type": "message", "subtype": "channel_join", "channel": "C1", "ts": "1.2"}
        assert normalize_event(e) is None

    def test_message_changed_uses_inner(self):
        e = {
            "type": "message",
            "subtype": "message_changed",
            "channel": "C1",
            "message": {"ts": "1.0", "text": "edited", "edited": {"ts": "2.0"}, "user": "U1"},
        }
        result = normalize_event(e)
        assert result is not None
        ch, msg = result
        assert ch == "C1"
        assert msg["ts"] == "1.0"
        assert msg["edited"]["ts"] == "2.0"

    def test_non_message_type(self):
        e = {"type": "reaction_added", "channel": "C1"}
        assert normalize_event(e) is None

    def test_missing_channel(self):
        e = {"type": "message", "ts": "1.2", "text": "x"}
        assert normalize_event(e) is None

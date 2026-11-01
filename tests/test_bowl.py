from datetime import date, datetime, timedelta

from rice.bowl import (Ledger, Snapshot, low_note, meals, panel_data, parse_balance, report, report_text,
                       strip_blocks)

T0 = datetime(2026, 11, 1, 9, 0).astimezone()


def snap(hours: float, total: float) -> Snapshot:
    return Snapshot(at=(T0 + timedelta(hours=hours)).isoformat(), total=total, topped_up=total)


def test_parse_balance_prefers_the_currency_with_money():
    data = {"is_available": True, "balance_infos": [
        {"currency": "USD", "total_balance": "0.00", "granted_balance": "0.00", "topped_up_balance": "0.00"},
        {"currency": "CNY", "total_balance": "18.30", "granted_balance": "0.00", "topped_up_balance": "18.30"}]}
    s = parse_balance(data, T0)
    assert (s.currency, s.total, s.topped_up) == ("CNY", 18.30, 18.30)
    assert parse_balance({}, T0) is None


def test_ledger_merges_unchanged_checks_and_keeps_changes(tmp_path):
    led = Ledger()
    led.add(snap(0, 10.0))
    led.add(snap(0.2, 10.0))  # same balance, 12 minutes later: replaces
    led.add(snap(0.4, 9.9))  # changed: kept
    assert [s.total for s in led.items()] == [10.0, 9.9]
    led.save(tmp_path / "l.json")
    assert [s.total for s in Ledger.load(tmp_path / "l.json").items()] == [10.0, 9.9]
    assert Ledger.load(tmp_path / "missing.json").items() == []


def test_eating_and_topping_up():
    items = [snap(0, 10.0), snap(24, 9.0), snap(30, 19.0), snap(48, 18.0)]
    m = meals(items)
    assert [round(x.eaten, 2) for x in m] == [1.0, 0.0, 1.0]
    assert [round(x.topped, 2) for x in m] == [0.0, 10.0, 0.0]
    r = report(items, T0 + timedelta(hours=48))
    assert r.balance == 18.0 and r.last_topup[1] == 10.0
    assert round(r.per_day, 2) == 1.0 and round(r.days_left) == 18
    assert [d for d, _ in r.days] == [date(2026, 11, 1), date(2026, 11, 2), date(2026, 11, 3)]


def test_too_little_history_says_so():
    r = report([snap(0, 10.0), snap(2, 9.9)], T0 + timedelta(hours=2))
    assert r.per_day is None and r.days_left is None and r.today_since == T0
    text = report_text(r, "主人")
    assert "还剩 ¥9.90" in text and "记账才刚开始" in text and "从 09:00 开始记" in text
    first = report_text(report([snap(0, 10.0)], T0), "主人")
    assert "09:00 才开始记账" in first and "¥0.00" not in first


def test_report_text_with_tokens_and_trend():
    items = [snap(0, 10.0), snap(24, 9.0), snap(48, 8.0)]
    r = report(items, T0 + timedelta(hours=48))
    text = report_text(r, "主人", [(date(2026, 11, 2), 350000, 210000), (date(2026, 11, 3), 8000, 0)])
    assert "还能吃大约 8 天" in text and "35.0万（60% 命中缓存）" in text and "大白饭" in text


def test_low_note_only_when_running_low():
    long_ok = report([snap(0, 30.0), snap(24, 29.0)], T0 + timedelta(hours=24))
    assert low_note(long_ok, "主人", warn_days=5, warn_balance=3) == ""
    low = report([snap(0, 4.0), snap(24, 3.0)], T0 + timedelta(hours=24))
    note = low_note(low, "主人", warn_days=5, warn_balance=3)
    assert note.startswith("<rice_bowl>") and "大约还能吃 3 天" in note
    poor = report([snap(0, 1.0)], T0)
    assert "还剩 ¥1.00" in low_note(poor, "主人", warn_days=5, warn_balance=3)


def test_strip_blocks_cleans_old_notes():
    ctx = [{"role": "user", "content": "饭够吗\n\n<rice_bowl>\n快吃完了\n</rice_bowl>"},
           {"role": "assistant", "content": "<rice_bowl>x</rice_bowl>"},
           {"role": "user", "content": [{"type": "text", "text": "hi"}, {"type": "text", "text": "<rice_bowl>a</rice_bowl>"}]}]
    assert strip_blocks(ctx, ("rice_bowl",)) == 2
    assert ctx[0]["content"] == "饭够吗" and ctx[1]["content"] == "<rice_bowl>x</rice_bowl>"
    assert ctx[2]["content"] == [{"type": "text", "text": "hi"}]


def test_panel_data_for_the_dashboard():
    items = [snap(0, 10.0), snap(12, 9.0), snap(24, 8.0), snap(30, 20.0), snap(48, 18.0)]
    now = T0 + timedelta(hours=50)
    d = panel_data(items, now, token_days=[(date(2026, 11, 2), 1000, 900), (date(2026, 11, 3), 0, 0)])
    assert not d["empty"] and d["balance"] == 18.0 and d["checks"] == 5
    assert [t["amount"] for t in d["topups"]] == [12.0]  # 8 -> 20
    assert [s["mark"] != "" for s in d["series"]] == [False, False, False, True, False]
    assert sum(r["eaten"] for r in d["daily"]) == 4.0  # 1 + 1 + 2; the top-up interval shows no eating
    assert d["daily"][0]["partial"] and d["daily"][-1]["partial"]
    assert d["tokens"] == [{"date": "2026-11-02", "input": 1000, "cached": 900, "hit": 90},
                           {"date": "2026-11-03", "input": 0, "cached": 0, "hit": None}]
    nothing = panel_data([], now)
    assert nothing["empty"] and nothing["tokens"] == []

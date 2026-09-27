import io
import json
import urllib.error
from datetime import datetime, timezone

import pytest

import amber_core as core


# --- config ----------------------------------------------------------------
def test_normalise_fills_defaults_and_rejects_junk():
    cfg = core.normalise_config({"mode": "sideways", "resolution": "7", "refresh_sec": 1,
                                 "notify_low": "abc", "notify_hysteresis": -5,
                                 "api_token": "  tok  ", "extra": 1})
    assert cfg["mode"] == "import"
    assert cfg["resolution"] == 5
    assert cfg["refresh_sec"] == 30
    assert cfg["notify_low"] == core.DEFAULT_CONFIG["notify_low"]
    assert cfg["notify_hysteresis"] == 0
    assert cfg["api_token"] == "tok"
    assert cfg["extra"] == 1  # unknown keys survive


def test_normalise_handles_non_dict():
    assert core.normalise_config(["not", "a", "dict"]) == core.DEFAULT_CONFIG


def test_normalise_resets_inverted_thresholds():
    cfg = core.normalise_config({"notify_low": 50, "notify_high": 10})
    assert (cfg["notify_low"], cfg["notify_high"]) == (19.0, 40.0)


def test_save_and_load_roundtrip(tmp_path):
    path = tmp_path / "sub" / "config.json"
    cfg = core.normalise_config({"api_token": "k", "site_id": "s", "mode": "both"})
    core.save_config(cfg, path)
    assert core.load_config(path) == cfg
    assert [p.name for p in path.parent.iterdir()] == ["config.json"]  # no temp files left


def test_load_corrupt_file_gives_defaults(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{ half written", encoding="utf-8")
    assert core.load_config(path) == core.DEFAULT_CONFIG


def test_update_config_only_touches_given_keys(tmp_path):
    path = tmp_path / "config.json"
    core.save_config(core.normalise_config({"api_token": "k", "mode": "both"}), path)
    core.update_config({"notify_low": 5.0}, path)
    cfg = json.loads(path.read_text(encoding="utf-8"))
    assert cfg["api_token"] == "k" and cfg["mode"] == "both" and cfg["notify_low"] == 5.0


# --- API -------------------------------------------------------------------
class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _http_error(code, headers=None):
    return urllib.error.HTTPError("u", code, "x", headers or {}, None)


@pytest.mark.parametrize("exc, kind", [
    (_http_error(401), "auth"),
    (_http_error(403), "auth"),
    (_http_error(429, {"Retry-After": "42"}), "rate"),
    (_http_error(500), "http"),
    (urllib.error.URLError("down"), "network"),
    (TimeoutError(), "network"),
])
def test_api_errors_are_classified(monkeypatch, exc, kind):
    def boom(*a, **k):
        raise exc
    monkeypatch.setattr(core.urllib.request, "urlopen", boom)
    with pytest.raises(core.ApiError) as ei:
        core.list_sites("t")
    assert ei.value.kind == kind
    if kind == "rate":
        assert ei.value.retry_after == 42


def test_bad_json_is_data_error(monkeypatch):
    monkeypatch.setattr(core.urllib.request, "urlopen", lambda *a, **k: FakeResp(b"<html>"))
    with pytest.raises(core.ApiError) as ei:
        core.fetch_prices("t", "s", 5)
    assert ei.value.kind == "data"


def test_fetch_sends_token_and_params(monkeypatch):
    seen = {}

    def fake(req, timeout):
        seen["url"], seen["auth"] = req.full_url, req.get_header("Authorization")
        return FakeResp(b"[]")
    monkeypatch.setattr(core.urllib.request, "urlopen", fake)
    core.fetch_prices("tok", "site1", 30, next_n=24)
    assert "/sites/site1/prices/current?" in seen["url"]
    assert "next=24" in seen["url"] and "resolution=30" in seen["url"]
    assert seen["auth"] == "Bearer tok"


def test_usable_sites_prefers_active():
    sites = [{"id": "a", "status": "closed"}, {"id": "b", "status": "active"}]
    assert [s["id"] for s in core.usable_sites(sites)] == ["b"]
    assert [s["id"] for s in core.usable_sites([{"id": "p", "status": "pending"}])] == ["p"]


def _row(type_, channel, price, start, end, **kw):
    return {"type": type_, "channelType": channel, "perKwh": price,
            "startTime": start, "endTime": end, **kw}


def test_current_by_channel_and_forecast_rows():
    rows = [
        _row("CurrentInterval", "general", 20.0, "2024-01-01T00:00:00Z", "2024-01-01T00:05:00Z"),
        _row("CurrentInterval", "feedIn", -8.0, "2024-01-01T00:00:00Z", "2024-01-01T00:05:00Z"),
        _row("ForecastInterval", "general", 12.0, "2024-01-01T01:00:00Z", "2024-01-01T01:30:00Z"),
        _row("ForecastInterval", "general", 11.0, "2024-01-01T00:30:00Z", "2024-01-01T01:00:00Z"),
        _row("ForecastInterval", "feedIn", -3.0, "2024-01-01T00:30:00Z", "2024-01-01T01:00:00Z"),
    ]
    cur = core.current_by_channel(rows)
    assert set(cur) == {"general", "feedIn"}
    fc = core.forecast_rows(rows, "general")
    assert [r["perKwh"] for r in fc] == [11.0, 12.0]  # sorted by start


def test_interval_is_stale():
    row = {"endTime": "2024-01-01T00:05:00Z"}
    assert core.interval_is_stale(row, datetime(2024, 1, 1, 0, 5, 3, tzinfo=timezone.utc))
    assert not core.interval_is_stale(row, datetime(2024, 1, 1, 0, 4, 59, tzinfo=timezone.utc))


# --- pricing / alerts -------------------------------------------------------
def test_sell_earn_flips_sign():
    assert core.sell_earn({"perKwh": -7.5}) == 7.5


def _fc(prices, start_hour=0):
    rows = []
    for i, p in enumerate(prices):
        m0, m1 = i * 30, i * 30 + 30
        s = f"2024-01-01T{start_hour + m0 // 60:02d}:{m0 % 60:02d}:00Z"
        e = f"2024-01-01T{start_hour + m1 // 60:02d}:{m1 % 60:02d}:00Z"
        rows.append(_row("ForecastInterval", "general", p, s, e))
    return rows


def test_cheap_window_finds_first_run():
    start, end, cheapest = core.cheap_window(_fc([30, 18, 15, 25, 10]), 19)
    assert start.strftime("%H:%M") == "00:30" and end.strftime("%H:%M") == "01:30"
    assert cheapest == 15


def test_cheap_window_none():
    assert core.cheap_window(_fc([30, 40]), 19) is None
    assert core.cheap_window([], 19) is None


def test_zone_alert_edge_triggered_with_hysteresis():
    a = core.ZoneAlert()
    assert a.update(25, 19, 40, 1) is None      # first reading only seeds
    assert a.update(18.9, 19, 40, 1) == "low"   # crossing in alerts
    assert a.update(19.1, 19, 40, 1) is None    # wobble inside margin: still low
    assert a.update(18.8, 19, 40, 1) is None    # ...so no repeat alert
    assert a.update(20.5, 19, 40, 1) is None    # moved back past margin: re-armed
    assert a.update(18.0, 19, 40, 1) == "low"
    assert a.update(45, 19, 40, 1) == "high"    # straight from low to high


def test_zone_alert_high_only():
    a = core.ZoneAlert()
    a.update(10, None, 30, 1)
    assert a.update(31, None, 30, 1) == "high"
    assert a.update(29.5, None, 30, 1) is None
    assert a.update(-50, None, 30, 1) is None   # no low threshold: never "low"


def test_zone_alert_reset_reseeds():
    a = core.ZoneAlert()
    a.update(25, 19, 40)
    a.reset()
    assert a.update(10, 19, 40) is None


# --- scheduling --------------------------------------------------------------
def test_poll_aligns_to_boundary_plus_offset():
    base = 1_700_000_100  # an exact 5-minute boundary (divisible by 300)
    assert base % 300 == 0
    # 100 s after the boundary: next poll is at boundary + 300 + offset.
    assert core.seconds_to_next_poll(base + 100, 1000) == 300 + core.POLL_OFFSET_SEC - 100
    # just before the post-boundary poll moment
    assert core.seconds_to_next_poll(base + 5, 1000) == core.POLL_OFFSET_SEC - 5
    # refresh_sec caps the wait
    assert core.seconds_to_next_poll(base + 100, 60) == 60


def test_error_delay():
    net = core.ApiError("network", "x")
    assert [core.error_delay(n, net, 120) for n in (1, 2, 3, 4, 9)] == [10, 30, 60, 120, 120]
    assert core.error_delay(1, core.ApiError("rate", "x", 42), 120) == 42
    assert core.error_delay(1, core.ApiError("rate", "x"), 120) == 300
    assert core.error_delay(1, core.ApiError("auth", "x"), 120) == 300


# --- icons -------------------------------------------------------------------
@pytest.mark.parametrize("mode", core.MODES)
def test_render_icon_modes(mode):
    state = {"general": {"perKwh": 23.4, "descriptor": "high"}, "feedIn": {"perKwh": -5.0}}
    img = core.render_icon(state, mode)
    assert img.size == (core.ICON_SIZE, core.ICON_SIZE)
    assert img.getbbox() is not None  # something was drawn


def test_render_icon_stale_is_grey():
    state = {"general": {"perKwh": 23.4, "descriptor": "spike"}}
    img = core.render_icon(state, "import", stale=True)
    colours = {c for _, c in img.getcolors(maxcolors=100_000) if c[3] == 255}
    assert core.STALE_TEXT + (255,) in colours
    assert core.DESCRIPTOR_TEXT["spike"] + (255,) not in colours


def test_render_icon_no_data_is_error_icon():
    assert core.render_icon({}, "import").tobytes() == core.make_icon_error().tobytes()


def test_render_icon_buy_mode_falls_back_to_sell():
    img = core.render_icon({"feedIn": {"perKwh": -5.0}}, "import")
    assert img.getbbox() is not None

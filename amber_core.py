"""Core logic for Amber Price Tray: config, Amber API, pricing, alerts,
scheduling and icon rendering.

Nothing here touches tkinter or pystray, so it can be unit-tested on any OS.
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

APP_NAME = "Amber Price Tray"
APP_VERSION = "1.2.0"
AMBER_BASE = "https://api.amber.com.au/v1"
AMBER_KEYS_URL = "https://app.amber.com.au/developers/"

CONFIG_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / "AmberPriceTray"
CONFIG_PATH = CONFIG_DIR / "config.json"
LOG_PATH = CONFIG_DIR / "amber_tray.log"

log = logging.getLogger("amber_tray")


def setup_logging() -> None:
    """Log to a small rotating file in the config dir (best effort)."""
    logging.raiseExceptions = False  # windowed exe has no stderr to complain to
    if log.handlers:
        return
    log.setLevel(logging.INFO)
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            LOG_PATH, maxBytes=200_000, backupCount=1, encoding="utf-8")
    except OSError:
        log.addHandler(logging.NullHandler())
        return
    handler.setFormatter(logging.Formatter("%(asctime)s  [%(process)d] %(message)s",
                                           "%Y-%m-%d %H:%M:%S"))
    log.addHandler(handler)


def resource_path(rel: str) -> Path:
    """Path to a bundled resource (works under PyInstaller and from source)."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / rel


# --- config ----------------------------------------------------------------
DEFAULT_CONFIG = {
    "api_token": "",
    "site_id": "",
    "mode": "import",           # import | feedin | both
    "resolution": 5,            # 5 = live spot, 30 = billing interval
    "refresh_sec": 120,         # max gap between polls (polls also align to 5-min boundaries)
    "notify_enabled": True,     # toast when the buy price crosses a threshold
    "notify_low": 19.0,         # buy price <= this c/kWh -> "good time to charge"
    "notify_high": 40.0,        # buy price >= this c/kWh -> "high prices ahead"
    "notify_sell_enabled": False,  # toast when the feed-in (sell) price is high
    "notify_sell_high": 30.0,   # sell earnings >= this c/kWh -> "export now"
    "notify_hysteresis": 1.0,   # c/kWh a price must move back before re-alerting
}

MODES = ("import", "feedin", "both")
RESOLUTIONS = (5, 30)


def _num(value, default: float, lo: float | None = None) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if v != v:  # NaN
        return default
    return max(lo, v) if lo is not None else v


def normalise_config(raw) -> dict:
    """Merge raw (possibly hand-edited or corrupt) values over the defaults,
    coercing anything invalid back to its default."""
    cfg = dict(DEFAULT_CONFIG)
    if isinstance(raw, dict):
        cfg.update(raw)
    cfg["api_token"] = str(cfg.get("api_token") or "").strip()
    cfg["site_id"] = str(cfg.get("site_id") or "").strip()
    if cfg["mode"] not in MODES:
        cfg["mode"] = DEFAULT_CONFIG["mode"]
    try:
        cfg["resolution"] = int(cfg["resolution"])
    except (TypeError, ValueError):
        cfg["resolution"] = DEFAULT_CONFIG["resolution"]
    if cfg["resolution"] not in RESOLUTIONS:
        cfg["resolution"] = DEFAULT_CONFIG["resolution"]
    cfg["refresh_sec"] = int(_num(cfg["refresh_sec"], DEFAULT_CONFIG["refresh_sec"], lo=30))
    for key in ("notify_low", "notify_high", "notify_sell_high"):
        cfg[key] = _num(cfg[key], DEFAULT_CONFIG[key])
    cfg["notify_hysteresis"] = _num(cfg["notify_hysteresis"], DEFAULT_CONFIG["notify_hysteresis"], lo=0)
    if cfg["notify_low"] >= cfg["notify_high"]:
        cfg["notify_low"], cfg["notify_high"] = DEFAULT_CONFIG["notify_low"], DEFAULT_CONFIG["notify_high"]
    cfg["notify_enabled"] = bool(cfg["notify_enabled"])
    cfg["notify_sell_enabled"] = bool(cfg["notify_sell_enabled"])
    return cfg


def load_config(path: Path = CONFIG_PATH) -> dict:
    raw = None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        pass
    except (ValueError, OSError) as e:
        log.warning("config unreadable, using defaults: %r", e)
    return normalise_config(raw)


def save_config(cfg: dict, path: Path = CONFIG_PATH) -> None:
    """Write atomically (temp file + rename) so a crash never leaves a
    half-written config that would lose the API key."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".config-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def update_config(changes: dict, path: Path = CONFIG_PATH) -> dict:
    """Re-read the file, apply only `changes`, save, and return the result.
    Keeps separate processes (tray + settings dialogs) from clobbering each
    other's keys."""
    cfg = load_config(path)
    cfg.update(changes)
    cfg = normalise_config(cfg)
    save_config(cfg, path)
    return cfg


# --- Amber API -------------------------------------------------------------
class ApiError(Exception):
    """A failed Amber API call, classified so the UI can say something useful.

    kind: "auth" (key rejected), "rate" (429), "http" (other status),
          "network" (couldn't reach Amber), "data" (unexpected response).
    """

    def __init__(self, kind: str, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.retry_after = retry_after


def _retry_after(err: urllib.error.HTTPError) -> float | None:
    try:
        return max(1.0, float(err.headers.get("Retry-After")))
    except (TypeError, ValueError, AttributeError):
        return None


def _api_get(path: str, token: str, params: dict | None = None, timeout: float = 20):
    url = f"{AMBER_BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": f"AmberPriceTray/{APP_VERSION}",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise ApiError("auth", "API key rejected") from e
        if e.code == 429:
            raise ApiError("rate", "rate limited by Amber", _retry_after(e)) from e
        raise ApiError("http", f"Amber error {e.code}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ApiError("network", f"offline ({e.__class__.__name__})") from e
    except ValueError as e:
        raise ApiError("data", "bad response from Amber") from e


def list_sites(token: str) -> list[dict]:
    data = _api_get("/sites", token)
    if not isinstance(data, list):
        raise ApiError("data", "bad response from Amber")
    return data


def usable_sites(sites: list[dict]) -> list[dict]:
    """Active sites, or every site if none are active (e.g. still pending)."""
    return [s for s in sites if s.get("status") == "active"] or list(sites)


def site_label(site: dict) -> str:
    nmi = site.get("nmi") or site.get("id", "?")
    status = site.get("status")
    return f"{nmi} ({status})" if status else str(nmi)


def fetch_prices(token: str, site_id: str, resolution: int, next_n: int = 0) -> list[dict]:
    data = _api_get(f"/sites/{site_id}/prices/current", token,
                    {"next": next_n, "previous": 0, "resolution": resolution})
    if not isinstance(data, list):
        raise ApiError("data", "bad response from Amber")
    return data


def current_by_channel(rows: list[dict]) -> dict[str, dict]:
    """{channelType: row} for the CurrentInterval rows."""
    out = {}
    for row in rows:
        if row.get("type") == "CurrentInterval" and isinstance(row.get("perKwh"), (int, float)):
            out[row.get("channelType")] = row
    return out


def forecast_rows(rows: list[dict], channel: str = "general") -> list[dict]:
    """ForecastInterval rows for one channel, sorted by start time."""
    out = [r for r in rows if r.get("type") == "ForecastInterval"
           and r.get("channelType") == channel
           and isinstance(r.get("perKwh"), (int, float))
           and parse_time(r.get("startTime")) and parse_time(r.get("endTime"))]
    return sorted(out, key=lambda r: parse_time(r["startTime"]))


def parse_time(value) -> datetime | None:
    """Parse Amber's ISO-8601 timestamps (e.g. 2024-01-01T00:30:00Z) to aware UTC."""
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def interval_is_stale(row: dict, now: datetime) -> bool:
    """True if Amber is still serving an interval that has already ended
    (it publishes the next one a little after the boundary)."""
    end = parse_time(row.get("endTime"))
    return end is not None and end <= now


# --- pricing ---------------------------------------------------------------
def sell_earn(row: dict) -> float:
    """Export earnings c/kWh. Amber's feedIn perKwh is negative when you're
    paid, so earnings = -perKwh (positive = paid, negative = you pay)."""
    return -row["perKwh"]


def cheap_window(forecast: list[dict], threshold: float) -> tuple[datetime, datetime, float] | None:
    """First contiguous run of forecast intervals at or below `threshold`.
    Returns (start, end, lowest price) or None."""
    run: list[dict] = []
    for row in forecast:
        if row["perKwh"] <= threshold:
            if run and parse_time(row["startTime"]) != parse_time(run[-1]["endTime"]):
                break  # gap in the data ends the run
            run.append(row)
        elif run:
            break
    if not run:
        return None
    return (parse_time(run[0]["startTime"]), parse_time(run[-1]["endTime"]),
            min(r["perKwh"] for r in run))


class ZoneAlert:
    """Edge-triggered threshold alert with hysteresis.

    zone() maps a price to "low" / "high" / "normal". An alert fires only on
    the transition into low or high; to leave a zone (and so re-arm) the price
    must move back past the threshold by `hysteresis`, so a price wobbling
    around the line doesn't toast every interval. The first reading only
    seeds the baseline."""

    def __init__(self):
        self.zone: str | None = None

    def reset(self) -> None:
        self.zone = None

    def update(self, price: float, low: float | None, high: float | None,
               hysteresis: float = 0.0) -> str | None:
        prev = self.zone
        if prev == "low" and low is not None and price <= low + hysteresis:
            zone = "low"
        elif prev == "high" and high is not None and price >= high - hysteresis:
            zone = "high"
        elif low is not None and price <= low:
            zone = "low"
        elif high is not None and price >= high:
            zone = "high"
        else:
            zone = "normal"
        self.zone = zone
        if prev is None or zone == prev or zone == "normal":
            return None
        return zone


# --- scheduling ------------------------------------------------------------
POLL_OFFSET_SEC = 12        # Amber publishes a new interval shortly after the boundary
STALE_RETRY_SEC = 15        # retry soon if the "current" interval has already ended
ERROR_BACKOFF_SEC = (10, 30, 60)


def seconds_to_next_poll(now_ts: float, refresh_sec: float, interval_sec: int = 300,
                         offset: float = POLL_OFFSET_SEC) -> float:
    """Seconds until the next poll: just after the next interval boundary, or
    after `refresh_sec`, whichever is sooner. Boundaries are epoch-aligned,
    which matches NEM intervals in every Australian time zone."""
    since = (now_ts - offset) % interval_sec
    to_boundary = interval_sec - since
    return max(1.0, min(to_boundary, refresh_sec))


def error_delay(failures: int, err: ApiError | None, refresh_sec: float) -> float:
    """Delay before retrying after `failures` consecutive failures."""
    if err is not None and err.kind == "rate":
        return err.retry_after or 300
    if err is not None and err.kind == "auth":
        return max(refresh_sec, 300)  # key won't fix itself; the dialog wakes us
    if 1 <= failures <= len(ERROR_BACKOFF_SEC):
        return ERROR_BACKOFF_SEC[failures - 1]
    return refresh_sec


# --- colours ---------------------------------------------------------------
# Buy/import colour follows Amber's descriptor, matching the Amber app palette.
DESCRIPTOR_TEXT = {
    "extremelyLow": (38, 166, 91),
    "veryLow": (76, 187, 95),
    "low": (150, 205, 60),
    "neutral": (245, 200, 40),
    "high": (240, 140, 40),
    "spike": (229, 57, 53),
}
ERROR_TEXT = (180, 180, 180)
STALE_TEXT = (150, 150, 150)


def buy_colour(row: dict | None) -> tuple[int, int, int]:
    return DESCRIPTOR_TEXT.get((row or {}).get("descriptor"), DESCRIPTOR_TEXT["neutral"])


def sell_colour(earn: float) -> tuple[int, int, int]:
    if earn >= 20:
        return (0, 220, 90)
    if earn >= 0:
        return (60, 210, 90)
    return (255, 90, 90)


def for_light_taskbar(colour: tuple[int, int, int]) -> tuple[int, int, int]:
    """Darken a colour so it keeps contrast on a light (white) taskbar."""
    return tuple(int(c * 0.72) for c in colour)


# --- icon rendering --------------------------------------------------------
ICON_SIZE = 128
_font_cache: dict[int, ImageFont.ImageFont] = {}


def _font(size: int) -> ImageFont.ImageFont:
    if size not in _font_cache:
        for name in ("arialbd.ttf", "segoeuib.ttf", "arial.ttf", "DejaVuSans-Bold.ttf"):
            try:
                _font_cache[size] = ImageFont.truetype(name, size)
                break
            except OSError:
                continue
        else:
            _font_cache[size] = ImageFont.load_default()
    return _font_cache[size]


def _fit_font(draw, text, max_w, max_h, stroke=2):
    fs = max_h
    while fs > 9:
        font = _font(fs)
        box = draw.textbbox((0, 0), text, font=font, stroke_width=stroke)
        if box[2] - box[0] <= max_w and box[3] - box[1] <= max_h:
            return font
        fs -= 2
    return _font(9)


def _draw_centred(draw, cx, cy, text, font, fill, stroke=2):
    box = draw.textbbox((0, 0), text, font=font, stroke_width=stroke)
    w, h = box[2] - box[0], box[3] - box[1]
    draw.text((cx - w / 2 - box[0], cy - h / 2 - box[1]), text, font=font,
              fill=tuple(fill) + (255,), stroke_width=stroke, stroke_fill=(0, 0, 0, 200))


def glyph(c: float) -> str:
    return f"{round(c)}"


def make_icon_single(text: str, colour: tuple) -> Image.Image:
    img = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    font = _fit_font(draw, text, ICON_SIZE - 6, ICON_SIZE - 8)
    _draw_centred(draw, ICON_SIZE / 2, ICON_SIZE / 2, text, font, colour)
    return img


def make_icon_stacked(top_text, top_col, bottom_text, bottom_col) -> Image.Image:
    img = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    row_h = ICON_SIZE / 2
    tf = _fit_font(draw, top_text, ICON_SIZE, int(row_h))
    bf = _fit_font(draw, bottom_text, ICON_SIZE, int(row_h))
    _draw_centred(draw, ICON_SIZE / 2, row_h / 2, top_text, tf, top_col)
    _draw_centred(draw, ICON_SIZE / 2, ICON_SIZE - row_h / 2, bottom_text, bf, bottom_col)
    return img


def make_icon_error() -> Image.Image:
    return make_icon_single("!", ERROR_TEXT)


def render_icon(state: dict[str, dict], mode: str, stale: bool = False,
                light: bool = False) -> Image.Image:
    """Tray icon for the given prices. Stale prices are drawn in grey."""
    g, f = state.get("general"), state.get("feedIn")
    if not g and not f:
        return make_icon_error()

    def col(c):
        if stale:
            return STALE_TEXT
        return for_light_taskbar(c) if light else c

    if mode == "feedin" and f:
        earn = sell_earn(f)
        return make_icon_single(glyph(earn), col(sell_colour(earn)))
    if mode == "both":
        return make_icon_stacked(
            glyph(g["perKwh"]) if g else "?", col(buy_colour(g)) if g else ERROR_TEXT,
            glyph(sell_earn(f)) if f else "?", col(sell_colour(sell_earn(f))) if f else ERROR_TEXT,
        )
    if g:
        return make_icon_single(glyph(g["perKwh"]), col(buy_colour(g)))
    earn = sell_earn(f)  # buy mode but only a feed-in channel came back
    return make_icon_single(glyph(earn), col(sell_colour(earn)))

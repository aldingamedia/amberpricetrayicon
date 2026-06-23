"""Amber Price Tray — live Amber Electric prices in the Windows system tray.

Standalone: talks directly to the Amber REST API with the user's own API
key (no Home Assistant required). On first run it asks for an API key and
auto-discovers the site. Config is stored per-user in %APPDATA%.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
import pystray

APP_NAME = "Amber Price Tray"
AMBER_BASE = "https://api.amber.com.au/v1"
AMBER_KEYS_URL = "https://app.amber.com.au/developers/"

CONFIG_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / "AmberPriceTray"
CONFIG_PATH = CONFIG_DIR / "config.json"

DEFAULT_CONFIG = {
    "api_token": "",
    "site_id": "",
    "mode": "import",       # import | feedin | both
    "resolution": 5,        # 5 = live spot, 30 = billing interval
    "refresh_sec": 300,
}


def resource_path(rel: str) -> Path:
    """Path to a bundled resource (works under PyInstaller and from source)."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / rel


# --- config ----------------------------------------------------------------
def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except (ValueError, OSError):
            pass
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


# --- Amber API -------------------------------------------------------------
def _api_get(path: str, token: str, params: dict | None = None):
    url = f"{AMBER_BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def list_sites(token: str) -> list[dict]:
    return _api_get("/sites", token)


def fetch_current(token: str, site_id: str, resolution: int) -> dict[str, dict]:
    data = _api_get(f"/sites/{site_id}/prices/current", token,
                    {"next": 0, "previous": 0, "resolution": resolution})
    return {row["channelType"]: row for row in data if row.get("type") == "CurrentInterval"}


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


def sell_earn(row: dict) -> float:
    """Export earnings c/kWh. Amber's feedIn perKwh is negative when you're
    paid, so earnings = -perKwh (positive = paid, negative = you pay)."""
    return -row["perKwh"]


def sell_colour(earn: float) -> tuple[int, int, int]:
    if earn >= 20:
        return (0, 220, 90)
    if earn >= 0:
        return (60, 210, 90)
    return (255, 90, 90)


# --- icon rendering --------------------------------------------------------
def _font(size: int) -> ImageFont.FreeTypeFont:
    for name in ("arialbd.ttf", "segoeuib.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


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
              fill=fill + (255,), stroke_width=stroke, stroke_fill=(0, 0, 0, 200))


def _glyph(c: float) -> str:
    return f"{round(c)}"


def make_icon_single(text: str, colour: tuple) -> Image.Image:
    size = 128
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    font = _fit_font(draw, text, size - 6, size - 8)
    _draw_centred(draw, size / 2, size / 2, text, font, colour)
    return img


def make_icon_stacked(top_text, top_col, bottom_text, bottom_col) -> Image.Image:
    size = 128
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    row_h = size / 2
    tf = _fit_font(draw, top_text, size, int(row_h), stroke=2)
    bf = _fit_font(draw, bottom_text, size, int(row_h), stroke=2)
    _draw_centred(draw, size / 2, row_h / 2, top_text, tf, top_col, stroke=2)
    _draw_centred(draw, size / 2, size - row_h / 2, bottom_text, bf, bottom_col, stroke=2)
    return img


def make_icon_error() -> Image.Image:
    return make_icon_single("!", ERROR_TEXT)


# --- first-run / settings dialog ------------------------------------------
def prompt_for_token(initial: str = "") -> dict | None:
    """Modal Tk dialog. Returns {'token', 'site_id'} or None if cancelled."""
    import tkinter as tk
    from tkinter import messagebox, ttk

    result: dict | None = None
    root = tk.Tk()
    root.title(f"{APP_NAME} — Setup")
    root.resizable(False, False)
    try:
        root.iconbitmap(str(resource_path("amber.ico")))
    except Exception:
        pass

    frm = ttk.Frame(root, padding=16)
    frm.grid()
    ttk.Label(frm, text="Enter your Amber Electric API key:").grid(column=0, row=0, sticky="w")
    entry = ttk.Entry(frm, width=52)
    entry.grid(column=0, row=1, pady=(4, 2), sticky="we")
    entry.insert(0, initial)
    entry.focus()
    link = ttk.Label(frm, text=f"Get a key: {AMBER_KEYS_URL}", foreground="#1a73e8", cursor="hand2")
    link.grid(column=0, row=2, sticky="w")
    link.bind("<Button-1>", lambda _e: __import__("webbrowser").open(AMBER_KEYS_URL))
    status = ttk.Label(frm, text="", foreground="#c0392b")
    status.grid(column=0, row=3, sticky="w", pady=(4, 0))

    btns = ttk.Frame(frm)
    btns.grid(column=0, row=4, pady=(12, 0), sticky="e")

    def on_ok():
        token = entry.get().strip()
        if not token:
            status.config(text="Please enter a key.")
            return
        status.config(text="Validating…", foreground="#555")
        root.update_idletasks()
        try:
            sites = list_sites(token)
        except urllib.error.HTTPError as e:
            status.config(text=f"Key rejected ({e.code}). Check it and retry.", foreground="#c0392b")
            return
        except (urllib.error.URLError, OSError) as e:
            status.config(text=f"Network error: {e.__class__.__name__}", foreground="#c0392b")
            return
        active = [s for s in sites if s.get("status") == "active"] or sites
        if not active:
            status.config(text="No sites found on this account.", foreground="#c0392b")
            return
        nonlocal result
        result = {"token": token, "site_id": active[0]["id"]}
        root.destroy()

    ttk.Button(btns, text="Cancel", command=root.destroy).grid(column=0, row=0, padx=(0, 8))
    ok = ttk.Button(btns, text="Save", command=on_ok)
    ok.grid(column=1, row=0)
    root.bind("<Return>", lambda _e: on_ok())
    root.bind("<Escape>", lambda _e: root.destroy())

    root.update_idletasks()
    root.eval("tk::PlaceWindow . center")
    root.mainloop()
    return result


# --- tray app --------------------------------------------------------------
class AmberTray:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.state: dict = {}
        self.error: str | None = None
        self._stop = threading.Event()
        self._settings_open = False
        self.icon = pystray.Icon(
            "AmberPriceTray",
            icon=make_icon_single("..", DESCRIPTOR_TEXT["neutral"]),
            title=f"{APP_NAME} — loading…",
            menu=self._menu(),
        )

    # --- menu --------------------------------------------------------------
    def _menu(self) -> pystray.Menu:
        def info(getter):
            return pystray.MenuItem(getter, None, enabled=False)

        def mode_item(label, value):
            return pystray.MenuItem(label, lambda: self._set("mode", value),
                                    checked=lambda _i: self.cfg["mode"] == value, radio=True)

        def res_item(label, value):
            return pystray.MenuItem(label, lambda: self._set("resolution", value),
                                    checked=lambda _i: self.cfg["resolution"] == value, radio=True)

        return pystray.Menu(
            info(lambda _: self._summary_line()),
            pystray.Menu.SEPARATOR,
            info(lambda _: f"Buy:   {self._price_str('general')}"),
            info(lambda _: f"Sell:  {self._price_str('feedIn')}"),
            info(lambda _: f"Renewables: {self._renewables_str()}"),
            info(lambda _: f"Updated:  {self._updated_str()}"),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Display", pystray.Menu(
                mode_item("Buy price", "import"),
                mode_item("Sell price", "feedin"),
                mode_item("Both", "both"),
            )),
            pystray.MenuItem("Price type", pystray.Menu(
                res_item("Live (5 min)", 5),
                res_item("Billing (30 min)", 30),
            )),
            pystray.MenuItem("Refresh now", lambda: self.refresh()),
            pystray.MenuItem("Change API key…", self._open_settings),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._quit),
        )

    def _set(self, key, value):
        self.cfg[key] = value
        save_config(self.cfg)
        self.refresh()

    def _open_settings(self):
        # Tkinter can't run on this (worker) thread, so launch the setup dialog
        # as a separate process, then reload the config it writes.
        if self._settings_open:
            return
        self._settings_open = True

        def run():
            try:
                if getattr(sys, "frozen", False):
                    args = [sys.executable, "--setup"]
                else:
                    args = [sys.executable, os.path.abspath(__file__), "--setup"]
                subprocess.run(args)
                self.cfg.update(load_config())
                self.refresh()
            finally:
                self._settings_open = False

        threading.Thread(target=run, daemon=True).start()

    # --- formatting --------------------------------------------------------
    def _price_str(self, channel: str) -> str:
        row = self.state.get(channel)
        if not row:
            return "—"
        if channel == "feedIn":
            earn = sell_earn(row)
            return f"{earn:.1f} c/kWh ({'paid' if earn >= 0 else 'you pay'})"
        suffix = f" ({row['descriptor']})" if "descriptor" in row else ""
        return f"{row['perKwh']:.1f} c/kWh{suffix}"

    def _summary_line(self) -> str:
        if self.error:
            return f"Amber: {self.error}"
        g, f = self.state.get("general"), self.state.get("feedIn")
        if not g and not f:
            return "Amber: no data"
        parts = []
        if g:
            parts.append(f"buy {g['perKwh']:.1f}c")
        if f:
            parts.append(f"sell {sell_earn(f):.1f}c")
        return "Amber: " + "  ".join(parts)

    def _renewables_str(self) -> str:
        row = self.state.get("general")
        return f"{row['renewables']:.0f}%" if row and "renewables" in row else "—"

    def _updated_str(self) -> str:
        row = self.state.get("general")
        if not row:
            return "—"
        try:
            end = datetime.fromisoformat(row["endTime"].replace("Z", "+00:00"))
            return end.astimezone().strftime("%H:%M")
        except (KeyError, ValueError):
            return "—"

    # --- refresh -----------------------------------------------------------
    def refresh(self):
        try:
            self.state = fetch_current(self.cfg["api_token"], self.cfg["site_id"],
                                       self.cfg["resolution"])
            self.error = None
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
            self.error = f"offline ({e.__class__.__name__})"
        except Exception as e:  # noqa: BLE001 — keep the tray alive on any parse error
            self.error = f"error: {e}"
        self._render()

    def _render(self):
        g, f = self.state.get("general"), self.state.get("feedIn")
        if self.error or (not g and not f):
            self.icon.icon = make_icon_error()
            self.icon.title = f"{APP_NAME} — {self.error or 'no data'}"
            self.icon.update_menu()
            return

        mode = self.cfg["mode"]
        if mode == "feedin" and f:
            self.icon.icon = make_icon_single(_glyph(sell_earn(f)), sell_colour(sell_earn(f)))
        elif mode == "both":
            buy_col = DESCRIPTOR_TEXT.get((g or {}).get("descriptor", "neutral"), DESCRIPTOR_TEXT["neutral"])
            self.icon.icon = make_icon_stacked(
                _glyph(g["perKwh"]) if g else "?", buy_col,
                _glyph(sell_earn(f)) if f else "?", sell_colour(sell_earn(f)) if f else ERROR_TEXT,
            )
        else:  # import
            colour = DESCRIPTOR_TEXT.get((g or {}).get("descriptor", "neutral"), DESCRIPTOR_TEXT["neutral"])
            self.icon.icon = make_icon_single(_glyph(g["perKwh"]) if g else "?", colour)

        tip = f"{APP_NAME}\n"
        tip += f"Buy: {g['perKwh']:.1f} c/kWh\n" if g else ""
        tip += f"Sell: {sell_earn(f):.1f} c/kWh\n" if f else ""
        tip += f"Updated {self._updated_str()}"
        self.icon.title = tip.strip()
        self.icon.update_menu()

    def _loop(self):
        while not self._stop.is_set():
            self.refresh()
            self._stop.wait(self.cfg["refresh_sec"])

    def _quit(self):
        self._stop.set()
        self.icon.stop()

    def run(self):
        threading.Thread(target=self._loop, daemon=True).start()
        self.icon.run()


def run_setup_dialog() -> bool:
    """Show the API-key dialog (must run on the main thread) and save. Returns
    True if the key was saved."""
    cfg = load_config()
    res = prompt_for_token(cfg.get("api_token", ""))
    if not res:
        return False
    cfg["api_token"], cfg["site_id"] = res["token"], res["site_id"]
    save_config(cfg)
    return True


def main():
    if "--setup" in sys.argv:
        run_setup_dialog()
        return
    cfg = load_config()
    if not cfg.get("api_token") or not cfg.get("site_id"):
        if not run_setup_dialog():
            return  # user cancelled setup
        cfg = load_config()
    AmberTray(cfg).run()


if __name__ == "__main__":
    main()

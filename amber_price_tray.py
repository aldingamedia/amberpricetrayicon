"""Amber Price Tray — live Amber Electric prices in the Windows system tray.

Standalone: talks directly to the Amber REST API with the user's own API
key (no Home Assistant required). On first run it asks for an API key and
auto-discovers the site. Config is stored per-user in %APPDATA%.

This module is the UI (tray icon + Tk dialogs); the logic lives in amber_core.
"""
from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone

import tkinter as tk
from tkinter import ttk

import pystray

from amber_core import (
    AMBER_KEYS_URL, APP_NAME, APP_VERSION, DEFAULT_CONFIG, DESCRIPTOR_TEXT, STALE_RETRY_SEC,
    ApiError, ZoneAlert, cheap_window, current_by_channel, error_delay, fetch_prices,
    forecast_rows, interval_is_stale, list_sites, load_config, log, make_icon_single,
    parse_time, render_icon, resource_path, seconds_to_next_poll, sell_earn, setup_logging,
    site_label, update_config, usable_sites,
)

MUTEX_NAME = "AmberPriceTrayMutex"
FORECAST_INTERVALS = 24          # 30-min intervals = next 12 hours
FORECAST_MAX_AGE_SEC = 15 * 60
FORECAST_MENU_ROWS = 12
MAX_STALE_RETRIES = 8
TOOLTIP_MAX = 127                # Windows tray tooltips are capped at 128 chars


def fix_tcl_env() -> None:
    """Drop a bogus global TCL_LIBRARY/TK_LIBRARY before tkinter starts.

    Some unrelated software (e.g. CSR BlueSuite) sets these machine-wide to its
    own Tcl, which has no usable init.tcl and breaks every tkinter app. If the
    pointed-at folder lacks the expected file, remove the var so Python's (or
    PyInstaller's bundled) Tcl is found instead."""
    from pathlib import Path
    for var, marker in (("TCL_LIBRARY", "init.tcl"), ("TK_LIBRARY", "tk.tcl")):
        val = os.environ.get(var)
        if val and not (Path(val) / marker).exists():
            log.info("dropping bad %s=%r", var, val)
            os.environ.pop(var, None)


# --- Windows helpers -------------------------------------------------------
def acquire_single_instance():
    """Hold a named mutex so only one tray icon runs per user session.
    Returns a handle to keep alive, or None if another instance owns it."""
    if sys.platform != "win32":
        return True
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
    handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        return None
    return handle or True  # if the mutex couldn't be made at all, just carry on


def message_box(text: str) -> None:
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, APP_NAME, 0x40)  # MB_ICONINFORMATION


def taskbar_is_light() -> bool:
    """True if Windows is using the light taskbar theme."""
    if sys.platform != "win32":
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            return winreg.QueryValueEx(key, "SystemUsesLightTheme")[0] == 1
    except OSError:
        return False


def self_command(*args: str) -> list[str]:
    """Command line to re-launch this app (frozen exe or script)."""
    if getattr(sys, "frozen", False):
        return [sys.executable, *args]
    return [sys.executable, os.path.abspath(__file__), *args]


def _local_hm(dt: datetime | None) -> str:
    return dt.astimezone().strftime("%H:%M") if dt else "?"


# --- dialogs ---------------------------------------------------------------
def _dialog_root(title: str) -> tk.Tk:
    root = tk.Tk()
    root.title(f"{APP_NAME} — {title}")
    root.resizable(False, False)
    try:
        root.iconbitmap(str(resource_path("amber.ico")))
    except Exception:  # noqa: BLE001 — cosmetic only
        pass
    return root


def _show_dialog(root: tk.Tk) -> None:
    root.update_idletasks()
    root.eval("tk::PlaceWindow . center")
    root.lift()
    root.attributes("-topmost", True)
    root.after(200, lambda: root.attributes("-topmost", False))
    root.mainloop()


def prompt_for_token(initial_token: str = "", initial_site: str = "") -> dict | None:
    """Modal Tk dialog. Returns {'token', 'site_id'} or None if cancelled.

    The key is validated on a background thread so the window stays
    responsive. If the account has several sites, a picker appears."""
    result: dict | None = None
    root = _dialog_root("Setup")

    frm = ttk.Frame(root, padding=16)
    frm.grid()
    ttk.Label(frm, text="Enter your Amber Electric API key:").grid(column=0, row=0, columnspan=2, sticky="w")
    entry = ttk.Entry(frm, width=52)
    entry.grid(column=0, row=1, columnspan=2, pady=(4, 2), sticky="we")
    entry.insert(0, initial_token)
    entry.focus()
    link = ttk.Label(frm, text=f"Get a key: {AMBER_KEYS_URL}", foreground="#1a73e8", cursor="hand2")
    link.grid(column=0, row=2, columnspan=2, sticky="w")
    link.bind("<Button-1>", lambda _e: webbrowser.open(AMBER_KEYS_URL))

    site_lbl = ttk.Label(frm, text="Site:")
    site_box = ttk.Combobox(frm, state="readonly", width=44)
    status = ttk.Label(frm, text="", foreground="#c0392b", wraplength=440)
    status.grid(column=0, row=4, columnspan=2, sticky="w", pady=(6, 0))

    btns = ttk.Frame(frm)
    btns.grid(column=0, row=5, columnspan=2, pady=(12, 0), sticky="e")
    ok = ttk.Button(btns, text="Save")

    sites_for: dict = {"token": None, "sites": []}
    results: queue.Queue = queue.Queue()
    busy = False

    def say(text: str, error: bool = True):
        status.config(text=text, foreground="#c0392b" if error else "#555")

    def finish(token: str, site: dict):
        nonlocal result
        result = {"token": token, "site_id": site["id"]}
        root.destroy()

    def on_sites(token: str, sites: list[dict]):
        sites = usable_sites(sites)
        if not sites:
            say("No sites found on this account.")
            return
        if len(sites) == 1:
            finish(token, sites[0])
            return
        sites_for.update(token=token, sites=sites)
        site_box["values"] = [site_label(s) for s in sites]
        ids = [s.get("id") for s in sites]
        site_box.current(ids.index(initial_site) if initial_site in ids else 0)
        site_lbl.grid(column=0, row=3, sticky="w", pady=(8, 0))
        site_box.grid(column=1, row=3, sticky="we", pady=(8, 0), padx=(8, 0))
        say(f"This account has {len(sites)} sites — choose one and press Save.", error=False)

    def poll():
        nonlocal busy
        try:
            token, outcome = results.get_nowait()
        except queue.Empty:
            root.after(100, poll)
            return
        busy = False
        ok.state(["!disabled"])
        if isinstance(outcome, ApiError):
            if outcome.kind == "auth":
                say("Key rejected. Check it and retry.")
            else:
                say(f"Couldn't check the key: {outcome.message}.")
        elif isinstance(outcome, Exception):
            say(f"Couldn't check the key: {outcome!r}")
        else:
            on_sites(token, outcome)

    def validate(token: str):
        try:
            results.put((token, list_sites(token)))
        except Exception as e:  # noqa: BLE001 — reported in the dialog
            results.put((token, e))

    def on_ok():
        nonlocal busy
        if busy:
            return
        token = entry.get().strip()
        if not token:
            say("Please enter a key.")
            return
        if sites_for["token"] == token and site_box.current() >= 0:
            finish(token, sites_for["sites"][site_box.current()])
            return
        busy = True
        ok.state(["disabled"])
        say("Checking key…", error=False)
        threading.Thread(target=validate, args=(token,), daemon=True).start()
        root.after(100, poll)

    ok.config(command=on_ok)
    ttk.Button(btns, text="Cancel", command=root.destroy).grid(column=0, row=0, padx=(0, 8))
    ok.grid(column=1, row=0)
    root.bind("<Return>", lambda _e: on_ok())
    root.bind("<Escape>", lambda _e: root.destroy())
    _show_dialog(root)
    return result


def prompt_for_notifications(cfg: dict) -> dict | None:
    """Modal Tk dialog to set price-alert thresholds. Returns the changed
    notify_* settings, or None if cancelled."""
    result: dict | None = None
    root = _dialog_root("Notifications")

    frm = ttk.Frame(root, padding=16)
    frm.grid()

    def number_row(row: int, label: str, value, pady=(4, 0)) -> ttk.Entry:
        ttk.Label(frm, text=label).grid(column=0, row=row, sticky="w", pady=pady)
        e = ttk.Entry(frm, width=8)
        e.grid(column=1, row=row, sticky="w", pady=pady, padx=(8, 0))
        e.insert(0, f"{value:g}")
        return e

    buy_on = tk.BooleanVar(value=cfg["notify_enabled"])
    ttk.Checkbutton(frm, text="Alert when the buy price crosses a threshold",
                    variable=buy_on).grid(column=0, row=0, columnspan=2, sticky="w")
    low_e = number_row(1, "Low price (good time to charge), c/kWh:", cfg["notify_low"], (8, 0))
    high_e = number_row(2, "High price (pause charging), c/kWh:", cfg["notify_high"])

    sell_on = tk.BooleanVar(value=cfg["notify_sell_enabled"])
    ttk.Checkbutton(frm, text="Alert when the sell (feed-in) price is high",
                    variable=sell_on).grid(column=0, row=3, columnspan=2, sticky="w", pady=(14, 0))
    sell_e = number_row(4, "High sell price (export now), c/kWh:", cfg["notify_sell_high"], (8, 0))

    hyst_e = number_row(5, "Re-alert only after moving back by, c/kWh:", cfg["notify_hysteresis"], (14, 0))

    status = ttk.Label(frm, text="", foreground="#c0392b")
    status.grid(column=0, row=6, columnspan=2, sticky="w", pady=(8, 0))
    btns = ttk.Frame(frm)
    btns.grid(column=0, row=7, columnspan=2, pady=(12, 0), sticky="e")

    def on_ok():
        nonlocal result
        try:
            low, high = float(low_e.get()), float(high_e.get())
            sell, hyst = float(sell_e.get()), float(hyst_e.get())
        except ValueError:
            status.config(text="Please enter numbers in every box.")
            return
        if low >= high:
            status.config(text="Low must be below high.")
            return
        if hyst < 0:
            status.config(text="The re-alert margin can't be negative.")
            return
        result = {"notify_enabled": buy_on.get(), "notify_low": low, "notify_high": high,
                  "notify_sell_enabled": sell_on.get(), "notify_sell_high": sell,
                  "notify_hysteresis": hyst}
        root.destroy()

    ttk.Button(btns, text="Cancel", command=root.destroy).grid(column=0, row=0, padx=(0, 8))
    ttk.Button(btns, text="Save", command=on_ok).grid(column=1, row=0)
    root.bind("<Return>", lambda _e: on_ok())
    root.bind("<Escape>", lambda _e: root.destroy())
    _show_dialog(root)
    return result


# --- tray app --------------------------------------------------------------
class AmberTray:
    """Tray icon. A single worker thread does all network fetching; menu
    callbacks only change settings and wake it, so the menu never blocks."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._lock = threading.RLock()
        self.state: dict[str, dict] = {}       # last good CurrentInterval rows by channel
        self.forecast: list[dict] = []         # general-channel forecast rows
        self.forecast_sell: list[dict] = []    # feedIn-channel forecast rows
        self._forecast_at = 0.0                # monotonic time of last forecast fetch
        self.error: ApiError | None = None
        self.last_update: datetime | None = None
        self._failures = 0
        self._stale_retries = 0
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._dialog_open = False
        self.buy_alert = ZoneAlert()
        self.sell_alert = ZoneAlert()
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
            return pystray.MenuItem(label, lambda: self._set_mode(value),
                                    checked=lambda _i: self.cfg["mode"] == value, radio=True)

        def res_item(label, value):
            return pystray.MenuItem(label, lambda: self._set_resolution(value),
                                    checked=lambda _i: self.cfg["resolution"] == value, radio=True)

        return pystray.Menu(
            info(lambda _: self._summary_line()),
            pystray.Menu.SEPARATOR,
            info(lambda _: f"Buy:   {self._price_str('general')}"),
            info(lambda _: f"Sell:  {self._price_str('feedIn')}"),
            info(lambda _: f"Renewables: {self._renewables_str()}"),
            info(lambda _: f"Updated:  {self._updated_str()}"),
            info(lambda _: self._cheap_str()),
            pystray.MenuItem("Forecast (next 6 h)", pystray.Menu(self._forecast_items)),
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
            pystray.MenuItem("Refresh now", self._refresh_now),
            pystray.MenuItem("Notifications…", lambda: self._open_dialog("--notify-settings")),
            pystray.MenuItem("API key / site…", lambda: self._open_dialog("--setup")),
            pystray.Menu.SEPARATOR,
            info(lambda _: f"Version {APP_VERSION}"),
            pystray.MenuItem("Quit", self._quit),
        )

    def _set_mode(self, value: str):
        # Display mode only changes how the icon is drawn: no fetch needed.
        with self._lock:
            self.cfg["mode"] = value
        update_config({"mode": value})
        self._render()

    def _set_resolution(self, value: int):
        with self._lock:
            self.cfg["resolution"] = value
        update_config({"resolution": value})
        self._wake.set()

    def _refresh_now(self):
        with self._lock:
            self._forecast_at = 0.0
        self._wake.set()

    def _open_dialog(self, flag: str):
        # Tkinter can't run on pystray's thread, so each dialog runs as a
        # separate process; afterwards reload whatever config it wrote.
        if self._dialog_open:
            return
        self._dialog_open = True

        def run():
            try:
                subprocess.run(self_command(flag))
                new = load_config()
                with self._lock:
                    creds_changed = (new["api_token"], new["site_id"]) != (
                        self.cfg["api_token"], self.cfg["site_id"])
                    self.cfg.update(new)
                    self.buy_alert.reset()
                    self.sell_alert.reset()
                    if creds_changed:
                        self.state, self.forecast, self.forecast_sell = {}, [], []
                        self.error, self.last_update = None, None
                        self._forecast_at = 0.0
                        self._failures = 0
                if creds_changed:
                    log.info("API key/site changed")
                    self._wake.set()
                else:
                    self._render()
            except OSError as e:
                log.warning("couldn't open %s: %r", flag, e)
            finally:
                self._dialog_open = False

        threading.Thread(target=run, daemon=True).start()

    # --- formatting --------------------------------------------------------
    def _price_str(self, channel: str) -> str:
        with self._lock:
            row = self.state.get(channel)
        if not row:
            return "—"
        est = ", estimate" if row.get("estimate") else ""
        if channel == "feedIn":
            earn = sell_earn(row)
            return f"{earn:.1f} c/kWh ({'paid' if earn >= 0 else 'you pay'}{est})"
        desc = row.get("descriptor")
        detail = ", ".join(x for x in (desc, est.lstrip(", ")) if x)
        return f"{row['perKwh']:.1f} c/kWh" + (f" ({detail})" if detail else "")

    def _summary_line(self) -> str:
        with self._lock:
            g, f, err = self.state.get("general"), self.state.get("feedIn"), self.error
        if err and err.kind == "auth":
            return "Amber: API key rejected — use API key / site…"
        if err:
            return f"Amber: {err.message}" + (" (showing last price)" if g or f else "")
        if not g and not f:
            return "Amber: loading…"
        parts = []
        if g:
            parts.append(f"buy {g['perKwh']:.1f}c")
        if f:
            parts.append(f"sell {sell_earn(f):.1f}c")
        return "Amber: " + "  ".join(parts)

    def _renewables_str(self) -> str:
        with self._lock:
            row = self.state.get("general")
        return f"{row['renewables']:.0f}%" if row and "renewables" in row else "—"

    def _updated_str(self) -> str:
        with self._lock:
            last, err = self.last_update, self.error
        if not last:
            return "—"
        return last.strftime("%H:%M:%S") + (" (stale)" if err else "")

    def _cheap_str(self) -> str:
        with self._lock:
            fc, low = list(self.forecast), self.cfg["notify_low"]
        if not fc:
            return "Next cheap: —"
        win = cheap_window(fc, low)
        if not win:
            return f"Next cheap: nothing ≤ {low:g}c in next 12 h"
        start, end, cheapest = win
        return f"Next cheap: {_local_hm(start)}–{_local_hm(end)} (from {cheapest:.1f}c)"

    def _forecast_items(self):
        with self._lock:
            fc, sell = list(self.forecast), list(self.forecast_sell)
        sell_at = {r["startTime"]: r for r in sell}
        if not fc:
            yield pystray.MenuItem("No forecast yet", None, enabled=False)
            return
        for row in fc[:FORECAST_MENU_ROWS]:
            text = f"{_local_hm(parse_time(row['startTime']))}   buy {row['perKwh']:.1f}c"
            s = sell_at.get(row["startTime"])
            if s:
                text += f"   sell {sell_earn(s):.1f}c"
            if row.get("descriptor"):
                text += f"   ({row['descriptor']})"
            yield pystray.MenuItem(text, None, enabled=False)

    def _tooltip(self) -> str:
        with self._lock:
            g, f, err = self.state.get("general"), self.state.get("feedIn"), self.error
        lines = [APP_NAME]
        if err:
            lines.append(err.message)
        if g:
            lines.append(f"Buy: {g['perKwh']:.1f} c/kWh" + (" (est.)" if g.get("estimate") else ""))
        if f:
            lines.append(f"Sell: {sell_earn(f):.1f} c/kWh")
        lines.append(f"Updated {self._updated_str()}")
        return "\n".join(lines)[:TOOLTIP_MAX]

    # --- rendering ---------------------------------------------------------
    def _render(self):
        try:
            with self._lock:
                state, mode, stale = dict(self.state), self.cfg["mode"], self.error is not None
            self.icon.icon = render_icon(state, mode, stale=stale, light=taskbar_is_light())
            self.icon.title = self._tooltip()
            self.icon.update_menu()
        except Exception as e:  # noqa: BLE001 — a drawing glitch must not kill the worker
            log.warning("render failed: %r", e)

    # --- fetching (worker thread only) -------------------------------------
    def _poll(self) -> float:
        """Fetch prices once, update the icon, and return seconds until the
        next poll."""
        with self._lock:
            cfg = dict(self.cfg)
        try:
            rows = fetch_prices(cfg["api_token"], cfg["site_id"], cfg["resolution"])
            current = current_by_channel(rows)
            if not current:
                raise ApiError("data", "no current price from Amber")
        except ApiError as e:
            return self._on_error(e, cfg)
        except Exception as e:  # noqa: BLE001 — keep the tray alive on anything odd
            return self._on_error(ApiError("data", f"error: {e}"), cfg)

        now = datetime.now(timezone.utc)
        stale = any(interval_is_stale(r, now) for r in current.values())
        with self._lock:
            self.state = current
            self.error = None
            self._failures = 0
            self.last_update = datetime.now()
        g, f = current.get("general"), current.get("feedIn")
        log.info("refresh ok buy=%s sell=%s%s", g["perKwh"] if g else "?",
                 sell_earn(f) if f else "?", " (previous interval)" if stale else "")

        self._maybe_fetch_forecast(cfg)
        self._maybe_notify(cfg, g, f)
        self._render()

        if stale and self._stale_retries < MAX_STALE_RETRIES:
            self._stale_retries += 1
            return STALE_RETRY_SEC
        self._stale_retries = 0
        return seconds_to_next_poll(time.time(), cfg["refresh_sec"])

    def _on_error(self, err: ApiError, cfg: dict) -> float:
        with self._lock:
            self.error = err
            self._failures += 1
            failures = self._failures
        delay = error_delay(failures, err, cfg["refresh_sec"])
        log.info("refresh failed (%s): %s; retry in %.0fs", err.kind, err.message, delay)
        self._render()
        return delay

    def _maybe_fetch_forecast(self, cfg: dict):
        if time.monotonic() - self._forecast_at < FORECAST_MAX_AGE_SEC and self._forecast_at:
            return
        try:
            rows = fetch_prices(cfg["api_token"], cfg["site_id"], 30, next_n=FORECAST_INTERVALS)
        except Exception as e:  # noqa: BLE001 — forecast is a nice-to-have
            log.info("forecast failed: %r", e)
            return
        with self._lock:
            self.forecast = forecast_rows(rows, "general")
            self.forecast_sell = forecast_rows(rows, "feedIn")
            self._forecast_at = time.monotonic()

    def _maybe_notify(self, cfg: dict, g: dict | None, f: dict | None):
        """Toast when a price moves into an alert zone (see ZoneAlert)."""
        hyst = cfg["notify_hysteresis"]
        messages = []
        if g:
            buy = g["perKwh"]
            zone = self.buy_alert.update(buy, cfg["notify_low"], cfg["notify_high"], hyst)
            if zone and cfg["notify_enabled"]:
                if zone == "low":
                    messages.append((f"Buy price {buy:.0f} c/kWh — good time to charge.",
                                     f"{APP_NAME}: low price"))
                else:
                    text = f"Buy price {buy:.0f} c/kWh — high prices, pause charging."
                    with self._lock:
                        win = cheap_window(self.forecast, cfg["notify_low"])
                    if win:
                        text += f" Cheap again from {_local_hm(win[0])}."
                    messages.append((text, f"{APP_NAME}: high price"))
        if f:
            earn = sell_earn(f)
            zone = self.sell_alert.update(earn, None, cfg["notify_sell_high"], hyst)
            if zone == "high" and cfg["notify_sell_enabled"]:
                messages.append((f"Feed-in paying {earn:.0f} c/kWh — good time to export.",
                                 f"{APP_NAME}: high sell price"))
        for text, title in messages:
            try:
                self.icon.notify(text, title)
                log.info("notify: %s", title)
            except Exception as e:  # noqa: BLE001 — a failed toast must not kill refresh
                log.warning("notify failed: %r", e)

    def _loop(self):
        log.info("worker started (v%s)", APP_VERSION)
        while not self._stop.is_set():
            self._wake.clear()
            try:
                delay = self._poll()
            except Exception as e:  # noqa: BLE001 — never let the worker die
                log.exception("poll crashed: %r", e)
                delay = DEFAULT_CONFIG["refresh_sec"]
            self._wake.wait(delay)

    def _quit(self):
        self._stop.set()
        self._wake.set()
        self.icon.stop()

    def run(self):
        threading.Thread(target=self._loop, daemon=True, name="amber-worker").start()
        self.icon.run()


def run_setup_dialog() -> bool:
    """Show the API-key dialog (must run on the main thread) and save. Returns
    True if the key was saved."""
    cfg = load_config()
    res = prompt_for_token(cfg["api_token"], cfg["site_id"])
    if not res:
        return False
    update_config({"api_token": res["token"], "site_id": res["site_id"]})
    return True


def run_notify_dialog() -> bool:
    """Show the notifications dialog (main thread) and save. Returns True if
    saved."""
    res = prompt_for_notifications(load_config())
    if not res:
        return False
    update_config(res)
    return True


def main():
    setup_logging()
    fix_tcl_env()
    if "--setup" in sys.argv:
        run_setup_dialog()
        return
    if "--notify-settings" in sys.argv:
        run_notify_dialog()
        return

    instance = acquire_single_instance()
    if instance is None:
        message_box(f"{APP_NAME} is already running.\n\nLook for it in the system tray "
                    "(you may need to click the ^ arrow to see hidden icons).")
        return

    cfg = load_config()
    if not cfg["api_token"] or not cfg["site_id"]:
        if not run_setup_dialog():
            return  # user cancelled setup
        cfg = load_config()
    log.info("starting %s %s", APP_NAME, APP_VERSION)
    AmberTray(cfg).run()


if __name__ == "__main__":
    main()

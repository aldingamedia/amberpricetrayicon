# Amber Price Tray

Show your live [Amber Electric](https://www.amber.com.au/) price in the Windows 11
system tray. The current price is drawn as a colour-coded number that follows
Amber's price descriptor (green = cheap → yellow → orange → red = spike).

It talks **directly to the Amber API** with your own API key — no Home Assistant
or any other service required.

![icon](assets/amber.png)

## Install

1. Download `AmberPriceTray-Setup-x.y.z.exe` from the
   [Releases](https://github.com/aldingamedia/amberpricetrayicon/releases) page.
2. Run it. It installs per-user (no admin needed) and lets you choose the install
   folder. Tick **"Start automatically when Windows starts"** if you want it
   always on. Installing a newer version over the top closes the running copy
   first.
3. On first launch it asks for your **Amber API key** (see below). It then finds
   your site automatically (or lets you pick one if your account has several)
   and starts showing the price.

Only one copy runs at a time; launching it again just tells you it's already in
the tray.

## Getting an Amber API key

1. Go to <https://app.amber.com.au/developers/>.
2. Generate a token and paste it into the setup window.

Your key is stored only on your machine at
`%APPDATA%\AmberPriceTray\config.json`. It is never sent anywhere except to
Amber's own API.

## Using it

Right-click the tray icon for:

- **Buy / Sell / Renewables / Updated** — current detail at a glance. Prices
  Amber hasn't confirmed yet are marked *estimate*.
- **Next cheap** — the next forecast window where the buy price is at or below
  your low-price threshold.
- **Forecast** — the next 6 hours of buy and sell prices, in 30-minute blocks.
- **Display** — show the **Buy price**, **Sell price**, or **Both** stacked.
- **Price type** — **Live (5 min)** spot price (matches the Amber app) or
  **Billing (30 min)** interval.
- **Refresh now**.
- **Notifications…** — set price alerts (see below).
- **API key / site…** — re-enter your key or switch site.
- **Quit**.

Hover the icon for a tooltip with buy, sell and last-updated.

The app checks for a new price a few seconds after each 5-minute boundary, when
Amber publishes it. If it can't reach Amber it keeps showing the last price in
**grey** and retries after 10 s, 30 s, then 60 s. A rejected API key is shown as
such in the menu.

> Sell price note: Amber returns feed-in as negative when you're *paid* to
> export, so this app shows it as a positive green number. It only turns red if
> the feed-in price flips to one where you'd have to pay to export.

### Price alerts

**Notifications…** lets you turn on Windows toasts for:

- **Low buy price** (default ≤ 19 c/kWh) — good time to charge.
- **High buy price** (default ≥ 40 c/kWh) — pause charging. The toast also says
  when prices are forecast to be cheap again.
- **High sell price** (off by default, ≥ 30 c/kWh) — good time to export.

Each alert fires once when the price moves into the zone. To stop a price that
hovers around a threshold from alerting over and over, the price has to move
back past the threshold by the **re-alert margin** (default 1 c/kWh) before
the alert can fire again.

## Build from source

Requires Python 3.11 and [Inno Setup 6](https://jrsoftware.org/isinfo.php).

```powershell
.\build.ps1
```

This creates a build virtualenv with the pinned dependencies, runs the tests,
builds `dist\AmberPriceTray.exe` with PyInstaller, and compiles the installer
into `installer\Output\`.

To just run it from source:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
python amber_price_tray.py
```

Run the tests with `python -m pytest`. They cover the logic in `amber_core.py`
(which has no tkinter/pystray dependency), so they also run on Linux and macOS.

### Releasing

The version lives in one place: `APP_VERSION` in `amber_core.py`. The build
stamps it into the exe and the installer. To release, bump it, commit, and push
a matching tag (e.g. `v1.2.0`). GitHub Actions builds the installer and attaches
it to a new GitHub Release.

## License

MIT — see [LICENSE](LICENSE).

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
   always on.
3. On first launch it asks for your **Amber API key** (see below). It then finds
   your site automatically and starts showing the price.

## Getting an Amber API key

1. Go to <https://app.amber.com.au/developers/>.
2. Generate a token and paste it into the setup window.

Your key is stored only on your machine at
`%APPDATA%\AmberPriceTray\config.json`. It is never sent anywhere except to
Amber's own API.

## Using it

Right-click the tray icon for:

- **Buy / Sell / Renewables / Updated** — current detail at a glance.
- **Display** — show the **Buy price**, **Sell price**, or **Both** stacked.
- **Price type** — **Live (5 min)** spot price (matches the Amber app) or
  **Billing (30 min)** interval.
- **Change API key…** — re-enter your key.
- **Quit**.

Hover the icon for a tooltip with buy, sell and last-updated.

> Sell price note: Amber returns feed-in as negative when you're *paid* to
> export, so this app shows it as a positive green number. It only turns red if
> the feed-in price flips to one where you'd have to pay to export.

## Build from source

Requires Python 3.11+ and [Inno Setup 6](https://jrsoftware.org/isinfo.php).

```powershell
.\build.ps1
```

This creates a build virtualenv, builds `dist\AmberPriceTray.exe` with
PyInstaller, and compiles the installer into `installer\Output\`.

To just run it from source:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python amber_price_tray.py
```

## License

MIT — see [LICENSE](LICENSE).

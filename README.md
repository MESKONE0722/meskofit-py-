# MeskoFit (Python)

Your workout regimen with exercise diagrams, weight/BMI tracking, a calorie and macro tracker with
barcode scanning, and detailed progress graphs. One Python program runs on your PC and opens on
your iPhone as a web app. Nothing goes to any cloud; your data stays in one folder.

This is the Python port of the Go app (`MESKONE0722/meskofit`). It serves the same web app and the
same API, and uses the same database format, so a `meskofit-data` folder works with either version.

## Quick start

Needs Python 3.11 or newer.

```
pip install .
meskofit
```

Or without installing: `pip install starlette uvicorn httpx cryptography segno`, then `python -m meskofit`.

Your browser opens a setup page with a QR code. Scan it with the iPhone camera, open the link, then
**Share → Add to Home Screen**. Keep the program running while you use the app; `Ctrl+C` stops it.

## Streamlit version

`streamlit_app.py` is the same app with a Streamlit interface, using the same database and logic.
Every exercise shows a front/back muscle map (main and helper muscles), start/finish photos and step-by-step instructions, plus an Exercise guide to look up any of 870+ exercises.
Tabs: Train, Food, Body & BMI (automatic BMI with a body figure), Goal (weight projection graph),
Shots (weekly injection log with weight graphs, for example Mounjaro), Progress and More.

```
pip install -r requirements.txt
streamlit run streamlit_app.py
```

Data is kept in `meskofit-data` (or the folder in `$MESKOFIT_DATA`). Set a password with
`$MESKOFIT_PASSWORD` or the Streamlit secret `password`. On Streamlit Community Cloud the disk is wiped
whenever the app restarts, so download a backup from the Settings tab; that is fine for testing, not for
your real logs. The Streamlit version has no live camera barcode scanner (type the barcode instead);
use the full web app for that.

## Away from home

- **Tailscale (recommended):** install it on the PC and iPhone, run
  `tailscale serve --bg --https=443 localhost:8080`, and open the `https://<pc>.<tailnet>.ts.net`
  address. Real HTTPS makes live camera barcode scanning work anywhere.
- **Home Wi-Fi only:** works with no setup; the scanner falls back to photographing the barcode.
  For live scanning at home, install the certificate profile from the in-app **Connect** page.

## Options

```
-port 8080         HTTP port            -https-port 8443   HTTPS port (0 turns it off)
-data <folder>     where data lives     -host <name/IP>    extra name for the certificate
-no-browser        don't open a browser -no-qr             don't print the QR code
-reset-password    remove the password  -reset-ca          make a new local certificate authority
-v                 log each request     -version
```

## Data

Everything is in `meskofit-data`: `meskofit.db`, `photos/`, and `certs/` (keep `ca.key` private).
Backup and JSON export are under More → Your data. To move PCs, copy the folder.

## Development

```
pip install -e . pytest
python -m pytest
```

Food data: Open Food Facts (ODbL) and USDA FoodData Central (public domain). Exercise photos from
free-exercise-db (Unlicense). Calorie, protein and body-fat figures are estimates, not medical advice.

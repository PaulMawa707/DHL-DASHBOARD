# DHL Fleet Health Dashboard

Plotly Dash dashboard that pulls **live** data from the VSS API and shows the
real-time health and alarms for the DHL fleet.

Pages:

- **Overview** -- KPIs, online vs offline pie, status breakdown, top fleets by faults, alarm types.
- **Real-Time Status** -- per-device live state, module health, camera-channel health, voltages, signal.
- **Alarms (24h)** -- alarm-type pie, per-hour trend, top devices, fleet-by-type heatmap, locations on a map.
- **Device Drilldown** -- pick one device to see its current state and last 24h alarm history.

## Quick start

1. Create a virtualenv and install requirements:

   ```powershell
   cd C:\Users\Paul\Downloads\dhl_dashboard
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```

2. (Optional) Copy `.env.example` to `.env` and adjust credentials.
   The app loads `.env` from the project folder automatically (no extra package).
   You can also export variables in your shell (e.g. `DHL_DASH_PORT`).

3. (Recommended on first start) Drop a known-good token into `.vss_token.txt`
   to skip the cold-start login (VSS rate-limits fresh logins as `10082`):

   ```text
   <token-from-jupyter-cell-0>  <pid-optional>
   ```

   The dashboard auto-refreshes the token when it expires.

4. Run the app:

   ```powershell
   python app.py
   ```

   Open **http://127.0.0.1:8050/** (default). Set `DHL_DASH_PORT` for another port. The console prints the exact URL on startup.

### Jupyter (`positions.ipynb`)

The notebook in this folder imports `vss_client` from the same tree as the Dash app.
Start the kernel with **working directory = `dhl_dashboard`**, or set **`DHL_DASHBOARD_DIR`**
to this folder if your IDE opens the notebook from elsewhere.

## How "real-time" works

- Every loader is wrapped in a 30-minute TTL cache.
- The sidebar **Refresh data** button clears the cache and re-pulls now.
- An interval timer auto-refreshes every 5 minutes in the background.
- A single VSS token is reused across all calls until the server itself rejects
  it (status `10023`), at which point we re-login once with backoff. The token
  is loaded from `.vss_token.txt` / `VSS_TOKEN` env var if present, so the
  dashboard normally never logs in on cold start.

### Default: faster first load

**By default** the app uses a **fast path**: keyword device discovery, a **shorter
alarm window** (2h unless changed), **parallel** realtime + alarm prewarm, and
realtime batching capped at **12 workers** (tunable via env; avoid pushing VSS
into rate limits).

Set **`DHL_FAST_MODE=0`** in `.env` only when you need **full fleet enumeration +
24h alarms** (slower). Use **`.vss_token.txt`** so long runs do not fall into
**10082** login throttling.

## Data scope

- Devices: all devices in fleets whose `fleetName` contains "DHL" (~349 devices).
- Realtime status: `POST /vss/vehicle/getDeviceStatus.action` in parallel
  batches (`DHL_REALTIME_BATCH` / `DHL_REALTIME_MAX_WORKERS`; safe caps in code).
- Alarms: `POST /vss/alarm/findAllByTime.action` over the alarm window
  (`alarm_query_hours()` — 24h by default; shorter when fast mode is on).

## Project layout

```
dhl_dashboard/
  app.py                # Dash shell + sidebar nav
  vss_client.py         # VSS API client (login, retries, endpoints)
  data.py               # cached loaders returning DataFrames
  components.py         # shared Plotly figure builders
  positions.ipynb       # Jupyter: same VSS client + optional MiX/Sheets cells
  pages/
    overview.py
    realtime.py
    alarms.py
    device.py
  assets/
    style.css
  requirements.txt
  .env.example
```

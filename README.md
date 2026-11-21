# TP-Link NE200-Outdoor Live Dashboard

A terminal dashboard that polls a TP-Link NE200-Outdoor (5G/4G outdoor CPE) status
page and shows live status, cellular info, and per-band (4G LTE anchor / 5G NR)
serving cell details, refreshing automatically.

## Files

- `dashboard.py` - the live terminal dashboard (main deliverable)
- `dump_serving_cell_raw.py` - one-shot diagnostic that dumps the full,
  unfiltered `DEV2_LTE_SERVING_CELL_INFO` record; use it to discover field
  names safely. Not needed for normal use.
- `requirements.txt` - Python dependencies
- `dist/tplink-dashboard.exe` - standalone bundled build (see Deployment)

## Requirements

- Python 3.10+
- `pip install -r requirements.txt` (installs `tplinkrouterc6u`, `rich`)

## Usage

```powershell
python dashboard.py --host http://192.168.254.1 --username user [--timeout 15] [--refresh 5] [--keep-alive]
```

- Password is read from `TPLINK_PASSWORD` env var, or prompted securely.
- `--username user` - **this device's actual login username is `user`, not the
  common default `admin`.** Using `admin` doesn't fail cleanly; it produces a
  connection-level truncated response that looks like a network bug (see below).
- `--refresh` - seconds between polls, default 5. See "Refresh rate" below
  before lowering it.
- `--keep-alive` - reuse one TCP connection instead of forcing
  `Connection: close` on every request. Untested on this firmware (see
  "Keep-alive" under Gotchas); off by default.

## How it works

Auth/session handling (RSA handshake, AES-encrypted `cgi_gdpr` calls, `TokenID`)
is delegated to the open-source [`tplinkrouterc6u`](https://github.com/AlexandrErohin/TP-Link-Archer-C6U)
library, which already has a client implementation covering `NE200-Outdoor v1.0`.
We never reverse-engineered the crypto ourselves - reinventing that would have
been slow and error-prone; the library already solves it.

`TplinkRouterProvider.get_client()` auto-detects the right internal client class
by trying several candidates' `supports()` methods. For this device it resolves
to an **EX-family client** (`TPLinkEXClient` or a variant) - confirmed by:
- `get_lte_serving_cells()` working at all (the library docs say this method is
  "for EX series routers" only)
- A traceback during troubleshooting showing `tplinkrouterc6u/client/ex.py` in
  the call stack for `_req_login`

This matters because EX-family and MR-family clients use **different OID names**
for the same data (e.g. device info is `DEV2_DEV_INFO` on EX vs `IGD_DEV_INFO` on
MR) and different wire formats (EX uses one JSON-per-act request via
`cgi_gdpr?9`; MR batches multiple acts into one request with a different
key=value response format). Don't assume MR-style OID names apply here.
The dashboard checks for this at startup and exits with a clear message if the
provider picks a non-EX client (e.g. during a login lockout).

## Request budget

On EX clients, `req_act()` sends **one encrypted HTTP request per act**. So a
library method that builds 6 acts costs 6 round-trips, each with an RSA
signature the router has to check. The high-level library methods were
costing about **17 requests per tick**:

| Library call | Requests | Actually used |
|---|---|---|
| `get_firmware()` | 1 | static, never changes |
| `get_status()` | 6 | only CPU + memory (also pulled LAN, WAN, Wi-Fi config, client list) |
| `get_ipv4_status()` | 2 | only WAN IP + gateway |
| `get_lte_status()` | 5 | 4 LTE acts + an internal `DEV2_LTE_SERVING_CELL_INFO` fetch |
| `get_lte_serving_cells()` | 1 | same OID again |
| extra cell info | 1 | same OID a 3rd time (unfiltered = superset of the other two) |
| uptime | 1 | `DEV2_DEV_INFO` |

`dashboard.py` now uses the library only for login/session handling and
`req_act()`, and sends its own acts (same OIDs/attrs/stacks the library uses,
so nothing new is guessed):

- **Every tick (6):** `DEV2_PROC_STATUS`, `DEV2_MEM_STATUS`,
  `DEV2_LTE_LINK_CFG`, `DEV2_XTP_LTE_INTF_CFG`, `DEV2_LTE_NET_STATUS`, and one
  `DEV2_LTE_SERVING_CELL_INFO` (explicit field list, see below) that the
  `ServingCell` rows are built from directly.
- **Every 60 s:** `DEV2_ADT_WAN` (IP/gateway), `DEV2_LTE_PROF_STAT` (ISP name).
- **Every 5 min:** `DEV2_DEV_INFO.upTime`; between resyncs uptime is counted
  locally.
- **Once:** firmware/model.

The header shows how many requests the last tick sent.

Error handling:

- Each act is its own section. If the router rejects one (`errorcode`, no data)
  or returns something unparseable, only that panel is marked `(stale)` and
  keeps its last value. The rest still update.
- A transport/HTTP error aborts the tick immediately, so you don't wait out the
  timeout on every remaining request. All panels keep their last data, marked
  stale, and the header shows the error.
- Re-login happens only when a tick fails completely (transport error, or every
  act came back empty). It runs straight away on the first failure, then at
  most every 30 s, to stay clear of the router's login lockout counter.
- Polls run on a fixed cadence: `--refresh` is measured from the start of one
  poll to the next, not added after the poll finishes.

## What's exposed by the library directly

- `get_firmware()`, `get_status()`, `get_ipv4_status()` - standard fields
- `get_lte_status()` - enable, connect_status, network_type (mapped via
  `LTEStatus.network_types`), sim_status, ISP name, signal level, RSRP/RSRQ/SNR
  **for whichever cell is currently primary** (single aggregate reading, not
  per-band), RX/TX speed, total data used, unread SMS
- `get_lte_serving_cells()` - **the key one for dual-connectivity (5G NSA) info**:
  returns a list of `ServingCell` objects, one per active band (4G LTE anchor +
  5G NR simultaneously), with band, ARFCN, DL bandwidth/frequency, DL/UL
  modulation, CQI, RI, resource blocks, RSRP, RSRQ.

### Known library gaps/quirks we worked around

(Development history. The current approach is described in "Request budget"
above and "Serving cell query now uses no attribute filter at all" below.)

- `ServingCell` has **no PCI and no SNR field at all**, even though the router
  itself queries and displays both. We added custom `req_act()` calls that
  extend the *exact same* OID/attrs the library already uses internally
  (`DEV2_LTE_SERVING_CELL_INFO`), just requesting one or two extra attribute
  names (`PCI`, `SSSINR`) that the library doesn't ask for. This worked.
- `ServingCell`'s RSRP/RSRQ come back as `0` (not populated) for whichever cell
  is *not* currently primary (in our case, always the 5G NR row). We fill that
  gap using `get_lte_status()`'s single aggregate reading, which corresponds to
  the currently-primary cell - confirmed by comparing values against the
  router's own web UI.
- `SSSINR` ("SS-SINR") is a **5G-NR-only** 3GPP measurement (Synchronization
  Signal based). It does **not** apply to the 4G LTE anchor row, which needs a
  different field name we never found (see "What we couldn't get" below).
- `wan_ipv4_uptime` in `Status` is **never populated for this client class** -
  it's simply not implemented in the library for MR/EX clients on this
  firmware. Device uptime (as shown in the router's own "Performance" panel)
  isn't exposed by any high-level library method at all. We fetch it via a
  direct `req_act()` call to `DEV2_DEV_INFO` with attribute `upTime` (same OID
  `get_firmware()` already queries successfully, just with one extra field).

## Per-cell SNR for both bands (resolved)

- `ServingCell` exposes no SNR field, and `SSSINR` (added first) turned out to
  be **5G-NR-only** (a 3GPP synchronization-signal measurement; it's `0` for
  LTE cells). Guessing a second attribute name (`SINR`) to cover the 4G LTE
  anchor **broke the entire request** - PCI, which had been working, started
  coming back empty too, because `SINR` isn't a real field on this OID at all
  and the router rejects the whole batch if any requested name doesn't exist.
- Rather than guess a third name, we ran `dump_serving_cell_raw.py` - a single
  isolated request to the same OID with **no attribute filter at all**. Some
  of these embedded APIs return every available field when you don't restrict
  the list, and that's exactly what happened here: the full record for every
  cell came back, revealing the real field is simply **`SNR`** (plain, no
  prefix) for the 4G LTE anchor, alongside `SSSINR` for the 5G NR cell - both
  now requested together, picking whichever one is non-zero per cell.
- **Lesson generalized:** when you need to discover a field name safely,
  prefer a single unfiltered/no-attrs request over accumulating guessed
  attribute names - this OID rejects the *whole* request on any single unknown
  name, so each blind guess risks regressing fields that already work.
  `dump_serving_cell_raw.py` is kept in this repo for reuse if another field
  needs discovering later.

## Serving cell query: explicit field list from the unfiltered dump

> **Update:** the unfiltered query (`attrs=[]`) turned out to break
> intermittently. Some fields (e.g. the 5G row's `CGI`) can contain non-UTF-8
> garbage, and the library decodes every response as strict UTF-8, so one bad
> byte raised `UnicodeDecodeError` and lost the whole response. The old code
> hid this by catching the error and reusing the previous tick's data.
>
> `dashboard.py` now requests an explicit list (`CELL_ATTRS`) made only of
> names seen in the full dump, so no name is guessed and the junk fields are
> skipped. Safety nets:
> - If the router rejects that list, the dashboard falls back to unfiltered.
> - Decoding is made lenient: bad bytes are replaced and control characters
>   become spaces.
> - A section whose response still can't be decoded or parsed is just marked
>   `(stale)`.

The original reasoning: since the full unfiltered dump proved safe and complete,
`fetch_extra_cell_info()` queried `DEV2_LTE_SERVING_CELL_INFO` with `attrs=[]`
instead of a curated list, to remove the "one bad guess breaks everything" risk.
From that full record we now also show, per band:

- `cid` (Cell ID) and `TAC` - real for the 4G LTE anchor, correctly shown as
  `n/a` for the 5G NR cell where the firmware reports the sentinel value
  `268435455` (matches `tplinkrouterc6u`'s own `clean_int()` convention for
  "not applicable" on this OID)
- `downMCS`/`upMCS` (DL/UL MCS index)
- `signalStrength` (per-cell signal level, 0-4 scale - same scale as
  `get_lte_status().sig_level`, just per-band instead of aggregate-only)
- RSRP/RSRQ are now sourced per-cell directly from `SSRSRP`/`SSRSRQ` (5G) or
  plain `RSRP`/`RSRQ` (4G) in this same response, instead of falling back to
  `get_lte_status()`'s single "currently active cell" reading. The old fallback
  approach only happened to work because 5G was the active cell during
  testing; it would have shown the wrong cell's values if the device ever
  made 4G primary instead. `DEV2_LTE_NET_STATUS`'s aggregate `rfInfo*` reading
  is now only a fallback, used for the active cell when it doesn't report its own
  value.

**Available in the raw dump but intentionally not shown** (either scale is
unconfirmed, or the field looked unpopulated/placeholder-like in testing - not
worth risking for unclear benefit):
- `txPowerPUCCH` - real field, but we have no independent way to confirm
  whether it needs the same "divide by 10" treatment as SNR (that scaling was
  confirmed by cross-checking against `get_lte_status()`'s independently
  computed value; there's no equivalent cross-check for Tx power)
- `RSSI`, `nodeBId`, `CGI`, `PMI`, `tbSize`, `PSC`, `LAC` - present in the dump
  but were `0`/empty/placeholder-looking (e.g. the 5G row's `CGI` contained
  stray backtick characters) in every capture during testing

## Gotchas / things to watch out for

- **Single session limit.** The TP-Link web UI only allows one logged-in
  session at a time. Running this dashboard logs out any open browser admin
  session, and vice versa. Close browser tabs to the router before starting
  the dashboard if login hangs/fails.
- **Wrong username produces a confusing error, not a clean rejection.**
  Trying `admin` when the real username is `user` doesn't fail with an auth
  error - it produces `IncompleteRead(0 bytes read, N more expected)` or a
  `ChunkedEncodingError`/connection reset, which looks like a network bug.
  If you see that, double-check the username first before assuming networking
  is broken.
- **Keep-alive connections can hang against this firmware.** We had to force
  `router.req.headers["Connection"] = "close"` after `get_client()` - some
  embedded HTTP servers silently hang on a second request over a reused
  keep-alive TCP connection instead of responding or erroring cleanly.
  It's possible this was really the wrong-username symptom above, which also
  shows up as truncated or reset connections. `--keep-alive` lets you retest
  with the correct username. If it works, it saves a TCP handshake on every
  request.
- **Rate limiting / anti-brute-force lockout risk.** The login flow has an
  explicit "Auth times: X/5, Forbid time: Y" mechanism visible in the
  library's error handling. During development, rapid repeated
  login/logout cycles (from aggressive diagnostic scripts) caused erratic
  behavior (a different client class got selected, connection resets on
  login). **Avoid scripts that repeatedly call `authorize()` in tight loops.**
  If you need to experiment with raw queries, space them out (seconds apart,
  not milliseconds) and prefer extending an already-successful request over
  firing many new isolated ones.
- **`{"success":false,"errorcode":9804}` responses** were seen in a few
  different circumstances during development (wrong OID entirely for the
  active client class; and once from adding a third guessed attribute name to
  a working request). It generally means "this exact request was rejected outright"
  - not a network issue - but the specific *reason* varies and isn't documented
  anywhere we found. Treat it as a signal to revert your last change, not to
  retry harder.
- **Don't assume a fixed client class.** Which concrete client
  (`TPLinkMRClient` vs `TPLinkEXClient` etc.) the provider picks isn't always
  obvious from the outside, and `Live(..., screen=True)` uses an alternate
  screen buffer that hides any earlier console output (including our
  `logger.info(...)` line showing which class got picked). The dashboard now
  refuses to start (with a message) if the picked class isn't `TPLinkEXClient`
  or a subclass.

## Refresh rate

Default is 5 seconds, which has been stable in extended testing. We did not
find hard evidence for a "correct" faster value - the `--refresh` flag lets you
step it down (e.g. `--refresh 2`) and watch for `poll failed` errors in the
header, which is the only reliable way to find this specific device's real
limit. A normal tick now sends 6 requests (it used to be ~17, see "Request
budget"), so lower refresh rates are less risky than before. That's still
untested on the device, though.

## Deployment

Copying the folder as-is means copying `.venv` (thousands of files). Two
better options:

1. **Lightweight (needs Python on target):** copy just `dashboard.py` +
   `requirements.txt`, then on the target machine:
   ```powershell
   python -m venv .venv
   .venv\Scripts\pip install -r requirements.txt
   .venv\Scripts\python dashboard.py --host ... --username user
   ```
2. **Standalone bundle (no Python needed on target):** build with PyInstaller
   and copy just the resulting `.exe`:
   ```powershell
   .venv\Scripts\pip install pyinstaller
   .venv\Scripts\pyinstaller --onefile --name tplink-dashboard --console dashboard.py
   ```
   Output: `dist/tplink-dashboard.exe` (~16 MB, fully self-contained). Rebuild
   this after any code changes.

## License

GPL-3.0-or-later (see `LICENSE`). This project depends on, and the standalone
`.exe` bundles, [`tplinkrouterc6u`](https://github.com/AlexandrErohin/TP-Link-Archer-C6U),
which is GPLv3+, so the same license is used here.

Unofficial project, not affiliated with or endorsed by TP-Link.

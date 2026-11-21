"""Live terminal dashboard for a TP-Link NE200-Outdoor (and similar EX-family) router.

Polls the router's management API and renders CPU/memory, WAN, cellular status
and per-band serving cell details using `rich`. Auth/session handling (RSA
handshake, AES-encrypted cgi_gdpr calls, TokenID) is delegated to the
`tplinkrouterc6u` library, which already supports this exact model.

The data itself is fetched with our own act queries instead of the library's
get_status()/get_ipv4_status()/get_lte_status()/get_lte_serving_cells(): on EX
clients every act is a separate encrypted HTTP request, and those methods pull
a lot we never show (Wi-Fi config, client list, LAN info, the serving cell OID
three times). See README "Request budget".

Usage:
    python dashboard.py --host http://192.168.254.1 --username user
    (password is read from the TPLINK_PASSWORD env var, or prompted securely)

Note: the TP-Link web UI only allows one logged-in session at a time, so
running this will log you out of the browser admin page while it's active.
"""
from __future__ import annotations

import argparse
import dataclasses
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from getpass import getpass

from requests.exceptions import RequestException
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from tplinkrouterc6u import (
    ClientError,
    ClientException,
    Firmware,
    LTEStatus,
    ServingCell,
    TPLinkEXClient,
    TplinkRouterProvider,
)

REFRESH_SECONDS = 5
SLOW_REFRESH_SECONDS = 60      # WAN IP/gateway and ISP name rarely change
UPTIME_RESYNC_SECONDS = 300    # uptime is counted locally between resyncs
REAUTH_MIN_INTERVAL = 30       # never log in more often than this (router has a lockout counter)

# '268435455' (0x0FFFFFFF) is this firmware's "not applicable" sentinel (cid, TAC, ...),
# same convention as tplinkrouterc6u's own clean_int() for the serving cell OID
NA_SENTINEL = "268435455"

# Fields requested from DEV2_LTE_SERVING_CELL_INFO: the library's own list plus extras, all of
# them seen in dump_serving_cell_raw.py's full record (an unknown name makes the router reject
# the whole request). Asking for these instead of everything also skips junk fields such as
# the 5G row's CGI, which can contain non-UTF-8 garbage that breaks decoding.
CELL_ATTRS = [
    "networkType", "cellConnectionStatus", "band", "ARFCN", "downBandWidth", "downFreq",
    "downlinkModType", "uplinkModType", "CQI", "RI", "numRbs", "RSRP", "RSRQ",
    "SSRSRP", "SSRSRQ", "SNR", "SSSINR", "PCI", "cid", "TAC", "downMCS", "upMCS", "signalStrength",
]

STATUS_SECTIONS = {"cpu", "mem", "wan", "uptime"}
CELLULAR_SECTIONS = {"lte_link", "lte_intf", "lte_net", "isp"}
CELL_SECTIONS = {"cells"}
ALL_SECTIONS = STATUS_SECTIONS | CELLULAR_SECTIONS | CELL_SECTIONS


# --------------------------------------------------------------------------- parsing helpers

def to_int(raw) -> int | None:
    """Lenient int conversion: None for missing/empty/sentinel/garbage instead of raising."""
    if raw is None or raw == "" or raw == NA_SENTINEL:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        try:
            return int(float(raw))
        except (TypeError, ValueError):
            return None


def first_nonzero(*raws) -> int | None:
    """First value that parses to a non-zero int. The serving cell OID reports the
    field for the 'other' RAT as 0 (e.g. SSRSRP on an LTE cell), so 0 means 'not this one'."""
    for raw in raws:
        value = to_int(raw)
        if value:
            return value
    return None


def first_entry(raw) -> dict:
    """GET/GO acts return a dict; GL acts return a list of dicts (or a dict if only one)."""
    if isinstance(raw, list):
        return raw[0] if raw else {}
    return raw if isinstance(raw, dict) else {}


def as_list(raw) -> list:
    if isinstance(raw, list):
        return raw
    return [raw] if isinstance(raw, dict) else []


@dataclass
class Cell:
    """One active serving cell: the library's ServingCell fields plus the extras it doesn't model."""
    info: ServingCell
    pci: int | None = None
    cid: int | None = None
    tac: int | None = None
    dl_mcs: int | None = None
    ul_mcs: int | None = None
    signal_level: int | None = None
    snr: int | None = None  # tenths of a dB


def make_decoding_lenient(router) -> None:
    """The library decodes every decrypted response as strict UTF-8, so a single garbage byte
    in any field (seen in the serving cell record) raises UnicodeDecodeError and loses the
    whole response. Replace undecodable bytes instead, and turn raw control characters
    (invalid inside JSON strings) into spaces so the JSON still parses."""
    enc = getattr(router, "_encryption", None)
    if enc is None or not hasattr(enc, "aes_decrypt"):
        return
    strict = enc.aes_decrypt

    def lenient(data):
        try:
            text = strict(data)
        except UnicodeDecodeError as exc:
            text = exc.object.decode("utf-8", errors="replace")
        return CONTROL_CHARS.sub(" ", text)

    enc.aes_decrypt = lenient


CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")  # keeps \t \r \n, which the library strips itself


def parse_cells(raw) -> list[Cell]:
    """Parse a DEV2_LTE_SERVING_CELL_INFO response (CELL_ATTRS, or unfiltered as a fallback;
    see dump_serving_cell_raw.py). Builds the same ServingCell the library would, but with
    RSRP/RSRQ picked per RAT (plain RSRP/RSRQ on LTE, SSRSRP/SSRSRQ on NR), plus extras."""
    cells = []
    for c in as_list(raw):
        if not isinstance(c, dict) or c.get("cellConnectionStatus") != "1":
            continue
        network_type = to_int(c.get("networkType"))
        if network_type is None:
            continue
        bandwidth = to_int(c.get("downBandWidth"))
        info = ServingCell(
            network_type=network_type,
            band=to_int(c.get("band")),
            arfcn=to_int(c.get("ARFCN")),
            downlink_bandwidth=bandwidth // 1000 if bandwidth is not None else None,
            downlink_frequency=to_int(c.get("downFreq")),
            downlink_modulation=c.get("downlinkModType") or None,
            uplink_modulation=c.get("uplinkModType") or None,
            cqi=to_int(c.get("CQI")),
            ri=to_int(c.get("RI")),
            resource_blocks=to_int(c.get("numRbs")),
            rsrp=first_nonzero(c.get("RSRP"), c.get("SSRSRP")),
            rsrq=first_nonzero(c.get("RSRQ"), c.get("SSRSRQ")),
        )
        cells.append(Cell(
            info=info,
            pci=to_int(c.get("PCI")),
            cid=to_int(c.get("cid")),
            tac=to_int(c.get("TAC")),
            dl_mcs=to_int(c.get("downMCS")),
            ul_mcs=to_int(c.get("upMCS")),
            signal_level=to_int(c.get("signalStrength")),
            # SSSINR is the 5G-NR reading, SNR the 4G-LTE one; only one is ever non-zero
            snr=first_nonzero(c.get("SSSINR"), c.get("SNR")),
        ))
    return cells


# --------------------------------------------------------------------------- polling

@dataclass
class Snapshot:
    """Latest known values. Sections keep their last good value when a query fails."""
    firmware: Firmware | None = None
    cpu: float | None = None
    mem: float | None = None
    wan_ip: str | None = None
    wan_gateway: str | None = None
    lte: LTEStatus | None = None
    cells: list[Cell] | None = None
    uptime_base: int | None = None
    uptime_at: float | None = None
    stale: set[str] = field(default_factory=set)
    last_ok: datetime | None = None
    last_error: str | None = None
    requests_last_tick: int = 0

    def uptime(self) -> int | None:
        if self.uptime_base is None:
            return None
        return self.uptime_base + int(time.monotonic() - self.uptime_at)

    def active_cell(self) -> Cell | None:
        if not self.lte or not self.cells:
            return None
        return next((c for c in self.cells if c.info.network_type == self.lte.network_type), None)


class Poller:
    def __init__(self, router: TPLinkEXClient) -> None:
        self.router = router
        self.data = Snapshot()
        self._next_slow = 0.0
        self._next_uptime = 0.0
        self._last_reauth = float("-inf")
        self._requests = 0
        self._cell_attrs = CELL_ATTRS  # switched to [] (unfiltered) if the router rejects the list

    def _query(self, act):
        """One act = one encrypted HTTP request on EX clients. Returns that act's data, or None
        when the router answered without data (e.g. {"errorcode":9804}). Transport/HTTP errors raise."""
        self._requests += 1
        _, values = self.router.req_act([act])
        return values[0] if values else None

    def _section(self, name: str, act, parse) -> bool:
        """Fetch + parse one section. On missing data or a parse error the section is marked
        stale and keeps its previous value, without affecting the other sections."""
        try:
            raw = self._query(act)
        except ClientError as exc:
            # an undecodable/unparseable body is this section's problem; HTTP errors abort the tick
            if "convert response to JSON" not in str(exc):
                raise
            raw = None
        except (UnicodeDecodeError, ValueError):
            raw = None
        if raw is not None:
            try:
                parse(raw)
                self.data.stale.discard(name)
                return True
            except Exception:
                pass
        self.data.stale.add(name)
        return False

    def _lte(self) -> LTEStatus:
        if self.data.lte is None:
            self.data.lte = LTEStatus()
        return self.data.lte

    # -- parsers (each raises on unexpected data; _section() turns that into "stale")

    def _parse_firmware(self, raw) -> None:
        d = first_entry(raw)
        self.data.firmware = Firmware(d["hardwareVersion"], d["modelName"], d.get("softwareVersion", ""))

    def _parse_cpu(self, raw) -> None:
        self.data.cpu = int(first_entry(raw)["CPUUsage"]) / 100

    def _parse_mem(self, raw) -> None:
        d = first_entry(raw)
        total, free = int(d["total"]), int(d["free"])
        self.data.mem = (total - free) / total

    def _parse_wan(self, raw) -> None:
        ip = gateway = None
        for item in as_list(raw):
            # same rule as the library: in a list of WAN entries, skip disabled ones
            if isinstance(raw, list) and to_int(item.get("enable")) == 0:
                continue
            ip = item.get("connIPv4Address") or None
            gateway = item.get("connIPv4Gateway") or None
        self.data.wan_ip, self.data.wan_gateway = ip, gateway

    def _parse_uptime(self, raw) -> None:
        value = to_int(first_entry(raw).get("upTime"))
        if value is None:
            raise ValueError("no upTime")
        self.data.uptime_base, self.data.uptime_at = value, time.monotonic()

    def _parse_lte_link(self, raw) -> None:
        d, lte = first_entry(raw), self._lte()
        lte.enable = int(d["enable"])
        lte.connect_status = int(d["connectStatus"])
        lte.network_type = int(d["networkType"])
        lte.sim_status = int(d["simStatus"])

    def _parse_lte_intf(self, raw) -> None:
        d, lte = first_entry(raw), self._lte()
        lte.total_statistics = int(float(d["totalStatistics"]))
        lte.cur_rx_speed = int(d["curRxSpeed"])
        lte.cur_tx_speed = int(d["curTxSpeed"])

    def _parse_lte_net(self, raw) -> None:
        d, lte = first_entry(raw), self._lte()
        lte.sms_unread_count = int(d["smsUnreadCount"])
        lte.sig_level = int(d["sigLevel"])
        lte.rsrp = int(d["rfInfoRsrp"])
        lte.rsrq = int(d["rfInfoRsrq"])
        lte.snr = int(d["rfInfoSnr"])

    def _parse_isp(self, raw) -> None:
        self._lte().isp_name = first_entry(raw)["ispName"]

    def _parse_cells(self, raw) -> None:
        self.data.cells = parse_cells(raw)

    # -- one tick

    def poll(self) -> None:
        A = self.router.ActItem
        now = time.monotonic()
        self._requests = 0
        results = []
        try:
            if self.data.firmware is None:
                self._section("firmware", A(A.GET, "DEV2_DEV_INFO",
                                            attrs=["hardwareVersion", "modelName", "softwareVersion"]),
                              self._parse_firmware)

            # every tick: 6 requests
            results += [
                self._section("cpu", A(A.GO, "DEV2_PROC_STATUS", attrs=["CPUUsage"]), self._parse_cpu),
                self._section("mem", A(A.GO, "DEV2_MEM_STATUS", attrs=["total", "free"]), self._parse_mem),
                self._section("lte_link", A(A.GET, "DEV2_LTE_LINK_CFG", "1,0,0,0,0,0",
                                            attrs=["enable", "connectStatus", "networkType", "simStatus"]),
                              self._parse_lte_link),
                self._section("lte_intf", A(A.GET, "DEV2_XTP_LTE_INTF_CFG", "1,0,0,0,0,0",
                                            attrs=["totalStatistics", "curRxSpeed", "curTxSpeed"]),
                              self._parse_lte_intf),
                self._section("lte_net", A(A.GET, "DEV2_LTE_NET_STATUS", "1,0,0,0,0,0",
                                           attrs=["smsUnreadCount", "sigLevel", "rfInfoRsrp", "rfInfoRsrq",
                                                  "rfInfoSnr"]),
                              self._parse_lte_net),
                # one request gives everything the library's two serving cell queries did, and more
                self._section("cells", A(A.GL, "DEV2_LTE_SERVING_CELL_INFO", attrs=self._cell_attrs),
                              self._parse_cells),
            ]
            if results[-1] is False and any(results[:-1]) and self._cell_attrs:
                # the router answered everything else but not our field list: fall back to unfiltered
                self._cell_attrs = []

            if now >= self._next_slow:
                slow = [
                    self._section("wan", A(A.GL, "DEV2_ADT_WAN",
                                           attrs=["enable", "MACAddr", "connIPv4Address", "connIPv4Gateway"]),
                                  self._parse_wan),
                    self._section("isp", A(A.GET, "DEV2_LTE_PROF_STAT", "1,0,0,0,0,0", attrs=["ispName"]),
                                  self._parse_isp),
                ]
                results += slow
                self._next_slow = now + SLOW_REFRESH_SECONDS if all(slow) else now

            if now >= self._next_uptime:
                ok = self._section("uptime", A(A.GET, "DEV2_DEV_INFO", attrs=["upTime"]), self._parse_uptime)
                results.append(ok)
                self._next_uptime = now + UPTIME_RESYNC_SECONDS if ok else now + SLOW_REFRESH_SECONDS
        except (ClientException, RequestException) as exc:
            # transport / HTTP-level failure: stop this tick right away instead of
            # waiting out the timeout on every remaining request
            self._fail(f"{type(exc).__name__}: {exc}")
            return
        except Exception as exc:
            # anything unexpected: show it and retry next tick rather than crash the dashboard
            self._fail(f"unexpected {type(exc).__name__}: {exc}")
            return
        finally:
            self.data.requests_last_tick = self._requests

        if not any(results):
            self._fail("router returned no data (session expired?)")
            return
        self.data.last_ok = datetime.now()
        self.data.last_error = None

    def _fail(self, message: str) -> None:
        self.data.last_error = message
        self.data.stale |= ALL_SECTIONS
        # refetch the slow sections on the first good tick instead of showing them stale for minutes
        self._next_slow = self._next_uptime = 0.0
        # re-login straight away on the first failure, then at most every REAUTH_MIN_INTERVAL
        now = time.monotonic()
        if now - self._last_reauth >= REAUTH_MIN_INTERVAL:
            self._last_reauth = now
            try:
                self.router.authorize()
            except Exception:
                pass


# --------------------------------------------------------------------------- rendering

def build_layout() -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="body"),
    )
    layout["body"].split_row(
        Layout(name="left", ratio=1),
        Layout(name="serving_cells", ratio=2),
    )
    layout["left"].split_column(
        Layout(name="stats", size=7),  # 5 content lines + top/bottom border
        Layout(name="cellular"),
    )
    return layout


def format_uptime(seconds: int) -> str:
    days, rem = divmod(int(seconds), 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    return f"{days}d {hours}h {minutes}m"


def format_speed(value) -> str:
    if not value:
        return "0"
    return f"{value / 1024:.1f} KB/s"


def format_bytes(value) -> str:
    if not value:
        return "0 B"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def fmt(value, suffix: str = "") -> str:
    return "n/a" if value is None else f"{value}{suffix}"


def fmt_snr(tenths) -> str:
    # router reports SNR in tenths of a dB (e.g. 275 -> 27.5)
    return "n/a" if tenths is None else f"{tenths / 10:.1f}"


def panel_title(name: str, data: Snapshot, sections: set[str]) -> str:
    return f"{name} (stale)" if data.stale & sections else name


def render_serving_cells(data: Snapshot) -> Table:
    table = Table(expand=True)
    table.add_column("Field")
    cells = data.cells
    if not cells:
        table.add_column("Value")
        table.add_row("n/a", "no active serving cell")
        return table

    for cell in cells:
        table.add_column(LTEStatus.network_types.get(cell.info.network_type, f"Type {cell.info.network_type}"),
                         justify="right")

    lte, active = data.lte, data.active_cell()

    def fallback(cell: Cell, attr: str):
        # the per-cell reading is preferred; for the currently active RAT, fall back to
        # LTE_NET_STATUS's aggregate reading if the cell didn't report one
        if cell is active and lte is not None:
            return getattr(lte, attr)
        return None

    def add_row(label: str, values) -> None:
        table.add_row(label, *values)

    extras_after = {
        "band": [("Pci", "pci"), ("Cell Id", "cid"), ("Tac", "tac")],
        "downlink_modulation": [("Downlink Mcs", "dl_mcs")],
        "uplink_modulation": [("Uplink Mcs", "ul_mcs")],
    }
    for f in dataclasses.fields(ServingCell):
        values = []
        for cell in cells:
            value = getattr(cell.info, f.name)
            if value is None and f.name in ("rsrp", "rsrq"):
                value = fallback(cell, f.name)
            values.append(fmt(value))
        add_row(f.name.replace("_", " ").title(), values)
        for label, attr in extras_after.get(f.name, []):
            add_row(label, [fmt(getattr(cell, attr)) for cell in cells])

    add_row("Signal Level", [fmt(cell.signal_level) for cell in cells])
    add_row("Snr", [fmt_snr(cell.snr if cell.snr is not None else fallback(cell, "snr")) for cell in cells])
    return table


def render_header(host: str, data: Snapshot) -> Panel:
    fw = data.firmware
    title = f"{fw.model} ({fw.hardware_version}) — {host}" if fw else host
    if data.last_error:
        # timestamp first: long library error messages get cut off at the panel border
        error = data.last_error if len(data.last_error) <= 80 else data.last_error[:80] + "…"
        since = f"last good {data.last_ok:%H:%M:%S}" if data.last_ok else "no data yet"
        subtitle = Text(f"poll failed ({since}): {error}", style="bold red")
    elif data.last_ok:
        subtitle = Text(f"updated {data.last_ok:%H:%M:%S} · {data.requests_last_tick} requests", style="dim")
    else:
        subtitle = Text("connecting…", style="dim")
    return Panel(Text(title, justify="center"), subtitle=subtitle, style="bold cyan")


def render(layout: Layout, host: str, data: Snapshot) -> None:
    layout["header"].update(render_header(host, data))

    lte, active = data.lte, data.active_cell()

    stats = Table.grid(padding=(0, 1))
    stats.add_column(justify="right", style="bold")
    stats.add_column()
    stats.add_row("CPU:", f"{data.cpu * 100:.0f}%" if data.cpu is not None else "n/a")
    stats.add_row("Memory:", f"{data.mem * 100:.0f}%" if data.mem is not None else "n/a")
    stats.add_row("WAN IP:", data.wan_ip or "n/a")
    stats.add_row("Gateway:", data.wan_gateway or "n/a")
    uptime = data.uptime()
    stats.add_row("Uptime:", format_uptime(uptime) if uptime is not None else "n/a")
    layout["stats"].update(Panel(stats, title=panel_title("Status", data, STATUS_SECTIONS)))

    cellular = Table.grid(padding=(0, 1))
    cellular.add_column(justify="right", style="bold")
    cellular.add_column()
    if lte is None:
        cellular.add_row("n/a", "cellular status unavailable")
    else:
        # signal values: the active serving cell's own reading when available (correct
        # whichever RAT is primary), otherwise LTE_NET_STATUS's aggregate reading
        sig_level = active.signal_level if active and active.signal_level is not None else lte.sig_level
        rsrp = active.info.rsrp if active and active.info.rsrp is not None else lte.rsrp
        rsrq = active.info.rsrq if active and active.info.rsrq is not None else lte.rsrq
        snr = active.snr if active and active.snr is not None else lte.snr
        cellular.add_row("Enabled:", "n/a" if lte.enable is None else ("yes" if lte.enable else "no"))
        cellular.add_row("Network type:", lte.network_type_info if lte.network_type is not None else "n/a")
        cellular.add_row("Connect status:", fmt(lte.connect_status))
        cellular.add_row("SIM status:", lte.sim_status_info if lte.sim_status is not None else "n/a")
        cellular.add_row("ISP:", lte.isp_name or "n/a")
        cellular.add_row("Signal level:", fmt(sig_level))
        cellular.add_row("RSRP:", fmt(rsrp, " dBm"))
        cellular.add_row("RSRQ:", fmt(rsrq, " dB"))
        cellular.add_row("SNR:", "n/a" if snr is None else f"{fmt_snr(snr)} dB")
        cellular.add_row("RX speed:", format_speed(lte.cur_rx_speed))
        cellular.add_row("TX speed:", format_speed(lte.cur_tx_speed))
        cellular.add_row("Total data used:", format_bytes(lte.total_statistics))
        cellular.add_row("Unread SMS:", fmt(lte.sms_unread_count))
    layout["cellular"].update(Panel(cellular, title=panel_title("Cellular (4G/5G)", data, CELLULAR_SECTIONS)))

    layout["serving_cells"].update(Panel(render_serving_cells(data),
                                         title=panel_title("Serving cells (per band)", data, CELL_SECTIONS)))


# --------------------------------------------------------------------------- main

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="http://192.168.254.1", help="Router base URL")
    parser.add_argument("--username", default="user", help="Router login username")
    parser.add_argument("--timeout", type=int, default=15, help="Per-request timeout in seconds (default 15)")
    parser.add_argument("--refresh", type=float, default=REFRESH_SECONDS, help="Seconds between polls (default 5)")
    parser.add_argument("--keep-alive", action="store_true",
                        help="Reuse the TCP connection between requests (untested on this firmware; "
                             "by default every request uses a fresh connection)")
    args = parser.parse_args()

    password = os.environ.get("TPLINK_PASSWORD") or getpass("Router password: ")

    logger = logging.getLogger("tplink_dashboard")
    logger.addHandler(logging.StreamHandler())
    logger.setLevel(logging.INFO)  # shows which client class the provider is probing/picked

    console = Console()
    layout = build_layout()

    try:
        router = TplinkRouterProvider.get_client(
            args.host, password, args.username, logger, timeout=args.timeout
        )
        if not args.keep_alive:
            # some embedded router HTTP servers hang on a 2nd request over a reused
            # keep-alive connection; force a fresh TCP connection per request instead
            router.req.headers["Connection"] = "close"
        router.authorize()
    except Exception as exc:
        console.print(f"[bold red]Login failed:[/] {exc}")
        console.print(
            "[yellow]Tip:[/] the router only allows one logged-in session at a time — "
            "make sure no browser tab is currently logged into the admin page, then retry."
        )
        return 1

    if not isinstance(router, TPLinkEXClient):
        # all queries below use EX-family (DEV2_*) OIDs; on another client class they'd all fail
        console.print(f"[bold red]Unexpected client class {type(router).__name__}[/] - this dashboard "
                      "only supports EX-family clients. If this router normally works, wait a minute "
                      "(possible login lockout) and retry.")
        try:
            router.logout()
        except Exception:
            pass
        return 1

    make_decoding_lenient(router)
    poller = Poller(router)
    try:
        with Live(layout, console=console, auto_refresh=False, screen=True) as live:
            render(layout, args.host, poller.data)
            live.refresh()
            next_tick = time.monotonic()
            while True:
                poller.poll()
                render(layout, args.host, poller.data)
                live.refresh()
                # fixed cadence: the interval includes the time the poll itself took
                next_tick += args.refresh
                delay = next_tick - time.monotonic()
                if delay < 0:  # poll took longer than the interval: don't try to catch up
                    next_tick, delay = time.monotonic(), 0
                time.sleep(delay)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            router.logout()
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    sys.exit(main())

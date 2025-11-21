# helpers.py (MEGA UI upgrade)
import re
import time
import json
import random
import asyncio
import aiohttp

from models import request, items
from typing import Optional, Union, List, Dict, TYPE_CHECKING

from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.panel import Panel
from rich.layout import Layout
from rich.align import Align
from rich.text import Text

if TYPE_CHECKING:
    from sniper import WatchLimiteds

# -------------------------------------------------------------------
# CombinedAttribute - forwards attributes from ProxyThread to WatchLimiteds
# -------------------------------------------------------------------
class CombinedAttribute:
    def __init__(self, watch_limiteds):
        super().__setattr__("watch_limiteds", watch_limiteds)

    def __getattr__(self, name):
        return getattr(self.watch_limiteds, name)

    def __setattr__(self, name, value):
        if name == "watch_limiteds":
            return super().__setattr__(name, value)
        setattr(self.watch_limiteds, name, value)

    def __delattr__(self, name):
        if name == "watch_limiteds":
            return super().__delattr__(name)
        delattr(self.watch_limiteds, name)

# -------------------------------------------------------------------
# UIManager - upgraded UI for Pro Sniper 2.0
# -------------------------------------------------------------------
class UIManager:
    def __init__(self, total_proxies: int, username: str, robux: str):
        self.start_time = time.time()
        self.total_proxies = total_proxies
        self.total_requests = 0
        self.total_items_checked = 0
        self.total_items_bought = 0
        self.total_failed_buys = 0
        self.username = username or ""
        self.robux = robux or "Onbekend"
        self.lock = asyncio.Lock()

        # logs buffer (recent events)
        self.logs: List[str] = []
        self.max_logs = 2000  # keep a lot

        # activity buffer - what items were last checked / examined
        # each entry: dict {timestamp, item_id, price, base_value, pct_off, proxy, reason}
        self.activity: List[Dict] = []
        self.max_activity = 200

        # proxy health: map proxy -> dict(status, last_latency_ms, last_error)
        self.proxy_health: Dict[str, Dict] = {}

    # LOGGING
    async def log_event(self, message: str, level: str = "INFO"):
        async with self.lock:
            timestamp = time.strftime("%H:%M:%S")
            entry = f"[{timestamp}] [{level}] {message}"
            self.logs.append(entry)
            if len(self.logs) > self.max_logs:
                # rotate
                self.logs = self.logs[-self.max_logs:]

    async def add_activity(self, item_id: int, price: int, base_value: int, pct_off: float, proxy: Optional[str], note: str):
        async with self.lock:
            now = time.strftime("%H:%M:%S")
            self.activity.append({
                "ts": now,
                "item_id": item_id,
                "price": price,
                "base_value": base_value,
                "pct_off": round(pct_off, 2),
                "proxy": proxy or "local",
                "note": note
            })
            if len(self.activity) > self.max_activity:
                self.activity = self.activity[-self.max_activity:]

    # METRICS
    async def add_requests(self, count: int = 1):
        async with self.lock:
            self.total_requests += count

    async def add_items(self, count: int = 1):
        async with self.lock:
            self.total_items_checked += count

    async def add_items_bought(self, count: int = 1):
        async with self.lock:
            self.total_items_bought += count

    async def add_failed_buy(self, count: int = 1):
        async with self.lock:
            self.total_failed_buys += count

    async def update_proxy_health(self, proxy: Optional[str], latency_ms: Optional[int], ok: bool, last_error: Optional[str] = None):
        async with self.lock:
            key = proxy or "local"
            self.proxy_health[key] = {
                "ok": ok,
                "latency_ms": latency_ms,
                "last_error": last_error,
                "ts": time.strftime("%H:%M:%S")
            }

    # RENDER
    def render(self):
        elapsed = int(time.time() - self.start_time)
        mins, secs = divmod(elapsed, 60)
        uptime = f"{mins}m {secs}s"

        # top-left: stats
        stats = Table.grid(padding=(0,0))
        stats.add_column(justify="right", style="cyan", ratio=1)
        stats.add_column(justify="left", style="white", ratio=2)
        stats.add_row("Proxies", str(self.total_proxies))
        stats.add_row("Requests", str(self.total_requests))
        stats.add_row("Items Checked", str(self.total_items_checked))
        stats.add_row("Items Bought", str(self.total_items_bought))
        stats.add_row("Failed Buys", str(self.total_failed_buys))
        stats.add_row("Uptime", uptime)

        # top-right: account
        acct = Table.grid(padding=(0,0))
        acct.add_column(justify="right", style="magenta")
        acct.add_column(justify="left", style="white")
        acct.add_row("Username", self.username)
        acct.add_row("Robux", str(self.robux))

        # proxy health table
        proxy_table = Table(show_header=True, header_style="bold blue")
        proxy_table.add_column("Proxy", overflow="fold")
        proxy_table.add_column("OK", justify="center")
        proxy_table.add_column("Latency ms", justify="right")
        proxy_table.add_column("Last Error", overflow="fold")
        proxy_table.add_column("TS", justify="center")

        # show all proxies, sorted
        for p, info in sorted(self.proxy_health.items()):
            proxy_table.add_row(p, "✓" if info.get("ok") else "✗", str(info.get("latency_ms") or "-"), str(info.get("last_error") or "-"), info.get("ts"))

        # recent activity table (last 8 rows)
        act_table = Table(show_header=True, header_style="bold green")
        act_table.add_column("TS", width=7)
        act_table.add_column("ItemID", justify="right")
        act_table.add_column("Base", justify="right")
        act_table.add_column("Price", justify="right")
        act_table.add_column("%Off", justify="right")
        act_table.add_column("Proxy", overflow="fold")
        act_table.add_column("Note", overflow="fold")

        # slice and show last 8 activities
        act_slice = self.activity[-8:]
        for a in reversed(act_slice):
            act_table.add_row(a["ts"], str(a["item_id"]), str(a["base_value"]), str(a["price"]), f"{a['pct_off']}%", a["proxy"], a["note"])

        # recent events panel - show last 14 lines to avoid overflow but internal buffer is huge
        recent_lines = self.logs[-14:]
        recent_text = "\n".join(recent_lines) if recent_lines else "[grey]No events yet..."

        # Layout
        layout = Layout()
        layout.split_row(
            Layout(name="left"),
            Layout(name="right", size=40)
        )

        # left column: stats up top, act table middle, recent events bottom
        left = Layout()
        left.split_column(
            Layout(Panel(stats, title="Stats", border_style="green"), size=9),
            Layout(Panel(act_table, title="Activity (recent checks)", border_style="blue"), size=12),
            Layout(Panel(recent_text, title=f"Recent Events (last {len(recent_lines)})", border_style="yellow"), ratio=1)
        )

        # right column: account + proxy health
        right = Layout()
        right.split_column(
            Layout(Panel(acct, title="Account Info", border_style="magenta"), size=6),
            Layout(Panel(proxy_table, title="Proxy Health", border_style="red"), ratio=1)
        )

        layout["left"].update(left)
        layout["right"].update(right)

        return layout


# UI runner
async def run_ui(ui_manager: UIManager):
    console = Console()
    # higher refresh for responsive UI
    with Live(ui_manager.render(), refresh_per_second=4, console=console) as live:
        while True:
            await asyncio.sleep(0.25)
            live.update(ui_manager.render())

class RolimonsDataScraper:
    def __init__(self):
        self.last_call_time = time.time()
        self.item_data: Dict[str, items.RolimonsData] = None
        
    async def __call__(self) -> Union[None, Dict[str, items.RolimonsData]]:
        now = time.time()
        elapsed = now - self.last_call_time
        
        # elke 10 minuten opnieuw ophalen
        if elapsed > 600 or not self.item_data:
            self.item_data = await self.retrieve_item_data()
            if self.item_data:
                self.last_call_time = now
                
        return self.item_data
    
    @staticmethod
    async def retrieve_item_data() -> Dict[str, items.RolimonsData]:
        response = await request.Request(
            url = "https://www.rolimons.com/itemapi/itemdetails",
            method = "get"
        ).send()
        
        data = response.response_json
        items_dataclass = {}
        items_dict = data.get("items", {})
        for item_id_str, arr in items_dict.items():
            if not isinstance(arr, list) or len(arr) < 10:
                continue
            rap = arr[2] if arr[2] != -1 else 0
            value = arr[4] if arr[4] != -1 else 0
            projected = arr[7]
            items_dataclass[item_id_str] = items.RolimonsData(
                rap=rap,
                value=value,
                projected=projected
            )
        return items_dataclass
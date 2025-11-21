# models/config.py
import re
import time
import json
import errors
import random
import asyncio
import aiohttp

from models import request, items
from typing import Optional, Union, List, Dict, TYPE_CHECKING

from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich.align import Align
from rich.layout import Layout
from rich.box import HEAVY

if TYPE_CHECKING:
    from sniper import WatchLimiteds

class UIManager:
    def __init__(self, total_proxies: int, username: str, robux: str):
        self.start_time = time.time()
        self.total_proxies = total_proxies
        self.total_requests = 0
        self.total_items_checked = 0
        self.total_items_bought = 0
        self.total_failed_buys = 0
        self.username = username
        self.robux = robux
        self.lock = asyncio.Lock()
        self.logs = []

    async def log_event(self, message: str):
        async with self.lock:
            timestamp = time.strftime('%H:%M:%S')
            self.logs.append(f"[{timestamp}] {message}")
            if len(self.logs) > 20:
                self.logs.pop(0)

    async def add_requests(self, count: int = 1):
        async with self.lock:
            self.total_requests += count

    async def add_items(self, count: int):
        async with self.lock:
            self.total_items_checked += count

    async def add_items_bought(self, count: int = 1):
        async with self.lock:
            self.total_items_bought += count

    async def add_failed_buy(self, count: int = 1):
        async with self.lock:
            self.total_failed_buys += count

    def render(self):
        elapsed = int(time.time() - self.start_time)
        mins, secs = divmod(elapsed, 60)
        uptime = f"{mins}m {secs}s"

        stats_table = Table.grid(padding=1)
        stats_table.add_column(justify="right", style="bold cyan")
        stats_table.add_column(style="bold white")

        stats_table.add_row("Proxies", str(self.total_proxies))
        stats_table.add_row("Requests", str(self.total_requests))
        stats_table.add_row("Items Checked", str(self.total_items_checked))
        stats_table.add_row("Items Bought", str(self.total_items_bought))
        stats_table.add_row("Items Failed to Buy", str(self.total_failed_buys))
        stats_table.add_row("Uptime", uptime)

        account_table = Table.grid(padding=1)
        account_table.add_column(justify="right", style="bold magenta")
        account_table.add_column(style="bold white")

        account_table.add_row("Username", self.username)
        account_table.add_row("Robux", str(self.robux))

        log_panel = Panel(
            "\n".join(self.logs) if self.logs else "[grey]No logs yet...",
            title="Recent Events",
            border_style="yellow",
            padding=(1, 2)
        )

        layout = Layout()
        layout.split_column(
            Layout(Panel(stats_table, title="Stats", border_style="green", padding=(1, 2)), name="stats", size=15),
            Layout(Panel(account_table, title="Account Info", border_style="magenta", padding=(1, 2)), name="account", size=8),
            Layout(log_panel, name="logs")
        )

        return layout

async def run_ui(ui_manager: UIManager):
    console = Console()
    with Live(ui_manager.render(), refresh_per_second=1, console=console) as live:
        while True:
            await asyncio.sleep(1)
            live.update(ui_manager.render())
                
class CombinedAttribute:
    def __init__(self, watch_limiteds: 'WatchLimiteds'):
        self.watch_limiteds = watch_limiteds
        
    def __getattr__(self, name):
        return getattr(self.watch_limiteds, name)

    def __setattr__(self, name, value):
        if name == "watch_limiteds" or name.startswith("_"):
            super().__setattr__(name, value)
        else:
            setattr(self.watch_limiteds, name, value)

    def __delattr__(self, name):
        if name == "watch_limiteds" or name.startswith("_"):
            super().__delattr__(name)
        else:
            delattr(self.watch_limiteds, name)

class Iterator:
    def __init__(self, data: List[items.Generic]):
        self.original_data = data[:]
        self._reset_pool()

    def _reset_pool(self):
        self.pool = self.original_data[:]
        random.shuffle(self.pool)
        self.index = 0

    def __call__(self, batch_size: int) -> List[items.Generic]:
        if batch_size >= len(self.original_data):
            return self.original_data[:]
        
        batch = []
        
        while len(batch) < batch_size:
            if self.index >= len(self.pool):
                self._reset_pool()

            needed = batch_size - len(batch)
            available = len(self.pool) - self.index
            take = min(needed, available)

            batch.extend(self.pool[self.index:self.index + take])
            self.index += take

        return batch

    def __len__(self):
        return len(self.original_data)

class XCsrfTokenWaiter:
    """Fetches and caches an x-csrf token immediately, then refreshes every 120 seconds."""

    def __init__(self, cookie: Optional[str] = None, proxy: Optional[str] = None, on_start: bool = False):
        self.last_call_time = time.time()
        self.cookie = cookie
        self.proxy = proxy
        self.x_crsf_token = None
        if on_start:
            loop = asyncio.get_event_loop()
            loop.run_until_complete(self.load_token())

    async def load_token(self):
        self.x_crsf_token = await self.generate_x_csrf_token(self.cookie, self.proxy)

    async def __call__(self) -> Union[None, str]:
        now = time.time()
        elapsed = now - self.last_call_time

        if self.x_crsf_token is None:
            self.x_crsf_token = await self.generate_x_csrf_token(self.cookie, self.proxy)
            if self.x_crsf_token:
                self.last_call_time = now
        elif elapsed > 120:
            new_token = await self.generate_x_csrf_token(self.cookie, self.proxy)
            if new_token:
                self.x_crsf_token = new_token
                self.last_call_time = now

        return self.x_crsf_token
    
    @staticmethod
    async def generate_x_csrf_token(cookie: Union[str, None], proxy: Union[str, None]) -> Union[str, None]:
        response: request.Response
        response = await request.Request(
            url = "https://auth.roblox.com/v2/logout",
            method = "post",
            headers = request.Headers(
                cookies = {".ROBLOSECURITY": cookie}
            ),
            success_status_codes = [403],
            proxy = proxy
        ).send()
        return response.response_headers.x_csrf_token

class RolimonsDataScraper:
    def __init__(self):
        self.last_call_time = time.time()
        self.item_data: Dict[str, items.RolimonsData] = None
        
    async def __call__(self) -> Union[None, Dict[str, items.RolimonsData]]:
        now = time.time()


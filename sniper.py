# sniper.py (MEGA upgrade)
from models import items, config, request
from typing import Union, Tuple, Optional, List, Dict, Any
import errors
import helpers
import asyncio
import time
import json

class BuyLimited:
    def __init__(self, user_data: config.Account, buy_data: items.BuyData, ui_manager: helpers.UIManager) -> None:
        self.user_data = user_data
        self.buy_data = buy_data
        self.ui_manager = ui_manager

    async def __call__(self) -> Union[bool, Tuple[bool, Any]]:
        url = f"https://apis.roblox.com/marketplace-sales/v1/item/{self.buy_data.collectible_item_id}/purchase-resale"
        await self.ui_manager.log_event(f"Buy attempt for item {self.buy_data.collectible_item_id} expected {self.buy_data.expected_price} R$")
        t0 = time.perf_counter()
        try:
            resp = await request.Request(
                url=url,
                method="post",
                headers=request.Headers(
                    x_csrf_token=await self.user_data.x_csrf_token(),
                    cookies={".ROBLOSECURITY": self.user_data.cookie}
                ),
                json_data=request.RequestJsons.jsonify_api_broad(url, self.buy_data),
                close_session=False,
                user_id=self.user_data.user_id
            ).send()
        except Exception as e:
            await self.ui_manager.log_event(f"Buy request failed (network): {e}", level="ERROR")
            await self.ui_manager.add_failed_buy(1)
            return False

        latency_ms = int((time.perf_counter() - t0) * 1000)
        await self.ui_manager.log_event(f"Buy request latency: {latency_ms} ms")
        await self.ui_manager.add_requests(1)

        if resp and resp.response_json and getattr(resp.response_json, "purchased", False):
            await self.ui_manager.log_event(f"GEKOCHT! Item {self.buy_data.collectible_item_id} voor {self.buy_data.expected_price} R$")
            await self.ui_manager.add_items_bought(1)
            return True, resp.response_json
        else:
            # try to extract error message
            err = None
            if resp and resp.response_json:
                if isinstance(resp.response_json, dict):
                    err = resp.response_json.get("errorMessage") or resp.response_json.get("error_message")
                else:
                    err = getattr(resp.response_json, "error_message", None)
            if not err and resp:
                err = (resp.response_text[:200] + "...") if resp.response_text else "Unknown"
            await self.ui_manager.log_event(f"Niet gekocht: {err}", level="WARN")
            await self.ui_manager.add_failed_buy(1)
            return False, resp.response_json if resp else None

class WatchLimiteds:
    def __init__(self, config: config.Settings, rolimon_limiteds: helpers.RolimonsDataScraper, robux: str) -> None:
        self.webhook = config.webhook
        self.account = config.account
        self.generic_settings = config.buy_settings.generic_settings or {}
        self.custom_settings = config.buy_settings.custom_settings or {}
        self.limiteds = config.limiteds
        self.rolimon_limiteds = rolimon_limiteds
        self.proxies = config.proxies or []
        self.ui_manager = helpers.UIManager(total_proxies = len(self.proxies), username = getattr(config.account, "user_name", ""), robux = robux)
        self.deal_mode = len(self.limiteds) == 0

        # new: min percent filter from generic settings or top-level config
        self.deal_filter_min_percentage = None
        # try from generic settings dict
        if isinstance(self.generic_settings, dict):
            self.deal_filter_min_percentage = self.generic_settings.get("deal_filter_min_percentage")
        # fallback if exists as attribute on config
        if getattr(config, "deal_filter_min_percentage", None):
            self.deal_filter_min_percentage = config.deal_filter_min_percentage

    async def __call__(self):
        # background account monitor
        acct_monitor = asyncio.create_task(self._account_monitor_loop())
        # start threads
        threads = [
            ProxyThread(self, proxy).watch()
            for proxy in (self.proxies if self.proxies else [None])
        ]
        # run UI + threads
        await asyncio.gather(*threads, helpers.run_ui(ui_manager = self.ui_manager), return_exceptions=True)
        acct_monitor.cancel()

    async def _account_monitor_loop(self):
        while True:
            try:
                if not getattr(self.account, "user_id", None) or not getattr(self.account, "user_name", None):
                    await self.account.populate_from_api()
                    if getattr(self.account, "user_name", None):
                        await self.ui_manager.log_event(f"Ingelogd als: {self.account.user_name}")

                # fetch robux
                if getattr(self.account, "user_id", None):
                    url = f"https://economy.roblox.com/v1/users/{self.account.user_id}/currency"
                    t0 = time.perf_counter()
                    try:
                        resp = await request.Request(
                            url=url,
                            method="get",
                            headers=request.Headers(cookies={".ROBLOSECURITY": self.account.cookie}),
                            retries=2
                        ).send()
                    except Exception as e:
                        await self.ui_manager.log_event(f"Robux ophalen faalde: {e}", level="ERROR")
                        resp = None
                    latency_ms = int((time.perf_counter() - t0) * 1000)
                    await self.ui_manager.add_requests(1)

                    # parse raw dict or fallback to text
                    new_robux = None
                    if resp and resp.response_json:
                        if isinstance(resp.response_json, dict):
                            new_robux = resp.response_json.get("robux") or resp.response_json.get("balance")
                        elif isinstance(resp.response_json, (int, float, str)):
                            new_robux = resp.response_json
                    if new_robux is None and resp and resp.response_text:
                        try:
                            parsed = json.loads(resp.response_text)
                            new_robux = parsed.get("robux") or parsed.get("balance")
                        except Exception:
                            new_robux = None

                    if new_robux is not None:
                        self.ui_manager.robux = str(new_robux)
                        await self.ui_manager.log_event(f"Robux updated: {new_robux} (latency {latency_ms} ms)")
                    else:
                        await self.ui_manager.log_event("Robux ophalen: geen geldige JSON ontvangen", level="WARN")
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                return
            except Exception as e:
                await self.ui_manager.log_event(f"Account monitor error: {e}", level="ERROR")
                await asyncio.sleep(10)

class ProxyThread(helpers.CombinedAttribute):
    def __init__(self, watch_limiteds: WatchLimiteds, proxy: Optional[str]):
        super().__init__(watch_limiteds)
        self._proxy = proxy
        self.deal_scraper = None

    def check_if_item_elligable(self, item_data: items.Data, item_value_rap: items.RolimonsData) -> bool:
        if not item_value_rap:
            return False
        if getattr(item_value_rap, "projected", -1) != -1:
            return False

        # get config for this item
        item_buy_config = self.generic_settings if str(item_data.item_id) not in self.custom_settings else self.custom_settings.get(str(item_data.item_id), self.generic_settings)

        # determine base_value according to price_measurer
        pm = item_buy_config.get("price_measurer", "value_rap") if isinstance(item_buy_config, dict) else "value_rap"

        base_value_item = 0
        if pm == "value":
            base_value_item = getattr(item_value_rap, "value", 0)
        elif pm == "rap":
            base_value_item = getattr(item_value_rap, "rap", 0)
        else:  # value_rap
            base_value_item = getattr(item_value_rap, "value", 0) or getattr(item_value_rap, "rap", 0)

        if base_value_item <= 0:
            return False

        price = getattr(item_data, "lowest_resale_price", 0) or 0
        percentage_off = ((base_value_item - price) / base_value_item * 100) if base_value_item else 0
        robux_off = base_value_item - price

        # apply thresholds from item_buy_config
        if isinstance(item_buy_config, dict):
            if item_buy_config.get("min_percentage_off") and percentage_off < item_buy_config.get("min_percentage_off"):
                return False
            if item_buy_config.get("min_robux_off") and robux_off < item_buy_config.get("min_robux_off"):
                return False
            if item_buy_config.get("max_robux_cost") and price > item_buy_config.get("max_robux_cost"):
                return False

        # apply global deal filter if set
        try:
            df = getattr(self, "deal_filter_min_percentage", None)
            if df is not None:
                if percentage_off < float(df):
                    return False
        except Exception:
            pass

        return True

    async def get_resale_data(self, item: items.Data) -> Union[request.ResponseJsons.ResaleResponse, None]:
        url = f"https://apis.roblox.com/marketplace-sales/v1/item/{item.collectible_item_id}/resellers?limit=1"
        await self.ui_manager.log_event(f"Fetching resale for {item.item_id} via {self._proxy or 'local'}")
        t0 = time.perf_counter()
        try:
            resp = await request.Request(url=url, method="get", proxy=self._proxy, retries=4).send()
        except Exception as e:
            await self.ui_manager.log_event(f"Resale request failed for {item.item_id}: {e}", level="ERROR")
            await self.ui_manager.update_proxy_health(self._proxy, None, False, str(e))
            return None
        latency_ms = int((time.perf_counter() - t0) * 1000)
        await self.ui_manager.update_proxy_health(self._proxy, latency_ms, True, None)
        await self.ui_manager.add_requests(1)
        return resp.response_json if resp else None

    async def handle_response(self, item_list: request.ResponseJsons.ItemDetails):
        if not item_list:
            return
        try:
            rolimons_data = await self.rolimon_limiteds()
        except Exception as e:
            rolimons_data = {}
            await self.ui_manager.log_event(f"Rolimons fetch failed: {e}", level="ERROR")

        for item in item_list.items:
            try:
                item_id = getattr(item, "item_id", None)
                if item_id is None:
                    continue
                iid = str(item_id)
                rdata = rolimons_data.get(iid) if rolimons_data else None

                if not rdata:
                    await self.ui_manager.log_event(f"Item {item_id} not present on Rolimons - skipping")
                    await self.ui_manager.add_items(1)
                    await self.ui_manager.add_requests(0)
                    continue

                # ensure lowest_resale_price exists; some endpoints don't include direct price
                price = getattr(item, "lowest_resale_price", 0) or 0

                # compute base_value
                pm = (self.generic_settings.get("price_measurer") if isinstance(self.generic_settings, dict) else "value_rap")
                if pm == "value":
                    base_val = getattr(rdata, "value", 0)
                elif pm == "rap":
                    base_val = getattr(rdata, "rap", 0)
                else:
                    base_val = getattr(rdata, "value", 0) or getattr(rdata, "rap", 0)

                # compute pct_off
                pct_off = ((base_val - price) / base_val * 100) if base_val else 0

                # log what we check
                await self.ui_manager.add_items(1)
                await self.ui_manager.add_requests(0)
                await self.ui_manager.add_activity(item_id, price, base_val, pct_off, self._proxy, "checked Rolimons & price")

                # eligibility
                if not self.check_if_item_elligable(item, rdata):
                    await self.ui_manager.log_event(f"Item {item_id} ineligible: base={base_val}, price={price}, pct_off={round(pct_off,2)}%")
                    continue

                # fetch resale details
                resale = await self.get_resale_data(item)
                if not resale:
                    continue

                # set item price from resale if available
                resale_price = getattr(resale, "price", None) or 0
                item.lowest_resale_price = resale_price

                # recalc pct_off with actual resale
                pct_off_real = ((base_val - resale_price) / base_val * 100) if base_val else 0

                # log decisive check
                await self.ui_manager.log_event(f"Potential deal: Item {item_id} base={base_val} resale={resale_price} pct_off={round(pct_off_real,2)}% via {self._proxy or 'local'}")
                await self.ui_manager.add_activity(item_id, resale_price, base_val, pct_off_real, self._proxy, "potential deal")

                # check global filter again before buy
                if self.deal_filter_min_percentage is not None and pct_off_real < float(self.deal_filter_min_percentage):
                    await self.ui_manager.log_event(f"Skipping buy: pct_off {round(pct_off_real,2)}% < filter {self.deal_filter_min_percentage}%")
                    continue

                # build buy payload
                buy_data = items.BuyData(
                    collectible_item_id = item.collectible_item_id,
                    collectible_item_instance_id = getattr(resale, "collectible_item_instance_id", ""),
                    collectible_product_id = getattr(resale, "collectible_product_id", ""),
                    expected_price = resale_price,
                    expected_purchaser_id = str(self.account.user_id)
                )

                buy_mgr = BuyLimited(self.account, buy_data, self.ui_manager)
                buy_result = await buy_mgr()
                success = (isinstance(buy_result, tuple) and buy_result[0]) or (buy_result is True)
                await self.ui_manager.log_event(f"{'BUY SUCCESS' if success else 'BUY FAIL'} for {item_id} at {resale_price} R$")
            except Exception as e:
                await self.ui_manager.log_event(f"Error handling item {getattr(item,'item_id','?')}: {e}", level="ERROR")

    async def get_batch_item_data(self, url: str, items: List[items.Generic], proxy: Optional[str] = None):
        if not items:
            return None
        await self.ui_manager.log_event(f"Requesting batch ({len(items)}) from {url} via {proxy or 'local'}")
        t0 = time.perf_counter()
        try:
            response = await request.Request(
                url = url,
                method = "post",
                headers = request.Headers(
                    cookies = {".ROBLOSECURITY": self.account.cookie},
                    x_csrf_token = await self.account.x_csrf_token()
                ),
                json_data = request.RequestJsons.jsonify_api_broad(url, items),
                proxy = proxy,
                retries = 3
            ).send()
        except Exception as e:
            await self.ui_manager.log_event(f"Batch request failed: {e}", level="ERROR")
            await self.ui_manager.update_proxy_health(proxy, None, False, str(e))
            return None
        latency_ms = int((time.perf_counter() - t0) * 1000)
        await self.ui_manager.add_requests(1)
        await self.ui_manager.log_event(f"Batch latency: {latency_ms} ms")
        await self.ui_manager.update_proxy_health(proxy, latency_ms, True, None)

        parsed = response.response_json if response else None
        # fallback: try parse response_text
        if parsed is None and response and response.response_text:
            try:
                raw = json.loads(response.response_text)
                parsed = request.ResponseJsons.validate_json(url, raw)
            except Exception:
                parsed = None

        await self.handle_response(parsed)
        return parsed

    async def watch(self):
        if self.deal_mode:
            self.deal_scraper = helpers.DealActivityScraper()
            await self._watch_deals()
        else:
            await self._watch_listed()

    async def _watch_deals(self):
        await self.ui_manager.log_event("Deal Sniper Mode GESTART - polling elke 60s...")
        while True:
            try:
                new_deals = await self.deal_scraper()
                if not new_deals:
                    await self.ui_manager.log_event("Geen/lege dealactivity response; wacht...", level="DEBUG")
                    await asyncio.sleep(60)
                    continue

                roli = await self.rolimon_limiteds()
                new_ids = []
                for act in new_deals:
                    try:
                        iid = int(act[2])
                    except Exception:
                        continue
                    r = roli.get(str(iid)) if roli else None
                    if r and getattr(r, "projected", -1) == -1 and getattr(r, "rap", 0) > 0:
                        new_ids.append(iid)

                if new_ids:
                    await self.ui_manager.log_event(f"{len(new_ids)} potentiële deals gevonden (voorbeeld: {new_ids[:20]})")
                    batch_size = 120
                    for i in range(0, len(new_ids), batch_size):
                        batch = new_ids[i:i+batch_size]
                        gen_items = [items.Generic(item_id=b, collectible_item_id="") for b in batch]
                        await self.get_batch_item_data(url="https://catalog.roblox.com/v1/catalog/items/details", items=gen_items, proxy=self._proxy)
                await asyncio.sleep(1)
            except Exception as e:
                await self.ui_manager.log_event(f"Fout in deal loop: {e}", level="ERROR")
                await asyncio.sleep(10)

    async def _watch_listed(self):
        while True:
            try:
                await asyncio.gather(
                    self.get_batch_item_data(url = "https://catalog.roblox.com/v1/catalog/items/details", items = self.limiteds(120), proxy = self._proxy),
                    self.get_batch_item_data(url = "https://apis.roblox.com/marketplace-items/v1/items/details", items = self.limiteds(30), proxy = self._proxy)
                )
            except Exception as e:
                await self.ui_manager.log_event(f"Fout in listed loop: {e}", level="ERROR")
            finally:
                await asyncio.sleep(1)

# main.py
import json
import asyncio
from pathlib import Path

from models import config as cfg
import helpers
import sniper
from models import items, request

CONFIG_PATH = Path(__file__).parent / "config.json"


class Account:
    def __init__(self, data: dict):
        self.cookie = data.get("cookie", "")
        self.otp_token = data.get("otp_token", "")
        self.user_id = None
        self.user_name = None
        self._xcsrfer = cfg.XCsrfTokenWaiter(cookie=self.cookie)

    async def x_csrf_token(self):
        return await self._xcsrfer()

    async def populate_from_api(self):
        try:
            resp = await request.Request(
                url="https://users.roblox.com/v1/users/authenticated",
                method="get",
                headers=request.Headers(cookies={".ROBLOSECURITY": self.cookie}),
            ).send()

            if resp.response_json:
                self.user_id = resp.response_json.user_id
                self.user_name = resp.response_json.user_name

        except:
            pass


class Settings:
    def __init__(self, path: Path):
        data = json.load(open(path, "r"))

        self.webhook = data.get("webhook")
        self.account = Account(data["account"])

        # buy_settings
        raw = data.get("buy_settings", {})
        self.buy_settings = type("x", (), {})()
        self.buy_settings.generic_settings = raw.get("generic_settings", {})
        self.buy_settings.custom_settings = raw.get("custom_settings", {})

        # limiteds into items.Generic()
        lim_raw = data.get("limiteds", [])
        lim_items = []
        for it in lim_raw:
            try:
                lim_items.append(items.Generic(item_id=int(it), collectible_item_id=""))
            except:
                pass

        self.limiteds = cfg.Iterator(lim_items)
        self.proxies = data.get("proxies", [])

    async def load(self):
        await self.account.populate_from_api()


async def get_robux(account: Account):
    try:
        if not account.user_id:
            await account.populate_from_api()

        resp = await request.Request(
            url=f"https://economy.roblox.com/v1/users/{account.user_id}/currency",
            method="get",
            headers=request.Headers(cookies={".ROBLOSECURITY": account.cookie})
        ).send()

        return resp.response_json.get("robux", "Onbekend") if resp.response_json else "Onbekend"

    except:
        return "Onbekend"


async def main():
    settings = Settings(CONFIG_PATH)
    await settings.load()

    rolis = helpers.RolimonsDataScraper()
    robux = await get_robux(settings.account)

    if len(settings.limiteds) == 0:
        print("Deal Sniper Mode: 24/7 monitoring Rolimons deal activity for automatic good deals!")
    else:
        print(f"Monitoring {len(settings.limiteds)} specific limiteds.")

    await sniper.WatchLimiteds(settings, rolis, robux)()


if __name__ == "__main__":
    asyncio.run(main())

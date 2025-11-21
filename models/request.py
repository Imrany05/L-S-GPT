# models/request.py (PATCHED)
import re
import json
import aiohttp
import asyncio
from dataclasses import dataclass
from typing import List, Optional, Union, Dict, Any

import errors
from models import items


# ---------------------------------------------------------
# RESPONSE OBJECTS
# ---------------------------------------------------------
class ResponseJsons:

    @dataclass
    class ItemDetails:
        items: List[items.Data]

    @dataclass
    class CookieInfo:
        user_id: Any
        user_name: Any
        display_name: Any = ""

    @dataclass
    class BuyResponse:
        purchased_result: Any = None
        purchased: bool = False
        pending: bool = False
        error_message: Any = None

    @dataclass
    class ResaleResponse:
        collectible_item_instance_id: str = ""
        collectible_product_id: str = ""
        seller_id: int = 0
        price: int = 0

    @dataclass
    class TwoStepVerification:
        verificationToken: str = ""

    @staticmethod
    def validate_json(url, response_json: Any):
        """
        Detect JSON based on endpoint patterns and normalize it.
        Returns: dataclass (ItemDetails/CookieInfo/ResaleResponse/BuyResponse) or a raw dict/list/int
        """

        if response_json is None:
            return None

        # ---------------------------
        # 1) ROLIMONS DEALACTIVITY: return raw dict so callers can .get("activities")
        # ---------------------------
        if "rolimons.com/market/v1/dealactivity" in url or "api.rolimons.com/market/v1/dealactivity" in url:
            if isinstance(response_json, dict) and "activities" in response_json:
                return response_json

        # ---------------------------
        # 2) ROBLOX CURRENCY endpoint (robux)
        # ---------------------------
        if re.search(r"/v1/users/\d+/currency$", url) or "/currency" in url and isinstance(response_json, dict):
            # return raw dict so caller can check .get("robux")
            return response_json

        # ---------------------------
        # 3) Items details endpoint
        # ---------------------------
        if "/items/details" in url:
            try:
                data_list = []
                if not isinstance(response_json, (dict, list)):
                    return None

                source = response_json.get("data", response_json) if isinstance(response_json, dict) else response_json

                if not isinstance(source, list):
                    return None

                if isinstance(source, list):
                    for it in source:
                        if not isinstance(it, dict):
                            return None
                        data_list.append(items.Data(
                            item_id=int(it.get("id", it.get("itemId", 0))),
                            product_id=int(it.get("productId", it.get("collectibleProductId", 0) or 0)),
                            collectible_item_id=str(it.get("collectibleItemId", it.get("collectible_item_id", ""))),
                            lowest_resale_price=int(it.get("lowestResalePrice", it.get("offer", {}).get("price", 0)))
                        ))
                return ResponseJsons.ItemDetails(items=data_list)
            except Exception:
                return None

        # ---------------------------
        # 4) Authenticated user info
        # ---------------------------
        if "/users/authenticated" in url:
            if isinstance(response_json, dict):
                return ResponseJsons.CookieInfo(
                    user_id=response_json.get("id"),
                    user_name=response_json.get("name"),
                    display_name=response_json.get("displayName", "")
                )
            return None

        # ---------------------------
        # 5) Purchase resale response
        # ---------------------------
        if re.match(r".*/purchase-resale$", url):
            if isinstance(response_json, dict):
                return ResponseJsons.BuyResponse(
                    purchased_result=response_json.get("purchasedResult"),
                    purchased=response_json.get("purchased", False),
                    pending=response_json.get("pending", False),
                    error_message=response_json.get("errorMessage")
                )
            return None

        # ---------------------------
        # 6) Resellers (take first)
        # ---------------------------
        if "/resellers" in url:
            try:
                if isinstance(response_json, list) and len(response_json) > 0:
                    first = response_json[0]
                elif isinstance(response_json, dict):
                    first = response_json
                else:
                    return None

                return ResponseJsons.ResaleResponse(
                    collectible_item_instance_id=str(first.get("collectibleItemInstanceId", first.get("collectible_item_instance_id", ""))),
                    collectible_product_id=str(first.get("collectibleProductId", first.get("collectible_product_id", ""))),
                    seller_id=int(first.get("sellerId", first.get("seller_id", 0))),
                    price=int(first.get("price", first.get("rap", 0)))
                )
            except Exception:
                return None

        # Fallback: return raw structure so callers can inspect it
        return response_json


# ---------------------------------------------------------
# REQUEST JSON PAYLOAD HANDLER
# ---------------------------------------------------------
class RequestJsons:

    @dataclass
    class WebhookMessage:
        content: str
        username: Optional[str] = None
        embeds: Optional[list] = None

    def jsonify_api_broad(url: str, data):
        """Correct JSON body based on endpoint"""

        # Items details: expects {"items":[{"itemId": <int>}, ...]}
        if "/items/details" in url and isinstance(data, list):
            return {"items": [{"itemId": i.item_id} for i in data]}

        # Purchase resale payload
        if re.match(r".*/purchase-resale$", url) and isinstance(data, items.BuyData):
            return {
                "collectibleItemId": data.collectible_item_id,
                "collectibleItemInstanceId": data.collectible_item_instance_id,
                "collectibleProductId": data.collectible_product_id,
                "expectedPrice": data.expected_price,
                "expectedPurchaserId": data.expected_purchaser_id,
                "expectedPurchaserType": data.expected_purchaser_type,
                "expectedCurrency": data.expected_currency,
                "expectedSellerId": data.expected_seller_id
            }

        # Discord webhook
        if "discord.com/api/webhooks" in url:
            return {"content": data.content, "username": data.username, "embeds": data.embeds}

        return {}


# ---------------------------------------------------------
# HEADERS / RESPONSE CONTAINERS
# ---------------------------------------------------------
@dataclass
class Headers:
    x_csrf_token: Optional[str] = ""
    cookies: Optional[dict] = None
    raw_headers: Optional[dict] = None


@dataclass
class Response:
    status_code: int
    response_headers: Headers
    response_json: Any
    response_text: Optional[str] = None


# ---------------------------------------------------------
# MAIN REQUEST CLASS
# ---------------------------------------------------------
@dataclass
class Request:
    url: str
    method: str = "get"
    headers: Optional[Headers] = None
    json_data: Optional[dict] = None
    proxy: Optional[str] = None
    session: Optional[aiohttp.ClientSession] = None
    close_session: bool = True
    retries: int = 2
    success_status_codes: List[int] = (200, 201, 204)
    otp_token: Optional[str] = None
    user_id: Optional[str] = None

    async def send(self):
        """
        Send request with retries, CSRF refresh and robust JSON fallback parsing.
        Returns Response where response_json is the validated dataclass OR raw parsed JSON if validation returned None.
        """

        session_created = False
        if not self.session:
            self.session = aiohttp.ClientSession()
            session_created = True

        last_exc = None

        # helper to create headers dict and add sane defaults
        def build_headers() -> Dict[str, str]:
            hdrs: Dict[str, str] = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) SniperGrok/1.0",
                "Accept": "application/json, text/plain, */*",
            }
            if self.headers:
                if self.headers.raw_headers:
                    hdrs.update(self.headers.raw_headers)
                if self.headers.x_csrf_token:
                    hdrs["x-csrf-token"] = self.headers.x_csrf_token
                if self.headers.cookies:
                    hdrs["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.headers.cookies.items())
            return hdrs

        try:
            for attempt in range(max(1, self.retries + 1)):
                hdrs = build_headers()
                try:
                    async with self.session.request(self.method.upper(), self.url, headers=hdrs, json=self.json_data, proxy=self.proxy) as resp:
                        text = await resp.text()
                        status = resp.status

                        # CSRF handling (Roblox returns 403 with x-csrf-token header)
                        if status == 403 and resp.headers.get("x-csrf-token"):
                            token = resp.headers.get("x-csrf-token")
                            if not self.headers:
                                self.headers = Headers()
                            self.headers.x_csrf_token = token
                            # try again immediately with token
                            last_exc = errors.Request.Failed(f"Updated x-csrf-token, retrying (attempt {attempt})")
                            continue

                        # If success status
                        if status in self.success_status_codes:
                            # try standard JSON parse
                            parsed_json = None
                            try:
                                parsed_json = await resp.json()
                            except Exception:
                                # fallback attempt: load from text
                                try:
                                    parsed_json = json.loads(text) if text else None
                                except Exception:
                                    parsed_json = None

                            # Validate/normalize JSON for known endpoints
                            validated = None
                            try:
                                validated = ResponseJsons.validate_json(self.url, parsed_json)
                            except Exception:
                                validated = None

                            # If validate_json returned None, but parsed_json exists, use it as response_json
                            final_json = validated if validated is not None else parsed_json

                            # Build response headers data
                            resp_cookies = {}
                            try:
                                # aiohttp resp.cookies is a dict-like
                                for k, morsel in resp.cookies.items():
                                    resp_cookies[k] = morsel.value
                            except Exception:
                                resp_cookies = None

                            rheaders = Headers(
                                x_csrf_token=resp.headers.get("x-csrf-token"),
                                cookies=resp_cookies,
                                raw_headers=dict(resp.headers)
                            )

                            return Response(status_code=status, response_headers=rheaders, response_json=final_json, response_text=text)

                        # handle 401 two-step verification style responses
                        if status == 401:
                            # try to parse JSON body
                            parsed_json = None
                            try:
                                parsed_json = await resp.json()
                            except Exception:
                                try:
                                    parsed_json = json.loads(text) if text else {}
                                except Exception:
                                    parsed_json = {}

                            validated = None
                            try:
                                # check for 2fa challenge
                                validated = ResponseJsons.validate_json(f"{self.url}/challenges/authenticator/verify", parsed_json)
                            except Exception:
                                validated = None

                            resp_cookies = {}
                            try:
                                for k, morsel in resp.cookies.items():
                                    resp_cookies[k] = morsel.value
                            except Exception:
                                resp_cookies = None

                            rheaders = Headers(x_csrf_token=resp.headers.get("x-csrf-token"), cookies=resp_cookies, raw_headers=dict(resp.headers))
                            return Response(status_code=status, response_headers=rheaders, response_json=(validated if validated is not None else parsed_json), response_text=text)

                        # other statuses: capture and retry
                        last_exc = errors.Request.Failed(f"Unexpected status {status}: {text}")

                except Exception as e:
                    last_exc = e
                    # small jitter/backoff
                    await asyncio.sleep(0.2 + (attempt * 0.1))

            # retries exhausted
        finally:
            if session_created and self.close_session and self.session:
                await self.session.close()

        raise errors.Request.Failed(last_exc)


import base64
import struct
import hmac
import hashlib
import time
import json
from dataclasses import dataclass
from typing import Union, Literal
import errors
from models import request

@dataclass
class ChallangeData:
    rblx_challange_id: str
    rblx_challange_metadata: str
    rblx_challange_type: Union[Literal["twostepverification"], str]

class AutoPass:
    def __init__(self, secret: str):
        self.secret = secret.strip().replace(" ", "")

    @staticmethod
    def _base32_decode(s: str) -> bytes:
        s2 = s.upper()
        # pad
        missing = len(s2) % 8
        if missing:
            s2 += "=" * (8 - missing)
        return base64.b32decode(s2)

    @staticmethod
    def totp(secret: str) -> Union[str, errors.InvalidOtp]:
        if not secret:
            raise errors.InvalidOtp("Empty secret")
        try:
            key = AutoPass._base32_decode(secret)
            timestep = int(time.time()) // 30
            msg = struct.pack(">Q", timestep)
            h = hmac.new(key, msg, hashlib.sha1).digest()
            o = h[19] & 15
            code = (struct.unpack(">I", h[o:o+4])[0] & 0x7fffffff) % 1000000
            return f"{code:06d}"
        except Exception:
            raise errors.InvalidOtp("Failed to generate TOTP")

    async def __call__(self, previous_request: "request.Request", challenge_data: ChallangeData) -> Union["request.Request", errors.InvalidOtp]:
        if challenge_data.rblx_challange_type != "twostepverification":
            raise errors.InvalidChallangeType("Not an authenticator challenge")
        try:
            meta = json.loads(base64.b64decode(challenge_data.rblx_challange_metadata).decode("utf-8"))
        except Exception as e:
            raise errors.InvalidOtp("Invalid challenge metadata")

        code = self.totp(self.secret)
        if not code:
            raise errors.InvalidOtp("Cannot generate otp")
        # Build verification request
        new_req = request.Request(
            url = f"https://twostepverification.roblox.com/v1/users/{previous_request.user_id}/challenges/authenticator/verify",
            method = "post",
            headers = previous_request.headers,
            proxy = previous_request.proxy,
            session = previous_request.session,
            close_session = previous_request.close_session,
            json_data = {
                "challengeId": meta.get("challengeId"),
                "actionType": meta.get("actionType"),
                "code": code
            }
        )
        return new_req
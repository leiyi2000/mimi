import json
import logging

import httpx


log = logging.getLogger(__name__)

USER_AGENT = "lolapp/12.8.1 (Android)"


class MlolError(Exception):
    """User-facing mlol request failure (message is safe to show)."""


class MlolCookies:
    """The cookie-based auth header the mlol container expects on /go/* calls.

    Mirrors com.tencent.container.d.a.a: identity travels in the Cookie header,
    not the JSON body. QQ login uses acctype=qc / accountType=5.
    """

    def __init__(
        self,
        *,
        openid: str,
        access_token: str,
        user_id: str = "",
        uin: str = "",
        tid: str = "",
        appid: str = "100543809",
    ) -> None:
        self.openid = openid
        self.access_token = access_token
        self.user_id = user_id
        self.uin = uin
        self.tid = tid
        self.appid = appid

    def header(self) -> str:
        parts = [("clientType", "9")]
        if self.uin:
            parts.append(("uin", f"o{self.uin}"))
            parts.append(("appid", self.appid))
            parts.append(("acctype", "qc"))
        parts.append(("openid", self.openid))
        parts.append(("access_token", self.access_token))
        if self.user_id:
            parts.append(("userId", self.user_id))
            parts.append(("accountType", "5"))
        if self.tid:
            parts.append(("tid", self.tid))
        return "; ".join(f"{k}={v}" for k, v in parts) + ";"


class MlolClient:
    """Thin mlol.qt.qq.com client.

    URL = https://<host><path>; unified {result, msg, data} response
    where result == 0 is success (list endpoints also accept 100 = guest-limited).
    """

    def __init__(self, *, host: str = "mlol.qt.qq.com", timeout: float = 15.0) -> None:
        self.host = host.rstrip("/")
        self.timeout = timeout

    async def get(
        self,
        path: str,
        params: dict,
        *,
        cookies: MlolCookies | None = None,
        allow_guest: bool = False,
    ) -> dict | list:
        parsed = await self.get_envelope(
            path,
            params,
            cookies=cookies,
            allow_guest=allow_guest,
        )
        return parsed.get("data") or {}

    async def get_envelope(
        self,
        path: str,
        params: dict,
        *,
        cookies: MlolCookies | None = None,
        allow_guest: bool = False,
    ) -> dict:
        headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
        if cookies is not None:
            headers["Cookie"] = cookies.header()
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(
                f"https://{self.host}{path}",
                params=params,
                headers=headers,
            )
        response.raise_for_status()
        return self._envelope(response.json(), allow_guest)

    async def post(
        self,
        path: str,
        body: dict,
        *,
        cookies: MlolCookies | None = None,
        allow_guest: bool = False,
    ) -> dict | list:
        parsed = await self.post_envelope(
            path,
            body,
            cookies=cookies,
            allow_guest=allow_guest,
        )
        return parsed.get("data") or {}

    async def post_envelope(
        self,
        path: str,
        body: dict,
        *,
        cookies: MlolCookies | None = None,
        allow_guest: bool = False,
    ) -> dict:
        url = f"https://{self.host}{path}"
        headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
        if cookies is not None:
            headers["Cookie"] = cookies.header()
        payload = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(url, content=payload, headers=headers)
        response.raise_for_status()
        return self._envelope(response.json(), allow_guest)

    async def post_form(
        self,
        path: str,
        fields: dict[str, str | int],
        *,
        cookies: MlolCookies | None = None,
        allow_guest: bool = False,
    ) -> dict | list:
        headers = {"User-Agent": USER_AGENT}
        if cookies is not None:
            headers["Cookie"] = cookies.header()
        files = {
            key: (None, str(value))
            for key, value in fields.items()
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"https://{self.host}{path}",
                files=files,
                headers=headers,
            )
        response.raise_for_status()
        return self._data(response.json(), allow_guest)

    @staticmethod
    def _data(parsed: object, allow_guest: bool) -> dict | list:
        return MlolClient._envelope(parsed, allow_guest).get("data") or {}

    @staticmethod
    def _envelope(parsed: object, allow_guest: bool) -> dict:
        if not isinstance(parsed, dict):
            raise MlolError("掌盟返回格式异常。")
        result = parsed.get("result", -1)
        ok = result == 0 or (allow_guest and result == 100)
        if not ok:
            msg = parsed.get("msg") or f"result={result}"
            raise MlolError(f"掌盟接口失败：{msg}")
        return parsed

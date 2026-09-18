import logging
from datetime import UTC, datetime, timedelta

from models import MlolSession
from mlol.client import MlolClient, MlolError, MlolCookies


log = logging.getLogger(__name__)

CLIENT_TYPE = 9
MAPP_ID = 10011
QQ_APP_ID = 100543809
SHARED_SESSION_ID = 1


class MlolAuth:
    """QQ -> mlol login exchange and ticket refresh, backed by MlolSession."""

    def __init__(self, client: MlolClient, *, device_model: str = "23049RAD8C") -> None:
        self.client = client
        self.device_model = device_model  # cosmetic client_dev_name for login

    async def login_by_qq(
        self, *, mcode: str, openid: str, access_token: str
    ) -> tuple[MlolSession, str]:
        """Exchange QQ OAuth for the plugin's single shared mlol session."""
        body = {
            "clienttype": CLIENT_TYPE,
            "mappid": MAPP_ID,
            "mcode": mcode,
            "config_params": {"client_dev_name": self.device_model, "lang_type": 0},
            "login_info": {
                "appid": QQ_APP_ID,
                "openid": openid,
                "qq_info_type": 5,
                "sig": access_token,
                "uin": 0,
            },
        }
        data = await self.client.post("/go/auth/login_by_qq", body)
        login_info = data.get("login_info") if isinstance(data, dict) else None
        if not isinstance(login_info, dict):
            raise MlolError("掌盟登录响应缺少 login_info。")
        if not login_info.get("ct") or not login_info.get("user_id"):
            raise MlolError("掌盟登录响应缺少有效票据。")

        now = datetime.now(UTC)
        ct_span = int(login_info.get("refresh_ct_span") or 0)
        wt_span = int(login_info.get("refresh_wt_span") or 0)
        mlol_user_id = str(login_info["user_id"])
        session = await MlolSession.update_or_create(
            user_id=SHARED_SESSION_ID,
            defaults={
                "openid": login_info.get("third_openid") or openid,
                "access_token": access_token,
                "ct": login_info["ct"],
                "ctt": login_info.get("ctt"),
                "wt": login_info.get("wt"),
                "sk": login_info.get("sk"),
                "uin": str(login_info.get("uin") or ""),
                "tid": login_info.get("tid"),
                "mlol_user_id": mlol_user_id,
                "ct_refresh_at": now + timedelta(seconds=ct_span) if ct_span else None,
                "wt_refresh_at": now + timedelta(seconds=wt_span) if wt_span else None,
                "expired": False,
            },
        )
        return session[0], mlol_user_id

    async def session(self) -> MlolSession | None:
        session = await MlolSession.get_or_none(user_id=SHARED_SESSION_ID)
        return None if session and session.expired else session

    async def logout(self) -> None:
        await MlolSession.filter(user_id=SHARED_SESSION_ID).delete()

    def cookies(self, session: MlolSession, mlol_user_id: str = "") -> MlolCookies:
        return MlolCookies(
            openid=session.openid,
            access_token=session.access_token,
            user_id=mlol_user_id or session.mlol_user_id or "",
            uin=session.uin or "",
            tid=session.wt or "",
        )

    async def ensure_fresh(self, session: MlolSession, mlol_user_id: str = "") -> None:
        """Renew tickets on demand, right before a business request.

        Replaces background polling: a ticket is refreshed only when it is at or
        past its server-advised refresh time, so an idle or logged-out user
        makes no requests. Raises MlolError (session marked expired) when a
        ticket can no longer be renewed, so the caller can prompt a re-login.
        """
        mlol_user_id = mlol_user_id or session.mlol_user_id or ""
        if not mlol_user_id:
            return
        now = datetime.now(UTC)
        if session.ct_refresh_at and now >= session.ct_refresh_at:
            await self.refresh_client_ticket(session, mlol_user_id)
        if session.wt_refresh_at and now >= session.wt_refresh_at:
            await self.refresh_web_ticket(session, mlol_user_id)

    async def save_role(
        self,
        session: MlolSession,
        *,
        uuid: str,
        scene: str,
        area_id: int | None,
        area_name: str | None,
    ) -> None:
        """Persist the resolved endgame role onto the session."""
        session.uuid = uuid
        session.scene = scene
        session.area_id = area_id
        session.area_name = area_name
        await session.save()

    async def refresh_web_ticket(self, session: MlolSession, mlol_user_id: str) -> None:
        """Refresh the short-lived web ticket (wt, ~30 min)."""
        body = {
            "config_params": {"lang_type": 0},
            "ct": session.ct,
            "user_id": mlol_user_id,
        }
        try:
            data = await self.client.post("/go/auth/get_web_ticket", body)
        except MlolError:
            session.expired = True
            await session.save()
            raise
        wt = data.get("wt")
        wt_span = int(data.get("refresh_wt_span") or 0)
        session.wt = wt or session.wt
        session.wt_refresh_at = (
            datetime.now(UTC) + timedelta(seconds=wt_span) if wt_span else None
        )
        await session.save()

    async def refresh_client_ticket(self, session: MlolSession, mlol_user_id: str) -> None:
        """Refresh the long-lived client ticket (ct)."""
        body = {"config_params": {"lang_type": 0}, "ct": session.ct, "user_id": mlol_user_id}
        try:
            data = await self.client.post("/go/auth/refresh_client_ticket", body)
        except MlolError:
            session.expired = True
            await session.save()
            raise
        ct = data.get("ct")
        ct_span = int(data.get("refresh_ct_span") or 0)
        session.ct = ct or session.ct
        session.ct_refresh_at = (
            datetime.now(UTC) + timedelta(seconds=ct_span) if ct_span else None
        )
        await session.save()

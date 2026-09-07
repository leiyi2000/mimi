import os
from dataclasses import dataclass, field


def _normalize_cookie(raw: str) -> str:
    """Accept either a full cookie string or a bare MUSIC_U value."""
    raw = raw.strip()
    if not raw:
        return ""
    if "MUSIC_U=" in raw or "=" in raw:
        return raw
    return f"MUSIC_U={raw};"


@dataclass(frozen=True)
class Config:
    napcat_host: str
    napcat_port: int
    napcat_token: str | None
    netease_api: str
    sign_api: str
    sign_key: str
    cookie_file: str
    cookie: str = ""
    admins: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def from_env(cls) -> "Config":
        admins = {
            a.strip() for a in os.getenv("MUSIC_ADMINS", "").split(",") if a.strip()
        }
        return cls(
            napcat_host=os.getenv("NAPCAT_HOST", "127.0.0.1"),
            napcat_port=int(os.getenv("NAPCAT_PORT", "3001")),
            napcat_token=os.getenv("NAPCAT_TOKEN") or None,
            netease_api=os.getenv("NETEASE_API", "http://ncm-api:3000"),
            sign_api=os.getenv(
                "MUSIC_SIGN_API", "https://apii.xianyuw.cn/api/v1/qq-musicArk"
            ),
            sign_key=os.getenv("MUSIC_SIGN_KEY", ""),
            cookie_file=os.getenv("NETEASE_COOKIE_FILE", "data/netease_cookie.txt"),
            cookie=_normalize_cookie(os.getenv("NETEASE_COOKIE", "")),
            admins=frozenset(admins),
        )


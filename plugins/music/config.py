import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    napcat_host: str
    napcat_port: int
    napcat_token: str | None
    netease_api: str
    sign_api: str
    sign_key: str

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            napcat_host=os.getenv("NAPCAT_HOST", "127.0.0.1"),
            napcat_port=int(os.getenv("NAPCAT_PORT", "3001")),
            napcat_token=os.getenv("NAPCAT_TOKEN") or None,
            netease_api=os.getenv("NETEASE_API", "http://ncm-api:3000"),
            sign_api=os.getenv(
                "MUSIC_SIGN_API", "https://apii.xianyuw.cn/api/v1/qq-musicArk"
            ),
            sign_key=os.getenv("MUSIC_SIGN_KEY", ""),
        )

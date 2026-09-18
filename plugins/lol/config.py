import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Config:
    napcat_host: str
    napcat_port: int
    napcat_token: str | None
    mlol_host: str
    qimei_url: str
    db_url: str
    admins: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def from_env(cls) -> "Config":
        admins = {
            value.strip()
            for value in os.getenv("ADMINS", "").split(",")
            if value.strip()
        }
        return cls(
            napcat_host=os.getenv("NAPCAT_HOST", "127.0.0.1"),
            napcat_port=int(os.getenv("NAPCAT_PORT", "3001")),
            napcat_token=os.getenv("NAPCAT_TOKEN") or None,
            mlol_host=os.getenv("MLOL_HOST", "mlol.qt.qq.com"),
            qimei_url=os.getenv("QIMEI_URL", "http://qimei:8080"),
            db_url=os.getenv("LOL_DATABASE_URL", "sqlite://data/lol.db"),
            admins=frozenset(admins),
        )

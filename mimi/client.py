import os
import logging

from napcat import NapCatClient
from napcat.connection import Connection


log = logging.getLogger(__name__)


class Client(NapCatClient):
    def __init__(
        self,
        ws_url: str | None = None,
        token: str | None = None,
        _existing_conn: Connection | None = None,
        rpc_mode: bool = False,
        rpc_host: str = "0.0.0.0",
        rpc_port: int = 0,
        rpc_token: str | None = None,
        rpc_public_host: str | None = None,
    ):
        super().__init__(
            ws_url,
            token,
            _existing_conn,
            rpc_mode,
            rpc_host,
            rpc_port,
            rpc_token,
            rpc_public_host,
        )

    @classmethod
    def load_from_env(cls):
        host = os.getenv("NAPCAT_HOST", "127.0.0.1")
        port = int(os.getenv("NAPCAT_PORT", 3001))
        token = os.getenv("NAPCAT_TOKEN", None)
        client_type = os.getenv("NAPCAT_CLIENT_TYPE", "ws")

        log.info(
            f"Loading client from env, host={host}, port={port}, token={token}, client_type={client_type}"
        )

        if client_type == "ws":
            ws_url = f"ws://{host}:{port}"
            return cls(ws_url, token)

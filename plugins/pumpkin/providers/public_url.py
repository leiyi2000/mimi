from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit


class PublicUrlError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


AddressResolver = Callable[[str, int], Awaitable[Sequence[str]]]


@dataclass(frozen=True, slots=True)
class ValidatedPublicUrl:
    url: str
    host: str
    port: int
    addresses: tuple[str, ...]


class PublicUrlGuard:
    def __init__(self, resolver: AddressResolver | None = None) -> None:
        self._resolver = resolver or self._resolve_addresses

    async def validate(self, url: str) -> ValidatedPublicUrl:
        value = url.strip()
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or any(char in value for char in "\r\n\x00")
        ):
            raise PublicUrlError("PUBLIC_URL_INVALID")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        try:
            raw_addresses = await self._resolver(parsed.hostname, port)
            addresses = tuple(dict.fromkeys(raw_addresses))
        except PublicUrlError:
            raise
        except (OSError, ValueError, UnicodeError) as exc:
            raise PublicUrlError("PUBLIC_URL_DNS_FAILED") from exc
        if not addresses:
            raise PublicUrlError("PUBLIC_URL_DNS_FAILED")
        try:
            if any(
                not ipaddress.ip_address(address).is_global
                for address in addresses
            ):
                raise PublicUrlError("PUBLIC_URL_PRIVATE_ADDRESS")
        except PublicUrlError:
            raise
        except ValueError as exc:
            raise PublicUrlError("PUBLIC_URL_DNS_FAILED") from exc
        return ValidatedPublicUrl(
            url=value,
            host=parsed.hostname,
            port=port,
            addresses=addresses,
        )

    @staticmethod
    async def _resolve_addresses(host: str, port: int) -> tuple[str, ...]:
        records = await asyncio.to_thread(
            socket.getaddrinfo,
            host,
            port,
            type=socket.SOCK_STREAM,
        )
        return tuple(record[4][0] for record in records)

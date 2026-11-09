from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit


_MAX_HEADER_BYTES = 64 * 1024
_RELAY_BYTES = 64 * 1024


class PublicProxyError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PublicEndpoint:
    host: str
    port: int
    address: str


class PublicEgressProxy:
    def __init__(self) -> None:
        self._server: asyncio.AbstractServer | None = None
        self._connections: set[asyncio.Task[None]] = set()

    @property
    def url(self) -> str:
        if self._server is None or not self._server.sockets:
            raise RuntimeError("public egress proxy is not running")
        port = int(self._server.sockets[0].getsockname()[1])
        return f"http://127.0.0.1:{port}"

    async def start(self) -> None:
        if self._server is not None:
            raise RuntimeError("public egress proxy already started")
        self._server = await asyncio.start_server(
            self._accept,
            host="127.0.0.1",
            port=0,
            limit=_MAX_HEADER_BYTES,
        )

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        for task in tuple(self._connections):
            task.cancel()
        if self._connections:
            await asyncio.gather(*self._connections, return_exceptions=True)
        self._connections.clear()

    async def _accept(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        task = asyncio.create_task(self._handle(reader, writer))
        self._connections.add(task)
        task.add_done_callback(self._finish_connection)

    def _finish_connection(self, task: asyncio.Task[None]) -> None:
        self._connections.discard(task)
        if not task.cancelled():
            task.exception()

    async def _handle(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> None:
        upstream_writer: asyncio.StreamWriter | None = None
        try:
            header = await asyncio.wait_for(
                client_reader.readuntil(b"\r\n\r\n"),
                timeout=10,
            )
            if len(header) > _MAX_HEADER_BYTES:
                raise PublicProxyError("proxy request headers are too large")
            request_line, headers = self._parse_header(header)
            method, target, version = request_line
            if method == "CONNECT":
                endpoint = await self.resolve_target(target, default_port=443)
                upstream_reader, upstream_writer = await asyncio.open_connection(
                    endpoint.address,
                    endpoint.port,
                )
                client_writer.write(
                    b"HTTP/1.1 200 Connection Established\r\n"
                    b"Connection: keep-alive\r\n\r\n"
                )
            else:
                parsed = urlsplit(target)
                if parsed.scheme != "http" or not parsed.hostname:
                    raise PublicProxyError("proxy only accepts HTTP absolute-form requests")
                endpoint = await self.resolve_host(
                    parsed.hostname,
                    parsed.port or 80,
                )
                upstream_reader, upstream_writer = await asyncio.open_connection(
                    endpoint.address,
                    endpoint.port,
                )
                path = parsed.path or "/"
                if parsed.query:
                    path = f"{path}?{parsed.query}"
                upstream_writer.write(
                    self._origin_request(method, path, version, headers)
                )
                await upstream_writer.drain()
            await self._relay_bidirectional(
                client_reader,
                client_writer,
                upstream_reader,
                upstream_writer,
            )
        except (
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
            OSError,
            PublicProxyError,
            TimeoutError,
            ValueError,
        ):
            if not client_writer.is_closing():
                client_writer.write(
                    b"HTTP/1.1 403 Forbidden\r\n"
                    b"Connection: close\r\n"
                    b"Content-Length: 0\r\n\r\n"
                )
                try:
                    await client_writer.drain()
                except (ConnectionError, OSError):
                    pass
        finally:
            if upstream_writer is not None:
                upstream_writer.close()
                await asyncio.gather(
                    upstream_writer.wait_closed(),
                    return_exceptions=True,
                )
            client_writer.close()
            await asyncio.gather(
                client_writer.wait_closed(),
                return_exceptions=True,
            )

    @staticmethod
    def _parse_header(
        header: bytes,
    ) -> tuple[tuple[str, str, str], tuple[tuple[str, str], ...]]:
        text = header.decode("iso-8859-1")
        lines = text.split("\r\n")
        parts = lines[0].split(" ")
        if len(parts) != 3:
            raise PublicProxyError("invalid proxy request line")
        method, target, version = parts
        if not method.isascii() or not version.startswith("HTTP/1."):
            raise PublicProxyError("invalid proxy request")
        headers: list[tuple[str, str]] = []
        for line in lines[1:]:
            if not line:
                continue
            name, separator, value = line.partition(":")
            if not separator or not name:
                raise PublicProxyError("invalid proxy header")
            headers.append((name.strip(), value.lstrip()))
        return (method.upper(), target, version), tuple(headers)

    @staticmethod
    def _origin_request(
        method: str,
        path: str,
        version: str,
        headers: tuple[tuple[str, str], ...],
    ) -> bytes:
        lines = [f"{method} {path} {version}"]
        for name, value in headers:
            if name.lower() in {"proxy-authorization", "proxy-connection"}:
                continue
            lines.append(f"{name}: {value}")
        return ("\r\n".join(lines) + "\r\n\r\n").encode("iso-8859-1")

    @classmethod
    async def resolve_target(
        cls,
        target: str,
        *,
        default_port: int,
    ) -> PublicEndpoint:
        parsed = urlsplit(f"//{target}")
        if (
            not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise PublicProxyError("invalid proxy target")
        return await cls.resolve_host(
            parsed.hostname,
            parsed.port or default_port,
        )

    @staticmethod
    async def resolve_host(host: str, port: int) -> PublicEndpoint:
        if not 1 <= port <= 65535:
            raise PublicProxyError("invalid proxy port")
        records = await asyncio.to_thread(
            socket.getaddrinfo,
            host,
            port,
            type=socket.SOCK_STREAM,
        )
        addresses = tuple(dict.fromkeys(record[4][0] for record in records))
        if not addresses:
            raise PublicProxyError("proxy target did not resolve")
        parsed = tuple(ipaddress.ip_address(address) for address in addresses)
        if any(not address.is_global for address in parsed):
            raise PublicProxyError("proxy target is not public")
        return PublicEndpoint(host=host, port=port, address=str(parsed[0]))

    @staticmethod
    async def _relay_bidirectional(
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        upstream_reader: asyncio.StreamReader,
        upstream_writer: asyncio.StreamWriter,
    ) -> None:
        async def relay(
            source: asyncio.StreamReader,
            destination: asyncio.StreamWriter,
        ) -> None:
            while data := await source.read(_RELAY_BYTES):
                destination.write(data)
                await destination.drain()

        tasks = {
            asyncio.create_task(relay(client_reader, upstream_writer)),
            asyncio.create_task(relay(upstream_reader, client_writer)),
        }
        done, pending = await asyncio.wait(
            tasks,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*done, *pending, return_exceptions=True)

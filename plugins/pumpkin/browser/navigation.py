from urllib.parse import urlsplit


class BrowserUrlError(ValueError):
    code = "BROWSER_URL_REJECTED"


class BrowserUrl:
    @staticmethod
    def validate(url: str) -> None:
        try:
            parsed = urlsplit(url)
            if (
                not isinstance(url, str) or not 1 <= len(url) <= 2_000
                or parsed.scheme not in {"http", "https"} or not parsed.hostname
                or any(char in url for char in "\r\n\x00")
                or parsed.port is not None and not 1 <= parsed.port <= 65535
            ):
                raise ValueError("invalid HTTP(S) URL")
        except (AttributeError, TypeError, ValueError) as exc:
            raise BrowserUrlError("navigation requires a valid HTTP(S) URL") from exc

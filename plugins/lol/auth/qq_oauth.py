import time
import hashlib
from urllib.parse import urlencode, urlsplit, parse_qs

from .device import DeviceInfo


AUTHORIZE_URL = "https://openmobile.qq.com/oauth2.0/m_authorize"
APP_ID = "100543809"
PACKAGE_NAME = "com.tencent.qt.qtl"
SDK_VERSION = "3.5.17.lite"
# Verified signing certificate MD5 for com.tencent.qt.qtl 12.8.1. Embedding it
# keeps URL generation pure Python with zero APK dependency at runtime.
CERT_MD5 = "4fbb147f3a7bea78fb36cb38a63e92fb"


class OAuthError(RuntimeError):
    """User-facing QQ authorization failure."""


def build_authorize_url(device: DeviceInfo, *, timestamp: int | None = None) -> str:
    """Build the QQ OpenSDK web authorization URL (QR-friendly for desktop).

    `device` comes from the same device profile the QIMEI36 is registered for, so
    the authorization page shows the device the mlol login will run as.
    """
    ts = int(time.time()) if timestamp is None else timestamp
    sign = hashlib.md5(f"{PACKAGE_NAME}_{CERT_MD5}_{ts}".encode()).hexdigest()
    params = [
        ("format", "json"),
        ("status_os", device.android_version),
        ("status_machine", device.model),
        ("status_version", str(device.api_level)),
        ("sdkv", SDK_VERSION),
        ("sdkp", "a"),
        ("pf", "openmobile_android"),
        ("isadd", "1"),
        ("scope", "all"),
        ("client_id", APP_ID),
        ("sign", sign),
        ("time", str(ts)),
        ("display", "mobile"),
        ("response_type", "token"),
        ("redirect_uri", "auth://tauth.qq.com/"),
        ("cancel_display", "1"),
        ("switch", "1"),
        ("compat_v", "1"),
        ("show_download_ui", "false"),
        ("style", "qr"),
    ]
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def parse_callback(url: str) -> tuple[str, str]:
    """Return (access_token, openid) from the QQ auth callback URL.

    The callback carries the token in the fragment, e.g.
    ``auth://tauth.qq.com/#access_token=...&expires_in=...&openid=...``.
    """
    split = urlsplit(url.strip())
    payload = split.fragment or split.query
    if not payload:
        raise OAuthError("回调链接缺少授权信息，请复制完整的回调 URL。")
    query = parse_qs(payload)
    access_token = (query.get("access_token") or [""])[0]
    openid = (query.get("openid") or [""])[0]
    if not access_token or not openid:
        raise OAuthError("回调链接未包含 access_token/openid，请确认授权已完成。")
    return access_token, openid

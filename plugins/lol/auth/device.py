import json
import uuid
import random
import hashlib
import logging
from pathlib import Path
from dataclasses import asdict, dataclass, fields


log = logging.getLogger(__name__)

APP_KEY = "0AND0GZQH44TDFUA"
SDK_VERSION = "5.1.2.132"
APP_VERSION = "12.8.1"

# Real-looking handset templates; fabricate() randomizes only per-install ids so a
# generated profile looks like a device without embedding a real person's.
_DEVICE_TEMPLATES = (
    {
        "android_api": 34,
        "android_release": "14",
        "model": "PJD110",
        "brand": "OPPO",
        "board": "pineapple",
        "device": "OP5929L1",
        "manufacturer": "OPPO",
        "product_name": "PJD110",
        "first_api_level": "33",
    },
    {
        "android_api": 34,
        "android_release": "14",
        "model": "V2309A",
        "brand": "vivo",
        "board": "pineapple",
        "device": "V2309A",
        "manufacturer": "vivo",
        "product_name": "V2309A",
        "first_api_level": "33",
    },
    {
        "android_api": 33,
        "android_release": "13",
        "model": "2210132C",
        "brand": "Redmi",
        "board": "kalama",
        "device": "kalama",
        "manufacturer": "Xiaomi",
        "product_name": "kalama",
        "first_api_level": "33",
    },
)


@dataclass(frozen=True)
class DeviceInfo:
    android_version: str
    api_level: int
    model: str


@dataclass(frozen=True)
class DeviceProfile:
    """Device fingerprint the QIMEI36 registration is bound to.

    Owned by this plugin: loaded from a JSON file, or fabricated + persisted on
    first use. Feeds both the native config sent to the qimei service and the QQ
    OAuth display fields, so both present the same device.
    """

    android_api: int = 0
    target_sdk: int = 30
    network_type: str = "WIFI"
    android_release: str = ""
    model: str = ""
    channel_id: str = "official"
    android_id: str = ""
    brand: str = ""
    board: str = ""
    device: str = ""
    first_api_level: str = ""
    manufacturer: str = ""
    product_name: str = ""
    build_host: str = ""
    kernel: str = ""
    device_type: str = "Phone"
    local_ip: str = ""
    boot_id: str = ""
    sdk_version: str = SDK_VERSION
    app_version: str = APP_VERSION
    user_id_param: str = ""
    oaid: str = ""
    imei: str = ""
    imsi: str = ""
    mac: str = ""
    cid: str = ""
    first_uptimes: str = ""
    pre_audit_state: str = "0"
    oz: str = ""

    @classmethod
    def _from_mapping(cls, data: dict) -> "DeviceProfile":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    @classmethod
    def resolve(cls, path: Path) -> "DeviceProfile":
        """Load the profile at `path`, or fabricate one and persist it there."""
        if path.is_file():
            try:
                return cls._from_mapping(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                log.warning("bad device profile at %s; refabricating", path)
        profile = cls.fabricate()
        profile.persist(path)
        return profile

    @classmethod
    def fabricate(cls) -> "DeviceProfile":
        template = dict(random.choice(_DEVICE_TEMPLATES))
        android_id = "".join(random.choices("0123456789abcdef", k=16))
        api = template["android_api"]
        return cls(
            **template,
            android_id=android_id,
            boot_id=str(uuid.uuid4()),
            local_ip=f"192.168.{random.randint(0, 255)}.{random.randint(2, 254)}",
            build_host="localhost",
            kernel=f"Linux localhost 5.15.0-android{api}-0 #1 SMP PREEMPT aarch64",
        )

    def persist(self, path: Path) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(asdict(self), ensure_ascii=True, indent=2), encoding="utf-8")
            log.info("device profile written to %s", path)
        except OSError as exc:
            log.warning("could not persist device profile: %s", exc)

    def to_native_config(self) -> dict:
        build_info = {
            "harmony": "0",
            "clone": "0",
            "containe": "",
            "oz": self.oz,
            "oz2": "",
            "oo": "",
            "kelong": "0",
            "ip": self.local_ip,
            "multiUser": "0",
            "bod": self.board,
            "brd": self.brand,
            "dv": self.device,
            "firstLevel": self.first_api_level,
            "manufact": self.manufacturer,
            "name": self.product_name,
            "host": self.build_host,
            "kernel": self.kernel,
            "pre": "0",
            "av": self.app_version,
            "ch": self.channel_id,
        }
        device_info = {
            "1": self.channel_id,
            "2": str(self.target_sdk),
            "3": self.user_id_param,
            "4": self.brand,
            "5": "",
            "6": self.oaid,
            "7": self.imei,
            "8": self.imsi,
            "9": self.android_id,
            "10": self.mac,
            "11": self.cid,
            "12": json.dumps(build_info, separators=(",", ":"), ensure_ascii=True),
            "13": self.local_ip,
            "14": self.device_type,
            "15": self.first_uptimes,
            "16": self.boot_id,
        }
        sdk_info = [
            APP_KEY,
            self.network_type,
            self.sdk_version,
            self.app_version,
            self.channel_id,
            self.user_id_param,
            f"Android {self.android_release},level {self.android_api}",
            "",
            "",
            self.model,
            self.pre_audit_state,
            self.oz,
        ]
        return {"androidApi": self.android_api, "deviceInfo": device_info, "sdkInfo": sdk_info}

    def digest(self) -> str:
        serialized = json.dumps(
            self.to_native_config(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")
        return hashlib.sha256(serialized).hexdigest()

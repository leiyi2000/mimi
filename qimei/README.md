# qimei service

Stateless QIMEI36 registration microservice for 掌上英雄联盟 (com.tencent.qt.qtl 12.8.1).

QIMEI36 is the `mcode` the mlol login requires. It cannot be produced in pure
Python/Java — Tencent's native `libqimei.so` signs the request. This service runs
that native library under Unidbg (ARM64 emulated in software, so it works on any
host arch) and returns the QIMEI36.

**Stateless by design.** The device fingerprint and the QIMEI36 cache are owned by
the caller (the lol plugin). This service holds no device state: each request
carries a native-config JSON, the service emulates the native lib and returns the
QIMEI36. It only keeps a local copy of the 12.8.1 APK (needed by Unidbg) so it need
not re-download it every run.

## HTTP API

| Method | Path | Description |
|---|---|---|
| `GET` | `/healthz` | liveness → `{"status":"ok"}` |
| `POST` | `/qimei36` | body = native config; runs native register → `{"qimei36":"..."}` |

Request body (produced by the caller's `DeviceProfile.to_native_config()`):

```json
{"androidApi": 34, "deviceInfo": {"1": "...", "...": "..."}, "sdkInfo": ["0AND0GZQH44TDFUA", "...12 items"]}
```

Requests are serialized (single-thread executor) so the heavy native run never races.

## Config (env)

| Env | Default | Purpose |
|---|---|---|
| `QIMEI_PORT` | `8080` | HTTP listen port |
| `QIMEI_DATA_DIR` | `/data` | where the downloaded APK is cached |
| `QIMEI_SO_PATH` | `/app/lib/libqimei.so` | bundled native lib |
| `QIMEI_APK_PATH` | — | local APK; else auto-download 12.8.1 |
| `QIMEI_APK_DOWNLOAD_URL` | pinned pp.cn mirror | override (still integrity-checked) |
| `QIMEI_DEBUG` | `false` | native emulation diagnostics on stderr |

The APK is verified against a pinned size + SHA-256 + MD5 before use.

## Build & run

```bash
# via docker-compose (recommended): service name `qimei`
docker compose up -d qimei

# or standalone
mvn -q package
java -jar target/qimei-service.jar
```

The Maven build (see `Dockerfile`) produces a shaded jar `qimei-service.jar` with
`Main-Class: qimei.QimeiServer`.

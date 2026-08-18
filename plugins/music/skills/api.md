# 网易云音乐 VIP 解析接口文档

> 来源站点：`https://tools.qzxdp.cn/wyy_vip`
> 说明：将网易云歌曲链接或歌曲 ID 解析为可播放/下载的直链。

## 接口概览

| 项目 | 内容 |
|------|------|
| URL | `https://tools.qzxdp.cn/api/wyy_vip/parse` |
| 方法 | `POST` |
| 请求格式 | `application/x-www-form-urlencoded` |
| 返回格式 | `application/json` |
| 鉴权 | 无需 token |

## 请求参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `url` | string | 是 | 网易云歌曲链接或纯数字歌曲 ID（如 `5256015`）。支持 `music.163.com` 与 `163cn.tv` 短链，也可直接传 ID |
| `musicType` | string | 是 | 音质等级，见下表 |

### musicType 可选值

| 值 | 音质 | 权限 |
|------|------|------|
| `standard` | 标准音质 | 免费 |
| `exhigh` | 极高音质 | 免费 |
| `lossless` | 无损音质 | VIP |
| `hires` | Hi-Res 音质 | VIP |
| `jyeffect` | 高清环绕声 | VIP |
| `sky` | 沉浸环绕声 | SVIP |
| `jymaster` | 超清母带 | SVIP |

> 注：当所请求音质无对应音源时，服务端会自动回落到该曲目最高可用音质，实际音质以返回的 `data.level` 为准。

## 返回字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `status` | string | 状态：`ok` 成功；`limit` 成功但有频率/次数限制提示；其他为失败 |
| `message` | string | 提示信息；`status` 非成功时为错误原因 |
| `data.name` | string | 歌曲名 |
| `data.pic` | string | 封面图 URL |
| `data.level` | string | 实际解析到的音质 |
| `data.ar_name` | string | 艺术家 |
| `data.al_name` | string | 专辑名 |
| `data.size` | string | 文件大小 |
| `data.url` | string | 音频直链（带签名参数 `vuutv`，有时效性，过期需重新解析） |

## 请求示例

### cURL

```bash
curl "https://tools.qzxdp.cn/api/wyy_vip/parse" \
  -H "Referer: https://tools.qzxdp.cn/wyy_vip" \
  -H "X-Requested-With: XMLHttpRequest" \
  --data "url=5256015&musicType=sky"
```

### Python

```python
import requests

def parse_wyy(song, music_type="standard"):
    resp = requests.post(
        "https://tools.qzxdp.cn/api/wyy_vip/parse",
        data={"url": song, "musicType": music_type},
        headers={
            "Referer": "https://tools.qzxdp.cn/wyy_vip",
            "X-Requested-With": "XMLHttpRequest",
        },
        timeout=15,
    )
    return resp.json()

result = parse_wyy("5256015", "sky")
print(result["data"]["url"])
```

## 返回示例

```json
{
  "status": "ok",
  "message": "success",
  "data": {
    "name": "灵气",
    "pic": "https://p1.music.126.net/N_UiOcUFNc6OMYjRyWAUdg==/47279000011457.jpg",
    "level": "无损音质",
    "ar_name": "风潮音乐",
    "al_name": "健康音乐馆-国外代理系列-瑜伽心境界-灵气瑜伽",
    "size": "51.00MB",
    "url": "https://m701.music.126.net/.../....flac?vuutv=..."
  }
}
```

## 前端处理逻辑

1. 用正则 `https?:\/\/\S+` 从输入文本中提取第一个链接。
2. 校验链接域名须包含 `music.163.com` 或 `163cn.tv`，否则退化为用正则 `\b\d+\b` 提取纯数字 ID。
3. 校验通过后以 `{url, musicType}` 提交 POST 请求。

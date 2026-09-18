# Music

网易云音乐搜索与 QQ 音乐卡片插件。歌曲信息和播放地址来自自建
NeteaseCloudMusicApi，卡片签名由外部签名接口生成。

## 命令

| 命令 | 说明 |
| --- | --- |
| `点歌 <歌曲名>[-歌手名]` | 搜索歌曲并发送网易云音乐卡片 |
| `网易云信息` | 查看当前共享登录账号 |
| `网易云登录` | 管理员扫码建立共享登录态 |
| `网易云登录 <MUSIC_U 值>` | 管理员直接设置共享登录态 |
| `网易云登出` | 管理员清除共享登录态 |

`网易云登录`、`网易云登出` 仅允许 `ADMINS` 中的 QQ 号使用。登录态由插件持久化，
所有群成员共享，不需要分别登录。

## 配置

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `NETEASE_API` | `http://ncm-api:3000` | 网易云 API 服务地址 |
| `MUSIC_SIGN_API` | `https://apii.xianyuw.cn/api/v1/qq-musicArk` | QQ 音乐卡片签名接口 |
| `MUSIC_SIGN_KEY` | 空 | 咸鱼 API 个人中心 Token |
| `NETEASE_COOKIE` | 空 | 可选的初始网易云登录 Cookie |
| `NETEASE_COOKIE_FILE` | `data/netease_cookie.txt` | 运行时登录态文件 |

这两个 Cookie 变量通常都不需要写入 `.env`。管理员执行 `网易云登录` 或
`网易云登录 <MUSIC_U 值>` 后，插件会自动保存登录态。

`MUSIC_SIGN_KEY` 可在 [咸鱼 API 开放平台](https://apii.xianyuw.cn/api/qq-musicArk)
免费申请。`NETEASE_COOKIE` 的规范格式为 `MUSIC_U=<value>`：登录网易云 Web 端后，
在浏览器开发者工具的 Cookies 中读取 `MUSIC_U`。它是账号凭据，不能提交到 Git、日志、
Issue 或测试；更推荐留空并使用 `网易云登录` 扫码。

完整环境变量和安全要求见 [部署规范](../../docs/spec/deploy-spec.md)。

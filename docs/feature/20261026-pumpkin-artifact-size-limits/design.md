---
status: implemented
owner: Mimi maintainers
updated: 2026-10-26
commits: []
---

# Pumpkin 图片与代码产物上限

## 1. 背景

Pumpkin 当前对不同产物使用以下单文件限制：

- Artifact：20 MiB；
- 代码沙箱文件：5 MiB，单次总量 20 MiB；
- 网页图片：默认 5 MiB，Provider 硬上限 20 MiB；
- NapCat 发送：本地文件读取后转为 Base64，通过 50 MiB WebSocket payload 发送。

网页图片和代码产物会在进入 Artifact 前被更小的 5 MiB 上限拦截。本特性只把这两个入口
提高到现有 Artifact 的 20 MiB 上限，不改变 Artifact、NapCat 发送或多产物交付设计。

## 2. 设计结论

### 2.1 网页图片

- `WebConfig.max_image_bytes` 默认值从 5 MiB 改为 20 MiB。
- `PUMPKIN_IMAGE_FETCH_MAX_BYTES` 的环境回退值和 `.env.example` 同步改为
  `20971520`。
- `WebSearchProvider` 现有 20 MiB 硬上限保持不变。
- 网页正文的 1 MiB 默认上限保持不变。

### 2.2 代码沙箱

- `MAX_FILE_BYTES` 从 5 MiB 改为 20 MiB。
- `MAX_TOTAL_FILE_BYTES` 继续保持 20 MiB。
- `MAX_FILES` 继续保持 8。
- 单个文件可以占满本次执行的全部产物预算；多个文件仍共享 20 MiB 总量。

### 2.3 Artifact 与发送

- `ArtifactService` 的 20 MiB 单文件硬上限保持不变。
- `NapCatSender` 继续使用 Base64，不增加共享卷或流式上传。
- `DeliveryService` 继续沿用当前多产物交付方式。
- 20 MiB 文件转为 Base64 后约 26.7 MiB，单文件低于 NapCat 的 50 MiB WebSocket
  payload；多产物合并后的总 payload 风险不在本次范围内。

## 3. 配置

| 配置或常量 | 当前值 | 目标值 |
| --- | ---: | ---: |
| `PUMPKIN_IMAGE_FETCH_MAX_BYTES` | 5 MiB | 20 MiB |
| `sandbox.MAX_FILE_BYTES` | 5 MiB | 20 MiB |
| `sandbox.MAX_TOTAL_FILE_BYTES` | 20 MiB | 20 MiB |
| Artifact 单文件硬上限 | 20 MiB | 20 MiB |

部署环境若显式设置 `PUMPKIN_IMAGE_FETCH_MAX_BYTES`，需要同步改为 `20971520`。代码沙箱
常量变化需要重建其镜像；Mimi 配置变化需要重启进程。

## 4. 失败行为

- 网页图片超过 20 MiB：保持 `FETCH_TOO_LARGE`。
- 沙箱单文件或累计文件超过 20 MiB：跳过该文件并返回 `truncated=true`。
- Artifact 超过 20 MiB：保持现有 Artifact 过大错误。
- 外部下载、Artifact 写入或发送失败均不得标记任务完成。

## 5. 非目标

- 不提高 Artifact 的 20 MiB 硬上限。
- 不提高代码沙箱单次 20 MiB 总量。
- 不修改 NapCat Base64、共享卷或流式上传协议。
- 不修改多 Artifact 的消息拆分与幂等交付。
- 不改变 QQ 服务端自身的图片或文件限制。

## 6. 验收标准

1. 未设置环境变量时，网页图片默认上限为 20 MiB。
2. 5–20 MiB 的网页图片可以下载并登记为 Artifact。
3. 5–20 MiB 的单个代码沙箱文件可以被收集。
4. 沙箱所有文件累计仍不超过 20 MiB，文件数仍不超过 8。
5. 超过 20 MiB 的网页图片和沙箱文件保持明确拒绝或截断。
6. Artifact、消息媒体、网页正文和 NapCat 发送行为不变。
7. Pumpkin 全量测试、Compose 配置校验和 `git diff --check` 通过。

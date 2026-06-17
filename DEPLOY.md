# 部署说明（NapCat 用 Docker）

面向你的实际环境：**NapCat 跑在 Docker 里**，Hermes Agent（含本插件）跑在宿主机上。

---

## 1. 前置

- Hermes Agent 已安装，能正常 `hermes gateway`（gateway 模式）。
- NapCat 已在 Docker 中登录 QQ。
- 无需额外 Python 依赖（仅用 Hermes 自带的 `aiohttp`）。

---

## 2. NapCat（Docker）开启正向 WebSocket

本插件是 **WS 客户端**，连入 NapCat 的 **正向 WebSocket 服务端**。在 NapCat WebUI → 网络配置里新建一个 **「WebSocket 服务器」**：

- 端口：`3001`（示例）
- 开启「上报自身消息」可不勾（插件已自行过滤自身消息）
- 如需鉴权，设一个 token，记下来填到 `ONEBOT_ACCESS_TOKEN`

容器要把该端口发布到宿主机。`docker run` 加 `-p 3001:3001`，或 compose：

```yaml
services:
  napcat:
    image: mlikiowa/napcat-docker:latest
    ports:
      - "3001:3001"        # 正向 WS 端口，发布到宿主机
      - "6099:6099"        # WebUI（可选）
    # ... 其余 NapCat 配置
```

> 关键点：插件不需要访问容器内的文件系统（见 §6），所以**不需要为收发媒体做任何卷挂载**。

---

## 3. 安装插件

```bash
git clone https://github.com/MiwooMiwoo/qq-hermes-bridge.git
ln -s "$(pwd)/qq-hermes-bridge/napcat" ~/.hermes/plugins/napcat
```

在 Hermes 配置里开启插件（用户安装的平台插件受此开关控制）：

```yaml
# ~/.hermes/config.yaml
plugins:
  enabled: true
```

---

## 4. 配置环境变量

写入 Hermes 的环境（如 `~/.hermes/.env` 或启动 gateway 的 systemd/shell 环境）：

```bash
# NapCat 正向 WS：宿主机访问容器已发布的端口
ONEBOT_WS_URL=ws://127.0.0.1:3001
ONEBOT_ACCESS_TOKEN=            # 与 NapCat 里设的一致；没设就留空
BOT_QQ=123456789               # 机器人 QQ，用于 @提及检测和自身消息过滤

NAPCAT_REQUIRE_MENTION=true     # 群里仅被 @ 时响应（命令如 /stop 不受限）
NAPCAT_QUOTE_REPLIES=false      # 默认不发 OneBot reply 引用，避免引用图片时每条回复都显示原图预览
NAPCAT_ALLOW_ALL_USERS=false    # 谁能对话由 gateway 统一鉴权
NAPCAT_ALLOWED_USERS=123456789  # 逗号分隔的 QQ 白名单
NAPCAT_HOME_CHANNEL=group:12345 # cron/通知默认投递目标（可选）
```

**网络说明**：
- Hermes 在宿主机、NapCat 在容器并 `-p 3001:3001` → `ws://127.0.0.1:3001`。
- 若 Hermes 也在容器、与 NapCat 同一 docker 网络 → 用服务名或网桥 IP，如 `ws://napcat:3001` 或 `ws://172.17.0.1:3001`。

---

## 5. 启动与验证

```bash
hermes gateway status      # 应能看到 NapCat 平台已配置
hermes gateway             # 启动 gateway（或用你既有的 hermes 常驻方式）
```

逐项手测：
- 群里 `@机器人 你好` → 正常回复；连发两条 → 后一条打断前一条。
- 发一张图片 → 让它描述图片内容（验证收图 + vision）。
- 让它发一张图 / 一个文件 → QQ 端收到（验证发图/发文件）。
- 触发高危命令 → 收到审批提示 → 回 `/approve` 或 `/deny`。

---

## 6. Docker 下图片/文件怎么正常工作（重点）

核心原则：**媒体数据走 WS 内联（base64）或宿主机直连下载，都不碰容器文件系统**，所以 Docker 下无需卷挂载。

| 方向 | 机制 | Docker 是否需要额外配置 |
|------|------|------------------------|
| **接收图片** | 插件在**宿主机侧**用图片段里的 QQ 图床 `url` 下载到本地缓存（`cache_image_from_url`），再交给 agent 的 vision | 否。只需运行 Hermes 的机器能访问外网 |
| **发送图片** | 插件把本地图片读成 `base64://` 放进 image 段，bytes 直接经 WS 传给 NapCat | 否，无需卷挂载 |
| **发送文件** | 走 NapCat 的**分片 Stream API**（`upload_file_stream`）：文件按 256KB 分片、base64 逐片传入容器，NapCat 合并并校验 SHA256 后返回容器内路径，再用该路径发送。旧版 NapCat 无此 API 时自动回退整文件 `base64://` | 否，无需卷挂载 |

也就是说：**常规图片与文件的收发，在你的 Docker 部署下开箱即用，不用做任何路径映射或卷挂载。** 这正是相比旧版（依赖 `/app/napcat/config` ↔ 宿主机路径硬编码映射）的改进点。

### 两个边界情况

1. **收图依赖外网 `url`**：NapCat 默认在图片段给出 QQ 图床的公网 `url`，插件从宿主机下载即可。
   - 前提：运行 Hermes 的机器能联外网。
   - 别把 NapCat 配置成把媒体 URL 改写成容器内网地址；若某张图没有可用 `url`，插件会回退到 `get_image`，但那返回的是**容器内路径**，宿主机读不到——此时该图会被跳过（不影响文字）。保持默认即可。

2. **发送大文件**：文件走分片 Stream API（每片仅 256KB，避开单帧过大问题），常规大小到几十 MB 都没问题。注意 NapCat 默认 >10MB 走磁盘缓存、内存总上限 100MB、流 10 分钟超时——超大文件请确保 NapCat 容器有足够磁盘与时间。若你的 NapCat 版本过旧不支持 `upload_file_stream`，会自动回退整文件 base64（此时超大文件可能受 WS 单帧上限限制）。

---

## 7. 常驻

Hermes gateway 由 Hermes 自身管理（`hermes gateway`）。插件只要放在 `~/.hermes/plugins/napcat/` 且 `plugins.enabled: true`，gateway 启动时会自动发现并加载。按你现有的 Hermes 常驻方式（systemd / 进程管理器）运行 gateway 即可，无需为插件单独建服务。

---

## 8. 排错

- `hermes gateway status` 看不到 NapCat → 检查 `plugins.enabled` 与 `ONEBOT_WS_URL` 是否设置；看 gateway 日志里有无 `NapCat: connected`。
- 连不上 → 确认容器端口已 `-p` 发布、`ONEBOT_WS_URL` 指向宿主机可达地址、token 一致。插件会自动指数退避重连。
- 收图失败 → 多为宿主机网络访问 QQ 图床受限，或 NapCat 改写了媒体 URL。
- 发文件失败且是大文件 → 见 §6 边界情况 2。

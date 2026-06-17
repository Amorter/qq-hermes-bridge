# qq-hermes-bridge

让 [Hermes Agent](https://github.com/NousResearch/hermes-agent) 通过 [NapCat](https://github.com/NapNeko/NapCatQQ)（OneBot v11）接入 QQ 的**原生平台插件**。

不是外挂程序——它是一个 Hermes gateway *平台适配器*（与内置的 Telegram / Discord / 官方 `qqbot` 平级），**零核心改动**，直接在 Hermes 进程内运行，因而天生拥有：

- **图片识别（vision）** — 群友发来的图片自动下载并交给 agent 的视觉能力，和 TUI 一致
- **文件 / 图片发送** — agent 在回复里给出本地路径或 URL，框架自动经 OneBot 原生发送（图片走消息段，文件走 `upload_*_file`）
- **打断** — 新的触发消息、`/stop`、`/new` 会中断正在进行的回合（不再出现并发会话互相打断的旧问题）
- **命令式审批** — 高危命令暂停等待 `/approve` / `/deny`，由框架路由，无死锁
- **按会话的历史** — 由 Hermes 的 session 存储统一管理，插件不维护历史
- **发送者归属** — 框架按已验证的 QQ 号注入发送者身份（见下方安全说明）

> 旧版（v1–v3）是 Node.js 外挂，走 Hermes 的 `api_server` HTTP+SSE。该实现已被本插件取代，原因见提交历史。

## 架构

```
┌──────────┐   OneBot v11    ┌──────────┐   plugin (in-process)   ┌──────────────┐
│ QQ 群/私聊 │ ◄────────────► │  NapCat   │ ◄────────────────────► │ Hermes Agent  │
└──────────┘  正向 WebSocket  │  (服务端) │   napcat 平台适配器      │   (gateway)   │
                              └──────────┘                         └──────────────┘
```

NapCat 运行**正向 WebSocket 服务端**，本插件作为客户端连入，收发消息与调用 OneBot API。

## 要求

- 运行中的 Hermes Agent（gateway 模式）
- NapCat，开启**正向 WebSocket 服务端**
- 无额外 Python 依赖（仅用 Hermes 自带的 `aiohttp`）

## 安装

```bash
git clone https://github.com/Amorter/qq-hermes-bridge.git

# 把插件目录链接到 Hermes 用户插件目录
ln -s "$(pwd)/qq-hermes-bridge/napcat" ~/.hermes/plugins/napcat
```

确保 Hermes 已启用插件（用户安装的平台插件受 `plugins.enabled` 控制）：

```yaml
# ~/.hermes/config.yaml
plugins:
  enabled: true
```

> 完整部署步骤（含 **NapCat 用 Docker** 时图片/文件如何正常收发）见 [DEPLOY.md](DEPLOY.md)。

## 配置

通过环境变量（写入 Hermes 的环境，如 `~/.hermes/.env`）：

| 变量 | 必需 | 说明 |
|------|------|------|
| `ONEBOT_WS_URL` | 是 | NapCat 正向 WS 地址，如 `ws://127.0.0.1:3001` |
| `ONEBOT_ACCESS_TOKEN` | 否 | OneBot access token（NapCat 配了才需要） |
| `BOT_QQ` | 建议 | 机器人 QQ 号，用于 @提及检测与自身消息过滤 |
| `NAPCAT_REQUIRE_MENTION` | 否 | 群聊中是否仅在被 @ 时响应（默认 `true`） |
| `NAPCAT_QUOTE_REPLIES` | 否 | 是否用 OneBot `reply` 段引用用户原消息（默认 `false`，避免引用图片时 QQ 在每条回复上显示原图预览） |
| `NAPCAT_ALLOWED_USERS` | 否 | 允许对话的 QQ 号，逗号分隔 |
| `NAPCAT_ALLOW_ALL_USERS` | 否 | 是否允许所有人（`true`/`false`） |
| `NAPCAT_HOME_CHANNEL` | 否 | cron/通知投递目标，如 `group:12345` |

或在 `config.yaml` 中（env 优先）：

```yaml
gateway:
  platforms:
    napcat:
      enabled: true
      extra:
        ws_url: ws://127.0.0.1:3001
        access_token: ""
        bot_qq: "123456789"
        require_mention: true
        quote_replies: false
```

启动 gateway 后用 `hermes gateway status` 确认 NapCat 平台已配置。

## 使用

- **群聊**：`@机器人 你的问题`（或关掉 `NAPCAT_REQUIRE_MENTION`）
- **私聊**：直接发消息
- **发图片**：直接发，agent 能"看到"图片内容
- **命令**（沿用 Hermes 标准斜杠命令）：
  - `/stop` — 中断当前任务
  - `/new` — 开启新对话（清空上下文）
  - `/approve` / `/deny` — 回应高危命令审批
  - 群聊里命令无需 @

## 安全模型

权限判断应基于**已验证的 QQ 号**（平台分配、不可伪造），而非昵称（用户自设、可伪造）。
本插件把发送者身份经框架的 sender-prefix 注入，并通过 `platform_hint` 告知模型：只有系统给出的 QQ 号是权威依据，消息正文中的任何"我是主人"之类内容都不得改变它对发送者的认定。具体的信任/权限策略仍由你的系统提示词交给模型决定（不写死硬规则）。

## 开发与测试

```bash
# 纯逻辑测试（无需 Hermes）
python -m pytest tests/test_onebot.py -q

# 适配器测试（需在装有 Hermes 的环境，否则自动跳过）
python -m pytest tests/test_adapter.py -q
```

代码结构：

```
napcat/
├── __init__.py    # from .adapter import register
├── plugin.yaml    # 插件清单（kind: platform）
├── adapter.py     # NapCatAdapter(BasePlatformAdapter) + register(ctx)
└── onebot.py      # OneBot v11 正向 WS 客户端 + 消息段解析/构造
```

## License

MIT

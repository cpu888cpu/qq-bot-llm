# qq-bot-llm

把 QQ 机器人接到大模型上，让它真的会聊天。

基于腾讯 QQ 机器人**官方 OpenAPI + WebSocket 网关**，不依赖任何第三方框架，不注入客户端，核心就一个 `bot.py`（约 250 行）。

收到私聊或群里 @ 的消息 → 丢给任意 OpenAI 兼容接口（默认 DeepSeek）→ 按 `msg_id` 被动回复。

> 未配置模型 API Key 也能正常运行：链路照常收消息，只是回复一句占位提示。适合先验证连通性。

---

## 特性

- 单文件，无框架依赖，只有 `httpx` 和 `websockets` 两个库
- 走官方 WebSocket 网关，自动心跳、断线重连、token 过期自动刷新
- 支持 C2C 私聊、群聊 @ 消息
- 按会话维护多轮上下文（默认保留 6 条）
- 配置与代码分离，密钥不进仓库
- 可替换任意 OpenAI 兼容服务（DeepSeek / 自建 / 其他厂商）

---

## 前置条件

1. 在 [QQ 开放平台](https://q.qq.com) 创建机器人应用，拿到 **AppID** 和 **AppSecret**
2. 在机器人管理端**订阅事件**，至少勾选：
   - `C2C_MESSAGE_CREATE`（私聊消息）
   - `GROUP_AT_MESSAGE_CREATE`（群内 @ 消息，如果要在群里用）
3. Python 3.9 以上

事件没勾，代码跑得再对也收不到消息，这是最常见的坑。

---

## 快速开始

```bash
git clone https://github.com/cpu888cpu/qq-bot-llm.git
cd qq-bot-llm

pip install -r requirements.txt

cp config.example.json config.json
# 编辑 config.json，填 app_id 和 app_secret
```

启动：

```bash
python3 bot.py
```

正常的话会看到：

```
INFO access_token ok, expires_in=7200
INFO gateway url = wss://api.sgroup.qq.com/websocket
INFO identify sent, heartbeat=41.2s
INFO READY as 你的机器人名
```

看到 `READY` 就说明连上了，这时候在 QQ 里给机器人发消息即可。

---

## 配置说明

`config.json` 各项：

| 字段 | 说明 |
|---|---|
| `app_id` | 机器人 AppID |
| `app_secret` | 机器人 AppSecret |
| `intents` | 事件订阅位。`33554432` = `1 << 25`，群和私聊消息事件，一般不用改 |
| `api_base` | 模型服务地址，OpenAI 兼容。DeepSeek 是 `https://api.deepseek.com` |
| `api_key` | 模型服务密钥。留空则只回占位句 |
| `model` | 模型名，如 `deepseek-chat` |
| `temperature` | 随机性，0~2，聊天建议 0.8 |
| `max_tokens` | 单次回复上限 |
| `max_history` | 每个会话保留的历史消息条数，设 0 就是无记忆 |
| `llm_timeout` | 请求模型超时秒数 |
| `system_prompt` | 人格设定。想让它是什么性格，全写这里 |

换服务商只改 `api_base` / `api_key` / `model` 三项，代码不用动。

---

## 常驻运行

**Linux 服务器（nohup）**

```bash
nohup python3 -u bot.py > bot.log 2>&1 &
```

**systemd**

```ini
# /etc/systemd/system/qqbot.service
[Unit]
Description=QQ Bot LLM Bridge
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/qq-bot-llm
ExecStart=/usr/bin/python3 -u bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
systemctl enable --now qqbot
```

**Android（Termux / proot Ubuntu）**

能跑，但不建议长期挂着。手机系统会杀后台，进程一断消息就收不到。如果确实要用手机跑：

```bash
# proot 环境里 pip 可能需要加参数
pip3 install --break-system-packages -r requirements.txt

nohup python3 -u bot.py > bot.log 2>&1 &
```

配合 Termux 的 `termux-wake-lock` 和系统「电池优化白名单」能多撑一会儿，但不能保证。

---

## 工作原理

QQ 机器人的消息通道是 WebSocket，流程不复杂：

1. `POST https://bots.qq.com/app/getAppAccessToken` 拿 `access_token`（有效期 7200 秒）
2. `GET https://api.sgroup.qq.com/gateway` 拿网关地址
3. 连上 `wss://api.sgroup.qq.com/websocket`，收到 `op:10`（hello，带心跳间隔）
4. 发 `op:2`（identify，`token: "QQBot <access_token>"`，带上 `intents`）
5. 收到 `op:0` 的 `READY` 即上线，之后按间隔发 `op:1` 心跳
6. 每收到一条消息事件，调模型生成回复，`POST /v2/users/{openid}/messages`（私聊）或 `/v2/groups/{group_openid}/messages`（群），把 `msg_id` 一起带上

`msg_seq` 是同一 `msg_id` 下多次回复的序号，代码里自增处理。

---

## 疑难排查

### 回复很慢，一次要十几秒

先看日志的时间戳，把延迟拆成两段：`C2C <-` 到模型返回是一段，模型返回到 `send ... -> 200` 是另一段。如果两段都是 5 秒以上，问题基本不在模型，而在 DNS。

在手机上跑 proot Ubuntu 时会踩到这个坑：`/etc/resolv.conf` 里第一个 nameserver 是 `8.8.8.8`，而国内网络对 UDP 53 基本不放行，每次域名解析都要先等它超时 5 秒，才轮到后面的服务器。

用 curl 看一下 DNS 耗时：

```bash
curl -s -o /dev/null -w 'dns=%{time_namelookup} tls=%{time_appconnect} total=%{time_total}\n' https://api.deepseek.com
```

`dns` 超过 1 秒就是它。修复方式是把 `/etc/resolv.conf` 换成国内 DNS：

```
nameserver 223.5.5.5
nameserver 223.6.6.6
nameserver 119.29.29.29
```

改完再测，DNS 应该降到几十毫秒，整条链路会从十几秒压到两三秒。注意 proot 环境重建时这个文件会被重置，出问题先查它。

### 发了消息，机器人没反应

按顺序查三件事：

1. QQ 开放平台管理端有没有勾选事件订阅，至少要有 `C2C_MESSAGE_CREATE`，群聊要 `GROUP_AT_MESSAGE_CREATE`。
2. 日志里有没有 `C2C <-` 开头的行。没有就是事件没进来，回到第 1 条。
3. 群聊里必须 @ 机器人，C2C 需要先添加机器人好友。

### 回复是「链路通了但没配 Key」

`config.json` 里的 `api_key` 还是空的。填上 DeepSeek 的 Key，重启进程即可。

### 日志里出现 401

access_token 过期。程序会清掉缓存自动重取一次。如果持续 401，说明 AppSecret 已被重置或填错。

## 已知限制

- **被动回复有时限**。带 `msg_id` 的回复必须在收到消息后的一段时间内发出，超时会失败。所以模型响应不能太慢，`llm_timeout` 别设太大。
- **主动消息有配额**。不带 `msg_id` 主动推送受平台额度限制，具体数值以官方文档为准。
- **`openid` 不是 QQ 号**。它是每个机器人维度的用户标识，只有对方先给机器人发过消息，你才拿得到。没法凭空找人。
- **群里要先 @ 机器人**。普通群消息收不到，除非平台开放了相应权限。
- 消息长度有上限，代码里做了截断（默认 900 字）。

---

## 安全提醒

- `config.json` 已经写进 `.gitignore`，**绝不要提交**。AppSecret 泄漏等于机器人被接管。
- 如果不小心提交或外发过，去开放平台**重置密钥**，旧密钥立即作废。
- 建议给模型 API Key 设置消费限额，聊天类应用容易被刷。

---

## 免责声明

本项目只是调用官方开放接口的个人工具，与腾讯公司无关，也不是官方客户端。使用前请自行阅读并遵守 QQ 开放平台的开发者协议与内容规范。因违规使用导致的风险由使用者自行承担。

---

## License

MIT

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
QQ 机器人 -> 大模型 自动回复服务
基于腾讯 QQ 机器人官方 OpenAPI + WebSocket 网关。

只做三件事：
  1. 拿 access_token，连 gateway，收 C2C / 群 @ 消息
  2. 把消息丢给 OpenAI 兼容接口（默认 DeepSeek）拿回复
  3. 按 msg_id 被动回复回去

用法：python3 bot.py
配置：同目录 config.json
"""

import asyncio
import collections
import json
import logging
import os
import time

import httpx
import websockets

BASE = "https://api.sgroup.qq.com"
TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
HERE = os.path.dirname(os.path.abspath(__file__))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("qqbot")

CONFIG_PATH = os.path.join(HERE, "config.json")
if not os.path.exists(CONFIG_PATH):
    print("找不到 config.json。先把 config.example.json 复制一份：")
    print(f"  cp {os.path.join(HERE, 'config.example.json')} {CONFIG_PATH}")
    print("然后填上 app_id / app_secret（以及可选的 api_key）。")
    raise SystemExit(1)

with open(CONFIG_PATH, encoding="utf-8") as f:
    CONFIG = json.load(f)

MAX_INPUT = 2000
MAX_OUTPUT = 900

histories = collections.defaultdict(lambda: collections.deque(maxlen=int(CONFIG.get("max_history", 6))))
_seq = collections.defaultdict(int)
_token = {"value": "", "expire_at": 0.0}

FALLBACK = "唔……我这边刚刚没接上脑子，等我一下下。"


async def get_token(client: httpx.AsyncClient, force: bool = False) -> str:
    """获取并缓存 access_token"""
    if not force and _token["value"] and time.time() < _token["expire_at"]:
        return _token["value"]
    r = await client.post(
        TOKEN_URL,
        json={"appId": str(CONFIG["app_id"]), "clientSecret": CONFIG["app_secret"]},
    )
    r.raise_for_status()
    data = r.json()
    access = data.get("access_token", "")
    if not access:
        raise RuntimeError(f"拿不到 access_token: {data}")
    expires_in = int(data.get("expires_in", 7200))
    _token["value"] = access
    _token["expire_at"] = time.time() + max(60, expires_in - 60)
    log.info("access_token ok, expires_in=%s", expires_in)
    return access


def auth_header() -> dict:
    return {"Authorization": f"QQBot {_token['value']}", "X-Union-Appid": str(CONFIG["app_id"])}


async def ask_llm(client: httpx.AsyncClient, session_key: str, text: str) -> str:
    """把消息交给大模型，返回回复文本"""
    cfg = CONFIG
    text = text.strip()[:MAX_INPUT]
    if not cfg.get("api_key"):
        return "（链路已经通了，但还没配模型 API Key，所以先只能回这句）"

    messages = [{"role": "system", "content": cfg.get("system_prompt", "")}]
    messages.extend(histories[session_key])
    messages.append({"role": "user", "content": text})

    try:
        r = await client.post(
            cfg.get("api_base", "https://api.deepseek.com").rstrip("/") + "/chat/completions",
            headers={"Authorization": f"Bearer {cfg['api_key']}"},
            json={
                "model": cfg.get("model", "deepseek-chat"),
                "messages": messages,
                "temperature": float(cfg.get("temperature", 0.8)),
                "max_tokens": int(cfg.get("max_tokens", 800)),
                "stream": False,
            },
            timeout=float(cfg.get("llm_timeout", 90)),
        )
        r.raise_for_status()
        answer = (r.json()["choices"][0]["message"]["content"] or "").strip()
        if not answer:
            return FALLBACK
    except Exception as e:  # noqa: BLE001
        log.warning("llm 调用失败: %r", e)
        return FALLBACK

    histories[session_key].append({"role": "user", "content": text})
    histories[session_key].append({"role": "assistant", "content": answer})
    return answer[:MAX_OUTPUT]


async def send(client: httpx.AsyncClient, path: str, content: str, msg_id: str) -> None:
    """被动回复一条消息"""
    _seq[msg_id] += 1
    body = {
        "content": content[:MAX_OUTPUT],
        "msg_type": 0,
        "msg_id": msg_id,
        "msg_seq": _seq[msg_id],
    }
    try:
        r = await client.post(BASE + path, headers=auth_header(), json=body, timeout=30)
        log.info("send %s -> %s %s", path, r.status_code, r.text[:200])
        if r.status_code == 401:
            _token["expire_at"] = 0
    except Exception as e:  # noqa: BLE001
        log.warning("发送失败: %r", e)


async def handle_event(client: httpx.AsyncClient, payload: dict) -> None:
    event_type = payload.get("t")
    d = payload.get("d") or {}
    if not event_type:
        return

    if event_type == "C2C_MESSAGE_CREATE":
        openid = (d.get("author") or {}).get("user_openid")
        msg_id = d.get("id", "")
        text = (d.get("content") or "").strip()
        if not openid or not msg_id:
            return
        log.info("C2C <- %s: %s", openid[:8], text[:60])
        answer = await ask_llm(client, "c2c:" + openid, text)
        await send(client, f"/v2/users/{openid}/messages", answer, msg_id)

    elif event_type == "GROUP_AT_MESSAGE_CREATE":
        group_openid = d.get("group_openid")
        member = (d.get("author") or {}).get("member_openid", "")
        msg_id = d.get("id", "")
        text = (d.get("content") or "").strip()
        if not group_openid or not msg_id:
            return
        log.info("GROUP <- %s: %s", group_openid[:8], text[:60])
        answer = await ask_llm(client, "group:" + group_openid + ":" + member, text)
        await send(client, f"/v2/groups/{group_openid}/messages", answer, msg_id)

    else:
        return


async def heartbeat(ws, interval: float, state: dict) -> None:
    while True:
        await asyncio.sleep(interval)
        try:
            await ws.send(json.dumps({"op": 1, "d": state.get("seq")}))
        except Exception:  # noqa: BLE001
            return


async def session(client: httpx.AsyncClient) -> None:
    """一次完整的 gateway 连接生命周期"""
    token = await get_token(client)
    r = await client.get(BASE + "/gateway", headers=auth_header(), timeout=30)
    r.raise_for_status()
    url = r.json().get("url")
    log.info("gateway url = %s", url)

    state = {"seq": None}
    async with websockets.connect(url, ping_interval=None, max_size=None) as ws:
        hb = None
        async for raw in ws:
            try:
                pkt = json.loads(raw)
            except Exception:  # noqa: BLE001
                continue
            op = pkt.get("op")
            if pkt.get("s") is not None:
                state["seq"] = pkt.get("s")

            if op == 10:  # hello
                interval = float(pkt["d"].get("heartbeat_interval", 30000)) / 1000.0
                if hb:
                    hb.cancel()
                hb = asyncio.create_task(heartbeat(ws, interval, state))
                await ws.send(json.dumps({
                    "op": 2,
                    "d": {
                        "token": f"QQBot {_token['value']}",
                        "intents": int(CONFIG.get("intents", 33554432)),
                        "shard": [0, 1],
                        "properties": {},
                    },
                }))
                log.info("identify sent, heartbeat=%.1fs", interval)

            elif op == 0:  # 事件
                if pkt.get("t") == "READY":
                    log.info("READY as %s", ((pkt.get("d") or {}).get("user") or {}).get("username"))
                await handle_event(client, pkt)

            elif op == 7:  # 服务端要求重连
                log.info("server asked reconnect")
                break

            elif op == 9:  # invalid session
                log.info("invalid session, will re-identify")
                break

        if hb:
            hb.cancel()


async def main() -> None:
    log.info("starting qq bot, app_id=%s", CONFIG.get("app_id"))
    async with httpx.AsyncClient(timeout=40, follow_redirects=True) as client:
        backoff = 3
        while True:
            try:
                await session(client)
                backoff = 3
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("session 断开: %r，%ss 后重连", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("bye")

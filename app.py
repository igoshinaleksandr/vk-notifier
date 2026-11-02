"""
vk-notifier — принимает события Callback API сообщества VK
и отправляет пуш через bark-server о новых сообщениях клиентов.

Переменные окружения:
  VK_GROUP_ID      — числовой ID сообщества (без минуса)
  VK_CONFIRMATION  — строка подтверждения из настроек Callback API
  VK_SECRET        — секретный ключ из настроек Callback API
  BARK_URL         — адрес bark-server (внутри кластера)
  BARK_KEY         — device_key телефона
  COOLDOWN         — антиспам, секунд на одного клиента (по умолчанию 120)
  BARK_ICON        — (необязательно) URL картинки для значка уведомления
  CLICK_URL        — (необязательно) шаблон ссылки по тапу на уведомление,
                     подстановки {group_id} и {from_id}
                     по умолчанию: https://vk.com/gim{group_id}?sel={from_id}
  VK_TOKEN         — (необязательно) ключ доступа сообщества: имя отправителя в пуше
                     и пересылка копий (для пересылки нужно право «Сообщения»)
  FORWARD_TO       — (необязательно) VK ID, кому пересылать копию сообщений клиентов
                     (нужны оба: VK_TOKEN и FORWARD_TO)
  VK_API           — адрес API VK (по умолчанию https://api.vk.com/method)
"""

import asyncio
import logging
import os
import random
import time

import aiohttp
from aiohttp import web

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("vk-notifier")

VK_GROUP_ID = int(os.environ["VK_GROUP_ID"])
VK_CONFIRMATION = os.environ["VK_CONFIRMATION"]
VK_SECRET = os.environ["VK_SECRET"]
BARK_URL = os.environ.get("BARK_URL", "http://bark-server.bark.svc.cluster.local:8080").rstrip("/")
BARK_KEY = os.environ["BARK_KEY"]
COOLDOWN = int(os.environ.get("COOLDOWN", "120"))
BARK_ICON = os.environ.get("BARK_ICON", "").strip()
CLICK_URL = os.environ.get("CLICK_URL", "").strip() or "https://vk.com/gim{group_id}?sel={from_id}"
VK_TOKEN = os.environ.get("VK_TOKEN", "").strip()
FORWARD_TO = int(os.environ.get("FORWARD_TO", "0") or 0)
VK_API = os.environ.get("VK_API", "https://api.vk.com/method").rstrip("/")
VK_V = "5.199"

_last_sent: dict[int, float] = {}
_names: dict[int, str] = {}         # кэш имён, чтобы не дёргать API на каждое сообщение
_tasks: set[asyncio.Task] = set()   # держим ссылки, чтобы задачи не собрал GC

ATTACHMENT_NAMES = {
    "photo": "фото", "video": "видео", "audio_message": "голосовое",
    "doc": "файл", "sticker": "стикер", "wall": "запись", "link": "ссылка",
}


def describe(msg: dict) -> str:
    text = (msg.get("text") or "").strip()
    if text:
        return text[:200]
    att = msg.get("attachments") or []
    if att:
        return "[" + ", ".join(ATTACHMENT_NAMES.get(a.get("type"), "вложение") for a in att) + "]"
    return "[сообщение без текста]"


async def send_push(session: aiohttp.ClientSession, msg: dict) -> None:
    from_id = msg.get("from_id")
    now = time.monotonic()
    if from_id in _last_sent and now - _last_sent[from_id] < COOLDOWN:
        log.info("cooldown для %s, пуш пропущен", from_id)
        return
    _last_sent[from_id] = now

    name = await get_name(session, from_id)
    payload = {
        "device_key": BARK_KEY,
        "title": name or "kris.tatts: новое сообщение",
        "body": describe(msg),
        "group": "kris.tatts",
        "level": "timeSensitive",
        "sound": "minuet",
        "url": CLICK_URL.format(group_id=VK_GROUP_ID, from_id=from_id),
    }
    if BARK_ICON:
        payload["icon"] = BARK_ICON
    try:
        async with session.post(f"{BARK_URL}/push", json=payload,
                                timeout=aiohttp.ClientTimeout(total=5)) as r:
            if r.status == 200:
                log.info("пуш отправлен (from_id=%s)", from_id)
            else:
                log.warning("bark ответил %s: %s", r.status, await r.text())
    except Exception as e:
        log.warning("bark недоступен: %s", e)


async def vk_call(session: aiohttp.ClientSession, method: str, **params) -> dict:
    params.update(access_token=VK_TOKEN, v=VK_V)
    async with session.post(f"{VK_API}/{method}", data=params,
                            timeout=aiohttp.ClientTimeout(total=5)) as r:
        data = await r.json(content_type=None)
    if "error" in data:
        raise RuntimeError(f"{method}: {data['error'].get('error_msg')}")
    return data["response"]


async def get_name(session: aiohttp.ClientSession, user_id: int) -> str | None:
    """Имя и фамилия отправителя через users.get (нужен VK_TOKEN)."""
    if not VK_TOKEN or not user_id or user_id < 0:
        return None
    if user_id in _names:
        return _names[user_id]
    try:
        users = await vk_call(session, "users.get", user_ids=user_id)
        if users:
            name = f"{users[0]['first_name']} {users[0]['last_name']}"
            _names[user_id] = name
            return name
    except Exception as e:
        log.info("не удалось получить имя %s: %s", user_id, e)
    return None


async def forward_copy(session: aiohttp.ClientSession, msg: dict) -> None:
    """Копия сообщения клиента тебе в личку — исходный диалог не открывается
    и остаётся непрочитанным."""
    from_id = msg.get("from_id")
    try:
        name = await get_name(session, from_id) or f"id{from_id}"

        text = (
            f"💬 {name}:\n{describe(msg)}\n\n"
            f"Диалог: https://vk.com/gim{VK_GROUP_ID}?sel={from_id}"
        )
        await vk_call(session, "messages.send", peer_id=FORWARD_TO,
                      random_id=random.getrandbits(31), message=text,
                      dont_parse_links=1)
        log.info("копия отправлена в личку (from_id=%s)", from_id)
    except Exception as e:
        log.warning("пересылка не удалась: %s", e)


def run_bg(coro) -> None:
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def callback(request: web.Request) -> web.Response:
    try:
        event = await request.json()
    except Exception:
        return web.Response(status=400, text="bad json")

    if event.get("group_id") != VK_GROUP_ID:
        log.warning("чужой group_id: %s", event.get("group_id"))
        return web.Response(status=403, text="forbidden")

    if event.get("type") == "confirmation":
        log.info("запрос подтверждения сервера")
        return web.Response(text=VK_CONFIRMATION)

    if event.get("secret") != VK_SECRET:
        log.warning("неверный secret")
        return web.Response(status=403, text="forbidden")

    if event.get("type") == "message_new":
        msg = event["object"]["message"]
        session = request.app["http"]
        run_bg(send_push(session, msg))
        # копию шлём на каждое сообщение (без антиспама), кроме твоих собственных
        if VK_TOKEN and FORWARD_TO and msg.get("from_id") != FORWARD_TO:
            run_bg(forward_copy(session, msg))

    # VK должен получить "ok" быстро, иначе будет повторять событие
    return web.Response(text="ok")


async def healthz(request: web.Request) -> web.Response:
    return web.Response(text="ok")


async def on_startup(app: web.Application) -> None:
    app["http"] = aiohttp.ClientSession()
    log.info("vk-notifier запущен, group_id=%s, bark=%s, пересылка=%s",
             VK_GROUP_ID, BARK_URL, FORWARD_TO if (VK_TOKEN and FORWARD_TO) else "выкл")


async def on_cleanup(app: web.Application) -> None:
    await app["http"].close()


def main() -> None:
    app = web.Application()
    app.router.add_post("/", callback)
    app.router.add_get("/healthz", healthz)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    web.run_app(app, host="0.0.0.0", port=8080, access_log=None)


if __name__ == "__main__":
    main()

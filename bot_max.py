"""MAX-версия бота цветочного магазина (библиотека maxapi). Запуск: python bot_max.py
Вся логика в core.py, здесь только показ экранов и приём событий.

Особенности MAX: нет постоянной клавиатуры (меню это inline-кнопки), лимит сообщения 4000 символов,
состояние диалога хранит core.py (по ID пользователя)."""
import asyncio
import logging
import re
import sys

from maxapi import Bot, Dispatcher
from maxapi.context import MemoryContext
from maxapi.types import (
    CallbackButton,
    InputMedia,
    LinkButton,
    MessageCallback,
    MessageCreated,
    RequestContactButton,
)
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

try:
    from maxapi.types import BotStarted
except ImportError:  # другая структура пакета в вашей версии maxapi
    from maxapi.types.updates.bot_started import BotStarted

import config as C
import core
import storage as S

log = logging.getLogger("flowers-max")
BOT = None
TEXT_LIMIT = 3900


def kb(rows):
    b = InlineKeyboardBuilder()
    for row in rows:
        buttons = []
        for text, data in row:
            if data == "!contact":
                buttons.append(RequestContactButton(text=text))
            elif data.startswith("http"):
                buttons.append(LinkButton(text=text, url=data))
            else:
                buttons.append(CallbackButton(text=text, payload=data))
        if buttons:
            b.row(*buttons)
    return b.as_markup()


def cut(text):
    return text if len(text) <= TEXT_LIMIT else text[: TEXT_LIMIT - 1] + "…"


def has_media(msg):
    body = getattr(msg, "body", None)
    for a in getattr(body, "attachments", None) or []:
        kind = getattr(a, "type", None)
        kind = str(getattr(kind, "value", kind)).lower()
        if kind in ("image", "video", "file", "audio"):
            return True
    return False


def sender_name(ev):
    user = getattr(getattr(ev, "callback", None), "user", None)
    if user is None:
        user = getattr(getattr(ev, "message", None), "sender", None)
    return getattr(user, "full_name", None) or getattr(user, "first_name", None) or "Гость"


def message_text(ev):
    body = getattr(ev.message, "body", None)
    return (getattr(body, "text", None) or "").strip()


def contact_phone(ev):
    """Номер из вложения-контакта (кнопка «Отправить мой номер») или None."""
    body = getattr(ev.message, "body", None)
    for a in getattr(body, "attachments", None) or []:
        kind = getattr(a, "type", None)
        kind = getattr(kind, "value", kind)
        if str(kind).lower() != "contact":
            continue
        payload = getattr(a, "payload", None)
        vcf = getattr(payload, "vcf_info", None) or ""
        m = re.search(r"TEL[^:\r\n]*:([^\r\n]+)", vcf)
        phone = core.normalize_phone(m.group(1)) if m else None
        if not phone:
            phone = core.normalize_phone(getattr(payload, "vcf_phone", None))
        if phone:
            return phone
    return None


async def send_new(ev, scr):
    """Отправить экран новым сообщением в тот же чат."""
    text, markup = cut(scr.text), kb(scr.rows)
    msg = getattr(ev, "message", None)
    atts = [markup]
    if scr.photo:
        atts = [InputMedia(path=scr.photo), markup]
    try:
        if msg is not None:
            await msg.answer(text, attachments=atts, format="html")
        else:
            chat_id, _ = ev.get_ids()
            await BOT.send_message(chat_id=chat_id, text=text, attachments=atts, format="html")
    except Exception as e:  # noqa: BLE001  не вышло с фото: покажем текстом
        if not scr.photo:
            raise
        log.info("фото не отправилось (%s), показываю текстом", e)
        if msg is not None:
            await msg.answer(text, attachments=[markup], format="html")
        else:
            chat_id, _ = ev.get_ids()
            await BOT.send_message(chat_id=chat_id, text=text, attachments=[markup], format="html")


async def show(ev, scr, replace):
    """replace=True: ответ на кнопку, правим прежнее сообщение."""
    msg = getattr(ev, "message", None)
    if replace and msg is not None and isinstance(ev, MessageCallback) and not (scr.new or scr.photo or has_media(msg)):
        try:
            await msg.edit(text=cut(scr.text), attachments=[kb(scr.rows)], format="html")
            return
        except Exception as e:  # noqa: BLE001
            log.info("edit не удался (%s), отправляю заново", e)
    if replace and msg is not None and isinstance(ev, MessageCallback):
        try:
            await msg.delete()
        except Exception:  # noqa: BLE001
            pass
    await send_new(ev, scr)


async def send_safe(uid, scr):
    try:
        await BOT.send_message(
            user_id=uid, text=cut(scr.text), attachments=[kb(scr.rows)] if scr.rows else None, format="html"
        )
        return True
    except Exception as e:  # noqa: BLE001  пользователь мог заблокировать бота, либо это демо-пользователь
        log.info("не удалось отправить %s: %s", uid, e)
        return False


async def dispatch(ev, uid, outs, replace):
    first = True
    for out in outs:
        if out.delay:
            await asyncio.sleep(out.delay)
        if out.to is not None and out.to != uid:
            await send_safe(out.to, out.screen)
            continue
        await show(ev, out.screen, replace and first)
        first = False


async def on_callback(event: MessageCallback, context: MemoryContext = None):
    try:
        await event.ack()
    except Exception:  # noqa: BLE001
        pass
    try:
        _, uid = event.get_ids()
        payload = event.callback.payload or ""
        log.info("кнопка от %s: %s", uid, payload)
        outs = core.handle_action(uid, sender_name(event), payload)
        await dispatch(event, uid, outs, replace=True)
    except Exception:  # noqa: BLE001
        log.exception("ошибка в обработчике кнопки %r", getattr(event.callback, "payload", None))


async def on_message(event: MessageCreated, context: MemoryContext = None):
    try:
        _, uid = event.get_ids()
        if uid is None or getattr(event.message, "body", None) is None:
            return
        name = sender_name(event)
        text = message_text(event)
        log.info("сообщение от %s: %r", uid, text[:60])
        cmd = text.split()[0][1:].split("@")[0].lower() if text.startswith("/") else None
        if cmd in ("start", "menu", "cancel"):
            outs = core.handle_start(uid, name)
        elif cmd == "id":
            return await event.message.answer(f"Ваш ID в MAX: {uid}")
        elif cmd == "admin":
            if not core.is_admin(uid):
                return await event.message.answer(
                    f"Админ-панель доступна только администратору. Ваш ID: {uid}. Добавьте его в MAX_ADMIN_IDS в .env."
                )
            outs = core.handle_action(uid, name, "adm")
        else:
            phone = contact_phone(event)
            outs = core.handle_contact(uid, name, phone) if phone else core.handle_text(uid, name, text)
        await dispatch(event, uid, outs, replace=False)
    except Exception:  # noqa: BLE001
        log.exception("ошибка в обработчике сообщения")


async def on_started(event: BotStarted, context: MemoryContext = None):
    """Пользователь нажал «Начать» в диалоге с ботом."""
    user = event.user
    name = getattr(user, "full_name", None) or getattr(user, "first_name", None) or "Гость"
    outs = core.handle_start(user.user_id, name)
    scr = outs[0].screen
    await BOT.send_message(chat_id=event.chat_id, text=cut(scr.text), attachments=[kb(scr.rows)], format="html")


async def reminder_loop():
    while True:
        try:
            for out in core.due_outs():
                await send_safe(out.to, out.screen)
        except Exception:  # noqa: BLE001
            log.exception("ошибка в цикле напоминаний")
        await asyncio.sleep(600)


def build_dispatcher():
    dp = Dispatcher()
    dp.bot_started()(on_started)
    dp.message_callback()(on_callback)
    dp.message_created()(on_message)
    return dp


async def main():
    global BOT
    try:
        sys.stdout.reconfigure(line_buffering=True)  # логи сразу попадают в панель хостинга
    except Exception:  # noqa: BLE001
        pass
    logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    token = (C.MAX_BOT_TOKEN or "").strip().strip("\"'")
    log.info("Запуск бота для MAX. Токен: %s, администраторов: %d, база: %s",
             f"задан ({len(token)} симв.)" if token else "НЕ ЗАДАН", len(C.MAX_ADMIN_IDS), C.MAX_DB_PATH)
    if not token:
        log.error("Не задан MAX_BOT_TOKEN. Добавьте его в переменные окружения панели или в файл .env")
        raise SystemExit(1)
    S.init_db(C.MAX_DB_PATH)
    core.configure(C.MAX_ADMIN_IDS)
    BOT = Bot(token=token)
    dp = build_dispatcher()
    try:
        me = await BOT.get_me()
        log.info("Подключено к MAX, бот: %s", getattr(me, "name", None) or getattr(me, "first_name", None) or me)
    except Exception as e:  # noqa: BLE001
        log.error("Не удалось получить данные бота: %r. Проверьте токен и доступ сервера к MAX.", e)
    try:
        await BOT.delete_webhook()  # polling не работает при активной webhook-подписке
    except Exception as e:  # noqa: BLE001
        log.info("delete_webhook: %s", e)
    reminders = asyncio.create_task(reminder_loop())
    try:
        while True:
            try:
                log.info("Бот слушает сообщения (polling)")
                await dp.start_polling(BOT)
                break
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("Polling остановился с ошибкой, повтор через 10 секунд")
                await asyncio.sleep(10)
    finally:
        reminders.cancel()


if __name__ == "__main__":
    asyncio.run(main())

# -*- coding: utf-8 -*-
"""
Beauty Guide — бот подписки на закрытый Telegram-канал.

Логика:
  девочка заходит -> читает о заказчице и FAQ ->
  оформляет подписку (оплата WayForPay) ->
  бот выдаёт персональную ссылку в закрытый канал ->
  через 30 дней подписка истекает, бот убирает её из канала.

Технологии: Python + aiogram 3 + aiohttp (webhook) + SQLite (база подписок).
"""

import os
import hmac
import hashlib
import logging
import sqlite3
import time
import asyncio
from datetime import datetime, timedelta

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from aiogram.filters import Command
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

# ============================================================
# НАСТРОЙКИ — из переменных окружения на Render
# ============================================================
BOT_TOKEN = os.getenv("BOT_TOKEN")              # токен от BotFather
ADMIN_ID = os.getenv("ADMIN_ID")                # Telegram ID заказчицы
CHANNEL_ID = os.getenv("CHANNEL_ID")            # ID закрытого канала, напр. -1001234567890
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST")        # адрес сервиса на Render

# WayForPay — ключи мерчанта от заказчицы
WFP_MERCHANT = os.getenv("WFP_MERCHANT")        # merchantAccount
WFP_SECRET = os.getenv("WFP_SECRET")            # merchantSecretKey

# Параметры подписки
SUB_PRICE = os.getenv("SUB_PRICE", "300")       # цена в гривнах
SUB_DAYS = int(os.getenv("SUB_DAYS", "30"))     # длительность подписки в днях

WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"
# отдельный адрес, на который WayForPay присылает подтверждение оплаты
WFP_CALLBACK_PATH = "/wayforpay-callback"
PORT = int(os.getenv("PORT", 10000))

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

DB_PATH = "subscriptions.db"


# ============================================================
# БАЗА ДАННЫХ — хранит, кто и до какого числа подписан
# ============================================================
def db_init():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS subs (
            user_id    INTEGER PRIMARY KEY,
            username   TEXT,
            full_name  TEXT,
            expires_at INTEGER,   -- метка времени окончания подписки
            active     INTEGER    -- 1 = в канале, 0 = удалён
        )
    """)
    conn.commit()
    conn.close()


def db_set_subscription(user_id, username, full_name, expires_at):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT INTO subs (user_id, username, full_name, expires_at, active)
        VALUES (?, ?, ?, ?, 1)
        ON CONFLICT(user_id) DO UPDATE SET
            username=excluded.username,
            full_name=excluded.full_name,
            expires_at=excluded.expires_at,
            active=1
    """, (user_id, username, full_name, expires_at))
    conn.commit()
    conn.close()


def db_get_expired():
    """Возвращает тех, у кого подписка кончилась, но они ещё числятся активными."""
    now = int(time.time())
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT user_id FROM subs WHERE expires_at < ? AND active = 1", (now,)
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


def db_deactivate(user_id):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE subs SET active = 0 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


def db_get_user(user_id):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT expires_at, active FROM subs WHERE user_id = ?", (user_id,)
    ).fetchone()
    conn.close()
    return row  # (expires_at, active) или None


# ============================================================
# ТЕКСТЫ
# ============================================================
ABOUT_TEXT = (
    "<b>Обо мне и моём Beauty Guide</b>\n\n"
    "11 лет назад я сделала первые шаги в индустрии красоты — тогда ещё "
    "не зная, что это станет важной частью моей жизни.\n\n"
    "За эти годы я работала моделью для салонов, участвовала в показах "
    "и съёмках, стала амбассадором брендов и партнёром мастеров, которым "
    "действительно доверяю.\n\n"
    "Свой Beauty Guide я создала как пространство, где делюсь опытом, "
    "любимыми находками и помогаю женщинам раскрывать свою красоту "
    "каждый день. И спустя 11 лет я понимаю — это только начало 🤍"
)

WELCOME_TEXT = (
    "Добро пожаловать в твой личный <b>Beauty Guide</b>! 🤍\n\n"
    "Здесь я делюсь любимыми средствами, проверенными процедурами "
    "и находками по уходу за кожей, волосами и телом.\n\n"
    "Контент регулярно обновляется — чтобы у тебя всегда были свежие "
    "идеи, вдохновение и новые beauty-находки.\n\n"
    "Оформи подписку и получи доступ к закрытому каналу 👇"
)

FAQ_TEXT = (
    "<b>Частые вопросы</b>\n\n"
    "<b>Что я получу по подписке?</b>\n"
    "Доступ к закрытому каналу с моими постами: уход за кожей, волосами "
    "и телом, любимые средства, процедуры и находки.\n\n"
    "<b>Сколько стоит и на какой срок?</b>\n"
    f"{SUB_PRICE} грн за {SUB_DAYS} дней доступа.\n\n"
    "<b>Что будет, когда подписка закончится?</b>\n"
    "Доступ к каналу закроется автоматически. Чтобы продолжить — просто "
    "оформи подписку снова.\n\n"
    "<b>Как оплатить?</b>\n"
    "Картой онлайн через защищённую систему WayForPay прямо из бота."
)


# ============================================================
# КЛАВИАТУРЫ
# ============================================================
def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Оформить подписку", callback_data="subscribe")],
        [InlineKeyboardButton(text="✨ О Beauty Guide", callback_data="about")],
        [InlineKeyboardButton(text="❓ Частые вопросы", callback_data="faq")],
        [InlineKeyboardButton(text="🔑 Моя подписка", callback_data="my_sub")],
    ])


def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ В меню", callback_data="to_menu")]
    ])


# ============================================================
# WAYFORPAY — формирование ссылки на оплату
# ============================================================
def make_payment_link(user_id: int) -> str:
    """
    Создаёт ссылку на оплату WayForPay.

    Пока WFP_MERCHANT / WFP_SECRET не заданы — возвращает пустую строку
    (бот работает в режиме без оплаты, для теста логики).

    Когда заказчица получит ключи мерчанта и впишет их в переменные —
    функцию нужно будет дополнить полем invoice по документации WayForPay.
    """
    if not WFP_MERCHANT or not WFP_SECRET:
        return ""  # режим заглушки

    # ЗАГОТОВКА. Реальная интеграция WayForPay делается через их Invoice API.
    # Здесь формируется подпись запроса; конкретные поля добавим по докам
    # WayForPay, когда будут ключи.
    order_ref = f"sub-{user_id}-{int(time.time())}"
    # подпись (пример структуры — уточняется по документации WayForPay)
    sign_str = ";".join([WFP_MERCHANT, order_ref, SUB_PRICE])
    signature = hmac.new(
        WFP_SECRET.encode(), sign_str.encode(), hashlib.md5
    ).hexdigest()
    logging.info(f"WayForPay order {order_ref}, signature {signature}")
    # вернётся реальная ссылка на оплату после доработки интеграции
    return ""


# ============================================================
# КОМАНДЫ И МЕНЮ
# ============================================================
@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(WELCOME_TEXT, reply_markup=main_menu())


@dp.callback_query(F.data == "to_menu")
async def to_menu(call: CallbackQuery):
    await call.message.answer("Главное меню:", reply_markup=main_menu())
    await call.answer()


@dp.callback_query(F.data == "about")
async def show_about(call: CallbackQuery):
    await call.message.answer(ABOUT_TEXT, reply_markup=back_kb())
    await call.answer()


@dp.callback_query(F.data == "faq")
async def show_faq(call: CallbackQuery):
    await call.message.answer(FAQ_TEXT, reply_markup=back_kb())
    await call.answer()


@dp.callback_query(F.data == "my_sub")
async def show_my_sub(call: CallbackQuery):
    row = db_get_user(call.from_user.id)
    if row and row[1] == 1:
        expires = datetime.fromtimestamp(row[0]).strftime("%d.%m.%Y")
        text = (
            "🔑 <b>Твоя подписка активна</b>\n\n"
            f"Доступ к каналу открыт до <b>{expires}</b>.\n"
            "После этой даты доступ закроется — продлить можно будет здесь же."
        )
    else:
        text = (
            "У тебя пока нет активной подписки.\n\n"
            "Оформи её, чтобы попасть в закрытый Beauty Guide 🤍"
        )
    await call.message.answer(text, reply_markup=main_menu())
    await call.answer()


# ============================================================
# ОФОРМЛЕНИЕ ПОДПИСКИ
# ============================================================
@dp.callback_query(F.data == "subscribe")
async def subscribe(call: CallbackQuery):
    pay_link = make_payment_link(call.from_user.id)

    if pay_link:
        # рабочий режим: есть ссылка на оплату WayForPay
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Оплатить", url=pay_link)],
            [InlineKeyboardButton(text="⬅️ В меню", callback_data="to_menu")],
        ])
        await call.message.answer(
            f"<b>Подписка на Beauty Guide</b>\n\n"
            f"Стоимость: {SUB_PRICE} грн\n"
            f"Срок доступа: {SUB_DAYS} дней\n\n"
            "Нажми «Оплатить» — после оплаты бот сразу пришлёт ссылку "
            "на закрытый канал.",
            reply_markup=kb,
        )
    else:
        # режим заглушки: оплата ещё не подключена
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="✅ Я оплатила (тестовый режим)",
                callback_data="test_paid"
            )],
            [InlineKeyboardButton(text="⬅️ В меню", callback_data="to_menu")],
        ])
        await call.message.answer(
            f"<b>Подписка на Beauty Guide</b>\n\n"
            f"Стоимость: {SUB_PRICE} грн\n"
            f"Срок доступа: {SUB_DAYS} дней\n\n"
            "⚠️ Оплата WayForPay ещё подключается. Пока кнопка ниже "
            "выдаёт доступ без оплаты — это для проверки работы бота.",
            reply_markup=kb,
        )
    await call.answer()


@dp.callback_query(F.data == "test_paid")
async def test_paid(call: CallbackQuery):
    """Тестовая выдача доступа без оплаты — удалить, когда подключится WayForPay."""
    await grant_access(call.from_user.id, call.from_user.username,
                       call.from_user.full_name)
    await call.answer()


async def grant_access(user_id, username, full_name):
    """Открывает доступ: сохраняет подписку и присылает ссылку в канал."""
    expires_at = int(time.time()) + SUB_DAYS * 86400
    db_set_subscription(user_id, username or "", full_name or "", expires_at)

    try:
        # одноразовая персональная ссылка-приглашение в закрытый канал
        invite = await bot.create_chat_invite_link(
            chat_id=CHANNEL_ID, member_limit=1,
            name=f"sub {user_id}",
        )
        expires = datetime.fromtimestamp(expires_at).strftime("%d.%m.%Y")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔓 Войти в канал", url=invite.invite_link)],
            [InlineKeyboardButton(text="⬅️ В меню", callback_data="to_menu")],
        ])
        await bot.send_message(
            user_id,
            "Готово! 🎉\n\n"
            f"Подписка активна до <b>{expires}</b>.\n"
            "Нажми кнопку ниже, чтобы войти в закрытый Beauty Guide 🤍\n\n"
            "Ссылка персональная и работает один раз.",
            reply_markup=kb,
        )
        # уведомление заказчице
        if ADMIN_ID:
            uname = f"@{username}" if username else "без username"
            await bot.send_message(
                ADMIN_ID,
                f"🔔 Новая подписка\n{full_name} ({uname})\nдо {expires}"
            )
    except Exception as e:
        logging.error(f"Ошибка выдачи доступа: {e}")
        await bot.send_message(
            user_id,
            "Оплата прошла, но не получилось создать ссылку на канал. "
            "Напиши, пожалуйста, в поддержку — доступ откроют вручную."
        )


@dp.message()
async def fallback(message: Message):
    await message.answer(
        "Нажми /start, чтобы открыть меню 🤍", reply_markup=main_menu()
    )


# ============================================================
# ФОНОВАЯ ПРОВЕРКА — раз в час убирает тех, у кого подписка истекла
# ============================================================
async def check_expired_loop():
    while True:
        try:
            for user_id in db_get_expired():
                try:
                    # удаляем из канала и сразу разбаниваем,
                    # чтобы человек мог вступить снова после новой оплаты
                    await bot.ban_chat_member(CHANNEL_ID, user_id)
                    await bot.unban_chat_member(CHANNEL_ID, user_id)
                    db_deactivate(user_id)
                    await bot.send_message(
                        user_id,
                        "Твоя подписка на Beauty Guide закончилась 🤍\n\n"
                        "Доступ к каналу закрыт. Чтобы вернуться — "
                        "оформи подписку снова через /start"
                    )
                    logging.info(f"Подписка истекла, удалён: {user_id}")
                except Exception as e:
                    logging.error(f"Не удалось удалить {user_id}: {e}")
        except Exception as e:
            logging.error(f"Ошибка проверки подписок: {e}")
        await asyncio.sleep(3600)  # пауза 1 час


# ============================================================
# ОБРАБОТКА ОПЛАТЫ ОТ WAYFORPAY
# ============================================================
async def wayforpay_callback(request: web.Request):
    """
    Сюда WayForPay присылает подтверждение оплаты.
    Полная проверка подписи будет добавлена вместе с интеграцией,
    когда заказчица предоставит ключи мерчанта.
    """
    try:
        data = await request.json()
        logging.info(f"WayForPay callback: {data}")
        # TODO: проверить подпись, извлечь user_id из orderReference,
        #       при успешной оплате вызвать grant_access(...)
    except Exception as e:
        logging.error(f"Ошибка callback WayForPay: {e}")
    return web.json_response({"status": "accept"})


# ============================================================
# ЗАПУСК
# ============================================================
async def on_startup(app: web.Application):
    db_init()
    await bot.set_webhook(WEBHOOK_URL, drop_pending_updates=True)
    asyncio.create_task(check_expired_loop())
    logging.info(f"Бот запущен. Webhook: {WEBHOOK_URL}")


async def on_shutdown(app: web.Application):
    await bot.delete_webhook()


async def health(request):
    return web.Response(text="Beauty Guide bot is running")


def main():
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_post(WFP_CALLBACK_PATH, wayforpay_callback)
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)

    web.run_app(app, host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()

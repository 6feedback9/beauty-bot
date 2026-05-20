# -*- coding: utf-8 -*-
"""
Beauty Guide — бот подписки на закрытый Telegram-канал.

  девочка заходит -> читает о Beauty Guide и FAQ ->
  оформляет подписку (оплата картой через WayForPay) ->
  бот выдаёт персональную ссылку в закрытый канал ->
  через 30 дней подписка истекает, бот убирает её из канала.

Технологии:
  Python + aiogram 3 + aiohttp (webhook) + PostgreSQL (хранение подписок).
  Оплата — WayForPay Invoice API.
"""

import os
import hmac
import hashlib
import json
import logging
import time
import asyncio
from datetime import datetime

import asyncpg
from aiohttp import web, ClientSession
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
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = os.getenv("ADMIN_ID")
CHANNEL_ID = os.getenv("CHANNEL_ID")
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST")

# база данных PostgreSQL (Render даёт строку подключения)
DATABASE_URL = os.getenv("DATABASE_URL")

# WayForPay
WFP_MERCHANT = os.getenv("WFP_MERCHANT")          # merchantAccount
WFP_SECRET = os.getenv("WFP_SECRET")              # merchantSecretKey
WFP_DOMAIN = os.getenv("WFP_DOMAIN", "")          # домен, привязанный в кабинете WayForPay

# подписка
SUB_PRICE = os.getenv("SUB_PRICE", "300")         # цена в гривнах (строкой)
SUB_DAYS = int(os.getenv("SUB_DAYS", "30"))       # длительность в днях

WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"
WFP_CALLBACK_PATH = "/wayforpay-callback"          # сюда WayForPay шлёт результат оплаты
WFP_API_URL = "https://api.wayforpay.com/api"
PORT = int(os.getenv("PORT", 10000))

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

db_pool: asyncpg.Pool | None = None  # пул соединений с базой


# ============================================================
# БАЗА ДАННЫХ (PostgreSQL)
# ============================================================
async def db_init():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL)
    async with db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS subs (
                user_id    BIGINT PRIMARY KEY,
                username   TEXT,
                full_name  TEXT,
                expires_at BIGINT,
                active     BOOLEAN DEFAULT TRUE
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                order_ref TEXT PRIMARY KEY,
                user_id   BIGINT,
                paid      BOOLEAN DEFAULT FALSE
            )
        """)
    logging.info("База данных готова")


async def db_create_order(order_ref, user_id):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO orders (order_ref, user_id, paid) VALUES ($1, $2, FALSE)",
            order_ref, user_id,
        )


async def db_get_order(order_ref):
    async with db_pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT user_id, paid FROM orders WHERE order_ref = $1", order_ref
        )


async def db_mark_order_paid(order_ref):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE orders SET paid = TRUE WHERE order_ref = $1", order_ref
        )


async def db_set_subscription(user_id, username, full_name, expires_at):
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO subs (user_id, username, full_name, expires_at, active)
            VALUES ($1, $2, $3, $4, TRUE)
            ON CONFLICT (user_id) DO UPDATE SET
                username = EXCLUDED.username,
                full_name = EXCLUDED.full_name,
                expires_at = EXCLUDED.expires_at,
                active = TRUE
        """, user_id, username, full_name, expires_at)


async def db_get_expired():
    now = int(time.time())
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT user_id FROM subs WHERE expires_at < $1 AND active = TRUE", now
        )
    return [r["user_id"] for r in rows]


async def db_deactivate(user_id):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE subs SET active = FALSE WHERE user_id = $1", user_id
        )


async def db_get_user(user_id):
    async with db_pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT expires_at, active FROM subs WHERE user_id = $1", user_id
        )


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
# WAYFORPAY — подписи и создание счёта
# ============================================================
def _hmac_md5(secret: str, parts: list) -> str:
    """HMAC_MD5 от строки параметров, склеенных через ';'."""
    base = ";".join(str(p) for p in parts)
    return hmac.new(secret.encode("utf-8"),
                    base.encode("utf-8"),
                    hashlib.md5).hexdigest()


async def create_invoice(user_id: int) -> str:
    """
    Создаёт счёт в WayForPay и возвращает ссылку на оплату (invoiceUrl).
    При ошибке возвращает пустую строку.
    """
    if not (WFP_MERCHANT and WFP_SECRET and WFP_DOMAIN):
        return ""  # ключи ещё не настроены

    order_ref = f"sub-{user_id}-{int(time.time())}"
    order_date = int(time.time())
    product_name = "Подписка Beauty Guide"
    product_price = SUB_PRICE
    product_count = "1"

    # подпись запроса: merchantAccount, merchantDomainName, orderReference,
    # orderDate, amount, currency, productName[], productCount[], productPrice[]
    signature = _hmac_md5(WFP_SECRET, [
        WFP_MERCHANT, WFP_DOMAIN, order_ref, order_date,
        SUB_PRICE, "UAH", product_name, product_count, product_price,
    ])

    payload = {
        "transactionType": "CREATE_INVOICE",
        "merchantAccount": WFP_MERCHANT,
        "merchantAuthType": "SimpleSignature",
        "merchantDomainName": WFP_DOMAIN,
        "merchantSignature": signature,
        "apiVersion": 1,
        "language": "UA",
        "serviceUrl": f"{WEBHOOK_HOST}{WFP_CALLBACK_PATH}",
        "orderReference": order_ref,
        "orderDate": order_date,
        "amount": SUB_PRICE,
        "currency": "UAH",
        "orderTimeout": 86400,
        "productName": [product_name],
        "productPrice": [product_price],
        "productCount": [product_count],
    }

    try:
        await db_create_order(order_ref, user_id)
        async with ClientSession() as session:
            async with session.post(WFP_API_URL, json=payload) as resp:
                data = await resp.json(content_type=None)
        logging.info(f"WayForPay create_invoice ответ: {data}")
        if data.get("invoiceUrl"):
            return data["invoiceUrl"]
        logging.error(f"WayForPay не вернул invoiceUrl: {data}")
        return ""
    except Exception as e:
        logging.error(f"Ошибка создания счёта WayForPay: {e}")
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
    row = await db_get_user(call.from_user.id)
    if row and row["active"]:
        expires = datetime.fromtimestamp(row["expires_at"]).strftime("%d.%m.%Y")
        text = (
            "🔑 <b>Твоя подписка активна</b>\n\n"
            f"Доступ к каналу открыт до <b>{expires}</b>.\n"
            "После этой даты доступ закроется — продлить можно здесь же."
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
    await call.answer()
    wait = await call.message.answer("Создаю счёт на оплату, секунду… ⏳")

    pay_link = await create_invoice(call.from_user.id)

    if pay_link:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Перейти к оплате", url=pay_link)],
            [InlineKeyboardButton(text="⬅️ В меню", callback_data="to_menu")],
        ])
        await wait.edit_text(
            f"<b>Подписка на Beauty Guide</b>\n\n"
            f"Стоимость: {SUB_PRICE} грн\n"
            f"Срок доступа: {SUB_DAYS} дней\n\n"
            "Нажми «Перейти к оплате» и оплати картой. "
            "Сразу после оплаты бот пришлёт ссылку на закрытый канал 🤍",
            reply_markup=kb,
        )
    else:
        await wait.edit_text(
            "Не получилось создать счёт на оплату 😔\n\n"
            "Попробуй ещё раз чуть позже или напиши в поддержку.",
            reply_markup=back_kb(),
        )


async def grant_access(user_id, username, full_name):
    """Открывает доступ: сохраняет подписку и присылает ссылку в канал."""
    expires_at = int(time.time()) + SUB_DAYS * 86400
    await db_set_subscription(user_id, username or "", full_name or "", expires_at)

    try:
        invite = await bot.create_chat_invite_link(
            chat_id=CHANNEL_ID, member_limit=1, name=f"sub {user_id}",
        )
        expires = datetime.fromtimestamp(expires_at).strftime("%d.%m.%Y")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔓 Войти в канал", url=invite.invite_link)],
            [InlineKeyboardButton(text="⬅️ В меню", callback_data="to_menu")],
        ])
        await bot.send_message(
            user_id,
            "Оплата прошла, спасибо! 🎉\n\n"
            f"Подписка активна до <b>{expires}</b>.\n"
            "Нажми кнопку ниже, чтобы войти в закрытый Beauty Guide 🤍\n\n"
            "Ссылка персональная и работает один раз.",
            reply_markup=kb,
        )
        if ADMIN_ID:
            uname = f"@{username}" if username else "без username"
            await bot.send_message(
                ADMIN_ID,
                f"🔔 Новая оплаченная подписка\n{full_name} ({uname})\nдо {expires}"
            )
    except Exception as e:
        logging.error(f"Ошибка выдачи доступа {user_id}: {e}")
        await bot.send_message(
            user_id,
            "Оплата прошла, но не получилось создать ссылку на канал. "
            "Напиши, пожалуйста, в поддержку — доступ откроют вручную."
        )


@dp.message()
async def fallback(message: Message):
    await message.answer("Нажми /start, чтобы открыть меню 🤍",
                         reply_markup=main_menu())


# ============================================================
# ОБРАБОТКА ОПЛАТЫ ОТ WAYFORPAY (serviceUrl)
# ============================================================
async def wayforpay_callback(request: web.Request):
    """
    WayForPay присылает сюда результат оплаты.
    Проверяем подпись, при успехе выдаём доступ, отвечаем подписанным accept.
    """
    try:
        data = await request.json()
    except Exception:
        # WayForPay иногда шлёт form-data, где JSON лежит ключом
        raw = await request.text()
        try:
            data = json.loads(raw)
        except Exception:
            data = json.loads(list(dict(await request.post()).keys())[0])

    logging.info(f"WayForPay callback: {data}")

    order_ref = data.get("orderReference", "")
    status = data.get("transactionStatus", "")

    # проверка подписи входящего запроса:
    # merchantAccount, orderReference, amount, currency,
    # authCode, cardPan, transactionStatus, reasonCode
    expected_sig = _hmac_md5(WFP_SECRET, [
        data.get("merchantAccount", ""),
        order_ref,
        data.get("amount", ""),
        data.get("currency", ""),
        data.get("authCode", ""),
        data.get("cardPan", ""),
        status,
        data.get("reasonCode", ""),
    ])

    if data.get("merchantSignature") != expected_sig:
        logging.error("WayForPay callback: подпись не совпала — запрос отклонён")
        return web.json_response({"status": "reject"})

    # подпись верна — обрабатываем оплату
    if status == "Approved":
        order = await db_get_order(order_ref)
        if order and not order["paid"]:
            await db_mark_order_paid(order_ref)
            user_id = order["user_id"]
            try:
                member = await bot.get_chat(user_id)
                username = member.username
                full_name = member.full_name
            except Exception:
                username, full_name = "", ""
            await grant_access(user_id, username, full_name)

    # ответ WayForPay: подпись из orderReference, status, time
    resp_time = int(time.time())
    resp_status = "accept"
    resp_sig = _hmac_md5(WFP_SECRET, [order_ref, resp_status, resp_time])
    return web.json_response({
        "orderReference": order_ref,
        "status": resp_status,
        "time": resp_time,
        "signature": resp_sig,
    })


# ============================================================
# ФОНОВАЯ ПРОВЕРКА — раз в час убирает истёкшие подписки
# ============================================================
async def check_expired_loop():
    while True:
        try:
            for user_id in await db_get_expired():
                try:
                    await bot.ban_chat_member(CHANNEL_ID, user_id)
                    await bot.unban_chat_member(CHANNEL_ID, user_id)
                    await db_deactivate(user_id)
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
        await asyncio.sleep(3600)


# ============================================================
# ЗАПУСК
# ============================================================
async def on_startup(app: web.Application):
    await db_init()
    await bot.set_webhook(WEBHOOK_URL, drop_pending_updates=True)
    asyncio.create_task(check_expired_loop())
    logging.info(f"Бот запущен. Webhook: {WEBHOOK_URL}")


async def on_shutdown(app: web.Application):
    await bot.delete_webhook()
    if db_pool:
        await db_pool.close()


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

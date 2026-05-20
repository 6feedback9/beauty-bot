# -*- coding: utf-8 -*-
"""
Бот для beauty-гайда: подбор ухода за кожей.
Клиентка выбирает тариф, проходит анкету, загружает фото,
заявка уходит заказчице в личку.

Технологии: Python + aiogram 3 + aiohttp (webhook для Render).
"""

import os
import logging
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, CallbackQuery, FSInputFile,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
)
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

# ============================================================
# НАСТРОЙКИ — берутся из переменных окружения на Render
# ============================================================
BOT_TOKEN = os.getenv("BOT_TOKEN")          # токен от BotFather
ADMIN_ID = os.getenv("ADMIN_ID")            # Telegram ID заказчицы (число)
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST")    # адрес сервиса на Render, напр. https://beauty-bot.onrender.com
WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"
PORT = int(os.getenv("PORT", 10000))        # Render сам подставляет PORT

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())

# ============================================================
# ТАРИФЫ — редактируй текст и цены под себя
# ============================================================
TARIFFS = {
    "express": {
        "name": "Экспресс-разбор",
        "price": "300 грн",
        "desc": (
            "Анализ средств, которыми ты уже пользуешься. "
            "Скажу, что работает, что лишнее, чего не хватает.\n"
            "Срок выполнения: 1–2 дня."
        ),
    },
    "full": {
        "name": "Полный разбор",
        "price": "600 грн",
        "desc": (
            "Разбор текущего ухода + подбор новой рутины под твою кожу: "
            "утро/вечер, конкретные средства и порядок нанесения.\n"
            "Срок выполнения: 2–3 дня."
        ),
    },
    "vip": {
        "name": "VIP-разбор",
        "price": "1200 грн",
        "desc": (
            "Полный разбор + видеоконсультация + сопровождение 2 недели: "
            "можешь задавать вопросы по ходу.\n"
            "Срок выполнения: 3–4 дня."
        ),
    },
}

# ============================================================
# СОСТОЯНИЯ АНКЕТЫ (FSM — пошаговый сбор данных)
# ============================================================
class Survey(StatesGroup):
    skin_type = State()      # тип кожи
    age = State()            # возраст
    problems = State()       # проблемы кожи
    allergy = State()        # аллергии
    budget = State()         # бюджет на уход
    city = State()           # город
    photo_face = State()     # фото лица
    photo_products = State() # фото средств
    phone = State()          # телефон


# ============================================================
# КЛАВИАТУРЫ
# ============================================================
def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 Услуги и цены", callback_data="services")],
        [InlineKeyboardButton(text="📝 Записаться на разбор", callback_data="order")],
        [InlineKeyboardButton(text="❓ Частые вопросы", callback_data="faq")],
    ])


def tariffs_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            text=f"{t['name']} — {t['price']}",
            callback_data=f"choose_{key}"
        )]
        for key, t in TARIFFS.items()
    ]
    rows.append([InlineKeyboardButton(text="⬅️ В меню", callback_data="to_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def skin_type_kb() -> InlineKeyboardMarkup:
    types = ["Сухая", "Жирная", "Комбинированная", "Нормальная", "Не знаю"]
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=f"skin_{t}")] for t in types
    ])


def phone_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Отправить мой номер", request_contact=True)]],
        resize_keyboard=True, one_time_keyboard=True,
    )


# ============================================================
# СТАРТ И МЕНЮ
# ============================================================
@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Привет! 🌿\n\n"
        "Это бот по подбору ухода за кожей. "
        "Здесь ты можешь записаться на персональный разбор: "
        "я посмотрю, чем ты пользуешься, и подберу то, что подойдёт именно тебе.\n\n"
        "С чего начнём?",
        reply_markup=main_menu(),
    )


@dp.callback_query(F.data == "to_menu")
async def to_menu(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.answer("Главное меню:", reply_markup=main_menu())
    await call.answer()


@dp.callback_query(F.data == "services")
async def show_services(call: CallbackQuery):
    text = "<b>Услуги и цены:</b>\n\n"
    for t in TARIFFS.values():
        text += f"<b>{t['name']} — {t['price']}</b>\n{t['desc']}\n\n"
    await call.message.answer(text, reply_markup=tariffs_kb())
    await call.answer()


@dp.callback_query(F.data == "faq")
async def show_faq(call: CallbackQuery):
    await call.message.answer(
        "<b>Частые вопросы</b>\n\n"
        "<b>Как проходит разбор?</b>\n"
        "Ты заполняешь анкету и присылаешь фото. Я анализирую и присылаю "
        "результат текстом, а для VIP — видео.\n\n"
        "<b>Нужно ли фото без макияжа?</b>\n"
        "Да, так я точнее оценю состояние кожи.\n\n"
        "<b>Сколько ждать результат?</b>\n"
        "От 1 до 4 дней в зависимости от тарифа.\n\n"
        "<b>Можно ли задать вопрос после разбора?</b>\n"
        "Да, особенно на VIP-тарифе — там сопровождение 2 недели.",
        reply_markup=main_menu(),
    )
    await call.answer()


# ============================================================
# НАЧАЛО АНКЕТЫ
# ============================================================
@dp.callback_query(F.data == "order")
async def start_order(call: CallbackQuery):
    await call.message.answer("Выбери тариф:", reply_markup=tariffs_kb())
    await call.answer()


@dp.callback_query(F.data.startswith("choose_"))
async def choose_tariff(call: CallbackQuery, state: FSMContext):
    key = call.data.replace("choose_", "")
    tariff = TARIFFS[key]
    await state.update_data(tariff=tariff["name"], tariff_price=tariff["price"])
    await call.message.answer(
        f"Отлично, ты выбрала <b>{tariff['name']}</b> ({tariff['price']}).\n\n"
        "Теперь несколько вопросов, чтобы разбор был точным.\n\n"
        "<b>1/9.</b> Какой у тебя тип кожи?",
        reply_markup=skin_type_kb(),
    )
    await state.set_state(Survey.skin_type)
    await call.answer()


@dp.callback_query(Survey.skin_type, F.data.startswith("skin_"))
async def get_skin(call: CallbackQuery, state: FSMContext):
    await state.update_data(skin_type=call.data.replace("skin_", ""))
    await call.message.answer("<b>2/9.</b> Сколько тебе лет? (просто число)")
    await state.set_state(Survey.age)
    await call.answer()


@dp.message(Survey.age)
async def get_age(message: Message, state: FSMContext):
    await state.update_data(age=message.text)
    await message.answer(
        "<b>3/9.</b> Какие проблемы кожи тебя беспокоят?\n"
        "Опиши своими словами: высыпания, сухость, жирный блеск, "
        "пигментация, покраснения, расширенные поры и т.д."
    )
    await state.set_state(Survey.problems)


@dp.message(Survey.problems)
async def get_problems(message: Message, state: FSMContext):
    await state.update_data(problems=message.text)
    await message.answer(
        "<b>4/9.</b> Есть ли аллергии или непереносимость каких-то компонентов?\n"
        "Если нет — напиши «нет»."
    )
    await state.set_state(Survey.allergy)


@dp.message(Survey.allergy)
async def get_allergy(message: Message, state: FSMContext):
    await state.update_data(allergy=message.text)
    await message.answer(
        "<b>5/9.</b> Какой примерный бюджет на уход в месяц? "
        "Это поможет подобрать средства по карману."
    )
    await state.set_state(Survey.budget)


@dp.message(Survey.budget)
async def get_budget(message: Message, state: FSMContext):
    await state.update_data(budget=message.text)
    await message.answer("<b>6/9.</b> В каком городе ты находишься?")
    await state.set_state(Survey.city)


@dp.message(Survey.city)
async def get_city(message: Message, state: FSMContext):
    await state.update_data(city=message.text)
    await message.answer(
        "<b>7/9.</b> Пришли фото лица без макияжа. 📸\n"
        "Лучше при дневном свете. Можно одно фото."
    )
    await state.set_state(Survey.photo_face)


@dp.message(Survey.photo_face, F.photo)
async def get_photo_face(message: Message, state: FSMContext):
    # сохраняем file_id самого крупного варианта фото
    await state.update_data(photo_face=message.photo[-1].file_id)
    await message.answer(
        "<b>8/9.</b> Теперь пришли фото средств, которыми пользуешься сейчас. 🧴\n"
        "Можно одним фото, где всё вместе."
    )
    await state.set_state(Survey.photo_products)


@dp.message(Survey.photo_face)
async def photo_face_wrong(message: Message):
    await message.answer("Пожалуйста, пришли именно фото 📸")


@dp.message(Survey.photo_products, F.photo)
async def get_photo_products(message: Message, state: FSMContext):
    await state.update_data(photo_products=message.photo[-1].file_id)
    await message.answer(
        "<b>9/9.</b> Последний шаг — оставь номер телефона для связи.\n"
        "Нажми кнопку ниже или впиши номер вручную.",
        reply_markup=phone_kb(),
    )
    await state.set_state(Survey.phone)


@dp.message(Survey.photo_products)
async def photo_products_wrong(message: Message):
    await message.answer("Пожалуйста, пришли именно фото 🧴")


# ============================================================
# ЗАВЕРШЕНИЕ — отправка заявки заказчице
# ============================================================
@dp.message(Survey.phone)
async def finish_survey(message: Message, state: FSMContext):
    # телефон может прийти как контакт или как текст
    phone = message.contact.phone_number if message.contact else message.text
    await state.update_data(phone=phone)
    data = await state.get_data()

    user = message.from_user
    username = f"@{user.username}" if user.username else "без username"

    # текст заявки для заказчицы
    summary = (
        "🔔 <b>НОВАЯ ЗАЯВКА НА РАЗБОР</b>\n\n"
        f"<b>Тариф:</b> {data.get('tariff')} ({data.get('tariff_price')})\n"
        f"<b>Клиент:</b> {user.full_name} ({username})\n"
        f"<b>ID:</b> <code>{user.id}</code>\n"
        f"<b>Телефон:</b> {data.get('phone')}\n\n"
        f"<b>Тип кожи:</b> {data.get('skin_type')}\n"
        f"<b>Возраст:</b> {data.get('age')}\n"
        f"<b>Проблемы:</b> {data.get('problems')}\n"
        f"<b>Аллергии:</b> {data.get('allergy')}\n"
        f"<b>Бюджет:</b> {data.get('budget')}\n"
        f"<b>Город:</b> {data.get('city')}\n\n"
        "Фото — ниже 👇"
    )

    try:
        await bot.send_message(ADMIN_ID, summary)
        await bot.send_photo(ADMIN_ID, data.get("photo_face"), caption="Фото лица")
        await bot.send_photo(ADMIN_ID, data.get("photo_products"), caption="Текущие средства")
    except Exception as e:
        logging.error(f"Не удалось отправить заявку админу: {e}")

    await message.answer(
        "Готово! 🎉\n\n"
        "Твоя заявка принята. Скоро с тобой свяжутся, "
        "и ты получишь персональный разбор в срок по выбранному тарифу.\n\n"
        "Спасибо за доверие! 🌿",
        reply_markup=ReplyKeyboardRemove(),
    )
    await message.answer("Главное меню:", reply_markup=main_menu())
    await state.clear()


# запасной обработчик — если человек пишет что-то вне сценария
@dp.message()
async def fallback(message: Message):
    await message.answer(
        "Не совсем поняла 🙈 Нажми /start, чтобы открыть меню.",
        reply_markup=main_menu(),
    )


# ============================================================
# ЗАПУСК ЧЕРЕЗ WEBHOOK (для Render)
# ============================================================
async def on_startup(app: web.Application):
    await bot.set_webhook(WEBHOOK_URL, drop_pending_updates=True)
    logging.info(f"Webhook установлен: {WEBHOOK_URL}")


async def on_shutdown(app: web.Application):
    await bot.delete_webhook()
    logging.info("Webhook удалён")


# простой ответ для проверки, что сервис жив
async def health(request):
    return web.Response(text="Bot is running")


def main():
    app = web.Application()
    app.router.add_get("/", health)
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)

    web.run_app(app, host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()

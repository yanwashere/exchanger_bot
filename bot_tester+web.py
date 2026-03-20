import logging  # <-- Можно для отладки
import sqlite3
import os
import math
import json
from datetime import datetime, timedelta
import nest_asyncio
import asyncio
from pathlib import Path
import aiohttp
import base64

from aiogram import Bot, Dispatcher, types
from aiogram.types import (
    ReplyKeyboardMarkup,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)
from aiogram.contrib.middlewares.logging import LoggingMiddleware
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram import executor
from aiogram.dispatcher.handler import CancelHandler
from aiogram.dispatcher.middlewares import BaseMiddleware
from aiogram.utils.executor import start_polling
from aiogram.types import BotCommand, BotCommandScopeDefault, BotCommandScopeChat, BotCommandScopeAllGroupChats, Message, CallbackQuery

nest_asyncio.apply()

# ---- Настройка логирования (по желанию) ----
logging.basicConfig(level=logging.INFO)

# ==== Ваши данные (ТЕСТОВЫЕ) ====
API_TOKEN = '8675618937:AAHMOyQ-LxoZj9_93i6ByQtERd6SwnoqrL8'
MODERATOR_CHAT_ID = 7064365721
PARTNER_CHAT_ID = -5203525715  # рабочий чат партнёров
# ===============================


# ── 1. НАСТРОЙКИ (добавить в начало бота рядом с API_TOKEN) ──
GITHUB_TOKEN     = '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' 
_github_last_pushed = None  # глобальная переменная — добавь рядом с GITHUB_TOKEN         # Personal Access Token (repo scope)
GITHUB_REPO      = 'yanwashere/exchange'      # имя репозитория
GITHUB_FILE_PATH = 'exchange_rate.json'       # путь к файлу в репо
SETTINGS_FILE    = Path('exchange_rate.json') # локальный файл (уже есть в боте)

db_path = "finance.db"
backup_folder = "backups"
os.makedirs(backup_folder, exist_ok=True)

bot = Bot(token=API_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)
dp.middleware.setup(LoggingMiddleware())



active_dialogs = {}
active_verify_dialogs = {}  # {user_id: moderator_id} — открытые диалоги верификации ЛК

# ==== NEW: глобальная переменная для остановки/возобновления бота ====
BOT_STOPPED = False

# === VIP LOGIC START ===
# Список VIP-пользователей (chat_id -> скидка)
# Например: {7064365721: 0.9} => коэффициент 0.9 => скидка 10% на курс
VIP_USERS = {
    1024314885: 0.991,#Аня 
    #900728983: 0.991, #Даша ...
    702546180: 0.991, #Валя Можно добавить ещё
    # Примечание: MODERATOR_CHAT_ID тоже можно сделать VIP, если захотите
}
# === VIP LOGIC END ===

# =========================================================
# =============== Работа с настройками (JSON) =============
# =========================================================

# Команды для обычных пользователей
user_commands = [
    BotCommand(command="rate", description="Курс"),
    BotCommand(command="profile", description="Личный кабинет"),
    BotCommand(command="exchange", description="Создать заявку"),
    BotCommand(command="ref", description="Реферальная программа"),
    BotCommand(command="menu", description="Главное меню"),
    BotCommand(command="start", description="Перезагрузить бота"),
]

# Команды для модератора
moderator_commands = [
    BotCommand(command="rate", description="Курс"),
    BotCommand(command="add_income", description="Добавить доход"),
    BotCommand(command="summary", description="Сумма за период"),
    BotCommand(command="start", description="Перезагрузить бота"), 
]

# Установка команд при запуске бота
async def set_bot_commands():
    await bot.set_my_commands(user_commands)  # для всех по умолчанию
    await bot.set_my_commands(moderator_commands, scope=BotCommandScopeChat(chat_id=MODERATOR_CHAT_ID))
    await bot.set_my_commands([], scope=BotCommandScopeAllGroupChats())  # убираем команды в группах

# Черный список бд

BLACKLIST_PATH = Path("blacklist.json")

def ensure_blacklist_exists():
    if not BLACKLIST_PATH.exists():
        with open(BLACKLIST_PATH, "w") as f:
            json.dump([], f)


def load_blacklist():
    if not BLACKLIST_PATH.exists():
        return set()
    with open(BLACKLIST_PATH, "r") as f:
        return set(json.load(f))
    
class BanMiddleware(BaseMiddleware):
    async def on_pre_process_message(self, message: Message, data: dict):
        if message.from_user.id in load_blacklist():
            await message.answer("Вам ограничен доступ к сервису.")
            raise CancelHandler()

    async def on_pre_process_callback_query(self, callback: CallbackQuery, data: dict):
        if callback.from_user.id in load_blacklist():
            await callback.answer("Вам ограничен доступ к сервису.", show_alert=True)
            raise CancelHandler()

# ==== JSON‑настройки ====
SETTINGS_FILE = Path('exchange_rate.json')

def load_settings():
    defaults = {
        'rub': 15.4,
        'min_amount': 300.0,
        'max_amount': 10_000.0,
        'work_time': {'start_h': 9, 'end_h': 18},
        'usdt_cny': 6.5,
        'rub_usdt_bonus': 77,
        'usdt_address': 'TXXXXXXXXXXXXXXXXXXXX'
    }
    if not SETTINGS_FILE.exists():
        SETTINGS_FILE.write_text(json.dumps(defaults, ensure_ascii=False, indent=4))
        return defaults
    data = json.loads(SETTINGS_FILE.read_text())
    changed = False
    for k, v in defaults.items():
        if k not in data:
            data[k] = v; changed = True
    if changed:
        SETTINGS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=4))
    return data

def save_settings(d):
    SETTINGS_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=4))


# =========================================================
# =============== Middleware для остановки бота ===========
# =========================================================


# ==========Классы для просмотра лк пользователей=========
class AdminUserLookup(StatesGroup):
    waiting_for_chat_id = State()

class AdminUserEdit(StatesGroup):
    waiting_for_chat_id = State()
    waiting_for_name = State()
    waiting_for_phone = State()
    waiting_for_qr_photo = State()


class BanUserState(StatesGroup):
    waiting_for_chat_id = State()

class VerifyDialog(StatesGroup):
    active = State()  # модератор ведёт диалог по верификации ЛК


class StopBotMiddleware(BaseMiddleware):
    """
    Если BOT_STOPPED=True и пользователь не админ,
    отправляем заглушку и отменяем дальнейшую обработку.
    """
    async def on_pre_process_message(self, message: types.Message, data: dict):
        global BOT_STOPPED
        if message.chat.id == MODERATOR_CHAT_ID:
            return
        if message.chat.type in ("group", "supergroup"):
            return
        if BOT_STOPPED:
            await message.answer("🚧Проводятся технические работы. Скоро работа бота восстановится")
            raise CancelHandler()

# =========================================================
# ============== Middleware для рабочих часов =============
# =========================================================

class WorkingHoursMiddleware(BaseMiddleware):
    """
    Проверяем, не вне ли рабочего времени (по Пекину).
    Если вне рабочего времени и пользователь не админ — отвечаем заглушкой.
    """
    async def on_pre_process_message(self, message: types.Message, data: dict):
        if message.chat.id == MODERATOR_CHAT_ID:
            return
        if message.chat.type in ("group", "supergroup"):
            return

        settings = load_settings()
        work_time = settings.get("work_time", {"start_h": 9, "end_h": 18})
        start_h = work_time.get("start_h", 9)
        end_h   = work_time.get("end_h", 18)

        utc_now = datetime.utcnow()
        beijing_now = utc_now + timedelta(hours=8)
        current_minutes = beijing_now.hour * 60 + beijing_now.minute
        start_minutes = start_h * 60
        end_minutes = end_h * 60 + 59

        if not (start_minutes <= current_minutes <= end_minutes):
            msg_text = (
                "Сейчас не рабочее время бота.\n"
                f"Наш график: с {start_h}:00 до {end_h}:00 по Пекину.\n"
                "Пожалуйста, повторите запрос в рабочее время."
            )
            await message.answer(msg_text)
            raise CancelHandler()

dp.middleware.setup(StopBotMiddleware())
dp.middleware.setup(WorkingHoursMiddleware())



# ===== Функция получения первой активной заявки ====

def get_current_order_id(chat_id):
    if chat_id in active_dialogs and active_dialogs[chat_id]:
        return sorted(active_dialogs[chat_id].keys())[0]
    return None

# === Создание базы данных бухгалтерии ===
def setup_finance_db():
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            date TEXT NOT NULL
        );
    """)
    conn.commit()
    conn.close()

setup_finance_db()


def init_databases():
    # balances
    conn=sqlite3.connect('user_balance.db'); cur=conn.cursor(); cur.execute('CREATE TABLE IF NOT EXISTS balances (user_id INTEGER PRIMARY KEY, balance INTEGER DEFAULT 0);'); conn.commit(); conn.close()
    # users
    conn=sqlite3.connect('users.db'); cur=conn.cursor(); cur.execute('''CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, chat_id INTEGER, username TEXT, name TEXT, phone TEXT, qr_photo TEXT, referrer INTEGER, is_registered INTEGER DEFAULT 0, is_verified INTEGER DEFAULT 0);'''); cur.execute("PRAGMA table_info(users)"); cols=[c[1] for c in cur.fetchall()]
    if 'is_verified' not in cols: cur.execute('ALTER TABLE users ADD COLUMN is_verified INTEGER DEFAULT 0')
    if 'old_name' not in cols: cur.execute('ALTER TABLE users ADD COLUMN old_name TEXT')
    if 'old_phone' not in cols: cur.execute('ALTER TABLE users ADD COLUMN old_phone TEXT')
    if 'old_qr_photo' not in cols: cur.execute('ALTER TABLE users ADD COLUMN old_qr_photo TEXT')
    conn.commit(); conn.close()
    # orders
    conn=sqlite3.connect('orders.db'); cur=conn.cursor(); cur.execute('''CREATE TABLE IF NOT EXISTS orders (order_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, chat_id INTEGER, rub_amount REAL, amount_usdt REAL, cny_amount REAL, rate REAL, created_at TEXT, status TEXT DEFAULT "pending", user_bank TEXT, user_order_number INTEGER, used_bonus INTEGER DEFAULT 0, referrer_id INTEGER, payment_currency TEXT DEFAULT "RUB");''');
    cur.execute('PRAGMA table_info(orders);'); cols=[c[1] for c in cur.fetchall()]
    if 'amount_usdt' not in cols: cur.execute('ALTER TABLE orders ADD COLUMN amount_usdt REAL;')
    if 'payment_currency' not in cols: cur.execute('ALTER TABLE orders ADD COLUMN payment_currency TEXT DEFAULT "RUB";')
    conn.commit(); conn.close()
init_databases()


# ---- Клавиатуры ----
main_menu = ReplyKeyboardMarkup(resize_keyboard=True).add(
    "Курс", "Личный кабинет", "Создать заявку", "Отзывы", "Реферальная программа"
)
registration_menu = ReplyKeyboardMarkup(resize_keyboard=True).add(
    "Зарегистрироваться", "Главное меню"
)
back_to_menu = ReplyKeyboardMarkup(resize_keyboard=True).add("Главное меню")

confirm_request_menu = InlineKeyboardMarkup(row_width=1)
confirm_request_menu.add(
    InlineKeyboardButton("Подтвердить заявку", callback_data="confirm_request"),
    InlineKeyboardButton("Отменить заявку", callback_data="cancel_request")
)

confirm_menu = ReplyKeyboardMarkup(resize_keyboard=True).add("Подтвердить", "Отменить")

personal_menu = ReplyKeyboardMarkup(resize_keyboard=True)
personal_menu.add("Обновить информацию", "Удалить информацию", "Главное меню")

admin_menu = ReplyKeyboardMarkup(resize_keyboard=True)
admin_menu.add("Изменить лимиты", "Изменить курс")
admin_menu.add("Общий анонс", "Написать пользователю")
admin_menu.add("Остановить бота", "Восстановить бота")
admin_menu.add("Изменить график работы","Лк пользователя")
admin_menu.add("Бухгалтерия","Создать бэкап")
admin_menu.add("Заблокировать пользователя","Изменить адрес USDT")


admin_menu.add("Главное меню")

# === Меню бухгалтера ===
finance_menu = ReplyKeyboardMarkup(resize_keyboard=True)
finance_menu.add("Добавить доход", "Добавить расход")
finance_menu.add("Сумма за период", "Главное меню")

# ======== Меню для просмотра лк пользователей =======
admin_user_menu = ReplyKeyboardMarkup(resize_keyboard=True)
admin_user_menu.add("Посмотреть ЛК пользователя", "Изменить данные ЛК пользователя")
admin_user_menu.add("Главное меню")

# === Бухгалтерия: состояния ===
class FinanceState(StatesGroup):
    waiting_for_income = State()
    waiting_for_expense = State()
    waiting_for_period_start = State()
    waiting_for_period_end = State()

# ---- Состояния ----
class Registration(StatesGroup):
    waiting_for_name = State()
    waiting_for_phone = State()
    waiting_for_qr_photo = State()

class UpdateInfo(StatesGroup):
    waiting_for_name = State()
    waiting_for_phone = State()
    waiting_for_qr_photo = State()

class RequestCreation(StatesGroup):
    waiting_for_amount_in_rub = State()
    waiting_for_amount_in_cny = State()
    waiting_for_amount_in_usdt = State()
    waiting_for_bank = State()
    waiting_for_confirmation = State()

class AdminSettings(StatesGroup):
    waiting_for_new_min = State()
    waiting_for_new_max = State()
    waiting_for_new_rate = State()
    waiting_for_work_start = State()
    waiting_for_work_end   = State()
    waiting_for_usdt_addr  = State()

class Feedback(StatesGroup):
    waiting_for_feedback = State()

class Broadcast(StatesGroup):
    waiting_for_text = State()

class PrivateMessage(StatesGroup):
    waiting_for_user_id = State()
    waiting_for_text = State()

# Функция бана

def add_to_blacklist(user_id: int):
    blacklist = load_blacklist()
    blacklist.add(user_id)
    with open("blacklist.json", "w") as f:
        json.dump(list(blacklist), f)


@dp.message_handler(lambda message: message.text == "Заблокировать пользователя")
async def block_user_prompt(message: types.Message, state: FSMContext):
    if message.from_user.id != MODERATOR_CHAT_ID:
        return  # Игнорируем всех, кроме модератора
    await message.answer("Введите chat_id пользователя, которого нужно заблокировать:")
    await state.set_state(BanUserState.waiting_for_chat_id)

@dp.message_handler(state=BanUserState.waiting_for_chat_id)
async def ban_user(message: types.Message, state: FSMContext):
    if message.from_user.id != MODERATOR_CHAT_ID:
        return await state.finish()  # Завершаем состояние, если это не модератор

    try:
        chat_id = int(message.text.strip())
        add_to_blacklist(chat_id)
        await message.answer(f"Пользователь {chat_id} добавлен в черный список.")
    except ValueError:
        await message.answer("Ошибка: chat_id должен быть числом.")
    await state.finish()


# ========== Функция вывода меню для просмотра лк пользователя ======
@dp.message_handler(lambda message: message.text == "Лк пользователя")
async def admin_user_menu_handler(message: types.Message, state: FSMContext):
    if message.chat.id != MODERATOR_CHAT_ID:
        return
    
    await state.finish()  # Сброс всех состояний на случай, если модератор вернулся сюда из FSM
    await message.answer("Работа с личными кабинетами пользователей:", reply_markup=admin_user_menu)



# ---- Функция корректного округления ----
def correct_rounding(amount):
    if (round(amount, 2) - round(amount)) > 0.5:
        return int(round(amount) + 1)
    else:
        return int(round(amount))

# === VIP HELPER FUNCTION ===
def get_effective_rate(chat_id: int) -> float:
    """
    Возвращает курс с учётом VIP-скидки (если пользователь в VIP_USERS).
    Если user_id нет в VIP_USERS, возвращаем обычный rate.
    """
    settings = load_settings()
    base_rate = settings.get("rub", 15.4)
    if chat_id in VIP_USERS:
        discount_coef = VIP_USERS[chat_id]  # например 0.9 => 10% скидка
        return round(base_rate * discount_coef, 2)
    else:
        return base_rate

        
        
# ---- Проверка и добавление баланса ----
def ensure_user_balance_exists(user_id: int):
    conn = sqlite3.connect("user_balance.db")
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM balances WHERE user_id = ?", (user_id,))
    exists = cursor.fetchone()
    if not exists:
        cursor.execute("INSERT INTO balances (user_id, balance) VALUES (?, ?)", (user_id, 0))
        conn.commit()
    conn.close()

# ---- Хендлеры start/help ----
@dp.message_handler(commands=["start", "help"])
async def send_welcome(message: types.Message):
    if message.chat.type in ("group", "supergroup"):
        return
    chat_id = message.chat.id
    username = message.from_user.username or "Нет"
    args = message.get_args()
    
    referrer_id = None
    if args.startswith("ref") and args[3:].isdigit():
        referrer_id = int(args[3:])
        if referrer_id == chat_id:
            referrer_id = None  # нельзя пригласить себя

    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE chat_id = ?", (chat_id,))
    existing = cursor.fetchone()

    if not existing:
        cursor.execute("""
            INSERT INTO users (chat_id, username, name, phone, qr_photo, referrer, is_registered)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (chat_id, username, "", "", "", referrer_id, 0))
        conn.commit()

    else:
        # обновим referrer, если он ещё не установлен
        if existing[6] is None and referrer_id:
            cursor.execute("UPDATE users SET referrer = ? WHERE chat_id = ?", (referrer_id, chat_id))
            conn.commit()

    conn.close()

    # ✅ Обеспечиваем наличие записи в balances
    ensure_user_balance_exists(chat_id)

    # --- UI в зависимости от роли ---
    if chat_id == MODERATOR_CHAT_ID:
        custom_main = ReplyKeyboardMarkup(resize_keyboard=True)
        custom_main.add("Курс", "Личный кабинет", "Создать заявку", "Отзывы", "Реферальная программа", "Админские настройки")
        await message.answer(
            "Привет!\nДобро пожаловать в наш обменник валюты!\nЗдесь можно пополнить баланс WeChat или Alipay аккаунта рублями или USDT\nЧтобы начать, выбери одну из опций ниже ⬇️",
            reply_markup=custom_main
        )
    else:
        await message.answer(
            "Привет!\nДобро пожаловать в наш обменник валюты!\nЗдесь можно пополнить баланс WeChat или Alipay аккаунта рублями или USDT\nЧтобы начать, выбери одну из опций ниже ⬇️",
            reply_markup=main_menu
        )
        

@dp.message_handler(lambda m: m.text == "Реферальная программа" or m.text == "/ref")
async def referral_program(message: types.Message):
    chat_id = message.chat.id

    # Считаем рефералов
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM users WHERE referrer = ?", (chat_id,))
    ref_count = cursor.fetchone()[0]
    conn.close()

    # Получаем баланс
    bconn = sqlite3.connect("user_balance.db")
    bcursor = bconn.cursor()
    bcursor.execute("SELECT balance FROM balances WHERE user_id = ?", (chat_id,))
    row = bcursor.fetchone()
    balance = row[0] if row else 0
    bconn.close()

    text = (
        f"👥 Ваши рефералы: {ref_count}\n"
        f"💰 Ваш баланс: {balance} бонусных рублей."
    )

    keyboard = InlineKeyboardMarkup().add(
        InlineKeyboardButton("О реферальной программе", url="https://t.me/LIVE_inc/16")
    )

    reply_kb = ReplyKeyboardMarkup(resize_keyboard=True)
    reply_kb.add("Получить реферальную ссылку", "Создать заявку")
    reply_kb.add("Главное меню")

    await message.answer(text, reply_markup=keyboard)
    await message.answer("Выберите действие:", reply_markup=reply_kb)

@dp.message_handler(lambda m: m.text == "Получить реферальную ссылку")
async def get_ref_link(message: types.Message):
    chat_id = message.chat.id
    bot_username = (await bot.get_me()).username
    link = f"https://t.me/{bot_username}?start=ref{chat_id}"

    await message.answer(
        f"🔗 Ваша реферальная ссылка:\n{link}",
        reply_markup=main_menu
    )


# =========Обновление адреса для получения юсдт========

# --- шаг 1: инициируем ввод адреса ---
@dp.message_handler(lambda m: m.text == "Изменить адрес USDT")
async def change_usdt_addr_start(message: types.Message, state: FSMContext):
    if message.chat.id != MODERATOR_CHAT_ID:          # не-админ → отказ
        return
    await AdminSettings.waiting_for_usdt_addr.set()
    await message.answer("Введите *новый* TRC-20-адрес для приёма USDT:",
                         reply_markup=back_to_menu,
                         parse_mode="Markdown")

# --- шаг 2: сохраняем адрес ---
@dp.message_handler(state=AdminSettings.waiting_for_usdt_addr)
async def save_new_usdt_addr(message: types.Message, state: FSMContext):
    if message.text == "Главное меню":                # пользователь передумал
        await go_to_main_menu(message, state)
        return

    addr = message.text.strip()

    # быстрая валидация TRC-20: начинается с T и ~34 символа
    if not (addr.startswith("T") and 30 <= len(addr) <= 40):
        return await message.answer("⚠️ Похоже, это не TRC-20-адрес. "
                                    "Попробуйте ещё раз или нажмите «Главное меню».")

    settings = load_settings()
    settings["usdt_address"] = addr
    save_settings(settings)

    await message.answer(f"✅ Адрес USDT сохранён:\n`{addr}`",
                         parse_mode="Markdown",
                         reply_markup=admin_menu)
    await state.finish()

        
#=====Бухгалтерия=======  
@dp.message_handler(lambda message: message.text == "Бухгалтерия")
async def open_finance_menu(message: types.Message):
    if message.chat.id == MODERATOR_CHAT_ID:
        await message.answer("Выберите действие:", reply_markup=finance_menu)

# === Добавление дохода ===
@dp.message_handler(lambda message: message.text == "Добавить доход" or message.text == "/add_income")
async def add_income(message: types.Message):
    if message.chat.id == MODERATOR_CHAT_ID:
        await FinanceState.waiting_for_income.set()
        await message.answer("Введите сумму дохода в USDT:")

@dp.message_handler(state=FinanceState.waiting_for_income)
async def save_income(message: types.Message, state: FSMContext):
    try:
        amount = float(message.text)
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("INSERT INTO transactions (chat_id, amount, date) VALUES (?, ?, ?)",
                       (message.chat.id, amount, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        conn.commit()
        conn.close()
        await state.finish()
        await message.answer("Доход успешно добавлен!", reply_markup=finance_menu)
    except ValueError:
        await message.answer("Ошибка: введите корректное число.")

# === Добавление расхода ===
@dp.message_handler(lambda message: message.text == "Добавить расход")
async def add_expense(message: types.Message):
    if message.chat.id == MODERATOR_CHAT_ID:
        await FinanceState.waiting_for_expense.set()
        await message.answer("Введите сумму расхода в USDT:")

@dp.message_handler(state=FinanceState.waiting_for_expense)
async def save_expense(message: types.Message, state: FSMContext):
    try:
        amount = -abs(float(message.text))
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("INSERT INTO transactions (chat_id, amount, date) VALUES (?, ?, ?)",
                       (message.chat.id, amount, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        conn.commit()
        conn.close()
        await state.finish()
        await message.answer("Расход успешно добавлен!", reply_markup=finance_menu)
    except ValueError:
        await message.answer("Ошибка: введите корректное число.")

# === Запрос суммы за период ===
@dp.message_handler(lambda message: message.text == "Сумма за период" or message.text == "/summary")
async def request_period_start(message: types.Message):
    if message.chat.id == MODERATOR_CHAT_ID:
        await FinanceState.waiting_for_period_start.set()
        await message.answer("Введите начальную дату периода (ГГГГ-ММ-ДД):")

@dp.message_handler(state=FinanceState.waiting_for_period_start)
async def request_period_end(message: types.Message, state: FSMContext):
    await state.update_data(start_date=message.text)
    await FinanceState.waiting_for_period_end.set()
    await message.answer("Введите конечную дату периода (ГГГГ-ММ-ДД):")

@dp.message_handler(state=FinanceState.waiting_for_period_end)
async def calculate_period_sum(message: types.Message, state: FSMContext):
    data = await state.get_data()
    start_date = data.get("start_date")
    end_date = message.text

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT SUM(amount) FROM transactions WHERE date >= ? AND date <= ?", (start_date, end_date + " 23:59:59"))
    total = cursor.fetchone()[0] or 0
    conn.close()

    await state.finish()
    await message.answer(f"Суммарный доход-расход за период {start_date} - {end_date}: {total:.2f} USDT", reply_markup=finance_menu)

async def calculate_period_sum(message: types.Message, state: FSMContext):
    data = await state.get_data()
    start_date = data.get("start_date")
    end_date = message.text

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT SUM(amount) FROM transactions WHERE date >= ? AND date <= ?", (start_date, end_date))
    total = cursor.fetchone()[0] or 0
    conn.close()

    await state.finish()
    await message.answer(f"Суммарный доход-расход за период {start_date} - {end_date}: {total:.2f} USDT", reply_markup=finance_menu)

# === Бэкап базы данных ===
def backup_databases():
    timestamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
    for db_name in ["users.db", "orders.db", "finance.db", "user_balance.db"]:
        backup_file = f"{backup_folder}/{timestamp}_{db_name}"
        if os.path.exists(db_name):
            os.system(f"cp {db_name} {backup_file}")

def manual_backup():
    backup_databases()
    for db_name in ["users.db", "orders.db", "finance.db", "user_balance.db"]:
        backup_file = f"{backup_folder}/{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}_{db_name}"
        if os.path.exists(backup_file):
            asyncio.create_task(bot.send_document(MODERATOR_CHAT_ID, types.InputFile(backup_file)))

@dp.message_handler(lambda message: message.text == "Создать бэкап")
async def trigger_manual_backup(message: types.Message):
    if message.chat.id == MODERATOR_CHAT_ID:
        manual_backup()
        await message.answer("Бэкап успешно создан и отправлен.")
        
async def auto_backup():
    while True:
        await asyncio.sleep(86400)  # Запускаем раз в сутки
        backup_databases()
        manual_backup()

        
# ========Авто обновление курса на сайте через гитхаб апи=======


async def push_rates_to_github():
    global _github_last_pushed

    if not SETTINGS_FILE.exists():
        return

    content_raw = SETTINGS_FILE.read_text(encoding='utf-8')
    try:
        data = json.loads(content_raw)
        site_data = {
            'rub':        data.get('rub', 15.4),
            'usdt_cny':   data.get('usdt_cny', 6.5),
            'min_amount': data.get('min_amount', 10.0),
            'max_amount': data.get('max_amount', 5000.0),
            'work_time':  data.get('work_time', {'start_h': 9, 'end_h': 18}),
        }
        content_raw = json.dumps(site_data, ensure_ascii=False, indent=4)
    except Exception:
        pass

    # Пушим только если данные изменились с прошлого раза
    if content_raw.strip() == _github_last_pushed:
        return

    content_b64 = base64.b64encode(content_raw.encode('utf-8')).decode('ascii')
    url = f'https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE_PATH}'
    headers = {
        'Authorization': f'token {GITHUB_TOKEN}',
        'Accept': 'application/vnd.github.v3+json',
    }

    connector = aiohttp.TCPConnector(ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        # Получаем SHA
        sha = None
        async with session.get(url, headers=headers) as resp:
            if resp.status == 200:
                sha = (await resp.json()).get('sha')
            elif resp.status != 404:
                return

        payload = {'message': 'auto: update rates', 'content': content_b64}
        if sha:
            payload['sha'] = sha

        async with session.put(url, headers=headers, json=payload) as resp:
            if resp.status in (200, 201):
                _github_last_pushed = content_raw.strip()  # запоминаем что запушили
            else:
                text = await resp.text()
                print(f'[GitHub sync] Ошибка {resp.status}: {text}')


                
async def sync_github_loop():
    """Обновляет exchange_rate.json на GitHub каждые 60 секунд."""
    while True:
        try:
            await push_rates_to_github()
        except Exception as e:
            print(f'[GitHub sync] Исключение: {e}')
        await asyncio.sleep(60)                
    
# ---- Курс ----
@dp.message_handler(lambda m: m.text in ("Курс", "/rate"))
async def send_exchange_rates(message: types.Message):
    """
    Показываем два курса:
    • сколько стоит 1 ¥ в рублях;
    • сколько стоит 1 USDT в юанях.

    Для VIP-пользователя скидка применяется ТОЛЬКО к рублёвому курсу.
    USDT-курс одинаковый для всех.
    """
    try:
        settings   = load_settings()
        rub_rate   = settings.get("rub")          # ₽  → ¥
        usdt_rate  = settings.get("usdt_cny")     # USDT → ¥

        if rub_rate is None or usdt_rate is None:
            raise ValueError("rates missing in JSON")

        # ----- модератор: показываем всё + VIP-пример -----
        if message.chat.id == MODERATOR_CHAT_ID:
            vip_rub = round(rub_rate * 0.991, 2)
            text = (
                "💹 *Курсы юаня*\n\n"
                f"За рубли: 1 ¥ = *{rub_rate} ₽*\n"
                f"За USDT : 1 USDT = *{usdt_rate} ¥*\n\n"
                f"💎 VIP: *{vip_rub} ₽*"
            )
            return await message.answer(text, parse_mode="Markdown")

        # ----- VIP-пользователь -----
        if message.chat.id in VIP_USERS:
            vip_rub = get_effective_rate(message.chat.id)
            text = (
                "💎 *Ваш VIP-курс*\n"
                f"• 1 ¥ = *{vip_rub} ₽*\n\n"
                "💹 *Курс за USDT*\n"
                f"• 1 USDT = *{usdt_rate} ¥*"
            )
            return await message.answer(text, parse_mode="Markdown")

        # ----- обычный пользователь -----
        text = (
            "💹 *Курсы на данный момент:*\n\n"
            f"• *{rub_rate} ₽ = 1 ¥*\n"
            f"• 1 USDT = *{usdt_rate} ¥*"
        )
        await message.answer(text, parse_mode="Markdown")

    except Exception as e:
        await message.answer("Ошибка при загрузке курса. Попробуйте позже.")
        print("Ошибка в /rate:", e)



# ======Личный кабинет========
@dp.message_handler(lambda message: message.text == "Личный кабинет" or message.text == "/profile")
async def personal_account(message: types.Message):
    chat_id = message.chat.id
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    cursor.execute("SELECT name, phone, qr_photo, is_registered FROM users WHERE chat_id = ?", (chat_id,))
    user = cursor.fetchone()
    conn.close()

    if user and user[3] == 1:  # is_registered = 1
        name, phone, qr_photo = user[0], user[1], user[2]
        response = f"Ваши данные:\nИмя: {name}\nНомер телефона: {phone}"
        await message.answer(response, reply_markup=personal_menu)

        if qr_photo:
            await bot.send_photo(chat_id, qr_photo, caption="Ваш QR-код")
    else:
        await message.answer(
            "Вы ещё не завершили регистрацию. Пожалуйста, зарегистрируйтесь:",
            reply_markup=registration_menu
        )



# -- Функция пересылки в канал (не анонимная) --
async def forward_message_to_channel(message: types.Message, channel_id: str):
    await message.forward(chat_id=channel_id)

# ---- Отзывы ----
FEEDBACK_CHANNEL = "@RYExchange_feedback"

@dp.message_handler(lambda m: m.text == "Отзывы")
async def show_feedback_channel(message: types.Message):
    keyboard = InlineKeyboardMarkup().add(
        InlineKeyboardButton("Канал отзывов", url="https://t.me/RYExchange_feedback")
    )
    await message.answer("Перейти в канал отзывов:", reply_markup=keyboard)

@dp.message_handler(lambda m: m.text == "Оставить отзыв")
async def start_feedback(message: types.Message):
    await Feedback.waiting_for_feedback.set()
    await message.answer(
        "Пожалуйста, напишите ваш отзыв одним сообщением. "
        "Мы перешлём его в наш канал отзывов."
    )





@dp.message_handler(state=Feedback.waiting_for_feedback, content_types=types.ContentTypes.ANY)
async def forward_user_feedback(message: types.Message, state: FSMContext):
    await forward_message_to_channel(message, FEEDBACK_CHANNEL)
    await message.answer("Спасибо, ваш отзыв отправлен!", reply_markup=main_menu)
    await state.finish()

# ---- Регистрация ----
@dp.message_handler(lambda message: message.text == "Зарегистрироваться")
async def start_registration(message: types.Message):
    await Registration.waiting_for_name.set()
    await message.answer(
        "Введите ваше имя, как в вашем паспорте, привязанном к Alipay или WeChat (пример: IVANOV IVAN):",
        reply_markup=back_to_menu
    )

@dp.message_handler(state=Registration.waiting_for_name)
async def get_name(message: types.Message, state: FSMContext):
    if message.text == "Главное меню":
        await go_to_main_menu(message, state)
        return

    if not all(part.isalpha() for part in message.text.split()):
        await message.answer("Имя должно содержать только буквы. Попробуйте еще раз:", reply_markup=back_to_menu)
        return

    name = message.text
    chat_id = message.chat.id
    username = message.from_user.username or "Никнейм отсутствует"

    await state.update_data(name=name, chat_id=chat_id, username=username)
    await Registration.next()
    await message.answer("Введите номер телефона, который привязан к аккаунту:", reply_markup=back_to_menu)

@dp.message_handler(state=Registration.waiting_for_phone)
async def get_phone(message: types.Message, state: FSMContext):
    if message.text == "Главное меню":
        await go_to_main_menu(message, state)
        return

    if not message.text.isdigit():
        await message.answer(
            "Номер телефона должен содержать только цифры. Попробуйте еще раз:",
            reply_markup=back_to_menu
        )
        return

    phone = message.text
    await state.update_data(phone=phone)

    await Registration.next()

    instructions_keyboard = InlineKeyboardMarkup(row_width=1)
    instructions_keyboard.add(
        InlineKeyboardButton("Где взять QR Code Alipay?", url="https://t.me/LIVE_inc/7"),
        InlineKeyboardButton("Где взять QR Code WeChat?", url="https://t.me/LIVE_inc/11")
    )

    await message.answer(
        "Отправьте фотографию QR-кода",
        reply_markup=back_to_menu
    )
    await message.answer("Инструкции:", reply_markup=instructions_keyboard)

@dp.message_handler(content_types=["photo"], state=Registration.waiting_for_qr_photo)
async def get_qr_photo(message: types.Message, state: FSMContext):
    if message.text == "Главное меню":
        await go_to_main_menu(message, state)
        return

    photo_id = message.photo[-1].file_id
    data = await state.get_data()

    chat_id = data["chat_id"]
    username = data["username"]
    name = data["name"]
    phone = data["phone"]

    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()

    # Обновляем данные пользователя без дублирования
    cursor.execute("""
        UPDATE users
        SET username = ?, name = ?, phone = ?, qr_photo = ?, is_registered = 1
        WHERE chat_id = ?
    """, (username, name, phone, photo_id, chat_id))

    conn.commit()
    conn.close()

    await state.finish()
    await message.answer(
        "✅ Данные отправлены на проверку модератором.\n"
        "Как только ЛК будет одобрен, вы сможете создавать заявки.",
        reply_markup=main_menu
    )

    # Уведомляем модератора
    verify_kb = InlineKeyboardMarkup(row_width=1)
    verify_kb.add(
        InlineKeyboardButton("✅ Одобрить ЛК", callback_data=f"verify_approve:{chat_id}"),
        InlineKeyboardButton("❌ Отклонить ЛК и закрыть диалог", callback_data=f"verify_close:{chat_id}")
    )
    mod_text = (
        f"📋 верификация №{chat_id}\n"
        f"Чат ID пользователя: {chat_id}\n"
        f"Username: @{username}\n"
        f"Имя: {name}\n"
        f"Телефон: {phone}"
    )
    # Открываем бридж верификации
    active_verify_dialogs[chat_id] = MODERATOR_CHAT_ID
    await bot.send_message(MODERATOR_CHAT_ID, mod_text)
    if photo_id:
        await bot.send_photo(MODERATOR_CHAT_ID, photo_id, caption="QR-код пользователя")
    await bot.send_message(MODERATOR_CHAT_ID,
        f"Для обработки ЛК №{chat_id} выберите действие:",
        reply_markup=verify_kb)

# ---- Обновление информации ----
@dp.message_handler(lambda message: message.text == "Обновить информацию")
async def start_update_info(message: types.Message):
    chat_id = message.chat.id
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    cursor.execute("SELECT is_registered, is_verified FROM users WHERE chat_id = ?", (chat_id,))
    user = cursor.fetchone()
    conn.close()

    if not user or user[0] == 0:
        await message.answer("Вы не зарегистрированы. Пожалуйста, сначала зарегистрируйтесь.", reply_markup=main_menu)
        return

    if user[1] == 0:
        await message.answer(
            "⏳ Ваш ЛК сейчас на проверке у модератора.\n"
            "Изменить данные можно только после завершения проверки."
        )
        return

    await UpdateInfo.waiting_for_name.set()
    await message.answer("Введите ваше имя, как в вашем паспорте, привязанном к Alipay или WeChat (пример: IVANOV IVAN):", reply_markup=back_to_menu)

@dp.message_handler(state=UpdateInfo.waiting_for_name)
async def collect_name_for_update(message: types.Message, state: FSMContext):
    if message.text == "Главное меню":
        await go_to_main_menu(message, state)
        return

    if not all(part.isalpha() for part in message.text.split()):
        await message.answer("Имя должно содержать только буквы. Попробуйте еще раз:", reply_markup=back_to_menu)
        return

    await state.update_data(name=message.text)
    await UpdateInfo.next()
    await message.answer("Введите номер телефона, который привязан к аккаунту:", reply_markup=back_to_menu)

@dp.message_handler(state=UpdateInfo.waiting_for_phone)
async def collect_phone_for_update(message: types.Message, state: FSMContext):
    if message.text == "Главное меню":
        await go_to_main_menu(message, state)
        return
    if not message.text.isdigit():
        await message.answer("Номер телефона должен содержать только цифры. Попробуйте еще раз:", reply_markup=back_to_menu)
        return

    await state.update_data(phone=message.text)
    await UpdateInfo.next()

    instructions_keyboard = InlineKeyboardMarkup(row_width=1)
    instructions_keyboard.add(
        InlineKeyboardButton("Где взять QR Code Alipay?", url="https://t.me/LIVE_inc/7"),
        InlineKeyboardButton("Где взять QR Code WeChat?", url="https://t.me/LIVE_inc/11")
    )

    await message.answer("Отправьте новый QR-код:", reply_markup=back_to_menu)
    await message.answer("Инструкции:", reply_markup=instructions_keyboard)

@dp.message_handler(content_types=["photo"], state=UpdateInfo.waiting_for_qr_photo)
async def finalize_update_info(message: types.Message, state: FSMContext):
    if message.text == "Главное меню":
        await go_to_main_menu(message, state)
        return

    photo_id = message.photo[-1].file_id
    data = await state.get_data()
    name = data.get("name")
    phone = data.get("phone")
    chat_id = message.chat.id

    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    # Сохраняем СТАРЫЕ данные в backup-колонки перед обновлением
    cursor.execute("SELECT name, phone, qr_photo FROM users WHERE chat_id = ?", (chat_id,))
    old_row = cursor.fetchone()
    old_name = old_row[0] if old_row else None
    old_phone = old_row[1] if old_row else None
    old_qr = old_row[2] if old_row else None

    # Обновляем данные и сбрасываем верификацию, сохраняем бэкап
    cursor.execute("""
        UPDATE users
        SET name = ?, phone = ?, qr_photo = ?,
            is_verified = 0,
            old_name = ?, old_phone = ?, old_qr_photo = ?
        WHERE chat_id = ?
    """, (name, phone, photo_id, old_name, old_phone, old_qr, chat_id))
    conn.commit()
    conn.close()

    # Открываем бридж верификации
    active_verify_dialogs[chat_id] = MODERATOR_CHAT_ID

    await state.finish()
    await message.answer(
        "✅ Данные обновлены и отправлены на повторную проверку.\n"
        "Создание заявок будет доступно после одобрения модератором.",
        reply_markup=main_menu
    )

    # Уведомляем модератора с кнопками
    verify_kb = InlineKeyboardMarkup(row_width=1)
    verify_kb.add(
        InlineKeyboardButton("✅ Одобрить ЛК", callback_data=f"verify_approve:{chat_id}"),
        InlineKeyboardButton("❌ Отклонить ЛК и закрыть диалог", callback_data=f"verify_close:{chat_id}")
    )
    mod_text = (
        f"📋 верификация №{chat_id}\n"
        f"Чат ID пользователя: {chat_id}\n"
        f"Повторная проверка ЛК\n"
        f"Имя: {name}\nТелефон: {phone}"
    )
    await bot.send_message(MODERATOR_CHAT_ID, mod_text)
    if photo_id:
        await bot.send_photo(MODERATOR_CHAT_ID, photo_id, caption="QR-код пользователя")
    await bot.send_message(MODERATOR_CHAT_ID,
        f"Для обработки ЛК №{chat_id} выберите действие:",
        reply_markup=verify_kb)

# ---- Удаление информации ----
@dp.message_handler(lambda message: message.text == "Удалить информацию")
async def delete_info_start(message: types.Message):
    chat_id = message.chat.id
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    cursor.execute("SELECT is_registered, is_verified FROM users WHERE chat_id = ?", (chat_id,))
    user = cursor.fetchone()
    conn.close()

    if not user or user[0] == 0:
        await message.answer("У вас нет зарегистрированных данных.", reply_markup=main_menu)
        return

    if user[1] == 0:
        await message.answer(
            "⏳ Ваш ЛК сейчас на проверке у модератора.\n"
            "Удалить данные можно только после завершения проверки."
        )
        return

    await message.answer("Вы уверены, что хотите удалить свои данные?", reply_markup=confirm_menu)


@dp.message_handler(lambda message: message.text == "Подтвердить")
async def delete_info_confirm(message: types.Message):
    chat_id = message.chat.id
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()

    cursor.execute("""
        UPDATE users
        SET name = NULL,
            phone = NULL,
            qr_photo = NULL,
            is_registered = 0,
            is_verified = 0
        WHERE chat_id = ?
    """, (chat_id,))

    conn.commit()
    conn.close()

    await message.answer("✅Ваши данные успешно удалены.", reply_markup=main_menu)


@dp.message_handler(lambda message: message.text == "Отменить")
async def cancel_delete(message: types.Message):
    await message.answer("Удаление отменено. Выберите действие:", reply_markup=personal_menu)

# ---- Админские настройки ----
@dp.message_handler(lambda message: message.text == "Админские настройки")
async def admin_settings_start(message: types.Message):
    if message.chat.id != MODERATOR_CHAT_ID:
        await message.answer("У вас нет прав доступа к админским настройкам.")
        return
    await message.answer("Выберите действие:", reply_markup=admin_menu)

@dp.message_handler(lambda message: message.text == "Изменить лимиты")
async def admin_change_limits(message: types.Message):
    if message.chat.id != MODERATOR_CHAT_ID:
        return
    await AdminSettings.waiting_for_new_min.set()
    await message.answer("Введите новый min_amount (в юанях):", reply_markup=back_to_menu)

@dp.message_handler(state=AdminSettings.waiting_for_new_min)
async def admin_set_new_min(message: types.Message, state: FSMContext):
    if message.text == "Главное меню":
        await go_to_main_menu(message, state)
        return
    try:
        new_min = float(message.text)
        await state.update_data(new_min=new_min)
        await AdminSettings.next()
        await message.answer("Теперь введите новый max_amount (в юанях):")
    except ValueError:
        await message.answer("Некорректный формат. Введите число (float).")

@dp.message_handler(state=AdminSettings.waiting_for_new_max)
async def admin_set_new_max(message: types.Message, state: FSMContext):
    if message.text == "Главное меню":
        await go_to_main_menu(message, state)
        return
    try:
        new_max = float(message.text)
        data = await state.get_data()
        new_min = data.get("new_min")

        if new_max <= new_min:
            await message.answer("max_amount должен быть больше min_amount. Повторите ввод.")
            return

        settings = load_settings()
        settings["min_amount"] = new_min
        settings["max_amount"] = new_max
        save_settings(settings)

        await state.finish()
        await message.answer(
            f"Лимиты успешно изменены:\nmin_amount = {new_min}\nmax_amount = {new_max}",
            reply_markup=admin_menu
        )
    except ValueError:
        await message.answer("Некорректный формат. Введите число (float).")

@dp.message_handler(lambda message: message.text == "Изменить курс")
async def admin_change_rate(message: types.Message):
    if message.chat.id != MODERATOR_CHAT_ID:
        return
    await AdminSettings.waiting_for_new_rate.set()
    await message.answer("Введите новый курс юаня (руб.):", reply_markup=back_to_menu)

@dp.message_handler(state=AdminSettings.waiting_for_new_rate)
async def admin_set_new_rate(message: types.Message, state: FSMContext):
    if message.text == "Главное меню":
        await go_to_main_menu(message, state)
        return
    try:
        new_rate = float(message.text)
        settings = load_settings()
        settings["rub"] = new_rate
        save_settings(settings)
        await state.finish()
        await message.answer(
            f"Курс успешно изменён: {new_rate} руб. за 1 юань",
            reply_markup=admin_menu
        )
    except ValueError:
        await message.answer("Некорректный формат. Введите число (float).")

# ---- Остановка / Возобновление бота ----
@dp.message_handler(lambda message: message.text == "Остановить бота")
async def stop_bot_cmd(message: types.Message):
    global BOT_STOPPED
    if message.chat.id != MODERATOR_CHAT_ID:
        return
    BOT_STOPPED = True
    await message.answer("Бот остановлен! Все пользователи (кроме вас) получат сообщение-заглушку.", reply_markup=admin_menu)

@dp.message_handler(lambda message: message.text == "Восстановить бота")
async def resume_bot_cmd(message: types.Message):
    global BOT_STOPPED
    if message.chat.id != MODERATOR_CHAT_ID:
        return
    BOT_STOPPED = False
    await message.answer("Бот снова активен!", reply_markup=admin_menu)

# ---- Общий анонс ----
class Broadcast(StatesGroup):
    waiting_for_text = State()

@dp.message_handler(lambda message: message.text == "Общий анонс")
async def broadcast_start(message: types.Message):
    if message.chat.id != MODERATOR_CHAT_ID:
        return
    await Broadcast.waiting_for_text.set()
    await message.answer("Введите текст анонса (одним сообщением):", reply_markup=back_to_menu)

@dp.message_handler(state=Broadcast.waiting_for_text, content_types=types.ContentTypes.TEXT)
async def broadcast_send(message: types.Message, state: FSMContext):
    if message.text == "Главное меню":
        await go_to_main_menu(message, state)
        return

    broadcast_text = message.text.strip()
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    cursor.execute("SELECT chat_id FROM users")
    all_users = cursor.fetchall()
    conn.close()

    counter = 0
    for row in all_users:
        user_chat_id = row[0]
        try:
            await bot.send_message(user_chat_id, f"📢 Автоматическое сообщение:\n\n{broadcast_text}")
            counter += 1
        except:
            pass

    await message.answer(f"✅ Анонс отправлен {counter} пользователям.", reply_markup=admin_menu)
    await state.finish()

# ---- Написать пользователю ----
class PrivateMessage(StatesGroup):
    waiting_for_user_id = State()
    waiting_for_text = State()

@dp.message_handler(lambda message: message.text == "Написать пользователю")
async def pm_to_user_start(message: types.Message):
    if message.chat.id != MODERATOR_CHAT_ID:
        return
    await PrivateMessage.waiting_for_user_id.set()
    await message.answer("Введите chat_id пользователя (целое число):", reply_markup=back_to_menu)

@dp.message_handler(state=PrivateMessage.waiting_for_user_id)
async def pm_get_user_id(message: types.Message, state: FSMContext):
    if message.text == "Главное меню":
        await go_to_main_menu(message, state)
        return
    try:
        user_id = int(message.text)
        await state.update_data(user_id=user_id)
        await PrivateMessage.next()
        await message.answer("Введите текст сообщения, которое нужно отправить:", reply_markup=back_to_menu)
    except ValueError:
        await message.answer("Пожалуйста, введите корректный chat_id (целое число).")

@dp.message_handler(state=PrivateMessage.waiting_for_text)
async def pm_send_message(message: types.Message, state: FSMContext):
    if message.text == "Главное меню":
        await go_to_main_menu(message, state)
        return

    data = await state.get_data()
    user_id = data["user_id"]
    text_to_send = message.text

    try:
        await bot.send_message(user_id, f"Сообщение от модератора:\n\n{text_to_send}")
        await message.answer("Сообщение успешно отправлено!", reply_markup=admin_menu)
    except Exception as e:
        await message.answer(f"Ошибка при отправке: {e}", reply_markup=admin_menu)

    await state.finish()

# ---- Изменить график работы (новый функционал) ----
@dp.message_handler(lambda message: message.text == "Изменить график работы")
async def change_work_time(message: types.Message):
    if message.chat.id != MODERATOR_CHAT_ID:
        return
    await AdminSettings.waiting_for_work_start.set()
    await message.answer("Введите **начальный час** работы бота (по Пекину). Например, 9:", reply_markup=back_to_menu)

@dp.message_handler(state=AdminSettings.waiting_for_work_start)
async def set_new_start_hour(message: types.Message, state: FSMContext):
    if message.text == "Главное меню":
        await go_to_main_menu(message, state)
        return

    try:
        start_hour = int(message.text)
        if not (0 <= start_hour <= 23):
            raise ValueError
        await state.update_data(start_hour=start_hour)
        await AdminSettings.next()
        await message.answer("Теперь введите **конечный час** работы бота (по Пекину). Например, 18:")
    except ValueError:
        await message.answer("Пожалуйста, введите целое число от 0 до 23.")

@dp.message_handler(state=AdminSettings.waiting_for_work_end)
async def set_new_end_hour(message: types.Message, state: FSMContext):
    if message.text == "Главное меню":
        await go_to_main_menu(message, state)
        return

    data = await state.get_data()
    start_hour = data["start_hour"]

    try:
        end_hour = int(message.text)
        if not (0 <= end_hour <= 23):
            raise ValueError

        if end_hour <= start_hour:
            await message.answer("Конечный час должен быть больше начального. Повторите ввод.")
            return

        settings = load_settings()
        settings["work_time"] = {
            "start_h": start_hour,
            "end_h": end_hour
        }
        save_settings(settings)

        await state.finish()
        await message.answer(
            f"Рабочий график бота (по Пекину) изменён.\n"
            f"С {start_hour}:00 до {end_hour}:00",
            reply_markup=admin_menu
        )
    except ValueError:
        await message.answer("Пожалуйста, введите целое число от 0 до 23.")

# ---- Возврат в главное меню ----
@dp.message_handler(lambda message: message.text in ["Главное меню", "Вернуться в меню", "/menu"], state="*")
async def go_to_main_menu(message: types.Message, state: FSMContext):
    await state.finish()
    if message.chat.id == MODERATOR_CHAT_ID:
        custom_main = ReplyKeyboardMarkup(resize_keyboard=True)
        custom_main.add("Курс", "Личный кабинет", "Создать заявку", "Отзывы", "Реферальная программа", "Админские настройки")
        await message.answer("Вы вернулись в главное меню.", reply_markup=custom_main)
    else:
        await message.answer("Вы вернулись в главное меню.", reply_markup=main_menu)


# ========= Просмотр лк пользователя админом=======

@dp.message_handler(lambda message: message.text == "Посмотреть ЛК пользователя")
async def ask_user_chat_id(message: types.Message):
    if message.chat.id != MODERATOR_CHAT_ID:
        return
    await message.answer("Введите chat_id пользователя, которого хотите просмотреть:", reply_markup=admin_user_menu)
    await AdminUserLookup.waiting_for_chat_id.set()

@dp.message_handler(state=AdminUserLookup.waiting_for_chat_id)
async def show_user_profile(message: types.Message, state: FSMContext):
    if message.text == "Главное меню":
        await go_to_main_menu(message, state)
        return

    try:
        target_chat_id = int(message.text)
    except ValueError:
        await message.answer("❌ chat_id должен быть числом. Попробуйте ещё раз:", reply_markup=admin_user_menu)
        return

    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    cursor.execute("SELECT name, phone, qr_photo, is_registered FROM users WHERE chat_id = ?", (target_chat_id,))
    user = cursor.fetchone()
    conn.close()

    if user and user[3] == 1:
        name, phone, qr_photo = user[0], user[1], user[2]
        response = f"📋 Данные пользователя:\nИмя: {name}\nТелефон: {phone}"
        await message.answer(response)

        if qr_photo:
            await bot.send_photo(message.chat.id, qr_photo, caption="QR-код пользователя")
    else:
        await message.answer("⚠️ Пользователь не зарегистрирован или не найден.", reply_markup=admin_user_menu)

    await state.finish()


# ========= Редактирование ЛК пользователя модератором =========

@dp.message_handler(lambda message: message.text == "Изменить данные ЛК пользователя")
async def ask_chat_id_for_edit(message: types.Message):
    if message.chat.id != MODERATOR_CHAT_ID:
        return
    await message.answer("Введите chat_id пользователя, чьи данные хотите изменить:", reply_markup=admin_user_menu)
    await AdminUserEdit.waiting_for_chat_id.set()

@dp.message_handler(state=AdminUserEdit.waiting_for_chat_id)
async def admin_edit_name(message: types.Message, state: FSMContext):
    if message.text == "Главное меню":
        await go_to_main_menu(message, state)
        return

    try:
        chat_id = int(message.text)
    except ValueError:
        await message.answer("❌ chat_id должен быть числом. Попробуйте ещё раз:", reply_markup=admin_user_menu)
        return

    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    cursor.execute("SELECT is_registered FROM users WHERE chat_id = ?", (chat_id,))
    user = cursor.fetchone()
    conn.close()

    if not user or user[0] != 1:
        await message.answer("⚠️ Пользователь не зарегистрирован или не найден.", reply_markup=admin_user_menu)
        await state.finish()
        return

    await state.update_data(chat_id=chat_id)
    await AdminUserEdit.waiting_for_name.set()
    await message.answer("Введите новое имя пользователя (пример: IVANOV IVAN):", reply_markup=admin_user_menu)

@dp.message_handler(state=AdminUserEdit.waiting_for_name)
async def admin_edit_phone(message: types.Message, state: FSMContext):
    if message.text == "Главное меню":
        await go_to_main_menu(message, state)
        return

    if not all(part.isalpha() for part in message.text.split()):
        await message.answer("❌ Имя должно содержать только буквы. Попробуйте ещё раз:", reply_markup=admin_user_menu)
        return

    await state.update_data(name=message.text)
    await AdminUserEdit.waiting_for_phone.set()
    await message.answer("Введите новый номер телефона пользователя:", reply_markup=admin_user_menu)

@dp.message_handler(state=AdminUserEdit.waiting_for_phone)
async def admin_edit_qr(message: types.Message, state: FSMContext):
    if message.text == "Главное меню":
        await go_to_main_menu(message, state)
        return

    if not message.text.isdigit():
        await message.answer("❌ Номер телефона должен содержать только цифры. Попробуйте ещё раз:", reply_markup=admin_user_menu)
        return

    await state.update_data(phone=message.text)
    await AdminUserEdit.waiting_for_qr_photo.set()
    await message.answer("Отправьте новый QR-код (фото):", reply_markup=admin_user_menu)

@dp.message_handler(content_types=["photo"], state=AdminUserEdit.waiting_for_qr_photo)
async def save_admin_edit(message: types.Message, state: FSMContext):
    if message.text == "Главное меню":
        await go_to_main_menu(message, state)
        return

    photo_id = message.photo[-1].file_id
    data = await state.get_data()

    chat_id = data["chat_id"]
    name = data["name"]
    phone = data["phone"]

    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE users
        SET name = ?, phone = ?, qr_photo = ?, is_registered = 1
        WHERE chat_id = ?
    """, (name, phone, photo_id, chat_id))
    conn.commit()
    conn.close()

    await state.finish()
    await message.answer("✅ Данные пользователя успешно обновлены.", reply_markup=admin_menu)





# =========================================================
# ======= Уведомление партнёров при рублёвой заявке =======
# =========================================================

PARTNER_MIN_RUB = 10_000

async def notify_partners_if_needed(order_id: int, rub_amount: float,
                                    user_name: str, user_bank: str):
    if rub_amount < PARTNER_MIN_RUB:
        return
    text = (
        f"Запрос реквизитов на {rub_amount:,.0f}р.\n"
        f"Банк: {user_bank}\n"
        f"Имя: {user_name}\n"
        f"Клиент проверенный"
    )
    try:
        from aiogram.types import ReplyKeyboardRemove
        await bot.send_message(PARTNER_CHAT_ID, text, reply_markup=ReplyKeyboardRemove())
    except Exception as e:
        await bot.send_message(
            MODERATOR_CHAT_ID,
            f"⚠️ Не удалось отправить заявку №{order_id} партнёрам: {e}"
        )

# ===== Создание заявки =======

@dp.message_handler(lambda message: message.text == "Создать заявку" or message.text == "/exchange")
async def create_request_start(message: types.Message):
    chat_id = message.chat.id
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    cursor.execute("SELECT is_registered, is_verified FROM users WHERE chat_id = ?", (chat_id,))
    result = cursor.fetchone()
    conn.close()

    if not result or result[0] != 1:
        await message.answer(
            "Вы не завершили регистрацию. Пожалуйста, сначала зарегистрируйтесь.",
            reply_markup=registration_menu
        )
        return

    if result[1] != 1:
        await message.answer(
            "⏳ Ваш личный кабинет ещё не прошёл проверку.\n"
            "Пожалуйста, дождитесь подтверждения модератора."
        )
        return

    # Клавиатура для модератора
    if chat_id == MODERATOR_CHAT_ID:
        custom_main = ReplyKeyboardMarkup(resize_keyboard=True)
        custom_main.add("Сумма в ₽", "Сумма в ¥", "Сумма в USDT")
        custom_main.add("Главное меню", "Админские настройки")
        await message.answer("Выберите способ ввода суммы:", reply_markup=custom_main)
    else:
        # Клавиатура для обычного пользователя
        keyboard = ReplyKeyboardMarkup(resize_keyboard=True).add("Сумма в ₽", "Сумма в ¥", "Сумма в USDT")
        keyboard.add("Главное меню")
        await message.answer("Выберите способ ввода суммы:", reply_markup=keyboard)



# ---------------- USDT ----------------

def bonus_balance(uid:int)->int:
    conn = sqlite3.connect('user_balance.db'); cur = conn.cursor(); cur.execute('SELECT balance FROM balances WHERE user_id=?',(uid,)); row = cur.fetchone(); conn.close(); return row[0] if row else 0

def change_bonus(uid:int, delta:int):
    conn = sqlite3.connect('user_balance.db'); cur = conn.cursor()
    cur.execute('INSERT INTO balances (user_id,balance) VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET balance = balance + ?;', (uid, max(delta,0), delta))
    conn.commit(); conn.close()
    
back_kb = ReplyKeyboardMarkup(resize_keyboard=True).add('Главное меню')

@dp.message_handler(lambda m:m.text=='Сумма в USDT')
async def ask_usdt(m:Message):
    await RequestCreation.waiting_for_amount_in_usdt.set(); await m.answer('Введите сумму в USDT целым числом:', reply_markup=back_kb)

@dp.message_handler(state=RequestCreation.waiting_for_amount_in_usdt)
async def calc_usdt(m:Message,state:FSMContext):
    try: usdt=int(m.text.replace(',', '.'))
    except: return await m.answer('Целое число USDT')
    s=load_settings(); rate_uc=s['usdt_cny']; min_u,max_u=math.ceil(s['min_amount']/rate_uc), math.floor(s['max_amount']/rate_uc)
    if not(min_u<=usdt<=max_u): return await m.answer(f'Мы меняем от {min_u} USDT до {max_u} USDT')
    cny=correct_rounding(usdt*rate_uc)
    bonus_rate=s['rub_usdt_bonus']; bal=bonus_balance(m.chat.id)
    max_bonus_usdt=int(usdt*0.30); need_rub=max_bonus_usdt*bonus_rate
    used_bonus_rub=min(bal, need_rub); used_bonus_usdt=used_bonus_rub/bonus_rate
    pay_usdt=usdt-used_bonus_usdt
    await state.update_data(amount_usdt=usdt, usdt_to_pay=pay_usdt, cny_amount=cny, rate=rate_uc, used_bonus=used_bonus_rub, payment_currency='USDT')
    txt=f'К оплате: {pay_usdt:.2f} USDT (TRC-20)\nК зачислению: {cny} ¥\nКурс: 1 USDT = {rate_uc} ¥'
    if used_bonus_rub: txt+=f"\n💰 Списано бонусов: {used_bonus_rub} ₽ (≈ {used_bonus_usdt:.2f} USDT)"
    # Показываем QR из ЛК
    conn_u = sqlite3.connect("users.db")
    cur_u = conn_u.cursor()
    cur_u.execute("SELECT qr_photo FROM users WHERE chat_id = ?", (m.chat.id,))
    qr_row = cur_u.fetchone()
    conn_u.close()
    qr_msg_id = None
    if qr_row and qr_row[0]:
        qr_msg = await bot.send_photo(
            m.chat.id, qr_row[0],
            caption="📎 Ваш QR-код из ЛК — проверьте корректность"
        )
        qr_msg_id = qr_msg.message_id
    await m.answer(txt, reply_markup=confirm_request_menu)
    await state.update_data(qr_msg_id=qr_msg_id)
    await RequestCreation.waiting_for_confirmation.set()


@dp.message_handler(lambda message: message.text == "Сумма в ₽")
async def request_amount_in_rub(message: types.Message):
    await RequestCreation.waiting_for_amount_in_rub.set()
    await message.answer("Введите сумму в рублях:", reply_markup=back_to_menu)

@dp.message_handler(state=RequestCreation.waiting_for_amount_in_rub)
async def calculate_request_in_rub(message: types.Message, state: FSMContext):
    try:
        value_str = message.text
        float_val = float(value_str)

        if float_val != int(float_val):
            await message.answer("Пожалуйста, введите целое число (без копеек). Попробуйте ещё раз.")
            return

        rub_amount = float_val

        # --- VIP logic: используем get_effective_rate ---
        rate = get_effective_rate(message.chat.id)
        # ------------

        settings = load_settings()
        min_cny = settings.get("min_amount")
        max_cny = settings.get("max_amount")

        min_rub = min_cny * rate
        max_rub = max_cny * rate

        if rub_amount < min_rub:
            await message.answer(f"Сумма слишком маленькая🥲\nМы меняем от {min_rub:.0f} ₽\nВведите сумму еще раз")
            return

        if rub_amount > max_rub:
            await message.answer(f"Сумма слишком большая😎\nМы меняем от {min_rub:.0f} ₽ до {max_rub:.0f} ₽\nПоменять больше? Свяжитесь с техподдержкой напрямую @RYEsupport")
            return

        raw_cny = rub_amount / rate
        cny_amount = correct_rounding(raw_cny)

        await state.update_data(
            rub_amount=int(rub_amount),
            cny_amount=cny_amount,
            rate=rate,
            payment_currency="RUB"
        )

        await RequestCreation.waiting_for_bank.set()
        await message.answer("С какого банка будет оплата? (Напишите текстом):", reply_markup=back_to_menu)

    except ValueError:
        await message.answer("Некорректный ввод. Введите целое число в рублях (без копеек).", reply_markup=back_to_menu)

@dp.message_handler(lambda message: message.text == "Сумма в ¥")
async def request_amount_in_cny(message: types.Message):
    await RequestCreation.waiting_for_amount_in_cny.set()
    await message.answer("Введите сумму в юанях:", reply_markup=back_to_menu)

@dp.message_handler(state=RequestCreation.waiting_for_amount_in_cny)
async def calculate_request_in_cny(message: types.Message, state: FSMContext):
    try:
        value_str = message.text
        float_val = float(value_str)

        if float_val != int(float_val):
            await message.answer("Пожалуйста, введите целое число (без копеек). Попробуйте ещё раз.")
            return

        cny_amount = float_val

        # --- VIP logic: используем get_effective_rate ---
        rate = get_effective_rate(message.chat.id)
        # ------------

        settings = load_settings()
        min_cny = settings.get("min_amount")
        max_cny = settings.get("max_amount")

        if cny_amount < min_cny:
            await message.answer(f"Сумма слишком маленькая🥲\nМы меняем от {min_cny:.0f} ¥\nВведите сумму еще раз")
            return

        if cny_amount > max_cny:
            await message.answer(f"Сумма слишком большая😎\nМы меняем от {min_cny:.0f} ¥ до {max_cny:.0f} ¥\nПоменять больше? Свяжитесь с техподдержкой напрямую @RYEsupport")
            return

        raw_rub = cny_amount * rate
        rub_amount = correct_rounding(raw_rub)

        await state.update_data(
            rub_amount=rub_amount,
            cny_amount=int(cny_amount),
            rate=rate,
            payment_currency="CNY"
        )

        await RequestCreation.waiting_for_bank.set()
        await message.answer("С какого банка будет оплата? (Напишите текстом):", reply_markup=back_to_menu)

    except ValueError:
        await message.answer("Некорректный ввод. Введите целое число (без копеек).", reply_markup=back_to_menu)

# ==== Создание заявки (ввод банка и расчёт) ====

@dp.message_handler(state=RequestCreation.waiting_for_bank)
async def get_user_bank(message: types.Message, state: FSMContext):
    if message.text == "Главное меню":
        await go_to_main_menu(message, state)
        return

    user_bank = message.text.strip()
    data = await state.get_data()

    rub_amount = data.get("rub_amount")
    cny_amount = data.get("cny_amount")
    rate = data.get("rate")

    # === Расчёт used_bonus ===
    def calculate_bonus_usage(user_id: int, rub_amount: float) -> int:
        conn = sqlite3.connect("user_balance.db")
        cursor = conn.cursor()
        cursor.execute("SELECT balance FROM balances WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        conn.close()
        current_balance = row[0] if row else 0
        max_bonus = int(rub_amount * 0.3)
        return min(current_balance, max_bonus)

    used_bonus = calculate_bonus_usage(message.chat.id, rub_amount)
    rub_to_pay = rub_amount - used_bonus

    await state.update_data(
        user_bank=user_bank,
        used_bonus=used_bonus,
        rub_to_pay=rub_to_pay
    )

    # Показываем QR из ЛК пользователя
    conn_u = sqlite3.connect("users.db")
    cur_u = conn_u.cursor()
    cur_u.execute("SELECT qr_photo FROM users WHERE chat_id = ?", (message.chat.id,))
    qr_row = cur_u.fetchone()
    conn_u.close()
    qr_msg_id = None
    if qr_row and qr_row[0]:
        qr_msg = await bot.send_photo(
            message.chat.id, qr_row[0],
            caption="📎 Ваш QR-код из ЛК — проверьте корректность"
        )
        qr_msg_id = qr_msg.message_id

    # Сводка
    text = (
        f"К оплате: {rub_to_pay} руб.\n"
        f"К зачислению: {cny_amount} юаней.\n"
        f"Банк: {user_bank}\n"
        f"Курс: {rate} руб."
    )
    if used_bonus > 0:
        text += f"\n💰 Будет списано с бонусного баланса: {used_bonus} руб."

    await message.answer(text, reply_markup=confirm_request_menu)
    await state.update_data(qr_msg_id=qr_msg_id)
    await RequestCreation.waiting_for_confirmation.set()


@dp.callback_query_handler(lambda c: c.data == "cancel_request", state=RequestCreation.waiting_for_confirmation)
async def cancel_user_request(callback_query: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    qr_msg_id = data.get("qr_msg_id")
    if qr_msg_id:
        try:
            await bot.delete_message(callback_query.message.chat.id, qr_msg_id)
        except Exception:
            pass
    await callback_query.answer("Заявка отменена.", show_alert=False)
    await callback_query.message.edit_text("❌ Вы отменили заявку.")
    await state.finish()


@dp.callback_query_handler(lambda c: c.data == "confirm_request",
                           state=RequestCreation.waiting_for_confirmation)
async def confirm_request(callback_query: types.CallbackQuery,
                          state: FSMContext):
    """Подтверждение заявки.
    • Если оплата - USDT (TRC-20) – реквизиты выдаются пользователю сразу.
    • Для RUB/¥ сохраняем прежний путь — реквизиты выдаёт модератор.
    """
    data = await state.get_data()
    # Удаляем QR-фото и сообщение с кнопками
    qr_msg_id = data.get("qr_msg_id")
    if qr_msg_id:
        try:
            await bot.delete_message(callback_query.message.chat.id, qr_msg_id)
        except Exception:
            pass
    try:
        await callback_query.message.delete()
    except Exception:
        pass
    payment_currency = data.get("payment_currency", "RUB")
    user_id = callback_query.from_user.id

    # ---------- общий блок: ищем реферера, номер заявки пользователя ----------
    conn_u = sqlite3.connect("users.db")
    cur_u = conn_u.cursor()
    cur_u.execute("SELECT referrer, name, phone, qr_photo FROM users "
                  "WHERE chat_id = ?", (user_id,))
    ref_row = cur_u.fetchone()
    referrer_id, name, phone, qr_photo = (ref_row or [None, "-", "-", None])
    conn_u.close()

    conn_o = sqlite3.connect("orders.db")
    cur_o = conn_o.cursor()
    cur_o.execute("SELECT MAX(user_order_number) FROM orders "
                  "WHERE user_id = ?", (user_id,))
    user_order_number = (cur_o.fetchone()[0] or 0) + 1

    # ========================================================================
    #  USDT-заявка: сразу выдаём реквизиты пользователю
    # ========================================================================
    if payment_currency == "USDT":
        usdt_to_pay = data.get("usdt_to_pay") or data.get("amount_usdt")
        cny_amount = data.get("cny_amount")
        rate_uc = data.get("rate")
        used_bonus = data.get("used_bonus", 0)

        # сохраняем заказ
        cur_o.execute("""
            INSERT INTO orders
            (user_id, chat_id, amount_usdt, cny_amount, rate,
             created_at, user_bank, user_order_number,
             used_bonus, referrer_id, payment_currency)
            VALUES (?,?,?,?,?,?,?,?,?,?, 'USDT')
        """, (
            user_id, user_id,
            usdt_to_pay, cny_amount, rate_uc,
            datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'TRC-20', user_order_number,
            used_bonus, referrer_id
        ))
        conn_o.commit()
        order_id = cur_o.lastrowid
        conn_o.close()

        # списываем бонусы
        if used_bonus:
            change_bonus(user_id, -used_bonus)

        # мост для чата
        active_dialogs.setdefault(user_id, {})[order_id] = MODERATOR_CHAT_ID

        # --- реквизиты пользователю ---
        settings = load_settings()
        usdt_addr = settings['usdt_address']
        pay_msg = (
            f"✅ Ваша заявка подтверждена!\n\n"
            f"💸 *Реквизиты для оплаты* (сеть *TRC-20*):\n\n"
            f"`{usdt_addr}`\n\n"
            f"Сумма к оплате: *{usdt_to_pay:.2f} USDT*\n"
            f"Курс: 1 USDT = {rate_uc} ¥\n"
            f"К зачислению: {cny_amount} ¥\n\n"
            "⚠️ Проверьте адрес и сеть перед отправкой\n"
            "⚠️ Просим отправить сумму с учетом комиссии сети для ускорения обработки платежа"
        )
        await callback_query.message.answer(pay_msg, parse_mode="Markdown")

        # --- уведомление модератору ---
        mod_text = (
            f"Поступила новая USDT-заявка №{order_id}\n"
            f"Чат ID пользователя: {user_id}\n"
            f"Локальный №: {user_order_number}\n"
            f"К оплате: {usdt_to_pay:.2f} USDT\n"
            f"К зачислению: {cny_amount} ¥\n"
            f"Курс: 1 USDT = {rate_uc} ¥"
        )
        if used_bonus:
            mod_text += f"\n💰 Списано бонусов: {used_bonus} ₽"
        mod_text += f"\n\nИмя: {name}\nТелефон: {phone}"
        await bot.send_message(MODERATOR_CHAT_ID, mod_text)
        if qr_photo:
            await bot.send_photo(MODERATOR_CHAT_ID, qr_photo,
                                 caption="QR-код пользователя")
        
        action_kb = InlineKeyboardMarkup().add(
            InlineKeyboardButton(f"✅ Завершить заявку {order_id}",
                                 callback_data=f"close_request:{user_id}:{order_id}"),
            InlineKeyboardButton(f"❌ Отменить заявку {order_id}",
                                 callback_data=f"cancel_admin:{user_id}:{order_id}")
        )
        await bot.send_message(
            MODERATOR_CHAT_ID,
            f"Чтобы обработать USDT-заявку №{order_id}, выберите действие:",
            reply_markup=action_kb
        )
        await state.finish()
        await callback_query.answer()
        return

    # ========================================================================
    #  RUB / CNY – старый путь (оставлен без изменений)
    # ========================================================================
    rub_amount   = data.get("rub_amount")
    cny_amount   = data.get("cny_amount")
    rate         = data.get("rate")
    user_bank    = data.get("user_bank", "Не указано")
    used_bonus   = data.get("used_bonus", 0)
    rub_to_pay   = rub_amount - used_bonus

    cur_o.execute("""
        INSERT INTO orders
        (user_id, chat_id, rub_amount, cny_amount, rate,
         created_at, user_bank, user_order_number,
         used_bonus, referrer_id, payment_currency)
        VALUES (?,?,?,?,?,?,?,?,?,?,'RUB')
    """, (
        user_id, user_id,
        rub_amount, cny_amount, rate,
        datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        user_bank, user_order_number,
        used_bonus, referrer_id
    ))
    conn_o.commit()
    order_id = cur_o.lastrowid
    conn_o.close()

    if used_bonus:
        change_bonus(user_id, -used_bonus)

    active_dialogs.setdefault(user_id, {})[order_id] = MODERATOR_CHAT_ID

    text = (
        f"Поступила новая заявка №{order_id}\n"
        f"Чат ID пользователя: {user_id}\n"
        f"Локальный №: {user_order_number}\n"
        f"К оплате: {rub_to_pay} ₽\n"
        f"К зачислению: {cny_amount} ¥\n"
        f"Банк: {user_bank}\n"
        f"Курс: {rate} ₽"
    )
    if used_bonus:
        text += f"\n💰 Списано бонусов: {used_bonus} ₽"
    text += f"\n\nИмя: {name}\nТелефон: {phone}"

    await bot.send_message(MODERATOR_CHAT_ID, text)
    if qr_photo:
        await bot.send_photo(MODERATOR_CHAT_ID, qr_photo,
                             caption="QR-код пользователя")

    await notify_partners_if_needed(order_id, rub_amount, name, user_bank)

    await bot.send_message(
        MODERATOR_CHAT_ID,
        f"Чтобы обработать заявку №{order_id}, выберите действие:",
        reply_markup=InlineKeyboardMarkup().add(
            InlineKeyboardButton(f"✅ Завершить {order_id}",callback_data=f"close_request:{user_id}:{order_id}"),
            InlineKeyboardButton(f"❌ Отменить {order_id}",callback_data=f"cancel_admin:{user_id}:{order_id}")
        )
    )

    await callback_query.message.answer(
        "Ваша заявка подтверждена!\n"
        "Модератор подготовит реквизиты и отправит их в чат.\n"
        "Обычно это занимает 15-20 минут."
    )
    await state.finish()
    await callback_query.answer()






def close_order_dialog(chat_id, order_id):
    if chat_id in active_dialogs:
        active_dialogs[chat_id].pop(order_id, None)
        if not active_dialogs[chat_id]:
            del active_dialogs[chat_id]


# ======= Закрытие заявки модератором =======

@dp.callback_query_handler(lambda c: c.data.startswith("cancel_admin"))
async def cancel_admin_request(callback_query: types.CallbackQuery):
    try:
        _, user_id_str, order_id_str = callback_query.data.split(":")
        user_id = int(user_id_str)
        order_id = int(order_id_str)
    except ValueError:
        await callback_query.answer("Неверный формат данных.")
        return

    if user_id not in active_dialogs or order_id not in active_dialogs[user_id]:
        await callback_query.answer("Заявка не найдена или уже отменена.")
        return

    # Удаляем заявку из active_dialogs
    close_order_dialog(user_id, order_id)

    # Получаем used_bonus из заявки
    conn = sqlite3.connect("orders.db")
    cursor = conn.cursor()
    cursor.execute("SELECT used_bonus FROM orders WHERE order_id = ?", (order_id,))
    row = cursor.fetchone()
    used_bonus = row[0] if row else 0

    # Обновляем статус заявки
    cursor.execute("UPDATE orders SET status = 'canceled' WHERE order_id = ?", (order_id,))
    conn.commit()
    conn.close()

    # Возвращаем бонусы, если они были списаны
    if used_bonus > 0:
        bconn = sqlite3.connect("user_balance.db")
        bcursor = bconn.cursor()
        bcursor.execute("UPDATE balances SET balance = balance + ? WHERE user_id = ?", (used_bonus, user_id))
        bconn.commit()
        bconn.close()

    # Уведомляем пользователя
    try:
        await bot.send_message(user_id, f"⛔ Ваша заявка была отменена модератором.")
    except Exception as e:
        await bot.send_message(MODERATOR_CHAT_ID, f"❗ Ошибка при уведомлении пользователя {user_id}: {e}")

    # Ответ модератору
    await callback_query.message.edit_text(f"🚫 Заявка №{order_id} была отменена.")
    await callback_query.answer("Заявка отменена.")






# =========================================================
# ========= Верификация личного кабинета модератором =======
# =========================================================

@dp.callback_query_handler(lambda c: c.data.startswith("verify_approve:"), state="*")
async def verify_approve(callback_query: types.CallbackQuery):
    user_id = int(callback_query.data.split(":")[1])

    # Закрываем бридж верификации
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET is_verified = 1 WHERE chat_id = ?", (user_id,))
    conn.commit()
    conn.close()

    await callback_query.message.edit_text(f"✅ ЛК пользователя {user_id} одобрен.", reply_markup=None)
    active_verify_dialogs.pop(user_id, None)
    await callback_query.answer("Одобрено")

    try:
        await bot.send_message(
            user_id,
            "✅ Ваш личный кабинет прошёл проверку!\n"
            "Теперь вы можете создавать заявки на обмен.",
            reply_markup=main_menu
        )
    except Exception as e:
        await bot.send_message(MODERATOR_CHAT_ID, f"❗ Не удалось уведомить пользователя {user_id}: {e}")


@dp.callback_query_handler(lambda c: c.data.startswith("verify_reject:"), state="*")
async def verify_reject(callback_query: types.CallbackQuery):
    user_id = int(callback_query.data.split(":")[1])

    # Бридж уже открыт с момента регистрации — просто показываем кнопку закрытия
    close_kb = InlineKeyboardMarkup().add(
        InlineKeyboardButton("❌ Отклонить ЛК и закрыть диалог", callback_data=f"verify_close:{user_id}")
    )
    await callback_query.message.edit_reply_markup(reply_markup=close_kb)
    await callback_query.answer("Общайтесь с пользователем, затем нажмите кнопку отклонения")

    try:
        await bot.send_message(
            user_id,
            "⚠️ Модератор хочет уточнить детали вашего ЛК.\n"
            "Напишите ему прямо здесь."
        )
    except Exception as e:
        await bot.send_message(MODERATOR_CHAT_ID, f"❗ Не удалось уведомить пользователя {user_id}: {e}")


@dp.callback_query_handler(lambda c: c.data.startswith("verify_close:"), state="*")
async def verify_close(callback_query: types.CallbackQuery):
    user_id = int(callback_query.data.split(":")[1])

    # Закрываем бридж
    active_verify_dialogs.pop(user_id, None)

    # Сбрасываем регистрацию
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    # Проверяем есть ли бэкап старых данных
    cursor.execute("SELECT old_name, old_phone, old_qr_photo FROM users WHERE chat_id = ?", (user_id,))
    backup = cursor.fetchone()
    has_backup = backup and backup[0] is not None

    if has_backup:
        # Откатываем к старым данным — ЛК остаётся активным
        cursor.execute("""
            UPDATE users SET
                name = old_name, phone = old_phone, qr_photo = old_qr_photo,
                old_name = NULL, old_phone = NULL, old_qr_photo = NULL,
                is_verified = 1
            WHERE chat_id = ?
        """, (user_id,))
        conn.commit()
        conn.close()
        try:
            await callback_query.message.edit_text(f"❌ Обновление ЛК пользователя {user_id} отклонено. Восстановлены старые данные.", reply_markup=None)
        except Exception as e:
            await bot.send_message(MODERATOR_CHAT_ID, f"⚠️ edit_text close1 error: {e}")
    else:
        # Нет бэкапа — первичная регистрация, сбрасываем
        cursor.execute("UPDATE users SET is_registered = 0, is_verified = 0 WHERE chat_id = ?", (user_id,))
        conn.commit()
        conn.close()
        try:
            await callback_query.message.edit_text(f"❌ ЛК пользователя {user_id} отклонён, регистрация сброшена.", reply_markup=None)
        except Exception as e:
            await bot.send_message(MODERATOR_CHAT_ID, f"⚠️ edit_text close2 error: {e}")
    await callback_query.answer("ЛК отклонён")

    try:
        if has_backup:
            await bot.send_message(
                user_id,
                "❌ Обновление личного кабинета отклонено.\n"
                "Ваши предыдущие данные восстановлены, вы можете создавать заявки.",
                reply_markup=main_menu
            )
        else:
            await bot.send_message(
                user_id,
                "❌ Ваш личный кабинет отклонён.\n"
                "Исправьте данные и зарегистрируйтесь заново.",
                reply_markup=registration_menu
            )
    except Exception as e:
        await bot.send_message(MODERATOR_CHAT_ID, f"❗ Не удалось уведомить пользователя {user_id}: {e}")

# ---- Переписка (модератор -> пользователь) ----
@dp.message_handler(lambda m: m.chat.id == MODERATOR_CHAT_ID, content_types=types.ContentTypes.ANY)
async def handle_moderator_reply(message: types.Message):
    # Проверяем verify-диалог — по reply на любое сообщение о верификации
    if active_verify_dialogs and message.reply_to_message:
        reply_text = message.reply_to_message.text or ""
        import re
        # Парсим оба формата: карточку "верификация №ID" и сообщения "верификации ЛК от ID"
        vm = re.search(r"верификаци[яи][^\d]*(№|ЛК от )\s*(\d+)", reply_text)
        if vm:
            target_uid = int(vm.group(2))
            if target_uid in active_verify_dialogs:
                prefix = "Сообщение от модератора:"
                try:
                    if message.content_type == "text":
                        await bot.send_message(target_uid, f"{prefix}\n{message.text}")
                    elif message.content_type == "photo":
                        await bot.send_message(target_uid, prefix)
                        await bot.send_photo(target_uid, message.photo[-1].file_id)
                    elif message.content_type == "voice":
                        await bot.send_message(target_uid, prefix)
                        await bot.send_voice(target_uid, message.voice.file_id)
                    else:
                        await bot.send_message(target_uid, f"{prefix} [{message.content_type}]")
                except Exception as e:
                    await message.reply(f"❗ Не удалось отправить пользователю {target_uid}: {e}")
                return

    # Если это reply на сообщение о верификации — не обрабатываем как заявку
    if message.reply_to_message:
        reply_text = message.reply_to_message.text or ""
        import re
        if re.search(r"верификаци", reply_text):
            return

    # Если reply на сообщение верификации — не обрабатываем как заявку
    if message.reply_to_message:
        import re
        if re.search(r"верификаци", message.reply_to_message.text or ""):
            return

    user_id, order_id = None, None

    if message.reply_to_message:
        original_text = message.reply_to_message.text or ""
        user_id, order_id = parse_user_and_order_from_text(original_text)
    else:
        user_id, order_id = parse_user_and_order_from_text(message.text or "")

    # Проверка существования диалога
    if user_id and order_id:
        if user_id in active_dialogs and order_id in active_dialogs[user_id]:
            await forward_message_to_user(message, user_id, order_id)
            return
        else:
            await message.reply(
                f"❗ Заявка №{order_id} у пользователя {user_id} не найдена или уже закрыта."
            )
            return

    await message.reply(
        "❗ Не удалось определить, к какой заявке относится сообщение.\n"
        "Сделайте Reply (ответ) на сообщение бота о заявке "
        "или явно укажите номер заявки в формате:\n"
        "«заявке №123 от пользователя 456»"
    )


def parse_user_and_order_from_text(text: str):
    import re
    pattern1 = re.compile(r"заявка\s*№(\d+).*Чат ID пользователя:\s*(\d+)", re.DOTALL)
    match1 = pattern1.search(text)
    if match1:
        order_id = int(match1.group(1))
        user_id = int(match1.group(2))
        return (user_id, order_id)

    pattern2 = re.compile(r"заявке\s*№(\d+).+пользователя\s+(\d+)", re.DOTALL)
    match2 = pattern2.search(text)
    if match2:
        order_id = int(match2.group(1))
        user_id = int(match2.group(2))
        return (user_id, order_id)

    pattern3 = re.compile(r"№(\d+)\s*\(user_id\s*=\s*(\d+)\)")
    match3 = re.search(pattern3, text)
    if match3:
        order_id = int(match3.group(1))
        user_id = int(match3.group(2))
        return (user_id, order_id)

    return (None, None)

async def forward_message_to_user(message: types.Message, user_id: int, order_id: int):
    if user_id not in active_dialogs or order_id not in active_dialogs[user_id]:
        await bot.send_message(MODERATOR_CHAT_ID, f"❗ Невозможно отправить сообщение: заявка №{order_id} у пользователя {user_id} не активна.")
        return

    prefix = f"Ответ от модератора:"

    try:
        if message.content_type == "text":
            await bot.send_message(user_id, f"{prefix}\n{message.text}")
        elif message.content_type == "photo":
            await bot.send_message(user_id, f"{prefix}\n")
            await bot.send_photo(user_id, message.photo[-1].file_id)
        elif message.content_type == "document":
            await bot.send_message(user_id, f"{prefix}\n")
            await bot.send_document(user_id, message.document.file_id)
        elif message.content_type == "voice":
            await bot.send_message(user_id, f"{prefix}\n")
            await bot.send_voice(user_id, message.voice.file_id)
        else:
            await bot.send_message(user_id, f"{prefix}\nТип: {message.content_type}")
    except Exception as e:
        await bot.send_message(MODERATOR_CHAT_ID, f"❗ Ошибка при отправке пользователю {user_id}: {e}")



# ---- Переписка (пользователь -> модератор) ----
@dp.message_handler(lambda m: m.chat.id != MODERATOR_CHAT_ID, content_types=types.ContentTypes.ANY)
async def forward_user_message(message: types.Message):
    if message.chat.type in ("group", "supergroup"):
        return
    user_id = message.chat.id

    # Активная заявка — приоритет
    user_orders = active_dialogs.get(user_id)

    if user_orders:
        pass  # идём дальше к роутингу заявок
    elif user_id in active_verify_dialogs:
        # Дополнительно проверяем в БД — вдруг ЛК уже одобрен после перезапуска бота
        _conn = sqlite3.connect("users.db")
        _cur = _conn.cursor()
        _cur.execute("SELECT is_verified FROM users WHERE chat_id = ?", (user_id,))
        _row = _cur.fetchone()
        _conn.close()
        if _row and _row[0] == 1:
            # ЛК одобрен — закрываем бридж и не пересылаем
            active_verify_dialogs.pop(user_id, None)
            return
        # Нет активных заявок — проверяем verify-диалог
        text = f"💬 Сообщение по верификации ЛК от {user_id}:"
        if message.content_type == "text":
            await bot.send_message(MODERATOR_CHAT_ID, f"{text}\n{message.text}")
        elif message.content_type == "photo":
            await bot.send_message(MODERATOR_CHAT_ID, text)
            await bot.send_photo(MODERATOR_CHAT_ID, message.photo[-1].file_id)
        elif message.content_type == "voice":
            await bot.send_message(MODERATOR_CHAT_ID, text)
            await bot.send_voice(MODERATOR_CHAT_ID, message.voice.file_id)
        else:
            await bot.send_message(MODERATOR_CHAT_ID, f"{text} [{message.content_type}]")
        return
    else:
        return  # Нет ни заявок, ни verify-диалога

    if not user_orders:
        return

    # Определяем первую активную заявку
    current_order_id = sorted(user_orders.keys())[0]
    moderator_id = user_orders[current_order_id]

    text = f"Сообщение по заявке №{current_order_id}  от пользователя {user_id}:"

    if message.content_type == "text":
        await bot.send_message(moderator_id, f"{text}\n{message.text}")
    elif message.content_type == "photo":
        await bot.send_message(moderator_id, f"{text}\n(Фото)")
        await bot.send_photo(moderator_id, message.photo[-1].file_id)
    elif message.content_type == "document":
        await bot.send_message(moderator_id, f"{text}\n(Документ)")
        await bot.send_document(moderator_id, message.document.file_id)
    elif message.content_type == "voice":
        await bot.send_message(moderator_id, f"{text}\n(Голосовое)")
        await bot.send_voice(moderator_id, message.voice.file_id)
    else:
        await bot.send_message(moderator_id, f"{text}\nТип: {message.content_type}")


# ======== Закрытие заявки =========

def close_order_dialog(chat_id, order_id):
    if chat_id in active_dialogs:
        active_dialogs[chat_id].pop(order_id, None)
        if not active_dialogs[chat_id]:
            del active_dialogs[chat_id]

@dp.callback_query_handler(lambda c: c.data.startswith("close_request"))
async def close_request(callback_query: types.CallbackQuery):
    try:
        _, user_id_str, order_id_str = callback_query.data.split(":")
        user_id = int(user_id_str)
        order_id = int(order_id_str)
    except ValueError:
        await callback_query.answer("Неверный формат данных.")
        return

    if user_id not in active_dialogs or order_id not in active_dialogs[user_id]:
        await callback_query.answer("Заявка не найдена или уже закрыта.")
        return

    # Удаляем только конкретную заявку
    close_order_dialog(user_id, order_id)

    # Получаем used_bonus и referrer_id из заявки
    conn = sqlite3.connect("orders.db")
    cursor = conn.cursor()
    cursor.execute("SELECT referrer_id, user_order_number FROM orders WHERE order_id = ?", (order_id,))
    row = cursor.fetchone()
    if not row:
        await callback_query.answer("Заявка не найдена в базе.")
        return

    referrer_id, user_order_number = row

    # Обновляем статус заявки
    cursor.execute("UPDATE orders SET status = 'done' WHERE order_id = ?", (order_id,))
    conn.commit()
    conn.close()

    # # Списываем бонусы
    # if used_bonus and used_bonus > 0:
    #     bconn = sqlite3.connect("user_balance.db")
    #     bcursor = bconn.cursor()
    #     bcursor.execute("UPDATE balances SET balance = balance - ? WHERE user_id = ?", (used_bonus, user_id))
    #     bconn.commit()
    #     bconn.close()

    # Начисляем бонус рефереру
    if referrer_id:
        bconn = sqlite3.connect("user_balance.db")
        bcursor = bconn.cursor()
        bcursor.execute("UPDATE balances SET balance = balance + 80 WHERE user_id = ?", (referrer_id,))
        bconn.commit()
        bconn.close()

        # try:
        #     await bot.send_message(referrer_id, "🎉 Ваш реферал успешно завершил заявку. +100 бонусов начислены!")
        # except Exception as e:
        #     await bot.send_message(MODERATOR_CHAT_ID, f"❗ Ошибка при отправке сообщения рефереру {referrer_id}: {e}")

    # Уведомление пользователя
    try:
        await bot.send_message(
            user_id,
            f"✅ Ваша заявка была успешно завершена.\nСпасибо за обращение!"
        )
        feedback_keyboard = ReplyKeyboardMarkup(resize_keyboard=True)
        feedback_keyboard.add("Оставить отзыв", "Главное меню")
        await bot.send_message(
            user_id,
            "Будем рады, если вы оставите отзыв о нашей работе 🫶",
            reply_markup=feedback_keyboard
        )
    except Exception as e:
        await bot.send_message(MODERATOR_CHAT_ID, f"❗ Ошибка при отправке пользователю {user_id}: {e}")

    # Ответ модератору
    await callback_query.message.edit_text(f"✅ Заявка №{order_id} успешно завершена.")
    await callback_query.answer("Заявка закрыта.")




# ---- Запуск бота ----
async def main():
    await set_bot_commands()
    asyncio.create_task(auto_backup())
    asyncio.create_task(sync_github_loop())
    ensure_blacklist_exists()
    dp.middleware.setup(BanMiddleware())
    await dp.start_polling()
    

    
    

if __name__ == "__main__":
    asyncio.run(main())

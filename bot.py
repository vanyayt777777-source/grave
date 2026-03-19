"""
Telegram бот для продажи аккаунтов, верификаций и обучения
Grave Shop - Полная версия
"""

import os
import logging
import asyncio
import json
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Any
from enum import Enum
import random
import string

from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.tl.functions.messages import GetHistoryRequest
import asyncpg
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, FSInputFile
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from cryptography.fernet import Fernet
import aiohttp

# Загрузка переменных окружения
load_dotenv()

# ==================== КОНФИГУРАЦИЯ ====================

class Config:
    BOT_TOKEN = os.getenv("BOT_TOKEN")
    POSTGRES_URL = os.getenv("POSTGRES_URL")
    API_ID = int(os.getenv("API_ID", "39487404"))
    API_HASH = os.getenv("API_HASH", "5820553b948619fc57ca9dac59ae9cfb")
    REVIEWS_CHANNEL_ID = int(os.getenv("REVIEWS_CHANNEL_ID", "-1003421602899"))
    
    # Администраторы (ID из ТЗ)
    ADMIN_IDS = [92333024, 1467521179, 7912833622, 7973988177, 512361845]
    MAIN_ADMIN_ID = 92333024  # Главный админ для верификаций/обучения
    
    # Курс валют
    USD_TO_RUB = 80  # 1 USDT = 80 RUB
    
    # Карта для верификаций/обучения
    MAIN_CARD = "2200701982520410"
    
    # Цены на верификации (в рублях)
    VERIFICATION_PRICES = {
        "yoomoney": {"name": "💳 Юмани", "price_rub": 270, "price_usd": 3.5, "price_coin": 2.9},
        "tsupis": {"name": "💳 Цупис", "price_rub": 309, "price_usd": 4, "price_coin": 3.25},
        "ypay": {"name": "💳 Я.pay", "price_rub": 386, "price_usd": 5, "price_coin": 4.1},
        "wb_bank": {"name": "🛍 WB Банк", "price_rub": 378, "price_usd": 4.5, "price_coin": 4},
        "vk_pay": {"name": "💙 VK Pay", "price_rub": 168, "price_usd": 2, "price_coin": 1.6},
        "avito": {"name": "🛍 Авито", "price_rub": 540, "price_usd": 7, "price_coin": 5.6},
        "binance": {"name": "💳 Binance", "price_rub": 850, "price_usd": 11, "price_coin": 9},
        "wallet": {"name": "👛 Wallet", "price_rub": 580, "price_usd": 7.5, "price_coin": 6.1},
        "bybit": {"name": "🌐 Bybit", "price_rub": 620, "price_usd": 8, "price_coin": 6.5},
        "cryptobot": {"name": "🤖 Cryptobot", "price_rub": 772, "price_usd": 10, "price_coin": 8},
        "fragment": {"name": "🤑 Fragment", "price_rub": 310, "price_usd": 4, "price_coin": 3.3},
    }
    
    # Обучающие материалы
    EDUCATION_PRODUCTS = {
        "fragment_verif": {"name": "📘 Научу верифать фрагмент", "price": 200},
        "fanstat_abuse": {"name": "📗 Научу абузить фанстат", "price": 100},
        "tgp_buy": {"name": "📕 Научу покупать ТГП за 44₽", "price": 150},
    }

config = Config()

# ==================== НАСТРОЙКА ЛОГИРОВАНИЯ ====================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ==================== ИНИЦИАЛИЗАЦИЯ БОТА ====================

bot = Bot(token=config.BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ==================== ШИФРОВАНИЕ ====================

class Encryption:
    def __init__(self):
        # В продакшене ключ должен храниться в .env
        self.key = Fernet.generate_key() if not os.getenv("ENCRYPTION_KEY") else os.getenv("ENCRYPTION_KEY").encode()
        self.cipher = Fernet(self.key)
    
    def encrypt(self, data: str) -> str:
        return self.cipher.encrypt(data.encode()).decode()
    
    def decrypt(self, encrypted_data: str) -> str:
        return self.cipher.decrypt(encrypted_data.encode()).decode()

encryption = Encryption()

# ==================== СОСТОЯНИЯ FSM ====================

class AddAccountStates(StatesGroup):
    waiting_for_phone = State()
    waiting_for_session = State()
    waiting_for_2fa = State()
    waiting_for_country = State()
    waiting_for_title = State()
    waiting_for_description = State()
    waiting_for_price = State()
    waiting_for_seller_note = State()

class PaymentStates(StatesGroup):
    waiting_for_receipt = State()

class AdminStates(StatesGroup):
    waiting_for_crypto_token = State()
    waiting_for_card_details = State()
    waiting_for_sbp_details = State()
    waiting_for_education_file = State()
    waiting_for_broadcast = State()
    waiting_for_promo_code = State()
    waiting_for_promo_discount = State()
    waiting_for_promo_valid = State()
    waiting_for_promo_uses = State()

class ReviewStates(StatesGroup):
    waiting_for_rating = State()
    waiting_for_comment = State()

# ==================== БАЗА ДАННЫХ ====================

class Database:
    def __init__(self, dsn):
        self.dsn = dsn
        self.pool = None
    
    async def connect(self):
        """Подключение к БД и создание таблиц"""
        self.pool = await asyncpg.create_pool(self.dsn)
        
        async with self.pool.acquire() as conn:
            # Таблица пользователей
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    telegram_id BIGINT UNIQUE NOT NULL,
                    username TEXT,
                    full_name TEXT,
                    balance DECIMAL(10,2) DEFAULT 0,
                    registered_at TIMESTAMP DEFAULT NOW(),
                    is_admin BOOLEAN DEFAULT FALSE
                )
            """)
            
            # Таблица администраторов с реквизитами
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS admins (
                    id SERIAL PRIMARY KEY,
                    telegram_id BIGINT UNIQUE NOT NULL,
                    crypto_token TEXT,
                    card_details JSONB,
                    sbp_details JSONB,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            
            # Добавляем администраторов из списка
            for admin_id in config.ADMIN_IDS:
                await conn.execute("""
                    INSERT INTO admins (telegram_id) VALUES ($1)
                    ON CONFLICT (telegram_id) DO NOTHING
                """, admin_id)
                
                # Делаем их администраторами в таблице users
                await conn.execute("""
                    INSERT INTO users (telegram_id, username, is_admin) 
                    VALUES ($1, '', TRUE)
                    ON CONFLICT (telegram_id) DO UPDATE SET is_admin = TRUE
                """, admin_id)
            
            # Таблица аккаунтов на продаже
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS accounts (
                    id SERIAL PRIMARY KEY,
                    phone_number TEXT NOT NULL,
                    session_string TEXT NOT NULL,
                    two_fa_password TEXT,
                    country TEXT,
                    title TEXT,
                    description TEXT,
                    price_rub INTEGER,
                    status TEXT DEFAULT 'available',
                    seller_note TEXT,
                    added_by BIGINT,
                    added_at TIMESTAMP DEFAULT NOW()
                )
            """)
            
            # Таблица покупок
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS purchases (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER REFERENCES users(id),
                    account_id INTEGER REFERENCES accounts(id),
                    account_title TEXT,
                    account_country TEXT,
                    amount INTEGER,
                    payment_method TEXT,
                    status TEXT DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT NOW(),
                    review_left BOOLEAN DEFAULT FALSE,
                    processed_by BIGINT
                )
            """)
            
            # Таблица отзывов
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS reviews (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER REFERENCES users(id),
                    purchase_id INTEGER UNIQUE REFERENCES purchases(id),
                    rating INTEGER CHECK (rating >= 1 AND rating <= 5),
                    comment TEXT,
                    created_at TIMESTAMP DEFAULT NOW(),
                    posted_to_channel BOOLEAN DEFAULT FALSE
                )
            """)
            
            # Таблица продуктов (верификации и обучение)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS products (
                    id SERIAL PRIMARY KEY,
                    type TEXT NOT NULL,
                    category TEXT NOT NULL,
                    title TEXT NOT NULL,
                    price_rub INTEGER,
                    price_usd DECIMAL(5,2),
                    price_coin DECIMAL(5,2),
                    description TEXT,
                    file_id TEXT,
                    available BOOLEAN DEFAULT TRUE
                )
            """)
            
            # Добавляем верификации в таблицу продуктов
            for cat, data in config.VERIFICATION_PRICES.items():
                await conn.execute("""
                    INSERT INTO products (type, category, title, price_rub, price_usd, price_coin)
                    VALUES ('verification', $1, $2, $3, $4, $5)
                    ON CONFLICT DO NOTHING
                """, cat, data["name"], data["price_rub"], data["price_usd"], data["price_coin"])
            
            # Добавляем обучающие материалы
            for cat, data in config.EDUCATION_PRODUCTS.items():
                await conn.execute("""
                    INSERT INTO products (type, category, title, price_rub)
                    VALUES ('education', $1, $2, $3)
                    ON CONFLICT DO NOTHING
                """, cat, data["name"], data["price"])
            
            # Таблица обучающих материалов (файлы)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS education_materials (
                    id SERIAL PRIMARY KEY,
                    product_id INTEGER REFERENCES products(id),
                    file_id TEXT NOT NULL,
                    file_name TEXT,
                    uploaded_by BIGINT,
                    uploaded_at TIMESTAMP DEFAULT NOW()
                )
            """)
            
            # Таблица покупок верификаций/обучения
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS verification_purchases (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER REFERENCES users(id),
                    product_id INTEGER REFERENCES products(id),
                    amount INTEGER,
                    payment_method TEXT,
                    status TEXT DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT NOW(),
                    processed_by BIGINT
                )
            """)
            
            # Таблица промокодов
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS promocodes (
                    id SERIAL PRIMARY KEY,
                    code TEXT UNIQUE NOT NULL,
                    discount_percent INTEGER NOT NULL,
                    valid_until TIMESTAMP,
                    max_uses INTEGER,
                    used_count INTEGER DEFAULT 0
                )
            """)
            
            # Таблица чеков на проверку
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS pending_checks (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER REFERENCES users(id),
                    product_id INTEGER,
                    account_id INTEGER,
                    amount INTEGER,
                    method TEXT,
                    screenshot_file_id TEXT,
                    status TEXT DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT NOW(),
                    assigned_admin BIGINT
                )
            """)
    
    async def get_user(self, telegram_id: int):
        """Получение пользователя по telegram_id"""
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(
                "SELECT * FROM users WHERE telegram_id = $1",
                telegram_id
            )
    
    async def create_user(self, telegram_id: int, username: str, full_name: str):
        """Создание нового пользователя"""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO users (telegram_id, username, full_name)
                VALUES ($1, $2, $3)
                ON CONFLICT (telegram_id) DO NOTHING
            """, telegram_id, username, full_name)
    
    async def get_admin(self, telegram_id: int):
        """Получение реквизитов администратора"""
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(
                "SELECT * FROM admins WHERE telegram_id = $1",
                telegram_id
            )
    
    async def update_admin_crypto(self, telegram_id: int, token: str):
        """Обновление Crypto токена администратора"""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE admins SET crypto_token = $1
                WHERE telegram_id = $2
            """, token, telegram_id)
    
    async def update_admin_card(self, telegram_id: int, card_details: dict):
        """Обновление карты администратора"""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE admins SET card_details = $1::jsonb
                WHERE telegram_id = $2
            """, json.dumps(card_details), telegram_id)
    
    async def update_admin_sbp(self, telegram_id: int, sbp_details: dict):
        """Обновление СБП администратора"""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE admins SET sbp_details = $1::jsonb
                WHERE telegram_id = $2
            """, json.dumps(sbp_details), telegram_id)
    
    async def add_account(self, data: dict):
        """Добавление аккаунта на продажу"""
        async with self.pool.acquire() as conn:
            # Шифруем чувствительные данные
            encrypted_phone = encryption.encrypt(data["phone_number"])
            encrypted_session = encryption.encrypt(data["session_string"])
            encrypted_2fa = encryption.encrypt(data.get("two_fa", "")) if data.get("two_fa") else ""
            
            await conn.execute("""
                INSERT INTO accounts 
                (phone_number, session_string, two_fa_password, country, title, description, price_rub, seller_note, added_by)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            """, encrypted_phone, encrypted_session, encrypted_2fa, data["country"], 
                data["title"], data["description"], data["price"], data["seller_note"], data["added_by"])
    
    async def get_available_accounts(self):
        """Получение списка доступных аккаунтов"""
        async with self.pool.acquire() as conn:
            return await conn.fetch("""
                SELECT * FROM accounts WHERE status = 'available'
                ORDER BY added_at DESC
            """)
    
    async def get_account(self, account_id: int):
        """Получение аккаунта по ID"""
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(
                "SELECT * FROM accounts WHERE id = $1",
                account_id
            )
    
    async def purchase_account(self, user_id: int, account_id: int, amount: int, method: str):
        """Создание записи о покупке аккаунта"""
        async with self.pool.acquire() as conn:
            # Получаем информацию об аккаунте
            account = await conn.fetchrow(
                "SELECT title, country FROM accounts WHERE id = $1",
                account_id
            )
            
            # Создаем запись о покупке
            purchase = await conn.fetchrow("""
                INSERT INTO purchases (user_id, account_id, account_title, account_country, amount, payment_method)
                VALUES ($1, $2, $3, $4, $5, $6)
                RETURNING id
            """, user_id, account_id, account["title"], account["country"], amount, method)
            
            # Меняем статус аккаунта
            await conn.execute("""
                UPDATE accounts SET status = 'sold'
                WHERE id = $1
            """, account_id)
            
            return purchase["id"]
    
    async def confirm_purchase(self, purchase_id: int, admin_id: int):
        """Подтверждение покупки админом"""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE purchases SET status = 'completed', processed_by = $1
                WHERE id = $2
            """, admin_id, purchase_id)
    
    async def get_user_purchases(self, user_id: int):
        """Получение истории покупок пользователя"""
        async with self.pool.acquire() as conn:
            return await conn.fetch("""
                SELECT * FROM purchases 
                WHERE user_id = $1 
                ORDER BY created_at DESC
            """, user_id)
    
    async def add_review(self, user_id: int, purchase_id: int, rating: int, comment: str):
        """Добавление отзыва"""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO reviews (user_id, purchase_id, rating, comment)
                VALUES ($1, $2, $3, $4)
            """, user_id, purchase_id, rating, comment)
            
            # Отмечаем, что отзыв оставлен
            await conn.execute("""
                UPDATE purchases SET review_left = TRUE
                WHERE id = $1
            """, purchase_id)
    
    async def get_pending_review_purchases(self, user_id: int):
        """Получение покупок без отзыва"""
        async with self.pool.acquire() as conn:
            return await conn.fetch("""
                SELECT * FROM purchases 
                WHERE user_id = $1 AND status = 'completed' AND review_left = FALSE
                ORDER BY created_at DESC
            """, user_id)
    
    async def mark_review_posted(self, review_id: int):
        """Отметить, что отзыв опубликован в канале"""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE reviews SET posted_to_channel = TRUE
                WHERE id = $1
            """, review_id)
    
    async def get_products_by_type(self, product_type: str):
        """Получение продуктов по типу"""
        async with self.pool.acquire() as conn:
            return await conn.fetch("""
                SELECT * FROM products 
                WHERE type = $1 AND available = TRUE
                ORDER BY id
            """, product_type)
    
    async def get_product(self, product_id: int):
        """Получение продукта по ID"""
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(
                "SELECT * FROM products WHERE id = $1",
                product_id
            )
    
    async def get_education_material(self, product_id: int):
        """Получение обучающего материала"""
        async with self.pool.acquire() as conn:
            return await conn.fetchrow("""
                SELECT * FROM education_materials 
                WHERE product_id = $1
                ORDER BY uploaded_at DESC
                LIMIT 1
            """, product_id)
    
    async def save_education_material(self, product_id: int, file_id: str, file_name: str, admin_id: int):
        """Сохранение обучающего материала"""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO education_materials (product_id, file_id, file_name, uploaded_by)
                VALUES ($1, $2, $3, $4)
            """, product_id, file_id, file_name, admin_id)
    
    async def create_verification_purchase(self, user_id: int, product_id: int, amount: int, method: str):
        """Создание покупки верификации/обучения"""
        async with self.pool.acquire() as conn:
            return await conn.fetchrow("""
                INSERT INTO verification_purchases (user_id, product_id, amount, payment_method)
                VALUES ($1, $2, $3, $4)
                RETURNING id
            """, user_id, product_id, amount, method)
    
    async def confirm_verification_purchase(self, purchase_id: int, admin_id: int):
        """Подтверждение покупки верификации/обучения"""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE verification_purchases 
                SET status = 'completed', processed_by = $1
                WHERE id = $2
            """, admin_id, purchase_id)
    
    async def create_pending_check(self, user_id: int, amount: int, method: str, file_id: str, 
                                   product_id: int = None, account_id: int = None):
        """Создание чека на проверку"""
        async with self.pool.acquire() as conn:
            assigned_admin = config.MAIN_ADMIN_ID if product_id else None
            
            # Если это аккаунт, назначаем админа, который его добавил
            if account_id:
                account = await conn.fetchrow("SELECT added_by FROM accounts WHERE id = $1", account_id)
                if account:
                    assigned_admin = account["added_by"]
            
            await conn.execute("""
                INSERT INTO pending_checks 
                (user_id, product_id, account_id, amount, method, screenshot_file_id, assigned_admin)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
            """, user_id, product_id, account_id, amount, method, file_id, assigned_admin)
    
    async def get_pending_checks(self, admin_id: int):
        """Получение чеков для конкретного админа"""
        async with self.pool.acquire() as conn:
            return await conn.fetch("""
                SELECT * FROM pending_checks 
                WHERE assigned_admin = $1 AND status = 'pending'
                ORDER BY created_at ASC
            """, admin_id)
    
    async def update_check_status(self, check_id: int, status: str):
        """Обновление статуса чека"""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE pending_checks SET status = $1
                WHERE id = $2
            """, status, check_id)
    
    async def get_user_reviews_count(self, user_id: int):
        """Количество отзывов пользователя"""
        async with self.pool.acquire() as conn:
            result = await conn.fetchval("""
                SELECT COUNT(*) FROM reviews WHERE user_id = $1
            """, user_id)
            return result or 0

# Инициализация базы данных
db = Database(config.POSTGRES_URL)

# ==================== KEYBOARDS ====================

class Keyboards:
    @staticmethod
    def main_menu(is_admin: bool = False):
        """Главное меню"""
        keyboard = [
            [KeyboardButton(text="🛒 Купить аккаунт")],
            [KeyboardButton(text="✅ Верификации"), KeyboardButton(text="📚 Обучение")],
            [KeyboardButton(text="👤 Профиль")]
        ]
        if is_admin:
            keyboard.append([KeyboardButton(text="🔧 Админ панель")])
        return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)
    
    @staticmethod
    def admin_menu():
        """Меню администратора"""
        keyboard = [
            [KeyboardButton(text="➕ Добавление аккаунтов")],
            [KeyboardButton(text="💰 Мои реквизиты"), KeyboardButton(text="📚 Управление обучением")],
            [KeyboardButton(text="✅ Управление верификациями")],
            [KeyboardButton(text="📨 Рассылка"), KeyboardButton(text="🎫 Создание промокодов")],
            [KeyboardButton(text="📊 Статистика"), KeyboardButton(text="🔙 Назад")]
        ]
        return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)
    
    @staticmethod
    def back_button():
        """Кнопка назад"""
        keyboard = [[KeyboardButton(text="🔙 Назад")]]
        return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)
    
    @staticmethod
    def cancel_button():
        """Кнопка отмены"""
        keyboard = [[KeyboardButton(text="❌ Отмена")]]
        return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)
    
    @staticmethod
    def account_selection(accounts):
        """Клавиатура для выбора аккаунта"""
        builder = InlineKeyboardBuilder()
        for acc in accounts:
            builder.button(
                text=f"{acc['title']} - {acc['price_rub']}₽",
                callback_data=f"account_{acc['id']}"
            )
        builder.button(text="◀️ Назад", callback_data="back_to_main")
        builder.adjust(1)
        return builder.as_markup()
    
    @staticmethod
    def payment_methods(has_crypto: bool = True):
        """Клавиатура выбора способа оплаты"""
        builder = InlineKeyboardBuilder()
        builder.button(text="💎 Crypto Bot (USDT)", callback_data="pay_crypto")
        builder.button(text="💳 Банковская карта", callback_data="pay_card")
        builder.button(text="📱 СБП", callback_data="pay_sbp")
        builder.button(text="◀️ Назад", callback_data="back_to_accounts")
        builder.adjust(1)
        return builder.as_markup()
    
    @staticmethod
    def verifications_list():
        """Список верификаций"""
        builder = InlineKeyboardBuilder()
        for cat, data in config.VERIFICATION_PRICES.items():
            builder.button(
                text=data["name"],
                callback_data=f"verif_{cat}"
            )
        builder.button(text="◀️ Назад", callback_data="back_to_main")
        builder.adjust(1)
        return builder.as_markup()
    
    @staticmethod
    def education_list():
        """Список обучающих материалов"""
        builder = InlineKeyboardBuilder()
        for cat, data in config.EDUCATION_PRODUCTS.items():
            builder.button(
                text=f"{data['name']} - {data['price']}₽",
                callback_data=f"edu_{cat}"
            )
        builder.button(text="◀️ Назад", callback_data="back_to_main")
        builder.adjust(1)
        return builder.as_markup()
    
    @staticmethod
    def admin_education_management():
        """Управление обучающими материалами для админа"""
        builder = InlineKeyboardBuilder()
        for cat, data in config.EDUCATION_PRODUCTS.items():
            builder.button(
                text=f"📤 {data['name']}",
                callback_data=f"admin_edu_{cat}"
            )
        builder.button(text="◀️ Назад", callback_data="admin_back")
        builder.adjust(1)
        return builder.as_markup()
    
    @staticmethod
    def admin_requisites_menu():
        """Меню реквизитов администратора"""
        builder = InlineKeyboardBuilder()
        builder.button(text="🤖 Crypto Bot API", callback_data="admin_req_crypto")
        builder.button(text="💳 Карта", callback_data="admin_req_card")
        builder.button(text="📱 СБП", callback_data="admin_req_sbp")
        builder.button(text="◀️ Назад", callback_data="admin_back")
        builder.adjust(1)
        return builder.as_markup()
    
    @staticmethod
    def review_rating():
        """Клавиатура для оценки отзыва"""
        builder = InlineKeyboardBuilder()
        for i in range(1, 6):
            builder.button(text="⭐" * i, callback_data=f"rating_{i}")
        builder.adjust(1)
        return builder.as_markup()
    
    @staticmethod
    def check_actions(check_id: int, is_account: bool = False):
        """Действия с чеком"""
        builder = InlineKeyboardBuilder()
        builder.button(text="✅ Подтвердить", callback_data=f"check_approve_{check_id}")
        builder.button(text="❌ Отклонить", callback_data=f"check_reject_{check_id}")
        if is_account:
            builder.button(text="📩 Получить СМС КОД", callback_data=f"check_sms_{check_id}")
        builder.adjust(1)
        return builder.as_markup()

# ==================== CRYPTO BOT API ====================

class CryptoBot:
    def __init__(self, token: str):
        self.token = token
        self.base_url = "https://pay.crypt.bot/api"
    
    async def create_invoice(self, amount: float, currency: str = "USDT", description: str = ""):
        """Создание счета в Crypto Bot"""
        async with aiohttp.ClientSession() as session:
            headers = {"Crypto-Pay-API-Token": self.token}
            data = {
                "asset": currency,
                "amount": str(amount),
                "description": description,
                "paid_btn_name": "viewItem",
                "paid_btn_url": "https://t.me/grave_shop_bot"
            }
            
            async with session.post(f"{self.base_url}/createInvoice", headers=headers, json=data) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    return result.get("result")
                return None
    
    async def get_balance(self):
        """Получение баланса"""
        async with aiohttp.ClientSession() as session:
            headers = {"Crypto-Pay-API-Token": self.token}
            async with session.get(f"{self.base_url}/getBalance", headers=headers) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    return result.get("result")
                return None

# ==================== TELETHON ДЛЯ СМС КОДОВ ====================

class TelegramSMS:
    def __init__(self, api_id: int, api_hash: str):
        self.api_id = api_id
        self.api_hash = api_hash
        self.client = None
    
    async def connect(self, session_string: str):
        """Подключение с сессией"""
        self.client = TelegramClient(StringSession(session_string), self.api_id, self.api_hash)
        await self.client.connect()
        return await self.client.is_user_authorized()
    
    async def get_sms_code(self):
        """Получение последнего СМС кода из чата Telegram"""
        try:
            # Ищем чат "Telegram"
            async for dialog in self.client.iter_dialogs():
                if dialog.name == "Telegram":
                    chat = dialog.entity
                    
                    # Получаем последние сообщения
                    history = await self.client(GetHistoryRequest(
                        peer=chat,
                        limit=10,
                        offset_date=None,
                        offset_id=0,
                        max_id=0,
                        min_id=0,
                        add_offset=0,
                        hash=0
                    ))
                    
                    # Ищем код в сообщениях (5 цифр подряд)
                    import re
                    for msg in history.messages:
                        if msg.message:
                            codes = re.findall(r'\b\d{5}\b', msg.message)
                            if codes:
                                return codes[0]
            return None
        except Exception as e:
            logger.error(f"Ошибка получения SMS: {e}")
            return None
        finally:
            if self.client:
                await self.client.disconnect()

# ==================== ХЕНДЛЕРЫ ====================

# ----- ОБЩИЕ ХЕНДЛЕРЫ -----

@dp.message(CommandStart())
async def cmd_start(message: Message):
    """Обработчик команды /start"""
    user = message.from_user
    await db.create_user(user.id, user.username or "", user.full_name or "")
    
    user_data = await db.get_user(user.id)
    is_admin = user_data["is_admin"] if user_data else False
    
    await message.answer(
        f"👋 Добро пожаловать в Grave Shop!\n\n"
        f"Здесь вы можете купить Telegram аккаунты, верификации и обучающие материалы.",
        reply_markup=Keyboards.main_menu(is_admin)
    )

@dp.message(F.text == "👤 Профиль")
async def profile_handler(message: Message):
    """Профиль пользователя"""
    user = await db.get_user(message.from_user.id)
    if not user:
        return
    
    purchases = await db.get_user_purchases(user["id"])
    reviews_count = await db.get_user_reviews_count(user["id"])
    
    completed_purchases = [p for p in purchases if p["status"] == "completed"]
    
    text = (
        f"👤 Ваш профиль\n\n"
        f"🆔 ID: {user['telegram_id']}\n"
        f"📅 Зарегистрирован: {user['registered_at'].strftime('%d.%m.%Y')}\n"
        f"💰 Баланс: {user['balance']}₽\n\n"
        f"📊 Статистика:\n"
        f"• Покупок всего: {len(purchases)}\n"
        f"• Успешных: {len(completed_purchases)}\n"
        f"• Отзывов оставлено: {reviews_count}\n\n"
    )
    
    if completed_purchases:
        text += "🛒 Последние покупки:\n"
        for p in completed_purchases[:3]:
            text += f"• {p['account_title']} - {p['created_at'].strftime('%d.%m.%Y')}\n"
    
    await message.answer(text)

@dp.message(F.text == "🔙 Назад")
@dp.message(F.text == "❌ Отмена")
async def back_handler(message: Message, state: FSMContext):
    """Возврат в главное меню"""
    await state.clear()
    user = await db.get_user(message.from_user.id)
    is_admin = user["is_admin"] if user else False
    await message.answer("Главное меню:", reply_markup=Keyboards.main_menu(is_admin))

# ----- ПОКУПКА АККАУНТОВ -----

@dp.message(F.text == "🛒 Купить аккаунт")
async def buy_accounts_handler(message: Message):
    """Список доступных аккаунтов"""
    accounts = await db.get_available_accounts()
    
    if not accounts:
        await message.answer("😕 Пока нет доступных аккаунтов. Попробуйте позже.")
        return
    
    text = "🛒 Доступные аккаунты:\n\n"
    for i, acc in enumerate(accounts, 1):
        decrypted_phone = encryption.decrypt(acc["phone_number"])
        text += f"{i}. {acc['title']} - {acc['price_rub']}₽\n"
        text += f"   🌍 {acc['country']}\n"
        text += f"   📝 {acc['description']}\n\n"
    
    await message.answer(text, reply_markup=Keyboards.account_selection(accounts))

@dp.callback_query(lambda c: c.data.startswith("account_"))
async def account_selected(callback: CallbackQuery, state: FSMContext):
    """Выбор конкретного аккаунта"""
    account_id = int(callback.data.split("_")[1])
    account = await db.get_account(account_id)
    
    if not account or account["status"] != "available":
        await callback.message.edit_text("❌ Этот аккаунт уже продан.")
        return
    
    # Сохраняем ID аккаунта в состоянии
    await state.update_data(account_id=account_id)
    
    decrypted_phone = encryption.decrypt(account["phone_number"])
    
    text = (
        f"📱 Аккаунт: {account['title']}\n"
        f"🌍 Страна: {account['country']}\n"
        f"💰 Цена: {account['price_rub']}₽\n\n"
        f"📝 Описание: {account['description']}\n"
    )
    
    await callback.message.edit_text(
        text,
        reply_markup=Keyboards.payment_methods()
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data == "back_to_accounts")
async def back_to_accounts(callback: CallbackQuery, state: FSMContext):
    """Назад к списку аккаунтов"""
    await state.clear()
    accounts = await db.get_available_accounts()
    await callback.message.edit_text(
        "🛒 Доступные аккаунты:",
        reply_markup=Keyboards.account_selection(accounts)
    )

# ----- ОПЛАТА -----

@dp.callback_query(lambda c: c.data.startswith("pay_"))
async def payment_method_selected(callback: CallbackQuery, state: FSMContext):
    """Выбор способа оплаты"""
    method = callback.data.split("_")[1]
    data = await state.get_data()
    account_id = data.get("account_id")
    
    if not account_id:
        await callback.message.edit_text("❌ Ошибка. Попробуйте снова.")
        return
    
    account = await db.get_account(account_id)
    if not account:
        await callback.message.edit_text("❌ Аккаунт не найден.")
        return
    
    await state.update_data(payment_method=method)
    
    if method == "crypto":
        # Оплата через Crypto Bot
        admin = await db.get_admin(account["added_by"])
        if not admin or not admin["crypto_token"]:
            await callback.message.edit_text(
                "❌ Оплата через Crypto Bot временно недоступна.",
                reply_markup=Keyboards.payment_methods()
            )
            return
        
        # Конвертация RUB в USDT (80 RUB = 1 USDT)
        amount_usdt = account["price_rub"] / config.USD_TO_RUB
        
        crypto = CryptoBot(admin["crypto_token"])
        invoice = await crypto.create_invoice(
            amount=amount_usdt,
            description=f"Оплата аккаунта {account['title']}"
        )
        
        if invoice:
            await callback.message.edit_text(
                f"💎 Оплата через Crypto Bot\n\n"
                f"Сумма: {amount_usdt:.2f} USDT\n"
                f"К оплате: {account['price_rub']}₽\n\n"
                f"Ссылка для оплаты:\n{invoice['pay_url']}\n\n"
                f"После оплаты нажмите кнопку ниже",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="✅ Я оплатил", callback_data=f"crypto_paid_{account_id}")]
                ])
            )
        else:
            await callback.message.edit_text(
                "❌ Ошибка создания счета. Попробуйте позже.",
                reply_markup=Keyboards.payment_methods()
            )
    
    elif method in ["card", "sbp"]:
        # Ручная оплата картой или СБП
        admin = await db.get_admin(account["added_by"])
        
        if method == "card":
            if not admin or not admin["card_details"]:
                await callback.message.edit_text(
                    "❌ Оплата картой временно недоступна.",
                    reply_markup=Keyboards.payment_methods()
                )
                return
            card = admin["card_details"]
            payment_text = (
                f"💳 Оплата банковской картой\n\n"
                f"Сумма: {account['price_rub']}₽\n\n"
                f"Реквизиты для перевода:\n"
                f"Карта: {card.get('number', config.MAIN_CARD)}\n"
                f"Получатель: {card.get('name', 'Администратор')}\n\n"
                f"После перевода отправьте скриншот чека."
            )
        else:  # sbp
            if not admin or not admin["sbp_details"]:
                await callback.message.edit_text(
                    "❌ Оплата через СБП временно недоступна.",
                    reply_markup=Keyboards.payment_methods()
                )
                return
            sbp = admin["sbp_details"]
            payment_text = (
                f"📱 Оплата через СБП\n\n"
                f"Сумма: {account['price_rub']}₽\n\n"
                f"Реквизиты для перевода:\n"
                f"Номер: {sbp.get('phone', 'Не указан')}\n"
                f"Банк: {sbp.get('bank', 'Не указан')}\n\n"
                f"После перевода отправьте скриншот чека."
            )
        
        await callback.message.edit_text(payment_text)
        await callback.message.answer(
            "📸 Отправьте скриншот чека об оплате:",
            reply_markup=Keyboards.cancel_button()
        )
        await state.set_state(PaymentStates.waiting_for_receipt)

@dp.message(PaymentStates.waiting_for_receipt, F.photo)
async def receipt_handler(message: Message, state: FSMContext):
    """Обработка чека об оплате"""
    data = await state.get_data()
    account_id = data.get("account_id")
    method = data.get("payment_method")
    
    if not account_id:
        await message.answer("❌ Ошибка. Начните заново.", reply_markup=Keyboards.main_menu())
        await state.clear()
        return
    
    account = await db.get_account(account_id)
    user = await db.get_user(message.from_user.id)
    
    # Сохраняем чек
    file_id = message.photo[-1].file_id
    await db.create_pending_check(
        user_id=user["id"],
        account_id=account_id,
        amount=account["price_rub"],
        method=method,
        file_id=file_id
    )
    
    # Создаем покупку
    purchase_id = await db.purchase_account(
        user_id=user["id"],
        account_id=account_id,
        amount=account["price_rub"],
        method=method
    )
    
    await state.update_data(purchase_id=purchase_id)
    
    # Отправляем чек админу
    admin_id = account["added_by"]
    
    caption = (
        f"📨 Новый чек на проверку\n\n"
        f"👤 Покупатель: @{message.from_user.username or 'нет'}\n"
        f"🆔 ID: {message.from_user.id}\n"
        f"📱 Аккаунт: {account['title']}\n"
        f"💰 Сумма: {account['price_rub']}₽\n"
        f"💳 Способ: {method}\n\n"
        f"Проверьте оплату и подтвердите выдачу."
    )
    
    try:
        await bot.send_photo(
            admin_id,
            photo=file_id,
            caption=caption,
            reply_markup=Keyboards.check_actions(purchase_id, is_account=True)
        )
    except Exception as e:
        logger.error(f"Не удалось отправить чек админу {admin_id}: {e}")
        # Отправляем главному админу как запасной вариант
        if admin_id != config.MAIN_ADMIN_ID:
            await bot.send_photo(
                config.MAIN_ADMIN_ID,
                photo=file_id,
                caption=f"{caption}\n\n⚠️ Админ {admin_id} недоступен, чек отправлен вам.",
                reply_markup=Keyboards.check_actions(purchase_id, is_account=True)
            )
    
    await message.answer(
        "✅ Чек отправлен администратору на проверку.\n"
        "Ожидайте подтверждения в течение нескольких минут.",
        reply_markup=Keyboards.main_menu()
    )
    await state.clear()

@dp.message(PaymentStates.waiting_for_receipt)
async def receipt_invalid_handler(message: Message):
    """Неверный формат чека"""
    await message.answer("❌ Пожалуйста, отправьте фото чека.")

# ----- ВЕРИФИКАЦИИ -----

@dp.message(F.text == "✅ Верификации")
async def verifications_handler(message: Message):
    """Список верификаций"""
    text = "✅ Доступные верификации:\n\n"
    for data in config.VERIFICATION_PRICES.values():
        text += f"{data['name']} - {data['price_rub']}₽ / {data['price_usd']}$ / {data['price_coin']}🪙\n"
    
    await message.answer(text, reply_markup=Keyboards.verifications_list())

@dp.callback_query(lambda c: c.data.startswith("verif_"))
async def verification_selected(callback: CallbackQuery, state: FSMContext):
    """Выбор верификации"""
    category = callback.data.split("_")[1]
    data = config.VERIFICATION_PRICES[category]
    
    await state.update_data(
        product_type="verification",
        category=category,
        title=data["name"],
        price_rub=data["price_rub"],
        price_usd=data["price_usd"],
        price_coin=data["price_coin"]
    )
    
    text = (
        f"{data['name']}\n\n"
        f"💰 Цены:\n"
        f"• {data['price_rub']}₽\n"
        f"• {data['price_usd']}$\n"
        f"• {data['price_coin']}🪙\n\n"
        f"Выберите способ оплаты:"
    )
    
    builder = InlineKeyboardBuilder()
    builder.button(text="💎 Crypto Bot (USDT)", callback_data="verif_pay_crypto")
    builder.button(text="💳 Банковская карта", callback_data="verif_pay_card")
    builder.button(text="📱 СБП", callback_data="verif_pay_sbp")
    builder.button(text="◀️ Назад", callback_data="back_to_verifications")
    builder.adjust(1)
    
    await callback.message.edit_text(text, reply_markup=builder.as_markup())
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("verif_pay_"))
async def verification_payment(callback: CallbackQuery, state: FSMContext):
    """Оплата верификации"""
    method = callback.data.split("_")[2]
    data = await state.get_data()
    
    await state.update_data(payment_method=method)
    
    if method == "crypto":
        # Crypto Bot оплата
        amount_coin = data["price_coin"]
        admin = await db.get_admin(config.MAIN_ADMIN_ID)
        
        if not admin or not admin["crypto_token"]:
            await callback.message.edit_text(
                "❌ Crypto Bot временно недоступен.",
                reply_markup=Keyboards.verifications_list()
            )
            return
        
        crypto = CryptoBot(admin["crypto_token"])
        invoice = await crypto.create_invoice(
            amount=amount_coin,
            description=f"Верификация {data['title']}"
        )
        
        if invoice:
            await callback.message.edit_text(
                f"💎 Оплата через Crypto Bot\n\n"
                f"Услуга: {data['title']}\n"
                f"Сумма: {amount_coin} USDT\n\n"
                f"Ссылка для оплаты:\n{invoice['pay_url']}\n\n"
                f"После оплаты нажмите кнопку ниже",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="✅ Я оплатил", callback_data="verif_crypto_paid")]
                ])
            )
        else:
            await callback.message.edit_text("❌ Ошибка создания счета.")
    
    elif method in ["card", "sbp"]:
        # Ручная оплата
        if method == "card":
            payment_text = (
                f"💳 Оплата банковской картой\n\n"
                f"Услуга: {data['title']}\n"
                f"Сумма: {data['price_rub']}₽\n\n"
                f"Реквизиты для перевода:\n"
                f"Карта: {config.MAIN_CARD}\n"
                f"Получатель: Администратор\n\n"
                f"После перевода отправьте скриншот чека."
            )
        else:
            payment_text = (
                f"📱 Оплата через СБП\n\n"
                f"Услуга: {data['title']}\n"
                f"Сумма: {data['price_rub']}₽\n\n"
                f"Реквизиты для перевода:\n"
                f"Номер: +7 (999) 999-99-99\n"
                f"Банк: Сбербанк\n\n"
                f"После перевода отправьте скриншот чека."
            )
        
        await callback.message.edit_text(payment_text)
        await callback.message.answer(
            "📸 Отправьте скриншот чека об оплате:",
            reply_markup=Keyboards.cancel_button()
        )
        await state.set_state(PaymentStates.waiting_for_receipt)

@dp.message(PaymentStates.waiting_for_receipt, F.photo)
async def verification_receipt_handler(message: Message, state: FSMContext):
    """Обработка чека для верификации"""
    data = await state.get_data()
    
    if data.get("product_type") != "verification":
        # Это не верификация, обрабатываем дальше
        return
    
    user = await db.get_user(message.from_user.id)
    file_id = message.photo[-1].file_id
    
    # Находим продукт в БД
    async with db.pool.acquire() as conn:
        product = await conn.fetchrow(
            "SELECT id FROM products WHERE type='verification' AND category=$1",
            data["category"]
        )
    
    if product:
        # Создаем запись о покупке
        purchase_id = await db.create_verification_purchase(
            user_id=user["id"],
            product_id=product["id"],
            amount=data["price_rub"],
            method=data["payment_method"]
        )
        
        await state.update_data(purchase_id=purchase_id, product_id=product["id"])
        
        # Создаем чек
        await db.create_pending_check(
            user_id=user["id"],
            product_id=product["id"],
            amount=data["price_rub"],
            method=data["payment_method"],
            file_id=file_id
        )
        
        # Отправляем главному админу
        caption = (
            f"📨 Новый чек на верификацию\n\n"
            f"👤 Покупатель: @{message.from_user.username or 'нет'}\n"
            f"🆔 ID: {message.from_user.id}\n"
            f"📱 Услуга: {data['title']}\n"
            f"💰 Сумма: {data['price_rub']}₽\n"
            f"💳 Способ: {data['payment_method']}\n\n"
            f"Проверьте оплату и подтвердите."
        )
        
        await bot.send_photo(
            config.MAIN_ADMIN_ID,
            photo=file_id,
            caption=caption,
            reply_markup=Keyboards.check_actions(purchase_id)
        )
        
        await message.answer(
            "✅ Чек отправлен администратору.\n"
            "После подтверждения верификация будет пройдена.",
            reply_markup=Keyboards.main_menu()
        )
    else:
        await message.answer("❌ Ошибка. Попробуйте позже.")
    
    await state.clear()

# ----- ОБУЧЕНИЕ -----

@dp.message(F.text == "📚 Обучение")
async def education_handler(message: Message):
    """Список обучающих материалов"""
    text = "📚 Доступные обучающие материалы:\n\n"
    for data in config.EDUCATION_PRODUCTS.values():
        text += f"{data['name']} - {data['price']}₽\n"
    
    await message.answer(text, reply_markup=Keyboards.education_list())

@dp.callback_query(lambda c: c.data.startswith("edu_"))
async def education_selected(callback: CallbackQuery, state: FSMContext):
    """Выбор обучающего материала"""
    category = callback.data.split("_")[1]
    data = config.EDUCATION_PRODUCTS[category]
    
    await state.update_data(
        product_type="education",
        category=category,
        title=data["name"],
        price=data["price"]
    )
    
    text = (
        f"{data['name']}\n\n"
        f"💰 Цена: {data['price']}₽\n\n"
        f"Выберите способ оплаты:"
    )
    
    builder = InlineKeyboardBuilder()
    builder.button(text="💎 Crypto Bot (USDT)", callback_data="edu_pay_crypto")
    builder.button(text="💳 Банковская карта", callback_data="edu_pay_card")
    builder.button(text="📱 СБП", callback_data="edu_pay_sbp")
    builder.button(text="◀️ Назад", callback_data="back_to_education")
    builder.adjust(1)
    
    await callback.message.edit_text(text, reply_markup=builder.as_markup())
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("edu_pay_"))
async def education_payment(callback: CallbackQuery, state: FSMContext):
    """Оплата обучения"""
    method = callback.data.split("_")[2]
    data = await state.get_data()
    
    await state.update_data(payment_method=method)
    
    if method == "crypto":
        # Crypto Bot оплата
        amount_usdt = data["price"] / config.USD_TO_RUB
        admin = await db.get_admin(config.MAIN_ADMIN_ID)
        
        if not admin or not admin["crypto_token"]:
            await callback.message.edit_text(
                "❌ Crypto Bot временно недоступен.",
                reply_markup=Keyboards.education_list()
            )
            return
        
        crypto = CryptoBot(admin["crypto_token"])
        invoice = await crypto.create_invoice(
            amount=amount_usdt,
            description=f"Обучение: {data['title']}"
        )
        
        if invoice:
            await callback.message.edit_text(
                f"💎 Оплата через Crypto Bot\n\n"
                f"Материал: {data['title']}\n"
                f"Сумма: {amount_usdt:.2f} USDT\n\n"
                f"Ссылка для оплаты:\n{invoice['pay_url']}\n\n"
                f"После оплаты нажмите кнопку ниже",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="✅ Я оплатил", callback_data="edu_crypto_paid")]
                ])
            )
        else:
            await callback.message.edit_text("❌ Ошибка создания счета.")
    
    elif method in ["card", "sbp"]:
        # Ручная оплата
        if method == "card":
            payment_text = (
                f"💳 Оплата банковской картой\n\n"
                f"Материал: {data['title']}\n"
                f"Сумма: {data['price']}₽\n\n"
                f"Реквизиты для перевода:\n"
                f"Карта: {config.MAIN_CARD}\n"
                f"Получатель: Администратор\n\n"
                f"После перевода отправьте скриншот чека."
            )
        else:
            payment_text = (
                f"📱 Оплата через СБП\n\n"
                f"Материал: {data['title']}\n"
                f"Сумма: {data['price']}₽\n\n"
                f"Реквизиты для перевода:\n"
                f"Номер: +7 (999) 999-99-99\n"
                f"Банк: Сбербанк\n\n"
                f"После перевода отправьте скриншот чека."
            )
        
        await callback.message.edit_text(payment_text)
        await callback.message.answer(
            "📸 Отправьте скриншот чека об оплате:",
            reply_markup=Keyboards.cancel_button()
        )
        await state.set_state(PaymentStates.waiting_for_receipt)

@dp.message(PaymentStates.waiting_for_receipt, F.photo)
async def education_receipt_handler(message: Message, state: FSMContext):
    """Обработка чека для обучения"""
    data = await state.get_data()
    
    if data.get("product_type") != "education":
        return
    
    user = await db.get_user(message.from_user.id)
    file_id = message.photo[-1].file_id
    
    # Находим продукт в БД
    async with db.pool.acquire() as conn:
        product = await conn.fetchrow(
            "SELECT id FROM products WHERE type='education' AND category=$1",
            data["category"]
        )
    
    if product:
        # Создаем запись о покупке
        purchase_id = await db.create_verification_purchase(
            user_id=user["id"],
            product_id=product["id"],
            amount=data["price"],
            method=data["payment_method"]
        )
        
        await state.update_data(purchase_id=purchase_id, product_id=product["id"])
        
        # Создаем чек
        await db.create_pending_check(
            user_id=user["id"],
            product_id=product["id"],
            amount=data["price"],
            method=data["payment_method"],
            file_id=file_id
        )
        
        # Отправляем главному админу
        caption = (
            f"📨 Новый чек на обучение\n\n"
            f"👤 Покупатель: @{message.from_user.username or 'нет'}\n"
            f"🆔 ID: {message.from_user.id}\n"
            f"📚 Материал: {data['title']}\n"
            f"💰 Сумма: {data['price']}₽\n"
            f"💳 Способ: {data['payment_method']}\n\n"
            f"Проверьте оплату и отправьте материал."
        )
        
        await bot.send_photo(
            config.MAIN_ADMIN_ID,
            photo=file_id,
            caption=caption,
            reply_markup=Keyboards.check_actions(purchase_id)
        )
        
        await message.answer(
            "✅ Чек отправлен администратору.\n"
            "После подтверждения вы получите материал.",
            reply_markup=Keyboards.main_menu()
        )
    else:
        await message.answer("❌ Ошибка. Попробуйте позже.")
    
    await state.clear()

# ----- НАВИГАЦИЯ НАЗАД -----

@dp.callback_query(lambda c: c.data == "back_to_main")
async def back_to_main(callback: CallbackQuery, state: FSMContext):
    """Возврат в главное меню"""
    await state.clear()
    user = await db.get_user(callback.from_user.id)
    is_admin = user["is_admin"] if user else False
    await callback.message.delete()
    await callback.message.answer(
        "Главное меню:",
        reply_markup=Keyboards.main_menu(is_admin)
    )

@dp.callback_query(lambda c: c.data == "back_to_verifications")
async def back_to_verifications(callback: CallbackQuery):
    """Назад к списку верификаций"""
    text = "✅ Доступные верификации:\n\n"
    for data in config.VERIFICATION_PRICES.values():
        text += f"{data['name']} - {data['price_rub']}₽ / {data['price_usd']}$ / {data['price_coin']}🪙\n"
    
    await callback.message.edit_text(text, reply_markup=Keyboards.verifications_list())

@dp.callback_query(lambda c: c.data == "back_to_education")
async def back_to_education(callback: CallbackQuery):
    """Назад к списку обучения"""
    text = "📚 Доступные обучающие материалы:\n\n"
    for data in config.EDUCATION_PRODUCTS.values():
        text += f"{data['name']} - {data['price']}₽\n"
    
    await callback.message.edit_text(text, reply_markup=Keyboards.education_list())

# ----- АДМИН ПАНЕЛЬ -----

@dp.message(F.text == "🔧 Админ панель")
async def admin_panel(message: Message):
    """Вход в админ панель"""
    user = await db.get_user(message.from_user.id)
    if not user or not user["is_admin"]:
        await message.answer("❌ У вас нет доступа к админ панели.")
        return
    
    await message.answer(
        "🔧 Панель администратора\n\nВыберите раздел:",
        reply_markup=Keyboards.admin_menu()
    )

@dp.message(F.text == "💰 Мои реквизиты")
async def admin_requisites(message: Message):
    """Управление реквизитами админа"""
    user = await db.get_user(message.from_user.id)
    if not user or not user["is_admin"]:
        return
    
    admin = await db.get_admin(message.from_user.id)
    
    text = "💰 Ваши реквизиты:\n\n"
    
    if admin and admin["crypto_token"]:
        text += f"🤖 Crypto Bot: ✅ Установлен\n"
    else:
        text += f"🤖 Crypto Bot: ❌ Не установлен\n"
    
    if admin and admin["card_details"]:
        card = admin["card_details"]
        text += f"💳 Карта: {card.get('number', 'Не указана')}\n"
    else:
        text += f"💳 Карта: ❌ Не указана\n"
    
    if admin and admin["sbp_details"]:
        sbp = admin["sbp_details"]
        text += f"📱 СБП: {sbp.get('phone', 'Не указан')} ({sbp.get('bank', '')})\n"
    else:
        text += f"📱 СБП: ❌ Не указан\n"
    
    await message.answer(text, reply_markup=Keyboards.admin_requisites_menu())

@dp.callback_query(lambda c: c.data == "admin_req_crypto")
async def admin_crypto_token(callback: CallbackQuery, state: FSMContext):
    """Установка Crypto токена"""
    await callback.message.edit_text(
        "🤖 Отправьте ваш Crypto Bot API токен:\n\n"
        "Как получить:\n"
        "1. Перейдите к @CryptoBot\n"
        "2. Нажмите Crypto Pay → Создать приложение\n"
        "3. Скопируйте токен"
    )
    await state.set_state(AdminStates.waiting_for_crypto_token)
    await callback.answer()

@dp.message(AdminStates.waiting_for_crypto_token)
async def process_crypto_token(message: Message, state: FSMContext):
    """Обработка Crypto токена"""
    token = message.text.strip()
    
    # Проверяем токен
    crypto = CryptoBot(token)
    balance = await crypto.get_balance()
    
    if balance is not None:
        await db.update_admin_crypto(message.from_user.id, token)
        await message.answer(
            "✅ Crypto Bot токен успешно сохранен!\n"
            f"Баланс: {balance}",
            reply_markup=Keyboards.admin_menu()
        )
    else:
        await message.answer(
            "❌ Неверный токен. Попробуйте снова.",
            reply_markup=Keyboards.admin_menu()
        )
    await state.clear()

@dp.callback_query(lambda c: c.data == "admin_req_card")
async def admin_card_details(callback: CallbackQuery, state: FSMContext):
    """Установка карты"""
    await callback.message.edit_text(
        "💳 Отправьте данные карты в формате:\n\n"
        "Номер карты\n"
        "Имя владельца\n\n"
        "Пример:\n"
        "2200701982520410\n"
        "Иван Иванов"
    )
    await state.set_state(AdminStates.waiting_for_card_details)
    await callback.answer()

@dp.message(AdminStates.waiting_for_card_details)
async def process_card_details(message: Message, state: FSMContext):
    """Обработка данных карты"""
    lines = message.text.strip().split('\n')
    if len(lines) >= 2:
        card_details = {
            "number": lines[0].strip(),
            "name": lines[1].strip()
        }
        await db.update_admin_card(message.from_user.id, card_details)
        await message.answer(
            "✅ Данные карты сохранены!",
            reply_markup=Keyboards.admin_menu()
        )
    else:
        await message.answer(
            "❌ Неверный формат. Используйте:\nНомер карты\nИмя владельца",
            reply_markup=Keyboards.admin_menu()
        )
    await state.clear()

@dp.callback_query(lambda c: c.data == "admin_req_sbp")
async def admin_sbp_details(callback: CallbackQuery, state: FSMContext):
    """Установка СБП"""
    await callback.message.edit_text(
        "📱 Отправьте данные СБП в формате:\n\n"
        "Номер телефона\n"
        "Банк\n\n"
        "Пример:\n"
        "+79991234567\n"
        "Т-Банк"
    )
    await state.set_state(AdminStates.waiting_for_sbp_details)
    await callback.answer()

@dp.message(AdminStates.waiting_for_sbp_details)
async def process_sbp_details(message: Message, state: FSMContext):
    """Обработка данных СБП"""
    lines = message.text.strip().split('\n')
    if len(lines) >= 2:
        sbp_details = {
            "phone": lines[0].strip(),
            "bank": lines[1].strip()
        }
        await db.update_admin_sbp(message.from_user.id, sbp_details)
        await message.answer(
            "✅ Данные СБП сохранены!",
            reply_markup=Keyboards.admin_menu()
        )
    else:
        await message.answer(
            "❌ Неверный формат. Используйте:\nНомер телефона\nБанк",
            reply_markup=Keyboards.admin_menu()
        )
    await state.clear()

# ----- ДОБАВЛЕНИЕ АККАУНТОВ -----

@dp.message(F.text == "➕ Добавление аккаунтов")
async def add_account_start(message: Message, state: FSMContext):
    """Начало добавления аккаунта"""
    user = await db.get_user(message.from_user.id)
    if not user or not user["is_admin"]:
        return
    
    await message.answer(
        "📱 Введите номер телефона аккаунта (в формате +79123456789):",
        reply_markup=Keyboards.cancel_button()
    )
    await state.set_state(AddAccountStates.waiting_for_phone)

@dp.message(AddAccountStates.waiting_for_phone)
async def add_account_phone(message: Message, state: FSMContext):
    """Ввод номера телефона"""
    phone = message.text.strip()
    await state.update_data(phone_number=phone)
    
    await message.answer(
        "🔑 Введите session string аккаунта (из Telethon):",
        reply_markup=Keyboards.cancel_button()
    )
    await state.set_state(AddAccountStates.waiting_for_session)

@dp.message(AddAccountStates.waiting_for_session)
async def add_account_session(message: Message, state: FSMContext):
    """Ввод session string"""
    session = message.text.strip()
    await state.update_data(session_string=session)
    
    await message.answer(
        "🔐 Введите пароль 2FA (если есть, иначе отправьте '-'):",
        reply_markup=Keyboards.cancel_button()
    )
    await state.set_state(AddAccountStates.waiting_for_2fa)

@dp.message(AddAccountStates.waiting_for_2fa)
async def add_account_2fa(message: Message, state: FSMContext):
    """Ввод 2FA"""
    two_fa = message.text.strip()
    if two_fa == "-":
        two_fa = ""
    
    await state.update_data(two_fa=two_fa)
    
    await message.answer(
        "🌍 Введите страну аккаунта (например, Россия):",
        reply_markup=Keyboards.cancel_button()
    )
    await state.set_state(AddAccountStates.waiting_for_country)

@dp.message(AddAccountStates.waiting_for_country)
async def add_account_country(message: Message, state: FSMContext):
    """Ввод страны"""
    await state.update_data(country=message.text.strip())
    
    await message.answer(
        "📝 Введите название/заголовок аккаунта:",
        reply_markup=Keyboards.cancel_button()
    )
    await state.set_state(AddAccountStates.waiting_for_title)

@dp.message(AddAccountStates.waiting_for_title)
async def add_account_title(message: Message, state: FSMContext):
    """Ввод названия"""
    await state.update_data(title=message.text.strip())
    
    await message.answer(
        "📄 Введите описание аккаунта:",
        reply_markup=Keyboards.cancel_button()
    )
    await state.set_state(AddAccountStates.waiting_for_description)

@dp.message(AddAccountStates.waiting_for_description)
async def add_account_description(message: Message, state: FSMContext):
    """Ввод описания"""
    await state.update_data(description=message.text.strip())
    
    await message.answer(
        "💰 Введите цену в рублях (только число):",
        reply_markup=Keyboards.cancel_button()
    )
    await state.set_state(AddAccountStates.waiting_for_price)

@dp.message(AddAccountStates.waiting_for_price)
async def add_account_price(message: Message, state: FSMContext):
    """Ввод цены"""
    try:
        price = int(message.text.strip())
        await state.update_data(price=price)
        
        await message.answer(
            "📌 Введите примечание для продавца (необязательно, можно отправить '-'):",
            reply_markup=Keyboards.cancel_button()
        )
        await state.set_state(AddAccountStates.waiting_for_seller_note)
    except ValueError:
        await message.answer("❌ Введите число!")

@dp.message(AddAccountStates.waiting_for_seller_note)
async def add_account_note(message: Message, state: FSMContext):
    """Ввод примечания и сохранение"""
    note = message.text.strip()
    if note == "-":
        note = ""
    
    data = await state.get_data()
    data["seller_note"] = note
    data["added_by"] = message.from_user.id
    
    await db.add_account(data)
    
    await message.answer(
        "✅ Аккаунт успешно добавлен в продажу!",
        reply_markup=Keyboards.admin_menu()
    )
    await state.clear()

# ----- УПРАВЛЕНИЕ ОБУЧЕНИЕМ -----

@dp.message(F.text == "📚 Управление обучением")
async def admin_education(message: Message):
    """Меню управления обучением"""
    user = await db.get_user(message.from_user.id)
    if not user or not user["is_admin"]:
        return
    
    await message.answer(
        "📚 Выберите материал для загрузки:",
        reply_markup=Keyboards.admin_education_management()
    )

@dp.callback_query(lambda c: c.data.startswith("admin_edu_"))
async def admin_education_upload(callback: CallbackQuery, state: FSMContext):
    """Загрузка обучающего материала"""
    category = callback.data.split("_")[2]
    
    # Находим продукт
    async with db.pool.acquire() as conn:
        product = await conn.fetchrow(
            "SELECT id FROM products WHERE type='education' AND category=$1",
            category
        )
    
    if product:
        await state.update_data(product_id=product["id"], category=category)
        
        await callback.message.edit_text(
            f"📤 Отправьте файл с материалом (pdf, txt, фото, видео):",
            reply_markup=Keyboards.cancel_button()
        )
        await state.set_state(AdminStates.waiting_for_education_file)
    else:
        await callback.message.edit_text("❌ Материал не найден.")
    
    await callback.answer()

@dp.message(AdminStates.waiting_for_education_file, F.document | F.photo | F.video)
async def admin_education_file(message: Message, state: FSMContext):
    """Сохранение файла обучения"""
    data = await state.get_data()
    
    if message.document:
        file_id = message.document.file_id
        file_name = message.document.file_name
    elif message.photo:
        file_id = message.photo[-1].file_id
        file_name = "photo.jpg"
    elif message.video:
        file_id = message.video.file_id
        file_name = "video.mp4"
    else:
        await message.answer("❌ Неподдерживаемый формат. Отправьте файл, фото или видео.")
        return
    
    await db.save_education_material(
        product_id=data["product_id"],
        file_id=file_id,
        file_name=file_name,
        admin_id=message.from_user.id
    )
    
    await message.answer(
        "✅ Материал успешно загружен!",
        reply_markup=Keyboards.admin_menu()
    )
    await state.clear()

# ----- ОБРАБОТКА ЧЕКОВ -----

@dp.callback_query(lambda c: c.data.startswith("check_approve_"))
async def check_approve(callback: CallbackQuery):
    """Подтверждение чека"""
    purchase_id = int(callback.data.split("_")[2])
    
    # Получаем информацию о покупке
    async with db.pool.acquire() as conn:
        purchase = await conn.fetchrow(
            "SELECT * FROM purchases WHERE id = $1",
            purchase_id
        )
        
        if purchase:
            # Подтверждаем покупку
            await conn.execute("""
                UPDATE purchases SET status = 'completed', processed_by = $1
                WHERE id = $2
            """, callback.from_user.id, purchase_id)
            
            # Получаем аккаунт
            account = await conn.fetchrow(
                "SELECT * FROM accounts WHERE id = $1",
                purchase["account_id"]
            )
            
            if account:
                # Расшифровываем данные
                phone = encryption.decrypt(account["phone_number"])
                session = encryption.decrypt(account["session_string"])
                two_fa = encryption.decrypt(account["two_fa_password"]) if account["two_fa_password"] else ""
                
                # Отправляем пользователю
                text = (
                    f"✅ Оплата подтверждена!\n\n"
                    f"📱 Аккаунт: {account['title']}\n"
                    f"📞 Телефон: {phone}\n"
                    f"🔐 2FA: {two_fa if two_fa else 'нет'}\n\n"
                    f"Для получения SMS кода нажмите кнопку ниже."
                )
                
                await bot.send_message(
                    purchase["user_id"],
                    text,
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="📩 Получить СМС КОД", callback_data=f"sms_{account['id']}")]
                    ])
                )
                
                # Обновляем статус чека
                await conn.execute("""
                    UPDATE pending_checks SET status = 'approved'
                    WHERE account_id = $1
                """, account["id"])
        
        else:
            # Возможно это верификация или обучение
            verif_purchase = await conn.fetchrow(
                "SELECT * FROM verification_purchases WHERE id = $1",
                purchase_id
            )
            
            if verif_purchase:
                await conn.execute("""
                    UPDATE verification_purchases SET status = 'completed', processed_by = $1
                    WHERE id = $2
                """, callback.from_user.id, purchase_id)
                
                # Для верификации отправляем сообщение
                if verif_purchase["product_id"]:
                    product = await conn.fetchrow(
                        "SELECT * FROM products WHERE id = $1",
                        verif_purchase["product_id"]
                    )
                    
                    if product["type"] == "verification":
                        await bot.send_message(
                            verif_purchase["user_id"],
                            f"✅ Верификация {product['title']} пройдена!"
                        )
                    elif product["type"] == "education":
                        # Отправляем обучающий материал
                        material = await conn.fetchrow(
                            "SELECT * FROM education_materials WHERE product_id = $1 ORDER BY uploaded_at DESC LIMIT 1",
                            product["id"]
                        )
                        
                        if material:
                            if material["file_name"].endswith(('.jpg', '.jpeg', '.png')):
                                await bot.send_photo(
                                    verif_purchase["user_id"],
                                    photo=material["file_id"],
                                    caption=f"📚 {product['title']}"
                                )
                            else:
                                await bot.send_document(
                                    verif_purchase["user_id"],
                                    document=material["file_id"],
                                    caption=f"📚 {product['title']}"
                                )
                        else:
                            await bot.send_message(
                                verif_purchase["user_id"],
                                f"✅ Оплата подтверждена! Материал '{product['title']}' будет отправлен администратором вручную."
                            )
                
                # Обновляем статус чека
                await conn.execute("""
                    UPDATE pending_checks SET status = 'approved'
                    WHERE product_id = $1
                """, verif_purchase["product_id"])
    
    await callback.message.edit_caption(
        callback.message.caption + "\n\n✅ Чек подтвержден!",
        reply_markup=None
    )
    await callback.answer("✅ Чек подтвержден!")

@dp.callback_query(lambda c: c.data.startswith("check_reject_"))
async def check_reject(callback: CallbackQuery):
    """Отклонение чека"""
    purchase_id = int(callback.data.split("_")[2])
    
    async with db.pool.acquire() as conn:
        purchase = await conn.fetchrow(
            "SELECT * FROM purchases WHERE id = $1",
            purchase_id
        )
        
        if purchase:
            await conn.execute("""
                UPDATE purchases SET status = 'rejected'
                WHERE id = $1
            """, purchase_id)
            
            await bot.send_message(
                purchase["user_id"],
                "❌ Ваш чек отклонен. Возможно, оплата не прошла. Попробуйте еще раз или свяжитесь с администратором."
            )
            
            await conn.execute("""
                UPDATE pending_checks SET status = 'rejected'
                WHERE account_id = $1
            """, purchase["account_id"])
    
    await callback.message.edit_caption(
        callback.message.caption + "\n\n❌ Чек отклонен!",
        reply_markup=None
    )
    await callback.answer("❌ Чек отклонен!")

@dp.callback_query(lambda c: c.data.startswith("sms_"))
async def get_sms_code_handler(callback: CallbackQuery):
    """Получение SMS кода"""
    account_id = int(callback.data.split("_")[1])
    
    async with db.pool.acquire() as conn:
        account = await conn.fetchrow(
            "SELECT * FROM accounts WHERE id = $1",
            account_id
        )
    
    if account:
        await callback.message.edit_text("🔄 Получаю SMS код...")
        
        # Расшифровываем сессию
        session_string = encryption.decrypt(account["session_string"])
        
        # Получаем SMS код
        sms = TelegramSMS(config.API_ID, config.API_HASH)
        try:
            await sms.connect(session_string)
            code = await sms.get_sms_code()
            
            if code:
                await callback.message.edit_text(
                    f"✅ Код подтверждения: {code}\n\n"
                    f"Введите его в приложении Telegram для входа."
                )
            else:
                await callback.message.edit_text(
                    "❌ Не удалось получить код. Попробуйте позже или войдите вручную."
                )
        except Exception as e:
            logger.error(f"Ошибка получения SMS: {e}")
            await callback.message.edit_text(
                "❌ Ошибка при получении кода."
            )
    
    await callback.answer()

# ----- ОТЗЫВЫ -----

@dp.callback_query(lambda c: c.data.startswith("review_"))
async def review_start(callback: CallbackQuery, state: FSMContext):
    """Начало создания отзыва"""
    purchase_id = int(callback.data.split("_")[1])
    
    await state.update_data(purchase_id=purchase_id)
    
    await callback.message.edit_text(
        "⭐️ Оцените покупку от 1 до 5:",
        reply_markup=Keyboards.review_rating()
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("rating_"))
async def review_rating(callback: CallbackQuery, state: FSMContext):
    """Выбор оценки"""
    rating = int(callback.data.split("_")[1])
    await state.update_data(rating=rating)
    
    await callback.message.edit_text(
        "💬 Напишите комментарий к отзыву:"
    )
    await state.set_state(ReviewStates.waiting_for_comment)
    await callback.answer()

@dp.message(ReviewStates.waiting_for_comment)
async def review_comment(message: Message, state: FSMContext):
    """Сохранение отзыва"""
    data = await state.get_data()
    purchase_id = data.get("purchase_id")
    rating = data.get("rating")
    comment = message.text
    
    user = await db.get_user(message.from_user.id)
    
    # Получаем информацию о покупке
    async with db.pool.acquire() as conn:
        purchase = await conn.fetchrow(
            "SELECT * FROM purchases WHERE id = $1",
            purchase_id
        )
    
    if purchase:
        # Сохраняем отзыв
        await db.add_review(user["id"], purchase_id, rating, comment)
        
        # Публикуем в канал
        review_text = (
            f"⭐️ НОВЫЙ ОТЗЫВ ⭐️\n\n"
            f"👤 Покупатель: @{message.from_user.username or 'Пользователь'}\n"
            f"📱 Аккаунт: {purchase['account_title']}\n"
            f"🌍 Страна: {purchase['account_country']}\n\n"
            f"Оценка: {'⭐️' * rating}/5\n\n"
            f"💬 Комментарий:\n"
            f"\"{comment}\"\n\n"
            f"🕐 Дата покупки: {purchase['created_at'].strftime('%d.%m.%Y')}"
        )
        
        try:
            await bot.send_message(
                config.REVIEWS_CHANNEL_ID,
                review_text
            )
            
            # Отмечаем, что отзыв опубликован
            async with db.pool.acquire() as conn:
                await conn.execute("""
                    UPDATE reviews SET posted_to_channel = TRUE
                    WHERE purchase_id = $1
                """, purchase_id)
            
            await message.answer(
                "✅ Спасибо за отзыв! Он опубликован в нашем канале.",
                reply_markup=Keyboards.main_menu()
            )
        except Exception as e:
            logger.error(f"Ошибка публикации отзыва: {e}")
            await message.answer(
                "✅ Спасибо за отзыв!",
                reply_markup=Keyboards.main_menu()
            )
    else:
        await message.answer("❌ Ошибка при сохранении отзыва.")
    
    await state.clear()

# ==================== ЗАПУСК БОТА ====================

async def on_startup():
    """Действия при запуске"""
    await db.connect()
    logger.info("База данных подключена")
    logger.info("Бот Grave Shop запущен!")

async def on_shutdown():
    """Действия при остановке"""
    await db.pool.close()
    logger.info("Бот остановлен")

async def main():
    """Главная функция"""
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())

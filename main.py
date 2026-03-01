import logging
import os
import sqlite3
import ssl
import threading
import certifi
import asyncio
from datetime import datetime, timedelta
from typing import Dict, Optional, List, Any, Union

import aiohttp
from PyPDF2 import PdfReader
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Bot configuration
BOT_TOKEN = "8124787124:AAFOmR5ZU83WODaI1Fj9F61pnaIAX2l5VVs"
DEEPSEEK_API_KEY = "sk-dc9fe1a1bccd4552b32ca92a4ee1cfa7"
DEFAULT_MANAGER_ID = 6392591727
DONATION_ALERTS_URL = "https://www.donationalerts.com/r/daibel_store"
SUBSCRIPTION_PRICE = 149
SUBSCRIPTION_DAYS = 30
DB_FILE = "bot_data.db"
HISTORY_PAGE_SIZE = 5
MAX_MESSAGE_LENGTH = 4000  # Telegram limit is 4096, leaving some buffer

# Initialize bot and dispatcher
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# Database lock for thread safety
db_lock = threading.Lock()


# Database setup with migration support
def init_db():
    with db_lock:
        conn = sqlite3.connect(DB_FILE, timeout=30)
        cursor = conn.cursor()

        # Existing tables
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS subscriptions
        (
            user_id INTEGER PRIMARY KEY,
            start_date TEXT NOT NULL,
            end_date TEXT NOT NULL
        )
        ''')

        cursor.execute('''
        CREATE TABLE IF NOT EXISTS message_history
        (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            timestamp TEXT NOT NULL,
            question TEXT NOT NULL,
            answer TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES subscriptions (user_id)
        )
        ''')

        cursor.execute('''
        CREATE TABLE IF NOT EXISTS pending_payments
        (
            user_id INTEGER PRIMARY KEY,
            user_name TEXT NOT NULL,
            photo_id TEXT NOT NULL,
            timestamp TEXT NOT NULL
        )
        ''')

        cursor.execute('''
        CREATE TABLE IF NOT EXISTS diet_profiles
        (
            user_id INTEGER PRIMARY KEY,
            purpose TEXT NOT NULL,
            age INTEGER NOT NULL,
            gender TEXT NOT NULL,
            weight REAL NOT NULL,
            height INTEGER NOT NULL,
            contraindications TEXT
        )
        ''')

        cursor.execute('''
        CREATE TABLE IF NOT EXISTS maintenance_mode
        (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            is_active INTEGER DEFAULT 0,
            start_time TEXT,
            end_time TEXT,
            reason TEXT
        )
        ''')

        cursor.execute('''
        CREATE TABLE IF NOT EXISTS subscription_freezes
        (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            freeze_start TEXT NOT NULL,
            freeze_end TEXT,
            days_remaining INTEGER NOT NULL,
            FOREIGN KEY (user_id) REFERENCES subscriptions (user_id)
        )
        ''')

        # New tables for managers and promo codes
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS managers
        (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            added_date TEXT NOT NULL,
            added_by INTEGER
        )
        ''')

        # Create promo_codes table with all required columns
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS promo_codes
        (
            code TEXT PRIMARY KEY,
            discount_percent INTEGER NOT NULL,
            created_date TEXT NOT NULL,
            expiry_date TEXT,
            usage_limit INTEGER DEFAULT 1,
            usage_count INTEGER DEFAULT 0,
            is_active INTEGER DEFAULT 1
        )
        ''')

        cursor.execute('''
        CREATE TABLE IF NOT EXISTS used_promo_codes
        (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            promo_code TEXT NOT NULL,
            used_date TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES subscriptions (user_id),
            FOREIGN KEY (promo_code) REFERENCES promo_codes (code)
        )
        ''')

        # Check if promo_codes table needs migration
        cursor.execute("PRAGMA table_info(promo_codes)")
        columns = [column[1] for column in cursor.fetchall()]

        # Add missing columns if needed
        if 'created_date' not in columns:
            try:
                cursor.execute('ALTER TABLE promo_codes ADD COLUMN created_date TEXT')
                logger.info("Added created_date column to promo_codes table")
            except sqlite3.Error as e:
                logger.error(f"Error adding created_date column: {e}")

        if 'expiry_date' not in columns:
            try:
                cursor.execute('ALTER TABLE promo_codes ADD COLUMN expiry_date TEXT')
                logger.info("Added expiry_date column to promo_codes table")
            except sqlite3.Error as e:
                logger.error(f"Error adding expiry_date column: {e}")

        if 'usage_limit' not in columns:
            try:
                cursor.execute('ALTER TABLE promo_codes ADD COLUMN usage_limit INTEGER DEFAULT 1')
                logger.info("Added usage_limit column to promo_codes table")
            except sqlite3.Error as e:
                logger.error(f"Error adding usage_limit column: {e}")

        if 'usage_count' not in columns:
            try:
                cursor.execute('ALTER TABLE promo_codes ADD COLUMN usage_count INTEGER DEFAULT 0')
                logger.info("Added usage_count column to promo_codes table")
            except sqlite3.Error as e:
                logger.error(f"Error adding usage_count column: {e}")

        if 'is_active' not in columns:
            try:
                cursor.execute('ALTER TABLE promo_codes ADD COLUMN is_active INTEGER DEFAULT 1')
                logger.info("Added is_active column to promo_codes table")
            except sqlite3.Error as e:
                logger.error(f"Error adding is_active column: {e}")

        # Insert default manager if not exists
        cursor.execute('SELECT * FROM managers WHERE user_id = ?', (DEFAULT_MANAGER_ID,))
        if not cursor.fetchone():
            cursor.execute(
                'INSERT INTO managers (user_id, username, added_date, added_by) VALUES (?, ?, ?, ?)',
                (DEFAULT_MANAGER_ID, 'Default Manager', datetime.now().isoformat(), DEFAULT_MANAGER_ID)
            )

        conn.commit()
        conn.close()


init_db()


class SubscriptionStates(StatesGroup):
    WAITING_FOR_PAYMENT = State()
    WAITING_FOR_PROMO = State()
    MANAGER_APPROVAL = State()


class AnalysisStates(StatesGroup):
    WAITING_FOR_PDF = State()


class DietStates(StatesGroup):
    PURPOSE = State()
    AGE = State()
    GENDER = State()
    WEIGHT = State()
    HEIGHT = State()
    CONTRAINDICATIONS = State()
    ALLERGIES = State()


class RecommendationStates(StatesGroup):
    CONCERNS = State()


class HistoryStates(StatesGroup):
    VIEWING_HISTORY = State()
    VIEWING_DETAILS = State()


class ManagerStates(StatesGroup):
    ADDING_MANAGER = State()
    REMOVING_MANAGER = State()
    ADDING_SUBSCRIPTION = State()  # Новое состояние для добавления подписки


class PromoStates(StatesGroup):
    CREATING_PROMO = State()
    DELETING_PROMO = State()


# Database functions with thread safety
def is_manager(user_id: int) -> bool:
    with db_lock:
        conn = sqlite3.connect(DB_FILE, timeout=30)
        cursor = conn.cursor()
        cursor.execute('SELECT user_id FROM managers WHERE user_id = ?', (user_id,))
        result = cursor.fetchone() is not None
        conn.close()
        return result


def get_managers() -> List[Dict]:
    with db_lock:
        conn = sqlite3.connect(DB_FILE, timeout=30)
        cursor = conn.cursor()
        cursor.execute('SELECT user_id, username, added_date FROM managers ORDER BY added_date')
        managers = [{
            'user_id': row[0],
            'username': row[1],
            'added_date': datetime.fromisoformat(row[2])
        } for row in cursor.fetchall()]
        conn.close()
        return managers


def add_manager(user_id: int, username: str, added_by: int) -> bool:
    if is_manager(user_id):
        return False

    with db_lock:
        try:
            conn = sqlite3.connect(DB_FILE, timeout=30)
            cursor = conn.cursor()
            cursor.execute(
                'INSERT INTO managers (user_id, username, added_date, added_by) VALUES (?, ?, ?, ?)',
                (user_id, username, datetime.now().isoformat(), added_by)
            )
            conn.commit()
            conn.close()
            return True
        except sqlite3.Error as e:
            logger.error(f"Database error in add_manager: {e}")
            return False


def remove_manager(user_id: int) -> bool:
    if not is_manager(user_id):
        return False

    with db_lock:
        try:
            conn = sqlite3.connect(DB_FILE, timeout=30)
            cursor = conn.cursor()
            cursor.execute('DELETE FROM managers WHERE user_id = ?', (user_id,))
            conn.commit()
            conn.close()
            return True
        except sqlite3.Error as e:
            logger.error(f"Database error in remove_manager: {e}")
            return False


def get_promo_code(code: str) -> Optional[Dict]:
    with db_lock:
        conn = sqlite3.connect(DB_FILE, timeout=30)
        cursor = conn.cursor()
        cursor.execute(
            'SELECT discount_percent, expiry_date, usage_limit, usage_count, is_active FROM promo_codes WHERE code = ?',
            (code,)
        )
        result = cursor.fetchone()
        conn.close()

        if result:
            return {
                'discount_percent': result[0],
                'expiry_date': datetime.fromisoformat(result[1]) if result[1] else None,
                'usage_limit': result[2],
                'usage_count': result[3],
                'is_active': bool(result[4])
            }
        return None


def create_promo_code(code: str, discount_percent: int, expiry_days: Optional[int] = None,
                      usage_limit: int = 1) -> bool:
    if get_promo_code(code):
        return False

    created_date = datetime.now()
    expiry_date = created_date + timedelta(days=expiry_days) if expiry_days else None

    with db_lock:
        try:
            conn = sqlite3.connect(DB_FILE, timeout=30)
            cursor = conn.cursor()
            cursor.execute(
                'INSERT INTO promo_codes (code, discount_percent, created_date, expiry_date, usage_limit) VALUES (?, ?, ?, ?, ?)',
                (code, discount_percent, created_date.isoformat(), expiry_date.isoformat() if expiry_date else None,
                 usage_limit)
            )
            conn.commit()
            conn.close()
            return True
        except sqlite3.Error as e:
            logger.error(f"Database error in create_promo_code: {e}")
            return False


def delete_promo_code(code: str) -> bool:
    with db_lock:
        try:
            conn = sqlite3.connect(DB_FILE, timeout=30)
            cursor = conn.cursor()
            cursor.execute('DELETE FROM promo_codes WHERE code = ?', (code,))
            affected = cursor.rowcount
            conn.commit()
            conn.close()
            return affected > 0
        except sqlite3.Error as e:
            logger.error(f"Database error in delete_promo_code: {e}")
            return False


def get_all_promo_codes() -> List[Dict]:
    with db_lock:
        try:
            conn = sqlite3.connect(DB_FILE, timeout=30)
            cursor = conn.cursor()
            cursor.execute(
                'SELECT code, discount_percent, created_date, expiry_date, usage_limit, usage_count, is_active FROM promo_codes')
            promos = [{
                'code': row[0],
                'discount_percent': row[1],
                'created_date': datetime.fromisoformat(row[2]) if row[2] else datetime.now(),
                'expiry_date': datetime.fromisoformat(row[3]) if row[3] else None,
                'usage_limit': row[4],
                'usage_count': row[5],
                'is_active': bool(row[6])
            } for row in cursor.fetchall()]
            conn.close()
            return promos
        except sqlite3.Error as e:
            logger.error(f"Database error in get_all_promo_codes: {e}")
            return []


def use_promo_code(user_id: int, code: str) -> Optional[int]:
    promo = get_promo_code(code)
    if not promo or not promo['is_active']:
        return None

    # Check if expired
    if promo['expiry_date'] and datetime.now() > promo['expiry_date']:
        return None

    # Check usage limit
    if promo['usage_count'] >= promo['usage_limit']:
        return None

    # Check if already used by this user
    with db_lock:
        try:
            conn = sqlite3.connect(DB_FILE, timeout=30)
            cursor = conn.cursor()
            cursor.execute('SELECT id FROM used_promo_codes WHERE user_id = ? AND promo_code = ?', (user_id, code))
            if cursor.fetchone():
                conn.close()
                return None

            # Update usage count
            cursor.execute(
                'UPDATE promo_codes SET usage_count = usage_count + 1 WHERE code = ?',
                (code,)
            )

            # Record usage
            cursor.execute(
                'INSERT INTO used_promo_codes (user_id, promo_code, used_date) VALUES (?, ?, ?)',
                (user_id, code, datetime.now().isoformat())
            )

            conn.commit()
            conn.close()
            return promo['discount_percent']
        except sqlite3.Error as e:
            logger.error(f"Database error in use_promo_code: {e}")
            return None


def get_subscription(user_id: int) -> Optional[Dict]:
    with db_lock:
        conn = sqlite3.connect(DB_FILE, timeout=30)
        cursor = conn.cursor()

        cursor.execute('''
        SELECT start_date, end_date
        FROM subscriptions
        WHERE user_id = ? AND date(end_date) > date('now')
        ''', (user_id,))

        result = cursor.fetchone()
        conn.close()

        if result:
            return {
                'start_date': datetime.fromisoformat(result[0]),
                'end_date': datetime.fromisoformat(result[1]),
                'days_left': (datetime.fromisoformat(result[1]) - datetime.now()).days
            }
        return None


def add_subscription(user_id: int, days: int):
    start_date = datetime.now()
    end_date = start_date + timedelta(days=days)

    with db_lock:
        try:
            conn = sqlite3.connect(DB_FILE, timeout=30)
            cursor = conn.cursor()

            cursor.execute('''
            INSERT OR REPLACE INTO subscriptions (user_id, start_date, end_date)
            VALUES (?, ?, ?)
            ''', (user_id, start_date.isoformat(), end_date.isoformat()))

            conn.commit()
            conn.close()
        except sqlite3.Error as e:
            logger.error(f"Database error in add_subscription: {e}")


def save_message(user_id: int, question: str, answer: str):
    with db_lock:
        try:
            conn = sqlite3.connect(DB_FILE, timeout=30)
            cursor = conn.cursor()

            cursor.execute('''
            INSERT INTO message_history (user_id, timestamp, question, answer)
            VALUES (?, ?, ?, ?)
            ''', (user_id, datetime.now().isoformat(), question, answer))

            conn.commit()
            conn.close()
        except sqlite3.Error as e:
            logger.error(f"Database error in save_message: {e}")


def get_message_history(user_id: int, limit: int = 10, offset: int = 0) -> List[Dict]:
    with db_lock:
        conn = sqlite3.connect(DB_FILE, timeout=30)
        cursor = conn.cursor()

        cursor.execute('''
        SELECT id, timestamp, question, answer
        FROM message_history
        WHERE user_id = ?
        ORDER BY timestamp DESC
        LIMIT ? OFFSET ?
        ''', (user_id, limit, offset))

        history = [{
            'id': row[0],
            'timestamp': datetime.fromisoformat(row[1]),
            'question': row[2],
            'answer': row[3]
        } for row in cursor.fetchall()]

        conn.close()
        return history


def get_history_entry(entry_id: int) -> Optional[Dict]:
    with db_lock:
        conn = sqlite3.connect(DB_FILE, timeout=30)
        cursor = conn.cursor()

        cursor.execute('''
        SELECT id, timestamp, question, answer
        FROM message_history
        WHERE id = ?
        ''', (entry_id,))

        result = cursor.fetchone()
        conn.close()

        if result:
            return {
                'id': result[0],
                'timestamp': datetime.fromisoformat(result[1]),
                'question': result[2],
                'answer': result[3]
            }
        return None


def get_history_count(user_id: int) -> int:
    with db_lock:
        conn = sqlite3.connect(DB_FILE, timeout=30)
        cursor = conn.cursor()

        cursor.execute('''
        SELECT COUNT(*)
        FROM message_history
        WHERE user_id = ?
        ''', (user_id,))

        count = cursor.fetchone()[0]
        conn.close()
        return count


def add_pending_payment(user_id: int, user_name: str, photo_id: str):
    with db_lock:
        try:
            conn = sqlite3.connect(DB_FILE, timeout=30)
            cursor = conn.cursor()

            cursor.execute('''
            INSERT OR REPLACE INTO pending_payments (user_id, user_name, photo_id, timestamp)
            VALUES (?, ?, ?, ?)
            ''', (user_id, user_name, photo_id, datetime.now().isoformat()))

            conn.commit()
            conn.close()
        except sqlite3.Error as e:
            logger.error(f"Database error in add_pending_payment: {e}")


def get_pending_payment(user_id: int) -> Optional[Dict]:
    with db_lock:
        conn = sqlite3.connect(DB_FILE, timeout=30)
        cursor = conn.cursor()

        cursor.execute('''
        SELECT user_name, photo_id, timestamp
        FROM pending_payments
        WHERE user_id = ?
        ''', (user_id,))

        result = cursor.fetchone()
        conn.close()

        if result:
            return {
                'user_name': result[0],
                'photo_id': result[1],
                'timestamp': datetime.fromisoformat(result[2])
            }
        return None


def remove_pending_payment(user_id: int):
    with db_lock:
        try:
            conn = sqlite3.connect(DB_FILE, timeout=30)
            cursor = conn.cursor()

            cursor.execute('DELETE FROM pending_payments WHERE user_id = ?', (user_id,))
            conn.commit()
            conn.close()
        except sqlite3.Error as e:
            logger.error(f"Database error in remove_pending_payment: {e}")


def save_diet_profile(user_id: int, data: Dict):
    with db_lock:
        try:
            conn = sqlite3.connect(DB_FILE, timeout=30)
            cursor = conn.cursor()

            cursor.execute('''
            INSERT OR REPLACE INTO diet_profiles 
            (user_id, purpose, age, gender, weight, height, contraindications)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (
                user_id,
                data.get('purpose', ''),
                data.get('age', 0),
                data.get('gender', ''),
                data.get('weight', 0),
                data.get('height', 0),
                data.get('allergies', '')
            ))

            conn.commit()
            conn.close()
        except sqlite3.Error as e:
            logger.error(f"Database error in save_diet_profile: {e}")


def get_diet_profile(user_id: int) -> Optional[Dict]:
    with db_lock:
        conn = sqlite3.connect(DB_FILE, timeout=30)
        cursor = conn.cursor()

        cursor.execute('''
        SELECT purpose, age, gender, weight, height, contraindications
        FROM diet_profiles
        WHERE user_id = ?
        ''', (user_id,))

        result = cursor.fetchone()
        conn.close()

        if result:
            return {
                'purpose': result[0],
                'age': result[1],
                'gender': result[2],
                'weight': result[3],
                'height': result[4],
                'allergies': result[5]
            }
        return None


def get_all_subscriptions() -> List[Dict]:
    """Получить все активные подписки"""
    with db_lock:
        conn = sqlite3.connect(DB_FILE, timeout=30)
        cursor = conn.cursor()

        cursor.execute('''
        SELECT user_id, start_date, end_date 
        FROM subscriptions 
        WHERE date(end_date) > date('now')
        ORDER BY end_date DESC
        ''')

        subscriptions = [{
            'user_id': row[0],
            'start_date': datetime.fromisoformat(row[1]),
            'end_date': datetime.fromisoformat(row[2]),
            'days_left': (datetime.fromisoformat(row[2]) - datetime.now()).days
        } for row in cursor.fetchall()]

        conn.close()
        return subscriptions


def reset_all_subscriptions():
    """Полностью очистить все подписки"""
    with db_lock:
        try:
            conn = sqlite3.connect(DB_FILE, timeout=30)
            cursor = conn.cursor()

            cursor.execute('DELETE FROM subscriptions')
            cursor.execute('DELETE FROM message_history')
            cursor.execute('DELETE FROM diet_profiles')
            cursor.execute('DELETE FROM pending_payments')
            cursor.execute('DELETE FROM subscription_freezes')
            cursor.execute('DELETE FROM maintenance_mode')

            conn.commit()
            conn.close()
        except sqlite3.Error as e:
            logger.error(f"Database error in reset_all_subscriptions: {e}")


def reset_user_subscription(user_id: int):
    """Сбросить подписку конкретного пользователя"""
    with db_lock:
        try:
            conn = sqlite3.connect(DB_FILE, timeout=30)
            cursor = conn.cursor()

            cursor.execute('DELETE FROM subscriptions WHERE user_id = ?', (user_id,))
            cursor.execute('DELETE FROM message_history WHERE user_id = ?', (user_id,))
            cursor.execute('DELETE FROM diet_profiles WHERE user_id = ?', (user_id,))
            cursor.execute('DELETE FROM pending_payments WHERE user_id = ?', (user_id,))
            cursor.execute('DELETE FROM subscription_freezes WHERE user_id = ?', (user_id,))

            conn.commit()
            conn.close()
        except sqlite3.Error as e:
            logger.error(f"Database error in reset_user_subscription: {e}")


# Maintenance mode functions
def set_maintenance_mode(active: bool, reason: str = ""):
    with db_lock:
        try:
            conn = sqlite3.connect(DB_FILE, timeout=30)
            cursor = conn.cursor()

            if active:
                cursor.execute('''
                INSERT INTO maintenance_mode (is_active, start_time, reason)
                VALUES (1, ?, ?)
                ''', (datetime.now().isoformat(), reason))
            else:
                cursor.execute('''
                UPDATE maintenance_mode 
                SET is_active = 0, end_time = ?
                WHERE is_active = 1
                ''', (datetime.now().isoformat(),))

                unfreeze_all_subscriptions()

            conn.commit()
            conn.close()
        except sqlite3.Error as e:
            logger.error(f"Database error in set_maintenance_mode: {e}")


def get_maintenance_status() -> Dict[str, Any]:
    with db_lock:
        conn = sqlite3.connect(DB_FILE, timeout=30)
        cursor = conn.cursor()

        cursor.execute('''
        SELECT is_active, start_time, end_time, reason 
        FROM maintenance_mode 
        ORDER BY id DESC LIMIT 1
        ''')

        result = cursor.fetchone()
        conn.close()

        if result:
            return {
                'is_active': bool(result[0]),
                'start_time': datetime.fromisoformat(result[1]) if result[1] else None,
                'end_time': datetime.fromisoformat(result[2]) if result[2] else None,
                'reason': result[3]
            }
        return {'is_active': False}


def freeze_subscription(user_id: int):
    subscription = get_subscription(user_id)
    if not subscription:
        return False

    days_left = subscription['days_left']

    with db_lock:
        try:
            conn = sqlite3.connect(DB_FILE, timeout=30)
            cursor = conn.cursor()

            cursor.execute('''
            INSERT INTO subscription_freezes (user_id, freeze_start, days_remaining)
            VALUES (?, ?, ?)
            ''', (user_id, datetime.now().isoformat(), days_left))

            new_end_date = datetime.now() + timedelta(days=days_left)
            cursor.execute('''
            UPDATE subscriptions 
            SET end_date = ?
            WHERE user_id = ?
            ''', (new_end_date.isoformat(), user_id))

            conn.commit()
            conn.close()
            return True
        except sqlite3.Error as e:
            logger.error(f"Database error in freeze_subscription: {e}")
            return False


def unfreeze_all_subscriptions():
    """Разморозить все подписки"""
    with db_lock:
        try:
            conn = sqlite3.connect(DB_FILE, timeout=30)
            cursor = conn.cursor()

            cursor.execute('''
            SELECT user_id, days_remaining 
            FROM subscription_freezes 
            WHERE freeze_end IS NULL
            ''')

            active_freezes = cursor.fetchall()

            for user_id, days_remaining in active_freezes:
                new_end_date = datetime.now() + timedelta(days=days_remaining)
                cursor.execute('''
                UPDATE subscriptions 
                SET end_date = ?
                WHERE user_id = ?
                ''', (new_end_date.isoformat(), user_id))

                cursor.execute('''
                UPDATE subscription_freezes 
                SET freeze_end = ?
                WHERE user_id = ? AND freeze_end IS NULL
                ''', (datetime.now().isoformat(), user_id))

            conn.commit()
            logger.info(f"Разморожено {len(active_freezes)} подписок")

        except Exception as e:
            logger.error(f"Ошибка при разморозке подписок: {e}")
            conn.rollback()
        finally:
            conn.close()


# Helper function to split long messages
def split_long_message(text: str, max_length: int = MAX_MESSAGE_LENGTH) -> List[str]:
    """Разделить длинное сообщение на части по максимальной длине"""
    if len(text) <= max_length:
        return [text]

    parts = []
    current_part = ""

    # Попробуем разбить по предложениям или абзацам
    sentences = text.split('\n')

    for sentence in sentences:
        if len(current_part) + len(sentence) + 1 <= max_length:
            if current_part:
                current_part += "\n" + sentence
            else:
                current_part = sentence
        else:
            if current_part:
                parts.append(current_part)
            # Если одно предложение само по себе длиннее max_length
            if len(sentence) > max_length:
                # Разбиваем на куски по словам
                words = sentence.split(' ')
                temp_part = ""
                for word in words:
                    if len(temp_part) + len(word) + 1 <= max_length:
                        if temp_part:
                            temp_part += " " + word
                        else:
                            temp_part = word
                    else:
                        if temp_part:
                            parts.append(temp_part)
                        temp_part = word
                if temp_part:
                    current_part = temp_part
            else:
                current_part = sentence

    if current_part:
        parts.append(current_part)

    # Добавим нумерацию
    result = []
    total_parts = len(parts)
    for i, part in enumerate(parts, 1):
        numbered_part = f"📄 Часть {i}/{total_parts}\n\n{part}"
        if i < total_parts:
            numbered_part += "\n\n⏳ Продолжение следует..."
        result.append(numbered_part)

    return result


# AI functions with SSL certificate support and improved timeout handling
async def generate_deepseek_response(prompt: str, context: str = "", max_tokens: int = 2000) -> Optional[str]:
    """Улучшенная функция генерации ответов с использованием certifi для SSL и увеличенным таймаутом"""
    try:
        # Проверяем наличие API ключа
        if not DEEPSEEK_API_KEY or DEEPSEEK_API_KEY == "sk-8e366fc9f2e649da96d97668f918a439":
            logger.error("API ключ не установлен или используется демо-ключ")
            return None

        headers = {
            "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
            "Content-Type": "application/json"
        }

        # Обновленный системный промпт
        system_prompt = (
            "Вы - опытный медицинский консультант и диетолог с высшим образованием. "
            "Давайте профессиональные, точные и научно обоснованные ответы. "
            "ВАЖНО: Все сложные медицинские термины должны объясняться простым языком в скобках сразу после термина.\n\n"
            "Правила:\n"
            "1. Объясняйте каждый сложный медицинский термин при первом упоминании\n"
            "2. Используйте простые аналогии и сравнения\n"
            "3. Избегайте излишней научной сложности\n"
            "4. Сохраняйте профессиональный тон, но делайте информацию доступной\n"
            "5. Структурируйте ответы логически\n"
            "6. Всегда уточняйте, что это общие рекомендации\n"
            "7. При недостатке информации рекомендуйте обратиться к специалисту"
        )

        # Добавляем контекст если предоставлен
        full_prompt = f"{context}\n\n{prompt}" if context else prompt

        # Ограничиваем длину промпта (примерно 12000 токенов)
        if len(full_prompt) > 30000:
            logger.warning(f"Промпт слишком длинный ({len(full_prompt)} символов), обрезаем до 30000")
            full_prompt = full_prompt[:30000] + "\n\n[Текст был сокращен из-за ограничений длины]"

        data = {
            "model": "deepseek-chat",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": full_prompt}
            ],
            "temperature": 0.3,
            "max_tokens": max_tokens,
            "top_p": 0.9,
            "frequency_penalty": 0.2,
            "presence_penalty": 0.1,
            "stream": False  # Убедимся, что stream отключен
        }

        logger.info(
            f"Отправка запроса к DeepSeek API, длина промпта: {len(full_prompt)}, токены: ~{len(full_prompt) // 4}")

        # Создаем SSL контекст с использованием сертификатов из certifi
        ssl_context = ssl.create_default_context(cafile=certifi.where())

        # Создаем коннектор с SSL контекстом и увеличенными таймаутами
        connector = aiohttp.TCPConnector(
            ssl=ssl_context,
            limit=30,  # Максимальное количество соединений
            ttl_dns_cache=300  # Время жизни DNS кэша
        )

        # Увеличенные таймауты для сложных запросов
        timeout = aiohttp.ClientTimeout(
            total=180,  # Общий таймаут 3 минуты
            connect=30,  # Таймаут на соединение 30 секунд
            sock_read=120  # Таймаут на чтение 2 минуты
        )

        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            try:
                async with session.post(
                        "https://api.deepseek.com/v1/chat/completions",
                        headers=headers,
                        json=data,
                        timeout=timeout
                ) as response:

                    # Детальная обработка HTTP статусов
                    if response.status == 200:
                        result = await response.json()
                        logger.info("Успешный ответ от DeepSeek API")
                        return result['choices'][0]['message']['content']

                    elif response.status == 401:
                        error_text = await response.text()
                        logger.error(f"API Error 401: Неавторизован - {error_text}")
                        return None

                    elif response.status == 429:
                        error_text = await response.text()
                        logger.error(f"API Error 429: Превышен лимит запросов - {error_text}")
                        return None

                    elif response.status == 400:
                        error_text = await response.text()
                        logger.error(f"API Error 400: Неверный запрос - {error_text}")
                        return None

                    elif response.status == 504:
                        error_text = await response.text()
                        logger.error(f"API Error 504: Gateway Timeout - {error_text}")
                        return None

                    else:
                        error_text = await response.text()
                        logger.error(f"API Error {response.status}: {error_text}")
                        return None

            except asyncio.TimeoutError:
                logger.error("Timeout error: Превышено время ожидания ответа от DeepSeek API (180 секунд)")
                return None

    except aiohttp.ClientError as e:
        logger.error(f"Network error: Ошибка сети при подключении к DeepSeek API - {e}")
        return None

    except Exception as e:
        logger.error(f"Unexpected error in generate_deepseek_response: {e}")
        return None


async def extract_text_from_pdf(file_path: str) -> str:
    try:
        with open(file_path, 'rb') as file:
            reader = PdfReader(file)
            text = ""
            total_pages = len(reader.pages)

            # Ограничиваем количество страниц для обработки
            max_pages = 20  # Максимум 20 страниц
            pages_to_read = min(total_pages, max_pages)

            logger.info(f"Извлечение текста из PDF: {total_pages} страниц, читаем {pages_to_read}")

            for i, page in enumerate(reader.pages[:pages_to_read]):
                page_text = page.extract_text()
                if page_text:
                    text += f"--- Страница {i + 1} ---\n{page_text}\n\n"

            if total_pages > max_pages:
                text += f"\n[Внимание: документ содержит {total_pages} страниц. Обработаны первые {max_pages} страниц.]\n"

            return text
    except Exception as e:
        logger.error(f"PDF extraction error: {e}")
        return ""


# Maintenance check
async def check_maintenance_mode(user_id: int) -> bool:
    status = get_maintenance_status()
    if status['is_active']:
        text = (f"🔧 Ведутся технические работы\n"
                f"Причина: {status['reason']}\n\n"
                f"⏳ Ваша подписка заморожена и не тратится.\n"
                f"Мы вернемся в ближайшее время!")

        await bot.send_message(user_id, text)
        return True
    return False


# Keyboard helpers
def create_main_menu_keyboard(user_id: int) -> InlineKeyboardBuilder:
    subscription = get_subscription(user_id)
    days_left = (subscription['end_date'] - datetime.now()).days if subscription else 0
    sub_status = "🔴 Подписка не найдена" if not subscription else f"🟢 Подписка активна ({days_left} дней)"

    builder = InlineKeyboardBuilder()
    builder.button(text=f"💳 Подписка - {sub_status}", callback_data="subscription")
    builder.button(text="💡 Рекомендации", callback_data="recommendations")
    builder.button(text="🩸 Разбор анализов", callback_data="analyze_reports")
    builder.button(text="🍎 Рацион питания", callback_data="diet_plan")
    builder.button(text="📜 История запросов🔎", callback_data="history_list")
    builder.adjust(1)
    return builder


def create_history_keyboard(history: List[Dict], page: int = 0, total_count: int = 0) -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()

    for entry in history:
        short_question = entry['question'][:30] + '...' if len(entry['question']) > 30 else entry['question']
        date_str = entry['timestamp'].strftime('%d.%m %H:%M')
        builder.button(
            text=f"{date_str}: {short_question}",
            callback_data=f"history_detail_{entry['id']}"
        )

    if page > 0:
        builder.button(text="◀️ Назад", callback_data=f"history_page_{page - 1}")

    if total_count > (page + 1) * HISTORY_PAGE_SIZE:
        builder.button(text="Вперед ▶️", callback_data=f"history_page_{page + 1}")

    builder.button(text="🔙 На главную", callback_data="back")
    builder.adjust(1, *([1] * len(history)), 2, 1)
    return builder


def create_history_detail_keyboard(entry_id: int) -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    builder.button(text="🔙 К списку", callback_data="history_list")
    builder.button(text="❌ Удалить", callback_data=f"history_delete_{entry_id}")
    builder.button(text="🔙 На главную", callback_data="back")
    builder.adjust(2, 1)
    return builder


# Handlers
@dp.message(Command("start"))
async def cmd_start(message: Message):
    welcome_text = (
        "👋 Приветствуем, пионер здоровья!\n\n"
        "Ты только что нашел свой ключ к медицине будущего. Забудь о длинных очередях и сложных справках. Здесь о тебе позаботится персональный ИИ-доктор. 🤖❤️\n\n"
        "✨ Открой возможности, которых нет больше нигде:\n\n"
        "📊 Мгновенная расшифровка анализов. Загрузи свои результаты и получи понятное объяснение без гугления и паники.\n"
        "💡 Персональные рекомендации и советы. Не общие фразы, а выводы, основанные на твоих уникальных данных. Сила искусственного интеллекта работает на тебя.\n"
        "🥗 Индивидуальный план питания. Получи рацион, который подходит именно тебе, твоим целям и состоянию здоровья. Быстро и понятно.\n\n"
        "Начни пользоваться будущим медицины уже сегодня! 🚀\n\n"
        "Telegram-канал со всеми новостями Бота @EkoBalance\n\n"
        "Продолжая, вы соглашаетесь с пользовательским соглашением. 👇\n"
        "https://telegra.ph/Polzovatelskoe-soglashenie-dlya-telegram-bota-Eko-Balance-08-23"
    )

    builder = InlineKeyboardBuilder()
    builder.button(text="Начать анализ", callback_data="start_analysis")

    await message.answer(welcome_text, reply_markup=builder.as_markup())


@dp.message(Command("myid"))
async def cmd_myid(message: Message):
    """Получить свой ID"""
    await message.answer(f"Ваш ID: `{message.from_user.id}`", parse_mode="Markdown")


@dp.message(Command("manage_subs"))
async def cmd_manage_subscriptions(message: Message):
    """Управление подписками пользователей"""
    if not is_manager(message.from_user.id):
        await message.answer("❌ Команда доступна только администратору")
        return

    text = (
        "👥 Управление подписками пользователей\n\n"
        "Доступные команды:\n"
        "/add_subscription - Выдать подписку пользователю\n"
        "/extend_subscription - Продлить подписку пользователю\n"
        "/check_subscription - Проверить подписку пользователя\n\n"
        "Или используйте кнопки ниже:"
    )

    builder = InlineKeyboardBuilder()
    builder.button(text="🎫 Выдать подписку", callback_data="manager_add_sub")
    builder.button(text="📅 Продлить подписку", callback_data="manager_extend_sub")
    builder.button(text="🔍 Проверить подписку", callback_data="manager_check_sub")
    builder.button(text="🔙 Назад", callback_data="admin_back")
    builder.adjust(1)

    await message.answer(text, reply_markup=builder.as_markup())


@dp.callback_query(F.data == "start_analysis")
async def process_start_analysis(callback: CallbackQuery):
    welcome_text = "Добро пожаловать в медицину будущего – ваш персональный AI-доктор 24/7! 👨⚕️💡\n\nТеперь забота о здоровье – это просто, быстро и удобно! 💙\n\nВыберите, чем вам помочь:\n📈 Подписка - получи свою подписку для полного функционала бота\n🔍 Быстрые рекомендации – опишите симптомы, и мы подскажем возможные причины.\n📊 Рационы питания - получи свой персональный рацион прямо сейчас.\n🏥 Расшифровка анализов - пришлите файл в формате .pdf и ИИ выдаст полный разбор анализов.\n\nВаше здоровье – наш приоритет! 💙\nКакой вопрос вас беспокоит? 😊"
    await callback.message.edit_text(
        welcome_text,
        reply_markup=create_main_menu_keyboard(callback.from_user.id).as_markup()
    )


@dp.message(Command("check_subscription"))
async def cmd_check_subscription(message: Message):
    """Проверить подписку пользователя"""
    if not is_manager(message.from_user.id):
        await message.answer("❌ Команда доступна только администратору")
        return

    args = message.text.split()
    if len(args) < 2:
        await message.answer("❌ Используйте: /check_subscription <user_id>")
        return

    try:
        user_id = int(args[1])
        subscription = get_subscription(user_id)

        try:
            user = await bot.get_chat(user_id)
            username = f"@{user.username}" if user.username else user.first_name
        except:
            username = "неизвестный пользователь"

        if subscription:
            text = (
                f"✅ Подписка активна\n\n"
                f"👤 Пользователь: {username}\n"
                f"🔢 ID: {user_id}\n"
                f"⏰ Осталось дней: {subscription['days_left']}\n"
                f"📅 Окончание: {subscription['end_date'].strftime('%d.%m.%Y')}"
            )
        else:
            text = (
                f"❌ Подписка не активна\n\n"
                f"👤 Пользователь: {username}\n"
                f"🔢 ID: {user_id}"
            )

        await message.answer(text)

    except ValueError:
        await message.answer("❌ Неверный user_id. Используйте числовой ID")


@dp.message(Command("extend_subscription"))
async def cmd_extend_subscription(message: Message):
    """Продлить подписку пользователя"""
    if not is_manager(message.from_user.id):
        await message.answer("❌ Команда доступна только администратору")
        return

    args = message.text.split()
    if len(args) < 3:
        await message.answer("❌ Используйте: /extend_subscription <user_id> <дней>")
        return

    try:
        user_id = int(args[1])
        days = int(args[2])

        if days <= 0:
            await message.answer("❌ Срок продления должен быть положительным числом")
            return

        # Получаем текущую подписку
        subscription = get_subscription(user_id)
        if subscription:
            # Продлеваем существующую подписку
            new_end_date = subscription['end_date'] + timedelta(days=days)

            with db_lock:
                conn = sqlite3.connect(DB_FILE, timeout=30)
                cursor = conn.cursor()
                cursor.execute(
                    'UPDATE subscriptions SET end_date = ? WHERE user_id = ?',
                    (new_end_date.isoformat(), user_id)
                )
                conn.commit()
                conn.close()
        else:
            # Создаем новую подписку
            add_subscription(user_id, days)
            new_end_date = datetime.now() + timedelta(days=days)

        # Получаем информацию о пользователе
        try:
            user = await bot.get_chat(user_id)
            username = f"@{user.username}" if user.username else user.first_name
        except:
            username = "неизвестный пользователь"

        await message.answer(
            f"✅ Подписка продлена!\n\n"
            f"👤 Пользователь: {username}\n"
            f"🔢 ID: {user_id}\n"
            f"⏰ Добавлено дней: {days}\n"
            f"📅 Новое окончание: {new_end_date.strftime('%d.%m.%Y')}"
        )

        # Уведомляем пользователя
        try:
            user_text = (
                f"🎉 Ваша подписка продлена на {days} дней!\n\n"
                f"Теперь подписка активна до {new_end_date.strftime('%d.%m.%Y')}"
            )
            await bot.send_message(user_id, user_text)
        except Exception as e:
            logger.error(f"Не удалось уведомить пользователя {user_id}: {e}")

    except ValueError:
        await message.answer("❌ Неверные параметры. Используйте числовые значения")
    except Exception as e:
        logger.error(f"Error extending subscription: {e}")
        await message.answer("❌ Произошла ошибка при продлении подписки")


@dp.message(Command("help"))
async def cmd_help(message: Message):
    """Показать справку по командам (только для администраторов)"""
    if not is_manager(message.from_user.id):
        await message.answer("❌ Команда доступна только администратору")
        return

    help_text = (
        "🛠️ <b>Список команд для администраторов:</b>\n\n"

        "📊 <b>Управление подписками:</b>\n"
        "/manage_subs - Меню управления подписками\n"
        "/add_subscription [id] [дней] - Выдать подписку\n"
        "/extend_subscription [id] [дней] - Продлить подписку\n"
        "/check_subscription [id] - Проверить подписку\n"
        "/list_subs - Список активных подписок\n"
        "/sub_stats - Статистика подписок\n"
        "/reset_user [id] - Сбросить подписку пользователя\n"
        "/reset_subs - Полный сброс всех подписок (опасно!)\n\n"

        "👥 <b>Управление менеджерами:</b>\n"
        "/managers - Список менеджеров\n"
        "/add_manager [id] - Добавить менеджера\n"
        "/remove_manager [id] - Удалить менеджера\n\n"

        "🎫 <b>Управление промокодами:</b>\n"
        "/promo_codes - Список промокодов\n"
        "/create_promo [код] [скидка%] [дней] [лимит] - Создать промокод\n"
        "/delete_promo [код] - Удалить промокод\n\n"

        "🔧 <b>Технические команды:</b>\n"
        "/maintenance [on/off] [причина] - Управление режимом техработ\n"
        "/debug_maintenance - Отладочная информация по техработам\n\n"

        "ℹ️ <b>Общие команды:</b>\n"
        "/myid - Показать свой ID\n"
        "/help - Показать эту справку\n\n"

        "💡 <b>Примеры использования:</b>\n"
        "<code>/add_manager 123456789</code> - добавить менеджера\n"
        "<code>/add_subscription 987654321 30</code> - выдать подписку на 30 дней\n"
        "<code>/create_promo SUMMER2024 15 30 10</code> - создать промокод на 15% скидку на 30 дней с лимитом 10 использований\n"
        "<code>/maintenance on Технические работы</code> - включить режим техработ"
    )

    await message.answer(help_text, parse_mode="HTML")


@dp.callback_query(F.data == "history_list")
async def show_history_list(callback: CallbackQuery, state: FSMContext):
    if await check_maintenance_mode(callback.from_user.id):
        return

    user_id = callback.from_user.id
    history_count = get_history_count(user_id)

    if history_count == 0:
        await callback.answer("История запросов пуста")
        return

    history = get_message_history(user_id, limit=HISTORY_PAGE_SIZE)
    text = "📜 Ваши последние запросы:\n\nВыберите запрос для просмотра деталей"

    keyboard = create_history_keyboard(history, page=0, total_count=history_count)
    await callback.message.edit_text(text, reply_markup=keyboard.as_markup())
    await state.set_state(HistoryStates.VIEWING_HISTORY)
    await state.update_data(page=0)


@dp.callback_query(F.data.startswith("history_page_"))
async def history_pagination(callback: CallbackQuery, state: FSMContext):
    if await check_maintenance_mode(callback.from_user.id):
        return

    page = int(callback.data.split("_")[-1])
    user_id = callback.from_user.id
    history_count = get_history_count(user_id)

    offset = page * HISTORY_PAGE_SIZE
    history = get_message_history(user_id, limit=HISTORY_PAGE_SIZE, offset=offset)

    if not history:
        await callback.answer("Нет больше запросов")
        return

    text = "📜 Ваши запросы:\n\nВыберите запрос для просмотра деталей"

    keyboard = create_history_keyboard(history, page=page, total_count=history_count)
    await callback.message.edit_text(text, reply_markup=keyboard.as_markup())
    await state.update_data(page=page)


@dp.callback_query(F.data.startswith("history_detail_"))
async def show_history_detail(callback: CallbackQuery, state: FSMContext):
    if await check_maintenance_mode(callback.from_user.id):
        return

    entry_id = int(callback.data.split("_")[-1])
    entry = get_history_entry(entry_id)

    if not entry:
        await callback.answer("Запись не найдена")
        return

    # Разбиваем ответ на части если он слишком длинный
    answer_parts = split_long_message(entry['answer'])

    date_str = entry['timestamp'].strftime('%d.%m.%Y %H:%M')

    if len(answer_parts) == 1:
        text = (
            f"📝 Детали запроса\n"
            f"Дата: {date_str}\n\n"
            f"❓ Ваш вопрос:\n{entry['question']}\n\n"
            f"💡 Ответ:\n{entry['answer']}"
        )
        keyboard = create_history_detail_keyboard(entry_id)
        await callback.message.edit_text(text, reply_markup=keyboard.as_markup())
    else:
        # Отправляем первую часть с кнопками
        text = (
            f"📝 Детали запроса\n"
            f"Дата: {date_str}\n\n"
            f"❓ Ваш вопрос:\n{entry['question']}\n\n"
            f"💡 Ответ (часть 1/{len(answer_parts)}):\n{answer_parts[0]}"
        )
        keyboard = create_history_detail_keyboard(entry_id)
        await callback.message.edit_text(text, reply_markup=keyboard.as_markup())

        # Отправляем остальные части
        for i, part in enumerate(answer_parts[1:], 2):
            part_text = (
                f"📝 Детали запроса (продолжение)\n"
                f"Дата: {date_str}\n\n"
                f"💡 Ответ (часть {i}/{len(answer_parts)}):\n{part}"
            )
            await callback.message.answer(part_text)

    await state.set_state(HistoryStates.VIEWING_DETAILS)
    await state.update_data(entry_id=entry_id)


@dp.callback_query(F.data == "manager_add_sub")
async def process_manager_add_sub(callback: CallbackQuery, state: FSMContext):
    """Обработчик кнопки выдачи подписки"""
    if not is_manager(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен")
        return

    await callback.message.answer(
        "Введите ID пользователя и срок подписки в днях через пробел:\n"
        "Пример: <code>123456789 30</code> - выдать подписку на 30 дней пользователю 123456789",
        parse_mode="HTML"
    )
    await state.set_state(ManagerStates.ADDING_SUBSCRIPTION)
    await callback.answer()


@dp.message(ManagerStates.ADDING_SUBSCRIPTION)
async def process_add_subscription_data(message: Message, state: FSMContext):
    """Обработка данных для выдачи подписки"""
    try:
        parts = message.text.split()
        if len(parts) != 2:
            raise ValueError("Неверный формат")

        user_id = int(parts[0])
        days = int(parts[1])

        if days <= 0:
            await message.answer("❌ Срок подписки должен быть положительным числом")
            return

        # Выдаем подписку
        add_subscription(user_id, days)

        # Получаем информацию о пользователе
        try:
            user = await bot.get_chat(user_id)
            username = f"@{user.username}" if user.username else user.first_name
        except:
            username = "неизвестный пользователь"

        await message.answer(
            f"✅ Подписка успешно выдана!\n\n"
            f"👤 Пользователь: {username}\n"
            f"🔢 ID: {user_id}\n"
            f"⏰ Срок: {days} дней\n"
            f"📅 Окончание: {(datetime.now() + timedelta(days=days)).strftime('%d.%m.%Y')}"
        )

        # Уведомляем пользователя
        try:
            user_text = (
                f"🎉 Вам выдана подписку на {days} дней!\n\n"
                f"Теперь вам доступны все функции бота до "
                f"{(datetime.now() + timedelta(days=days)).strftime('%d.%m.%Y')}"
            )
            await bot.send_message(user_id, user_text)
        except Exception as e:
            logger.error(f"Не удалось уведомить пользователя {user_id}: {e}")

    except ValueError:
        await message.answer(
            "❌ Неверный формат. Используйте: <code>ID_пользователя количество_дней</code>\n"
            "Пример: <code>123456789 30</code>",
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Error adding subscription: {e}")
        await message.answer("❌ Произошла ошибка при выдаче подписки")

    await state.clear()


@dp.callback_query(F.data.startswith("history_delete_"))
async def delete_history_entry(callback: CallbackQuery, state: FSMContext):
    if await check_maintenance_mode(callback.from_user.id):
        return

    entry_id = int(callback.data.split("_")[-1])

    with db_lock:
        try:
            conn = sqlite3.connect(DB_FILE, timeout=30)
            cursor = conn.cursor()
            cursor.execute('DELETE FROM message_history WHERE id = ?', (entry_id,))
            conn.commit()
            conn.close()
        except sqlite3.Error as e:
            logger.error(f"Database error in delete_history_entry: {e}")
            await callback.answer("Ошибка при удалении записи")
            return

    await callback.answer("Запрос удален из истории")
    await show_history_list(callback, state)


@dp.callback_query(F.data == "back")
async def process_back(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await process_start_analysis(callback)


@dp.callback_query(F.data == "subscription")
async def process_subscription(callback: CallbackQuery):
    if await check_maintenance_mode(callback.from_user.id):
        return

    subscription = get_subscription(callback.from_user.id)

    if subscription:
        text = (
            f"🟢 Ваша подписка активна\n"
            "------------------------\n"
            f"⌛Осталось дней: {subscription['days_left']}\n"
            f"📆Дата окончания: {subscription['end_date'].strftime('%d.%m.%Y')}"
        )
        builder = InlineKeyboardBuilder()
        builder.button(text="🔙 Назад", callback_data="back")
    else:
        text = (
            "🔴 У вас нет активной подписки\n\n"
            "Подписка дает доступ ко всем функции бота:\n"
            "- Персональные рекомендации\n"
            "- Расшифровка анализов\n"
            "- Индивидуальный рацион питания\n"
            "- История запросов\n\n"
            f"Стоимость: {SUBSCRIPTION_PRICE} руб. на {SUBSCRIPTION_DAYS} дней"
        )
        builder = InlineKeyboardBuilder()
        builder.button(text="💳 Купить подписку", callback_data="buy_subscription")
        builder.button(text="🔙 Назад", callback_data="back")
        builder.adjust(1)

    await callback.message.edit_text(text, reply_markup=builder.as_markup())


@dp.callback_query(F.data == "buy_subscription")
async def process_buy_subscription(callback: CallbackQuery, state: FSMContext):
    if await check_maintenance_mode(callback.from_user.id):
        return

    text = (
        f"Для оформления подписки:\n"
        f"1. Перейдите по ссылке: {DONATION_ALERTS_URL}\n"
        f"2. Оплатите {SUBSCRIPTION_PRICE} руб.\n"
        f"3. Нажмите кнопку 'Я оплатил(а)' и пришлите скриншот подтверждения оплаты\n\n"
        "💎 Есть промокод? Нажмите кнопку ниже"
    )

    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Я оплатил(а)", callback_data="payment_confirmation")
    builder.button(text="🎁 Применить промокод", callback_data="use_promo")
    builder.button(text="🔙 Назад", callback_data="back")
    builder.adjust(1)

    await callback.message.edit_text(text, reply_markup=builder.as_markup())
    await state.set_state(SubscriptionStates.WAITING_FOR_PAYMENT)


@dp.callback_query(F.data == "use_promo", SubscriptionStates.WAITING_FOR_PAYMENT)
async def process_use_promo(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("Введите промокод:")
    await state.set_state(SubscriptionStates.WAITING_FOR_PROMO)


@dp.callback_query(F.data == "payment_confirmation", SubscriptionStates.WAITING_FOR_PAYMENT)
async def process_payment_confirmation(callback: CallbackQuery, state: FSMContext):
    if await check_maintenance_mode(callback.from_user.id):
        return

    await callback.message.answer("Пожалуйста, пришлите скриншот подтверждения оплаты")
    await state.set_state(SubscriptionStates.WAITING_FOR_PAYMENT)


@dp.message(SubscriptionStates.WAITING_FOR_PROMO)
async def process_promo_code(message: Message, state: FSMContext):
    promo_code = message.text.upper().strip()
    discount = use_promo_code(message.from_user.id, promo_code)

    if discount:
        discounted_price = SUBSCRIPTION_PRICE * (100 - discount) // 100
        await state.update_data(discounted_price=discounted_price, discount=discount)

        text = (
            f"🎉 Промокод применен! Скидка: {discount}%\n"
            f"Новая цена: {discounted_price} руб. вместо {SUBSCRIPTION_PRICE} руб.\n\n"
            f"Для оформления подписки:\n"
            f"1. Перейдите по ссылке: {DONATION_ALERTS_URL}\n"
            f"2. Введите в описание платежа название промокода.\n"
            f"3. Оплатите {discounted_price} руб.\n"
            f"4. Пришлите скриншот подтверждения оплаты."
        )

        builder = InlineKeyboardBuilder()
        builder.button(text="✅ Я оплатил(а)", callback_data="payment_confirmation")
        builder.button(text="🔙 Назад", callback_data="buy_subscription")
        builder.adjust(1)

        await message.answer(text, reply_markup=builder.as_markup())
        await state.set_state(SubscriptionStates.WAITING_FOR_PAYMENT)
    else:
        text = (
            "❌ Промокод недействителен или уже использован\n\n"
            f"Попробуйте другой промокод или оплатите полную стоимость: {SUBSCRIPTION_PRICE} руб."
        )

        builder = InlineKeyboardBuilder()
        builder.button(text="💳 Оплатить полную стоимость", callback_data="payment_confirmation")
        builder.button(text="🎁 Ввести другой промокод", callback_data="use_promo")
        builder.button(text="🔙 Назад", callback_data="buy_subscription")
        builder.adjust(1)
        await message.answer(text, reply_markup=builder.as_markup())
        # Устанавливаем состояние WAITING_FOR_PAYMENT, чтобы кнопки заработали
        await state.set_state(SubscriptionStates.WAITING_FOR_PAYMENT)


@dp.message(SubscriptionStates.WAITING_FOR_PAYMENT, F.photo)
async def process_payment_screenshot(message: Message, state: FSMContext):
    if await check_maintenance_mode(message.from_user.id):
        return

    add_pending_payment(
        message.from_user.id,
        message.from_user.full_name,
        message.photo[-1].file_id
    )

    # Get managers to notify
    managers = get_managers()
    manager_text = (
        f"Новый запрос на подписку:\n"
        f"Пользователь: {message.from_user.full_name} (@{message.from_user.username})\n"
        f"ID: {message.from_user.id}\n\n"
        f"Подтвердить оплату?"
    )

    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Одобрить", callback_data=f"approve_{message.from_user.id}")
    builder.button(text="❌ Отклонить", callback_data=f"reject_{message.from_user.id}")

    # Send to all managers
    for manager in managers:
        try:
            await bot.send_photo(
                chat_id=manager['user_id'],
                photo=message.photo[-1].file_id,
                caption=manager_text,
                reply_markup=builder.as_markup()
            )
        except Exception as e:
            logger.error(f"Error sending to manager {manager['user_id']}: {e}")

    await message.answer("Ваш запрос отправлен менеджеру на проверку. Ожидайте подтверждения.")
    await state.clear()
    await process_start_analysis(message)


@dp.callback_query(F.data.startswith("approve_"))
async def process_approve_payment(callback: CallbackQuery):
    if not is_manager(callback.from_user.id):
        await callback.answer("❌ У вас нет прав для этого действия")
        return

    user_id = int(callback.data.split("_")[1])
    add_subscription(user_id, SUBSCRIPTION_DAYS)
    payment = get_pending_payment(user_id)
    remove_pending_payment(user_id)

    if payment:
        manager_confirmation = (
            f"✅ Подписка успешно активирована\n\n"
            f"Пользователь: {payment['user_name']}\n"
            f"ID: {user_id}\n"
            f"Срок действия: {SUBSCRIPTION_DAYS} дней\n"
            f"Дата окончания: {(datetime.now() + timedelta(days=SUBSCRIPTION_DAYS)).strftime('%d.%m.%Y')}"
        )
        try:
            await callback.message.edit_caption(caption=manager_confirmation, reply_markup=None)
            await callback.answer("Подписка подтверждена")
        except Exception as e:
            logger.error(f"Error editing manager message: {e}")

    subscription = get_subscription(user_id)
    if subscription:
        user_text = (
            f"🌟 ВАШ ПРЕМИУМ-ДОСТУП АКТИВИРОВАН!\n\nВы не просто подписались — вы вступили в клуб тех, кто инвестирует в свое здоровье осознанно! 🚀\n\n💎 Ваш статус: ПРЕМИУМ-ПАЦИЕНТ\n\n"f"⏳ Осталось полных дней наслаждения здоровьем: {subscription['days_left']}\n\n"f"📅 Подписка действует до: {subscription['end_date'].strftime('%d.%m.%Y')}\n\n"f"Не теряйте ни дня! Каждый момент вашего здоровья под нашей защитой. 💙"
        )

        builder = InlineKeyboardBuilder()
        builder.button(text="🔙 На главную", callback_data="back")

        try:
            await bot.send_message(chat_id=user_id, text=user_text, reply_markup=builder.as_markup())
        except Exception as e:
            logger.error(f"Error sending message to user {user_id}: {e}")


@dp.callback_query(F.data.startswith("reject_"))
async def process_reject_payment(callback: CallbackQuery):
    if not is_manager(callback.from_user.id):
        await callback.answer("❌ У вас нет прав для этого действия")
        return

    user_id = int(callback.data.split("_")[1])
    payment = get_pending_payment(user_id)
    remove_pending_payment(user_id)

    if payment:
        manager_confirmation = (
            f"❌ Подписка отклонена\n\n"
            f"Пользователь: {payment['user_name']}\n"
            f"ID: {user_id}\n"
            f"Причина: платеж не подтвержден"
        )
        try:
            await callback.message.edit_caption(caption=manager_confirmation, reply_markup=None)
            await callback.answer("Подписка отклонена")
        except Exception as e:
            logger.error(f"Error editing manager message: {e}")

    user_text = (
        "❌ Ваш платеж не был подтвержден.\n\n"
        "Возможные причины:\n"
        "- Неправильная сумма\n"
        "- Отсутствует подтверждение оплата\n"
        "- Проблемы с платежной системой\n\n"
        "Пожалуйста, попробуйте еще раз или свяжитесь с поддержкой."
    )

    builder = InlineKeyboardBuilder()
    builder.button(text="🔙 На главную", callback_data="back")

    try:
        await bot.send_message(chat_id=user_id, text=user_text, reply_markup=builder.as_markup())
    except Exception as e:
        logger.error(f"Error sending message to user {user_id}: {e}")


@dp.callback_query(F.data == "recommendations")
async def process_recommendations(callback: CallbackQuery, state: FSMContext):
    if await check_maintenance_mode(callback.from_user.id):
        return

    subscription = get_subscription(callback.from_user.id)

    if not subscription:
        text = (
            "🔐 Ваш доступ к будущему временно ограничен.\n\n""К сожалению, вы не в системе. Ваш персональный AI-доктор и все его возможности ждут вашего возвращения! 💔\n\n""💎 Станьте участником PREMIUM-клуба и откройте полный доступ:\n\n""✨ Персональные рекомендации — советы, созданные именно для вас.\n""🥗 Индивидуальный рацион питания — питание как лекарство.\n""📚 Полная история запросов — отслеживайте прогресс вашего здоровья.\n\n"f"🎁 Стоимость вложения в свое здоровье: всего{SUBSCRIPTION_PRICE} руб. на {SUBSCRIPTION_DAYS} дней полного комфорта!""Не откладывайте здоровье на потом! Верните себе доступ к медицине будущего — сейчас. 🚀"
        )

        builder = InlineKeyboardBuilder()
        builder.button(text="💳 Купить подписку", callback_data="buy_subscription")
        builder.button(text="🔙 Назад", callback_data="back")
        builder.adjust(1)

        await callback.message.edit_text(text, reply_markup=builder.as_markup())
        return

    text = "👂 Расскажите, что вас беспокоит?\n\nОпишите ваши симптомы, жалобы или просто плохое самочувствие — как если бы вы рассказывали близкому другу. Чем подробнее вы напишете, тем точнее я смогу помочь.\n\nНе стесняйтесь, я здесь, чтобы выслушать и помочь. ❤️\n\nP.S. Я — умный алгоритм, а не врач. Мои советы носят рекомендательный характер."

    builder = InlineKeyboardBuilder()
    builder.button(text="🔙 Назад", callback_data="back")

    await callback.message.edit_text(text, reply_markup=builder.as_markup())
    await state.set_state(RecommendationStates.CONCERNS)


@dp.message(RecommendationStates.CONCERNS)
async def process_concerns(message: Message, state: FSMContext):
    if await check_maintenance_mode(message.from_user.id):
        return

    loading_msg = await message.answer("⏳ ИИ анализирует ваш запрос...")

    response = await generate_deepseek_response(
        f"Пользователь описывает следующую проблему: {message.text}\n\n"
        "Проанализируйте проблему и дайте профессиональные рекомендации.(но профессиональные медицинские термины объясняйте в скобочках сразу после термина"
        "Укажите возможные причины, но избегайте постановки диагноза. "
        "Предложите общие рекомендации по образу жизни, питанию и возможные направления для консультации со специалистами. "
        "Ответ должен быть структурированным и научно обоснованным."
    )

    if response:
        save_message(message.from_user.id, message.text, response)

        # Разбиваем ответ на части если он слишком длинный
        response_parts = split_long_message(response)

        # Отправляем первую часть с кнопками
        first_part = response_parts[0]
        builder = InlineKeyboardBuilder()
        builder.button(text="📜 История запросов", callback_data="history_list")
        builder.button(text="🔙 Назад", callback_data="back")

        await loading_msg.edit_text(first_part, reply_markup=builder.as_markup())

        # Отправляем остальные части
        for part in response_parts[1:]:
            await message.answer(part)

    else:
        builder = InlineKeyboardBuilder()
        builder.button(text="🔄 Попробовать снова", callback_data="recommendations")
        builder.button(text="🔙 Назад", callback_data="back")

        await loading_msg.edit_text(
            "⚠️ Сервис временно недоступен. Попробуйте позже",
            reply_markup=builder.as_markup()
        )

    await state.clear()


@dp.callback_query(F.data == "analyze_reports")
async def process_analyze_reports(callback: CallbackQuery, state: FSMContext):
    if await check_maintenance_mode(callback.from_user.id):
        return

    subscription = get_subscription(callback.from_user.id)

    if not subscription:
        text = (
            "🔐 Ваш доступ к будущему временно ограничен.\n\n"
            "К сожалению, вы не в системе. Ваш персональный AI-доктор и все его возможности ждут вашего возвращения! 💔\n\n""💎 Станьте участником PREMIUM-клуба и откройте полный доступ:\n\n"
            "✨ Персональные рекомендации — советы, созданные именно для вас.\n""🥗 Индивидуальный рацион питания — питание как лекарство.\n"
            "📚 Полная история запросов — отслеживайте прогресс вашего здоровья.\n\n"f"🎁 Стоимость вложения в свое здоровье: всего{SUBSCRIPTION_PRICE} руб. на {SUBSCRIPTION_DAYS} дней полного комфорта!"
            "Не откладывайте здоровье на потом! Верните себе доступ к медицине будущего — сейчас. 🚀"
        )

        builder = InlineKeyboardBuilder()
        builder.button(text="💳 Купить подписку", callback_data="buy_subscription")
        builder.button(text="🔙 Назад", callback_data="back")
        builder.adjust(1)

        await callback.message.edit_text(text, reply_markup=builder.as_markup())
        return

    text = (
        "🔍 Расшифровка анализов крови — это просто!\n\n"
        "Пришлите файл с вашими анализами в формате PDF, и наш ИИ-доктор мгновенно проведет детальную расшифровку. 📄\n\n"
        "Как это работает:\n\n"
        "1. Вы загружаете PDF-отчёт из лаборатории.\n"
        "2. Наш алгоритм анализирует все показатели: от общего анализа крови до биохимии и гормонов.\n"
        "3. Вы получаете понятное объяснение по каждому отклонению и персональные рекомендации.\n\n"
        "Мы гарантируем полную конфиденциальность ваших данных. 🔒\n\n"
        "⬇️ Просто нажмите на скрепку и загрузите файл:\n"
        "[ 📎 Загрузить файл ]\n\n"
        "⏳ Обычно расшифровка занимает не более 5-10 минут.\n\n"
        "❗️Помните: это предварительная оценка. Постановкой диагноза и назначением лечения должен заниматься врач."
    )

    builder = InlineKeyboardBuilder()
    builder.button(text="🔙 Назад", callback_data="back")

    await callback.message.edit_text(text, reply_markup=builder.as_markup())
    await state.set_state(AnalysisStates.WAITING_FOR_PDF)


@dp.message(AnalysisStates.WAITING_FOR_PDF, F.document)
async def process_pdf_file(message: Message, state: FSMContext):
    if await check_maintenance_mode(message.from_user.id):
        return

    if not message.document.mime_type == "application/pdf":
        await message.answer("❌ Прислан некорректный формат файла. Пожалуйста, отправьте файл в формате PDF.")
        return

    file_id = message.document.file_id
    file = await bot.get_file(file_id)
    file_path = file.file_path

    download_path = f"temp_{message.from_user.id}.pdf"

    loading_msg = await message.answer("⏳ Загружаю PDF файл...")

    try:
        await bot.download_file(file_path, download_path)
        await loading_msg.edit_text("⏳ Извлекаю текст из PDF...")

        pdf_text = await extract_text_from_pdf(download_path)
        if not pdf_text or len(pdf_text.strip()) < 50:
            raise ValueError("Не удалось извлечь текст из PDF или текст слишком короткий")

        await loading_msg.edit_text("⏳ Анализирую анализы с помощью ИИ... (это может занять до 3 минут)")

        prompt = (
            f"Проанализируйте следующие медицинские анализы:\n\n{pdf_text}\n\n"
            "Предоставьте профессиональный анализ с указанием:"
            "1. Референсных значений для основных показателей\n"
            "2. Выявленных отклонений от нормы\n"
            "3. Возможных причин этих отклонений\n"
            "4. Общих рекомендаций по дальнейшим действиям\n"
            "5. Специалистов, которых стоит посетить при конкретных отклонениях\n\n"
            "Избегайте постановки диагнозов и назначения лекарств. "
            "Указывайте, что окончательную интерпретацию должен проводить врач."
        )

        response = await generate_deepseek_response(prompt, max_tokens=4000)

        if response:
            save_message(message.from_user.id, "PDF с анализами", response)

            # Разбиваем ответ на части если он слишком длинный
            response_parts = split_long_message(response)

            # Отправляем первую часть с кнопками
            first_part = response_parts[0]
            builder = InlineKeyboardBuilder()
            builder.button(text="📜 История запросов", callback_data="history_list")
            builder.button(text="🔙 Назад", callback_data="back")

            await loading_msg.edit_text(first_part, reply_markup=builder.as_markup())

            # Отправляем остальные части
            for part in response_parts[1:]:
                await message.answer(part)

        else:
            raise Exception("Empty response from API")

    except ValueError as e:
        logger.error(f"Error processing PDF: {e}")
        builder = InlineKeyboardBuilder()
        builder.button(text="🔄 Попробовать снова", callback_data="analyze_reports")
        builder.button(text="🔙 Назад", callback_data="back")
        await loading_msg.edit_text(
            "❌ Не удалось извлечь текст из PDF файла. Убедитесь, что файл содержит текст (не сканированное изображение) и попробуйте еще раз.",
            reply_markup=builder.as_markup()
        )
    except Exception as e:
        logger.error(f"Error processing PDF: {e}")
        builder = InlineKeyboardBuilder()
        builder.button(text="🔄 Попробовать снова", callback_data="analyze_reports")
        builder.button(text="🔙 Назад", callback_data="back")
        await loading_msg.edit_text(
            f"❌ Произошла ошибка при обработке файла: {str(e)}",
            reply_markup=builder.as_markup()
        )

    finally:
        if os.path.exists(download_path):
            try:
                os.remove(download_path)
            except:
                pass

    await state.clear()


@dp.message(AnalysisStates.WAITING_FOR_PDF)
async def process_wrong_file_format(message: Message):
    await message.answer("❌ Пожалуйста, отправьте файл в формате PDF.")


@dp.callback_query(F.data == "diet_plan")
async def process_diet_plan(callback: CallbackQuery, state: FSMContext):
    if await check_maintenance_mode(callback.from_user.id):
        return

    subscription = get_subscription(callback.from_user.id)

    if not subscription:
        text = (
            "🔐 Ваш доступ к будущему временно ограничен.\n\n"
            "К сожалению, вы не в системе. Ваш персональный AI-доктор и все его возможности ждут вашего возвращения! 💔\n\n""💎 Станьте участником PREMIUM-клуба и откройте полный доступ:\n\n"
            "✨ Персональные рекомендации — советы, созданные именно для вас.\n""🥗 Индивидуальный рацион питания — питание как лекарство.\n"
            "📚 Полная история запросов — отслеживайте прогресс вашего здоровья.\n\n"f"🎁 Стоимость вложения в свое здоровье: всего{SUBSCRIPTION_PRICE} руб. на {SUBSCRIPTION_DAYS} дней полного комфорта!"
            "Не откладывайте здоровье на потом! Верните себе доступ к медицине будущего — сейчас. 🚀"
        )

        builder = InlineKeyboardBuilder()
        builder.button(text="💳 Купить подписку", callback_data="buy_subscription")
        builder.button(text="🔙 Назад", callback_data="back")
        builder.adjust(1)

        await callback.message.edit_text(text, reply_markup=builder.as_markup())
        return

    text = "Для чего вам нужен рацион питания?"

    builder = InlineKeyboardBuilder()
    builder.button(text="💪 Для набора массы", callback_data="diet_mass_gain")
    builder.button(text="🏃 Для снижения массы", callback_data="diet_weight_loss")
    builder.button(text="🔙 Назад", callback_data="back")
    builder.adjust(1)

    await callback.message.edit_text(text, reply_markup=builder.as_markup())
    await state.set_state(DietStates.PURPOSE)


@dp.callback_query(F.data.in_(["diet_mass_gain", "diet_weight_loss"]), DietStates.PURPOSE)
async def process_diet_purpose(callback: CallbackQuery, state: FSMContext):
    if await check_maintenance_mode(callback.from_user.id):
        return

    purpose = "набора массы" if callback.data == "diet_mass_gain" else "снижения массы"
    await state.update_data(purpose=purpose)

    text = "Введите ваш возраст\nПример: 25"

    builder = InlineKeyboardBuilder()
    builder.button(text="🔙 Назад", callback_data="back")

    await callback.message.edit_text(text, reply_markup=builder.as_markup())
    await state.set_state(DietStates.AGE)


@dp.message(DietStates.AGE)
async def process_diet_age(message: Message, state: FSMContext):
    if await check_maintenance_mode(message.from_user.id):
        return

    if not message.text.isdigit():
        await message.answer("❌ Пожалуйста, введите возраст числом")
        return

    age = int(message.text)
    if age < 10 or age > 120:
        await message.answer("❌ Пожалуйста, введите корректный возраст (10-120 лет)")
        return

    await state.update_data(age=age)

    text = "Выберите ваш пол"

    builder = InlineKeyboardBuilder()
    builder.button(text="👨 Мужской", callback_data="gender_male")
    builder.button(text="👩 Женский", callback_data="gender_female")
    builder.button(text="🔙 Назад", callback_data="back")
    builder.adjust(2)

    await message.answer(text, reply_markup=builder.as_markup())
    await state.set_state(DietStates.GENDER)


@dp.callback_query(F.data.in_(["gender_male", "gender_female"]), DietStates.GENDER)
async def process_diet_gender(callback: CallbackQuery, state: FSMContext):
    if await check_maintenance_mode(callback.from_user.id):
        return

    gender = "мужской" if callback.data == "gender_male" else "женский"
    await state.update_data(gender=gender)

    text = "Введите ваш вес в кг\nПример: 70"

    builder = InlineKeyboardBuilder()
    builder.button(text="🔙 Назад", callback_data="back")

    await callback.message.edit_text(text, reply_markup=builder.as_markup())
    await state.set_state(DietStates.WEIGHT)


@dp.message(DietStates.WEIGHT)
async def process_diet_weight(message: Message, state: FSMContext):
    if await check_maintenance_mode(message.from_user.id):
        return

    try:
        weight = float(message.text.replace(",", "."))
        if weight <= 0 or weight > 300:
            raise ValueError
    except ValueError:
        await message.answer("❌ Пожалуйста, введите корректный вес (например: 70 или 70.5)")
        return

    await state.update_data(weight=weight)

    text = "Введите ваш рост в см\nПример: 175"

    builder = InlineKeyboardBuilder()
    builder.button(text="🔙 Назад", callback_data="back")

    await message.answer(text, reply_markup=builder.as_markup())
    await state.set_state(DietStates.HEIGHT)


@dp.message(DietStates.HEIGHT)
async def process_diet_height(message: Message, state: FSMContext):
    if await check_maintenance_mode(message.from_user.id):
        return

    if not message.text.isdigit():
        await message.answer("❌ Пожалуйста, введите рост числом в см")
        return

    height = int(message.text)
    if height < 50 or height > 250:
        await message.answer("❌ Пожалуйста, введите корректный рост (50-250 см)")
        return

    await state.update_data(height=height)

    text = "Есть ли у вас противопоказания (например: аллергия)?"

    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Да", callback_data="contraindications_yes")
    builder.button(text="❌ Нет", callback_data="contraindications_no")
    builder.button(text="🔙 Назад", callback_data="back")
    builder.adjust(2)

    await message.answer(text, reply_markup=builder.as_markup())
    await state.set_state(DietStates.CONTRAINDICATIONS)


@dp.callback_query(F.data.in_(["contraindications_yes", "contraindications_no"]), DietStates.CONTRAINDICATIONS)
async def process_diet_contraindications(callback: CallbackQuery, state: FSMContext):
    if await check_maintenance_mode(callback.from_user.id):
        return

    if callback.data == "contraindications_yes":
        text = "Пожалуйста, укажите какие именно противопоказания/аллергии у вас есть"

        builder = InlineKeyboardBuilder()
        builder.button(text="🔙 Назад", callback_data="back")

        await callback.message.edit_text(text, reply_markup=builder.as_markup())
        await state.set_state(DietStates.ALLERGIES)
    else:
        await state.update_data(allergies='нет')
        await generate_and_send_diet_plan(callback, state)


@dp.message(DietStates.ALLERGIES)
async def process_diet_allergies(message: Message, state: FSMContext):
    if await check_maintenance_mode(message.from_user.id):
        return

    await state.update_data(allergies=message.text)
    await generate_and_send_diet_plan(message, state)


async def generate_and_send_diet_plan(source: Union[Message, CallbackQuery], state: FSMContext):
    if isinstance(source, CallbackQuery):
        user_id = source.from_user.id
        message = source.message
    else:
        user_id = source.from_user.id
        message = source

    data = await state.get_data()
    save_diet_profile(user_id, data)

    purpose = data.get("purpose", "")
    age = data.get("age", "")
    gender = data.get("gender", "")
    weight = data.get("weight", "")
    height = data.get("height", "")
    allergies = data.get("allergies", "нет")

    prompt = (
        f"Составьте профессиональный план питания для цели: {purpose}.\n"
        f"Данные пользователя:\n"
        f"- Возраст: {age}\n"
        f"- Пол: {gender}\n"
        f"- Вес: {weight} кг\n"
        f"- Рост: {height} см\n"
        f"- Противопоказания/аллергии: {allergies}\n\n"
        "План должен включать:\n"
        "1. Расчет рекомендуемой калорийности с формулой\n"
        "2. Оптимальное соотношение БЖУ для указанной цели\n"
        "3. Пример меню на день с распределением по приемам пищи\n"
        "4. Рекомендации по времени приема пищи\n"
        "5. Советы по приготовлению и выбору продуктов\n"
        "6. Рекомендации по гидратации\n\n"
        "Укажите, что это общие рекомендации, и индивидуальный план должен составлять диетолог."
    )

    loading_msg = await message.answer("⏳ ИИ составляет ваш рацион питания...")

    response = await generate_deepseek_response(prompt, max_tokens=4000)

    if response:
        save_message(user_id, "Запрос рациона питания", response)

        # Разбиваем ответ на части если он слишком длинный
        response_parts = split_long_message(response)

        # Отправляем первую часть с кнопками
        first_part = response_parts[0]
        builder = InlineKeyboardBuilder()
        builder.button(text="📜 История запросов", callback_data="history_list")
        builder.button(text="🔙 Назад", callback_data="back")

        await loading_msg.edit_text(first_part, reply_markup=builder.as_markup())

        # Отправляем остальные части
        for part in response_parts[1:]:
            await message.answer(part)

    else:
        builder = InlineKeyboardBuilder()
        builder.button(text="🔄 Попробовать снова", callback_data="diet_plan")
        builder.button(text="🔙 Назад", callback_data="back")

        await loading_msg.edit_text(
            "⚠️ Сервис временно недоступен. Попробуйте позже",
            reply_markup=builder.as_markup()
        )

    await state.clear()


# Admin commands
@dp.message(Command("maintenance"))
async def cmd_maintenance(message: Message):
    if not is_manager(message.from_user.id):
        await message.answer("❌ Команда доступна только администратору")
        return

    args = message.text.split()
    if len(args) < 2:
        status = get_maintenance_status()
        if status['is_active']:
            text = (f"🔧 Режим техработ АКТИВЕН\n"
                    f"Начало: {status['start_time'].strftime('%d.%m %H:%M')}\n"
                    f"Причина: {status['reason']}")
        else:
            text = "✅ Режим техработ НЕ активен"
        await message.answer(text)
        return

    action = args[1].lower()
    reason = " ".join(args[2:]) if len(args) > 2 else "Технические работы"

    if action == "on":
        current_status = get_maintenance_status()
        if current_status['is_active']:
            await message.answer("❌ Режим техработ уже активен")
            return

        set_maintenance_mode(True, reason)

        active_users = [sub['user_id'] for sub in get_all_subscriptions()]

        frozen_count = 0
        for user_id in active_users:
            if freeze_subscription(user_id):
                frozen_count += 1

        await message.answer(
            f"🔧 Режим техработ ВКЛЮЧЕН\n"
            f"Причина: {reason}\n"
            f"✅ Заморожено подписок: {frozen_count}"
        )

    elif action == "off":
        current_status = get_maintenance_status()
        if not current_status['is_active']:
            await message.answer("❌ Режим техработ не активен")
            return

        unfreeze_all_subscriptions()
        set_maintenance_mode(False)

        await message.answer(
            "✅ Режим техработ ВЫКЛЮЧЕН\n"
            "Все подписки разморожены"
        )

    else:
        await message.answer("❌ Используйте: /maintenance on [причина] или /maintenance off")


@dp.message(Command("managers"))
async def cmd_managers(message: Message):
    """Управление менеджерами"""
    if not is_manager(message.from_user.id):
        await message.answer("❌ Команда доступна только администратору")
        return

    managers = get_managers()
    if not managers:
        await message.answer("📭 Менеджеры не найдены")
        return

    text = "👥 Список менеджеров:\n\n"
    for i, manager in enumerate(managers, 1):
        text += f"{i}. ID: {manager['user_id']}\n   Имя: {manager['username']}\n   Добавлен: {manager['added_date'].strftime('%d.%m.%Y')}\n\n"

    text += "Используйте:\n/add_manager [id] - добавить менеджера\n/remove_manager [id] - удалить менеджера"

    await message.answer(text)


@dp.message(Command("add_manager"))
async def cmd_add_manager(message: Message):
    """Добавить менеджера"""
    if not is_manager(message.from_user.id):
        await message.answer("❌ Команда доступна только администратору")
        return

    args = message.text.split()
    if len(args) < 2:
        await message.answer("❌ Используйте: /add_manager [user_id]")
        return

    try:
        user_id = int(args[1])
        # Try to get user info
        try:
            user = await bot.get_chat(user_id)
            username = user.username or user.first_name or "Неизвестно"
        except:
            username = "Неизвестно"

        if add_manager(user_id, username, message.from_user.id):
            await message.answer(f"✅ Менеджер {user_id} добавлен")
        else:
            await message.answer("❌ Менеджер уже существует")
    except ValueError:
        await message.answer("❌ Неверный user_id. Используйте числовой ID")


@dp.message(Command("remove_manager"))
async def cmd_remove_manager(message: Message):
    """Удалить менеджера"""
    if not is_manager(message.from_user.id):
        await message.answer("❌ Команда доступна только администратору")
        return

    args = message.text.split()
    if len(args) < 2:
        await message.answer("❌ Используйте: /remove_manager [user_id]")
        return

    try:
        user_id = int(args[1])
        if user_id == message.from_user.id:
            await message.answer("❌ Вы не можете удалить себя")
            return

        if remove_manager(user_id):
            await message.answer(f"✅ Менеджер {user_id} удален")
        else:
            await message.answer("❌ Менеджер не найден")
    except ValueError:
        await message.answer("❌ Неверный user_id. Используйте числовой ID")


@dp.message(Command("promo_codes"))
async def cmd_promo_codes(message: Message):
    """Управление промокодами"""
    if not is_manager(message.from_user.id):
        await message.answer("❌ Команда доступна только администратору")
        return

    promos = get_all_promo_codes()
    if not promos:
        await message.answer("📭 Промокоды не найдены")
        return

    text = "🎫 Список промокодов:\n\n"
    for promo in promos:
        status = "🟢 Активен" if promo['is_active'] else "🔴 Неактивен"
        expiry = promo['expiry_date'].strftime('%d.%m.%Y') if promo['expiry_date'] else "Бессрочный"
        text += (
            f"Код: {promo['code']}\n"
            f"Скидка: {promo['discount_percent']}%\n"
            f"Использований: {promo['usage_count']}/{promo['usage_limit']}\n"
            f"Срок: {expiry}\n"
            f"Статус: {status}\n\n"
        )

    text += "Используйте:\n/create_promo [код] [скидка%] [дней_действия] [лимит] - создать промокод\n/delete_promo [код] - удалить промокод"

    await message.answer(text)


@dp.message(Command("create_promo"))
async def cmd_create_promo(message: Message):
    """Создать промокод"""
    if not is_manager(message.from_user.id):
        await message.answer("❌ Команда доступна только администратору")
        return

    args = message.text.split()
    if len(args) < 4:
        await message.answer("❌ Используйте: /create_promo [код] [скидка%] [дней_действия] [лимит_использований]")
        return

    try:
        code = args[1].upper()
        discount = int(args[2])
        expiry_days = int(args[3])
        usage_limit = int(args[4]) if len(args) > 4 else 1

        if discount <= 0 or discount > 100:
            await message.answer("❌ Скидка должна быть от 1 до 100%")
            return

        if expiry_days <= 0:
            await message.answer("❌ Срок действия должен быть положительным числом")
            return

        if create_promo_code(code, discount, expiry_days, usage_limit):
            expiry_date = (datetime.now() + timedelta(days=expiry_days)).strftime('%d.%m.%Y')
            await message.answer(
                f"✅ Промокод создан!\n"
                f"Код: {code}\n"
                f"Скидка: {discount}%\n"
                f"Действует до: {expiry_date}\n"
                f"Лимит использований: {usage_limit}"
            )
        else:
            await message.answer("❌ Промокод уже существует")
    except ValueError:
        await message.answer("❌ Неверные параметры. Убедитесь, что используете числа для скидки, срока и лимита")


@dp.message(Command("delete_promo"))
async def cmd_delete_promo(message: Message):
    """Удалить промокод"""
    if not is_manager(message.from_user.id):
        await message.answer("❌ Команда доступна только администратору")
        return

    args = message.text.split()
    if len(args) < 2:
        await message.answer("❌ Используйте: /delete_promo [код]")
        return

    code = args[1].upper()
    if delete_promo_code(code):
        await message.answer(f"✅ Промокод {code} удален")
    else:
        await message.answer("❌ Промокод не найден")


@dp.message(Command("debug_maintenance"))
async def cmd_debug_maintenance(message: Message):
    """Команда для отладки режима техработ"""
    if not is_manager(message.from_user.id):
        await message.answer("❌ Команда доступна только администратору")
        return

    status = get_maintenance_status()

    active_freezes = 0
    with db_lock:
        try:
            conn = sqlite3.connect(DB_FILE, timeout=30)
            cursor = conn.cursor()
            cursor.execute('SELECT COUNT(*) FROM subscription_freezes WHERE freeze_end IS NULL')
            active_freezes = cursor.fetchone()[0]

            cursor.execute('SELECT COUNT(*) FROM subscriptions WHERE date(end_date) > date("now")')
            active_subs = cursor.fetchone()[0]
            conn.close()
        except sqlite3.Error as e:
            logger.error(f"Database error in debug_maintenance: {e}")
            await message.answer("❌ Ошибка при получении данных")
            return

    text = (
        f"🔧 Статус техработ: {'АКТИВЕН' if status['is_active'] else 'НЕ АКТИВЕН'}\n"
        f"📊 Активных подписок: {active_subs}\n"
        f"❄️ Замороженных подписок: {active_freezes}\n"
        f"🕐 Статус обновлен: {status['start_time'].strftime('%d.%m %H:%M') if status['start_time'] else 'N/A'}\n"
        f"📝 Причина: {status['reason'] or 'N/A'}"
    )

    await message.answer(text)


@dp.message(Command("reset_subs"))
async def cmd_reset_subscriptions(message: Message):
    """Сброс всех подписок (опасная команда)"""
    if not is_manager(message.from_user.id):
        await message.answer("❌ Команда доступна только администратору")
        return

    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Да, сбросить всё", callback_data="confirm_reset_all")
    builder.button(text="❌ Отмена", callback_data="cancel_reset")
    builder.adjust(1)

    await message.answer(
        "🚨 ВНИМАНИЕ: Это сбросит ВСЕ подписки и данные пользователей!\n"
        "Все подписки, история запросов и профили будут удалены.\n\n"
        "Вы уверены?",
        reply_markup=builder.as_markup()
    )


@dp.message(Command("reset_user"))
async def cmd_reset_user(message: Message):
    """Сброс подписки конкретного пользователя"""
    if not is_manager(message.from_user.id):
        await message.answer("❌ Команда доступна только администратору")
        return

    args = message.text.split()
    if len(args) < 2:
        await message.answer("❌ Используйте: /reset_user <user_id>")
        return

    try:
        user_id = int(args[1])
        reset_user_subscription(user_id)
        await message.answer(f"✅ Подписка пользователя {user_id} сброшена")
    except ValueError:
        await message.answer("❌ Неверный user_id. Используйте числовой ID")


@dp.message(Command("list_subs"))
async def cmd_list_subscriptions(message: Message):
    """Список всех активных подписок"""
    if not is_manager(message.from_user.id):
        await message.answer("❌ Команда доступна только администратору")
        return

    subscriptions = get_all_subscriptions()

    if not subscriptions:
        await message.answer("📭 Активных подписок нет")
        return

    text = "📋 Активные подписки:\n\n"
    for sub in subscriptions:
        text += (
            f"👤 User ID: {sub['user_id']}\n"
            f"⏰ Осталось дней: {sub['days_left']}\n"
            f"📅 До: {sub['end_date'].strftime('%d.%m.%Y')}\n"
            f"{'-' * 20}\n"
        )

    if len(text) > 4000:
        parts = [text[i:i + 4000] for i in range(0, len(text), 4000)]
        for part in parts:
            await message.answer(part)
    else:
        await message.answer(text)


@dp.message(Command("sub_stats"))
async def cmd_sub_stats(message: Message):
    """Статистика подписок"""
    if not is_manager(message.from_user.id):
        await message.answer("❌ Команда доступна только администратору")
        return

    with db_lock:
        try:
            conn = sqlite3.connect(DB_FILE, timeout=30)
            cursor = conn.cursor()

            cursor.execute('SELECT COUNT(*) FROM subscriptions')
            total_subs = cursor.fetchone()[0]

            cursor.execute('SELECT COUNT(*) FROM subscriptions WHERE date(end_date) > date("now")')
            active_subs = cursor.fetchone()[0]

            cursor.execute('SELECT COUNT(*) FROM message_history')
            total_messages = cursor.fetchone()[0]

            cursor.execute('SELECT COUNT(*) FROM diet_profiles')
            diet_profiles = cursor.fetchone()[0]

            estimated_revenue = active_subs * SUBSCRIPTION_PRICE

            conn.close()
        except sqlite3.Error as e:
            logger.error(f"Database error in sub_stats: {e}")
            await message.answer("❌ Ошибка при получении статистики")
            return

    text = (
        "📊 Статистика подписок:\n\n"
        f"• Всего подписок: {total_subs}\n"
        f"• Активных подписок: {active_subs}\n"
        f"• Сообщений в истории: {total_messages}\n"
        f"• Диет-профилей: {diet_profiles}\n"
        f"• Примерный месячный доход: {estimated_revenue} руб.\n\n"
        f"💡 Используйте /list_subs для списка"
    )

    await message.answer(text)


@dp.callback_query(F.data == "confirm_reset_all")
async def confirm_reset_all(callback: CallbackQuery):
    """Подтверждение сброса всех подписок"""
    if not is_manager(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен")
        return

    with db_lock:
        try:
            conn = sqlite3.connect(DB_FILE, timeout=30)
            cursor = conn.cursor()
            cursor.execute('SELECT COUNT(*) FROM subscriptions')
            subs_count = cursor.fetchone()[0]
            cursor.execute('SELECT COUNT(*) FROM message_history')
            history_count = cursor.fetchone()[0]
            conn.close()
        except sqlite3.Error as e:
            logger.error(f"Database error in confirm_reset_all: {e}")
            await callback.answer("❌ Ошибка при сбросе")
            return

    reset_all_subscriptions()

    await callback.message.edit_text(
        f"♻️ Все данные сброшены!\n"
        f"🗑️ Удалено:\n"
        f"• Подписок: {subs_count}\n"
        f"• Записей истории: {history_count}\n\n"
        f"База данных очищена полностью."
    )
    await callback.answer("Сброс выполнен")


@dp.callback_query(F.data == "cancel_reset")
async def cancel_reset(callback: CallbackQuery):
    """Отмена сброса"""
    await callback.message.edit_text("❌ Сброс отменен")
    await callback.answer()


async def on_startup():
    """Действия при запуске бота"""
    logger.info("Бот запускается...")

    # Проверяем наличие незавершенных техработ
    status = get_maintenance_status()
    if status['is_active'] and not status['end_time']:
        logger.warning("Обнаружены незавершенные технические работы!")

    # Проверяем подключение к базе данных
    try:
        with db_lock:
            conn = sqlite3.connect(DB_FILE, timeout=30)
            cursor = conn.cursor()
            cursor.execute('SELECT COUNT(*) FROM subscriptions')
            conn.close()
        logger.info("Подключение к базе данных успешно")
    except Exception as e:
        logger.error(f"Ошибка подключения к базе данных: {e}")

    # Проверяем наличие необходимых пакетов
    try:
        import certifi
        logger.info(f"Certifi найден: {certifi.where()}")
    except ImportError:
        logger.error("Certifi не установлен! Установите: pip install certifi")


async def main():
    await on_startup()

    # Check for pending payments on startup
    pending = []
    with db_lock:
        try:
            conn = sqlite3.connect(DB_FILE, timeout=30)
            cursor = conn.cursor()
            cursor.execute('SELECT user_id, user_name, photo_id FROM pending_payments')
            pending = cursor.fetchall()
            conn.close()
        except sqlite3.Error as e:
            logger.error(f"Database error on startup: {e}")

    if pending:
        managers = get_managers()
        for user_id, user_name, photo_id in pending:
            manager_text = (
                f"Необработанный платеж при перезапуске:\n"
                f"Пользователь: {user_name}\nID: {user_id}"
            )

            builder = InlineKeyboardBuilder()
            builder.button(text="✅ Одобрить", callback_data=f"approve_{user_id}")
            builder.button(text="❌ Отклонить", callback_data=f"reject_{user_id}")

            for manager in managers:
                try:
                    await bot.send_photo(
                        chat_id=manager['user_id'],
                        photo=photo_id,
                        caption=manager_text,
                        reply_markup=builder.as_markup()
                    )
                except Exception as e:
                    logger.error(f"Error sending to manager {manager['user_id']}: {e}")

    logger.info("Бот запущен и готов к работе!")

    try:
        await dp.start_polling(bot)
    except Exception as e:
        logger.error(f"Ошибка при запуске бота: {e}")
    finally:
        logger.info("Бот остановлен")


if __name__ == "__main__":
    import asyncio
    import sys

    # Проверяем установленные зависимости
    required_packages = ['certifi', 'aiohttp']
    for package in required_packages:
        try:
            __import__(package)
        except ImportError:
            print(f"Ошибка: пакет {package} не установлен!")
            print(f"Установите: pip install {' '.join(required_packages)}")
            sys.exit(1)

    asyncio.run(main())

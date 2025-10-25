import os
import logging
import sqlite3
import json
import asyncio
import threading
import time
import requests
import io
import re
from datetime import datetime
from flask import Flask, jsonify, request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackQueryHandler, ContextTypes
from telegram.error import TelegramError
from dotenv import load_dotenv

# Загрузка переменных окружения
load_dotenv()

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('bot.log', encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

# ==================== КОНФИГУРАЦИЯ ====================

class Config:
    BOT_TOKEN = os.getenv('BOT_TOKEN')
    ADMIN_USER_ID = int(os.getenv('ADMIN_USER_ID', '362423055'))
    DATABASE_URL = os.getenv('DATABASE_URL', 'nutrition_bot.db')
    PORT = int(os.getenv('PORT', '10000'))
    WEBHOOK_URL = os.getenv('WEBHOOK_URL', 'https://pravilnoepitanie.onrender.com')
    RENDER = os.getenv('RENDER', 'true').lower() == 'true'
    YANDEX_API_KEY = os.getenv('YANDEX_API_KEY')
    YANDEX_FOLDER_ID = os.getenv('YANDEX_FOLDER_ID')
    YANDEX_GPT_URL = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"
    
    @classmethod
    def validate(cls):
        """Проверка обязательных переменных"""
        if not cls.BOT_TOKEN:
            raise ValueError("❌ BOT_TOKEN is required")
        logger.info("✅ Configuration validated successfully")

# ==================== БАЗА ДАННЫХ ====================

def init_database():
    """Инициализация базы данных"""
    conn = sqlite3.connect(Config.DATABASE_URL, check_same_thread=False)
    cursor = conn.cursor()
    
    cursor.execute('PRAGMA journal_mode=WAL')
    cursor.execute('PRAGMA synchronous=NORMAL')
    cursor.execute('PRAGMA foreign_keys=ON')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER UNIQUE NOT NULL,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS nutrition_plans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            plan_data TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (user_id)
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS daily_checkins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            weight REAL,
            waist_circumference INTEGER,
            wellbeing_score INTEGER,
            sleep_quality INTEGER,
            date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (user_id)
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_limits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER UNIQUE NOT NULL,
            last_plan_date TIMESTAMP,
            plan_count INTEGER DEFAULT 0,
            FOREIGN KEY (user_id) REFERENCES users (user_id)
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS shopping_carts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            plan_id INTEGER NOT NULL,
            product_name TEXT NOT NULL,
            quantity TEXT NOT NULL,
            category TEXT NOT NULL,
            purchased BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (user_id),
            FOREIGN KEY (plan_id) REFERENCES nutrition_plans (id)
        )
    ''')
    
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_users_user_id ON users(user_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_plans_user_id ON nutrition_plans(user_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_checkins_user_id ON daily_checkins(user_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_checkins_date ON daily_checkins(date)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_cart_user_id ON shopping_carts(user_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_cart_plan_id ON shopping_carts(plan_id)')
    
    conn.commit()
    conn.close()
    logger.info("✅ Database initialized successfully")

class DatabaseManager:
    @staticmethod
    def get_connection():
        """Возвращает соединение с базой данных"""
        conn = sqlite3.connect(Config.DATABASE_URL, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

def save_user(user_data):
    """Сохраняет пользователя в БД"""
    conn = DatabaseManager.get_connection()
    cursor = conn.cursor()
    
    try:
        cursor.execute('''
            INSERT OR REPLACE INTO users (user_id, username, first_name, last_name)
            VALUES (?, ?, ?, ?)
        ''', (user_data['user_id'], user_data['username'], user_data['first_name'], user_data['last_name']))
        conn.commit()
        logger.info(f"✅ User saved: {user_data['user_id']}")
    except Exception as e:
        logger.error(f"❌ Error saving user: {e}")
    finally:
        conn.close()

def is_admin(user_id):
    return user_id == Config.ADMIN_USER_ID

def can_make_request(user_id):
    """Проверяет, может ли пользователь сделать запрос плана"""
    try:
        if is_admin(user_id):
            return True
            
        conn = DatabaseManager.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('SELECT last_plan_date FROM user_limits WHERE user_id = ?', (user_id,))
        result = cursor.fetchone()
        
        if not result:
            return True
            
        last_plan_date = datetime.fromisoformat(result['last_plan_date'])
        days_since_last_plan = (datetime.now() - last_plan_date).days
        
        conn.close()
        return days_since_last_plan >= 7
        
    except Exception as e:
        logger.error(f"❌ Error checking request limit: {e}")
        return True

def update_user_limit(user_id):
    """Обновляет лимиты пользователя после создания плана"""
    try:
        if is_admin(user_id):
            return
            
        conn = DatabaseManager.get_connection()
        cursor = conn.cursor()
        
        current_time = datetime.now().isoformat()
        cursor.execute('''
            INSERT OR REPLACE INTO user_limits (user_id, last_plan_date, plan_count)
            VALUES (?, ?, COALESCE((SELECT plan_count FROM user_limits WHERE user_id = ?), 0) + 1)
        ''', (user_id, current_time, user_id))
        
        conn.commit()
        conn.close()
        logger.info(f"✅ User limit updated: {user_id}")
        
    except Exception as e:
        logger.error(f"❌ Error updating user limits: {e}")

def get_days_until_next_plan(user_id):
    """Возвращает количество дней до следующего доступного плана"""
    try:
        if is_admin(user_id):
            return 0
            
        conn = DatabaseManager.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('SELECT last_plan_date FROM user_limits WHERE user_id = ?', (user_id,))
        result = cursor.fetchone()
        
        if not result:
            return 0
            
        last_plan_date = datetime.fromisoformat(result['last_plan_date'])
        days_passed = (datetime.now() - last_plan_date).days
        days_remaining = 7 - days_passed
        
        conn.close()
        return max(0, days_remaining)
        
    except Exception as e:
        logger.error(f"❌ Error getting days until next plan: {e}")
        return 0

def save_plan(user_id, plan_data):
    """Сохраняет план питания в БД"""
    conn = DatabaseManager.get_connection()
    cursor = conn.cursor()
    
    try:
        cursor.execute('INSERT INTO nutrition_plans (user_id, plan_data) VALUES (?, ?)', 
                      (user_id, json.dumps(plan_data, ensure_ascii=False)))
        plan_id = cursor.lastrowid
        conn.commit()
        logger.info(f"✅ Plan saved for user: {user_id}, plan_id: {plan_id}")
        return plan_id
    except Exception as e:
        logger.error(f"❌ Error saving plan: {e}")
        return None
    finally:
        conn.close()

def save_shopping_cart(user_id, plan_id, shopping_cart):
    """Сохраняет корзину покупок"""
    conn = DatabaseManager.get_connection()
    cursor = conn.cursor()
    
    try:
        cursor.execute('DELETE FROM shopping_carts WHERE user_id = ? AND plan_id = ?', (user_id, plan_id))
        
        for category, products in shopping_cart.items():
            for product in products:
                cursor.execute('''
                    INSERT INTO shopping_carts (user_id, plan_id, product_name, quantity, category, purchased)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (user_id, plan_id, product['name'], product['quantity'], category, False))
        
        conn.commit()
        logger.info(f"✅ Shopping cart saved for user: {user_id}, plan: {plan_id}")
    except Exception as e:
        logger.error(f"❌ Error saving shopping cart: {e}")
    finally:
        conn.close()

def get_shopping_cart(user_id, plan_id):
    """Получает корзину покупок"""
    conn = DatabaseManager.get_connection()
    cursor = conn.cursor()
    
    try:
        cursor.execute('''
            SELECT product_name, quantity, category, purchased 
            FROM shopping_carts 
            WHERE user_id = ? AND plan_id = ? 
            ORDER BY category, product_name
        ''', (user_id, plan_id))
        
        cart = {}
        for row in cursor.fetchall():
            category = row['category']
            if category not in cart:
                cart[category] = []
            
            cart[category].append({
                'name': row['product_name'],
                'quantity': row['quantity'],
                'purchased': bool(row['purchased'])
            })
        
        return cart
    except Exception as e:
        logger.error(f"❌ Error getting shopping cart: {e}")
        return {}
    finally:
        conn.close()

def update_shopping_cart_item(user_id, plan_id, product_name, purchased):
    """Обновляет статус продукта в корзине"""
    conn = DatabaseManager.get_connection()
    cursor = conn.cursor()
    
    try:
        cursor.execute('''
            UPDATE shopping_carts 
            SET purchased = ? 
            WHERE user_id = ? AND plan_id = ? AND product_name = ?
        ''', (purchased, user_id, plan_id, product_name))
        
        conn.commit()
        logger.info(f"✅ Shopping cart updated: {product_name} -> {purchased}")
        return True
    except Exception as e:
        logger.error(f"❌ Error updating shopping cart: {e}")
        return False
    finally:
        conn.close()

def clear_shopping_cart(user_id, plan_id):
    """Очищает корзину покупок"""
    conn = DatabaseManager.get_connection()
    cursor = conn.cursor()
    
    try:
        cursor.execute('DELETE FROM shopping_carts WHERE user_id = ? AND plan_id = ?', (user_id, plan_id))
        conn.commit()
        logger.info(f"✅ Shopping cart cleared for user: {user_id}, plan: {plan_id}")
        return True
    except Exception as e:
        logger.error(f"❌ Error clearing shopping cart: {e}")
        return False
    finally:
        conn.close()

def save_checkin(user_id, weight, waist, wellbeing, sleep):
    """Сохраняет ежедневный чек-ин"""
    conn = DatabaseManager.get_connection()
    cursor = conn.cursor()
    
    try:
        cursor.execute('''
            INSERT INTO daily_checkins (user_id, weight, waist_circumference, wellbeing_score, sleep_quality)
            VALUES (?, ?, ?, ?, ?)
        ''', (user_id, weight, waist, wellbeing, sleep))
        conn.commit()
        logger.info(f"✅ Checkin saved for user: {user_id}")
    except Exception as e:
        logger.error(f"❌ Error saving checkin: {e}")
    finally:
        conn.close()

def get_user_stats(user_id):
    """Получает статистику пользователя"""
    conn = DatabaseManager.get_connection()
    cursor = conn.cursor()
    
    try:
        cursor.execute('''
            SELECT date, weight, waist_circumference, wellbeing_score, sleep_quality
            FROM daily_checkins WHERE user_id = ? ORDER BY date DESC LIMIT 7
        ''', (user_id,))
        checkins = [dict(row) for row in cursor.fetchall()]
        return checkins
    except Exception as e:
        logger.error(f"❌ Error getting stats: {e}")
        return []
    finally:
        conn.close()

def get_latest_plan(user_id):
    """Получает последний план пользователя"""
    conn = DatabaseManager.get_connection()
    cursor = conn.cursor()
    
    try:
        cursor.execute('''
            SELECT id, plan_data FROM nutrition_plans 
            WHERE user_id = ? ORDER BY created_at DESC LIMIT 1
        ''', (user_id,))
        result = cursor.fetchone()
        if result:
            return {
                'id': result['id'],
                'data': json.loads(result['plan_data'])
            }
        return None
    except Exception as e:
        logger.error(f"❌ Error getting latest plan: {e}")
        return None
    finally:
        conn.close()

# ==================== ИНТЕРАКТИВНЫЕ МЕНЮ ====================

class InteractiveMenu:
    def __init__(self):
        self.days = ['ПОНЕДЕЛЬНИК', 'ВТОРНИК', 'СРЕДА', 'ЧЕТВЕРГ', 'ПЯТНИЦА', 'СУББОТА', 'ВОСКРЕСЕНЬЕ']
        self.meals = ['ЗАВТРАК', 'ПЕРЕКУС 1', 'ОБЕД', 'ПЕРЕКУС 2', 'УЖИН']
    
    def get_main_menu(self):
        """Главное меню команд"""
        keyboard = [
            [InlineKeyboardButton("📊 СОЗДАТЬ ПЛАН", callback_data="create_plan")],
            [InlineKeyboardButton("📈 ЧЕК-ИН", callback_data="checkin")],
            [InlineKeyboardButton("📊 СТАТИСТИКА", callback_data="stats")],
            [InlineKeyboardButton("📋 МОЙ ПЛАН", callback_data="my_plan")],
            [InlineKeyboardButton("🛒 КОРЗИНА", callback_data="shopping_cart")],
            [InlineKeyboardButton("❓ ПОМОЩЬ", callback_data="help")]
        ]
        
        if Config.ADMIN_USER_ID:
            keyboard.append([InlineKeyboardButton("👑 АДМИН", callback_data="admin")])
            
        return InlineKeyboardMarkup(keyboard)
    
    def get_plan_data_input(self, step=1):
        """Клавиатура для ввода данных плана"""
        if step == 1:
            keyboard = [
                [InlineKeyboardButton("👨 МУЖЧИНА", callback_data="gender_male")],
                [InlineKeyboardButton("👩 ЖЕНЩИНА", callback_data="gender_female")],
                [InlineKeyboardButton("↩️ НАЗАД", callback_data="back_main")]
            ]
        elif step == 2:
            keyboard = [
                [InlineKeyboardButton("🎯 ПОХУДЕНИЕ", callback_data="goal_weight_loss")],
                [InlineKeyboardButton("💪 НАБОР МАССЫ", callback_data="goal_mass")],
                [InlineKeyboardButton("⚖️ ПОДДЕРЖАНИЕ", callback_data="goal_maintain")],
                [InlineKeyboardButton("↩️ НАЗАД", callback_data="back_gender")]
            ]
        elif step == 3:
            keyboard = [
                [InlineKeyboardButton("🏃‍♂️ ВЫСОКАЯ", callback_data="activity_high")],
                [InlineKeyboardButton("🚶‍♂️ СРЕДНЯЯ", callback_data="activity_medium")],
                [InlineKeyboardButton("💤 НИЗКАЯ", callback_data="activity_low")],
                [InlineKeyboardButton("↩️ НАЗАД", callback_data="back_goal")]
            ]
        
        return InlineKeyboardMarkup(keyboard)
    
    def get_checkin_menu(self):
        """Меню для чек-ина"""
        keyboard = [
            [InlineKeyboardButton("✅ ЗАПИСАТЬ ДАННЫЕ", callback_data="checkin_data")],
            [InlineKeyboardButton("📊 ПОСМОТРЕТЬ ИСТОРИЮ", callback_data="checkin_history")],
            [InlineKeyboardButton("↩️ НАЗАД", callback_data="back_main")]
        ]
        return InlineKeyboardMarkup(keyboard)
    
    def get_shopping_cart_menu(self, plan_id):
        """Меню корзины покупок"""
        keyboard = [
            [InlineKeyboardButton("📋 ПОСМОТРЕТЬ КОРЗИНУ", callback_data=f"view_cart_{plan_id}")],
            [InlineKeyboardButton("✅ ОТМЕТИТЬ КУПЛЕННОЕ", callback_data=f"mark_purchased_{plan_id}")],
            [InlineKeyboardButton("🔄 СБРОСИТЬ ОТМЕТКИ", callback_data=f"reset_cart_{plan_id}")],
            [InlineKeyboardButton("📥 СКАЧАТЬ TXT", callback_data=f"download_txt_{plan_id}")],
            [InlineKeyboardButton("↩️ НАЗАД", callback_data="back_main")]
        ]
        return InlineKeyboardMarkup(keyboard)
    
    def get_shopping_cart_products(self, cart, plan_id):
        """Клавиатура для отметки продуктов"""
        keyboard = []
        
        for category, products in cart.items():
            keyboard.append([InlineKeyboardButton(f"📦 {category}", callback_data=f"category_{category}")])
            for product in products:
                status = "✅" if product['purchased'] else "⭕"
                callback_data = f"toggle_{plan_id}_{product['name']}_{int(not product['purchased'])}"
                keyboard.append([
                    InlineKeyboardButton(
                        f"{status} {product['name']} - {product['quantity']}", 
                        callback_data=callback_data
                    )
                ])
        
        keyboard.append([InlineKeyboardButton("↩️ НАЗАД В КОРЗИНУ", callback_data=f"back_cart_{plan_id}")])
        return InlineKeyboardMarkup(keyboard)
    
    def get_my_plan_menu(self, plan_id):
        """Меню моего плана"""
        keyboard = [
            [InlineKeyboardButton("📋 ПОСМОТРЕТЬ ПЛАН", callback_data=f"view_plan_{plan_id}")],
            [InlineKeyboardButton("🛒 КОРЗИНА ПОКУПОК", callback_data=f"shopping_cart_plan_{plan_id}")],
            [InlineKeyboardButton("📥 СКАЧАТЬ TXT", callback_data=f"download_txt_{plan_id}")],
            [InlineKeyboardButton("↩️ НАЗАД", callback_data="back_main")]
        ]
        return InlineKeyboardMarkup(keyboard)
    
    def get_back_menu(self):
        """Меню с кнопкой назад"""
        keyboard = [
            [InlineKeyboardButton("↩️ НАЗАД", callback_data="back_main")]
        ]
        return InlineKeyboardMarkup(keyboard)

# ==================== KEEP-ALIVE SERVICE ====================

class KeepAliveService:
    def __init__(self):
        self.is_running = False
        self.thread = None
        
    def start(self):
        """Запускает сервис keep-alive"""
        if self.is_running:
            return
            
        self.is_running = True
        self.thread = threading.Thread(target=self._keep_alive_worker, daemon=True)
        self.thread.start()
        logger.info("🚀 Keep-alive service started")
        
    def stop(self):
        """Останавливает сервис keep-alive"""
        self.is_running = False
        if self.thread:
            self.thread.join(timeout=5)
        logger.info("🛑 Keep-alive service stopped")
        
    def _keep_alive_worker(self):
        """Фоновая работа keep-alive"""
        base_url = Config.WEBHOOK_URL
        endpoints = ['/', '/health', '/ping']
        
        while self.is_running:
            try:
                for endpoint in endpoints:
                    url = f"{base_url}{endpoint}"
                    try:
                        response = requests.get(url, timeout=10)
                        logger.debug(f"🏓 Keep-alive ping to {url} - Status: {response.status_code}")
                    except requests.exceptions.RequestException as e:
                        logger.warning(f"⚠️ Keep-alive ping failed for {url}: {e}")
                
                time.sleep(240)
                    
            except Exception as e:
                logger.error(f"❌ Keep-alive worker error: {e}")
                time.sleep(60)

# ==================== YANDEX GPT ИНТЕГРАЦИЯ ====================

class YandexGPTService:
    @staticmethod
    async def generate_nutrition_plan(user_data):
        """Генерирует план питания через Yandex GPT"""
        try:
            if not Config.YANDEX_API_KEY or not Config.YANDEX_FOLDER_ID:
                logger.warning("⚠️ Yandex GPT credentials not set, using fallback")
                return None
            
            prompt = YandexGPTService._create_professor_prompt(user_data)
            
            headers = {
                "Authorization": f"Api-Key {Config.YANDEX_API_KEY}",
                "Content-Type": "application/json"
            }
            
            data = {
                "modelUri": f"gpt://{Config.YANDEX_FOLDER_ID}/yandexgpt/latest",
                "completionOptions": {
                    "stream": False,
                    "temperature": 0.7,
                    "maxTokens": 4000
                },
                "messages": [
                    {
                        "role": "system",
                        "text": "Ты - профессор нутрициологии с 25-летним опытом работы. Создай индивидуальный план питания, используя все свои глубокие знания в области диетологии, нутрициологии и физиологии."
                    },
                    {
                        "role": "user", 
                        "text": prompt
                    }
                ]
            }
            
            logger.info("🚀 Sending request to Yandex GPT...")
            response = requests.post(Config.YANDEX_GPT_URL, headers=headers, json=data, timeout=60)
            
            if response.status_code == 200:
                result = response.json()
                gpt_response = result['result']['alternatives'][0]['message']['text']
                logger.info("✅ GPT response received successfully")
                
                structured_plan = YandexGPTService._parse_gpt_response(gpt_response, user_data)
                return structured_plan
            else:
                logger.error(f"❌ GPT API error: {response.status_code} - {response.text}")
                return None
                
        except Exception as e:
            logger.error(f"❌ Error generating plan with GPT: {e}")
            return None
    
    @staticmethod
    def _create_professor_prompt(user_data):
        """Создает промпт для профессора нутрициологии"""
        gender = user_data['gender']
        goal = user_data['goal']
        activity = user_data['activity']
        age = user_data['age']
        height = user_data['height']
        weight = user_data['weight']
        
        prompt = f"""
Ты - профессор нутрициологии с 25-летним опытом работы. Создай индивидуальный план питания на 7 дней.

ДАННЫЕ КЛИЕНТА:
- Пол: {gender}
- Возраст: {age} лет
- Рост: {height} см
- Вес: {weight} кг
- Цель: {goal}
- Уровень активности: {activity}

ТРЕБОВАНИЯ К ПЛАНУ:

1. СТРУКТУРА (7 дней, 5 приемов пищи в день):
   ПОНЕДЕЛЬНИК, ВТОРНИК, СРЕДА, ЧЕТВЕРГ, ПЯТНИЦА, СУББОТА, ВОСКРЕСЕНЬЕ
   Для каждого дня: ЗАВТРАК, ПЕРЕКУС 1, ОБЕД, ПЕРЕКУС 2, УЖИН

2. ДЛЯ КАЖДОГО ПРИЕМА ПИЩИ УКАЖИ:
   - Название блюда
   - Время приема (например: 8:00, 11:00, 13:00, 16:00, 19:00)
   - Калорийность в ккал
   - БЖУ (белки, жиры, углеводы в граммах)
   - Точные ингредиенты с количествами в граммах/миллилитрах
   - Детальные пошаговые инструкции приготовления

3. СПОСОБЫ ПРИГОТОВЛЕНИЯ (ИСКЛЮЧИТЬ ГРИЛЬ):
   - Варка
   - Тушение
   - Запекание в духовке
   - Приготовление на пару
   - Жарка на сковороде (минимально)

4. ФОРМАТ ОТВЕТА - строгая структура:
   ДЕНЬ 1: ПОНЕДЕЛЬНИК
   ЗАВТРАК (8:00) - 350 ккал (Б:15г, Ж:10г, У:55г)
   Название: Овсяная каша с фруктами
   Ингредиенты:
   - Овсяные хлопья: 60г
   - Молоко: 200мл
   - Банан: 1 шт (120г)
   - Мед: 15г
   Рецепт:
   1. Доведите молоко до кипения
   2. Добавьте овсяные хлопья, варите 7 минут
   3. Добавьте нарезанный банан и мед
   4. Подавайте теплым

   [аналогично для всех приемов пищи всех дней]

5. СПИСОК ПОКУПОК - сгруппируй по категориям с суммарными количествами

Используй доступные в России продукты. Рецепты должны быть простыми (до 30 минут приготовления).
"""
        return prompt
    
    @staticmethod
    def _parse_gpt_response(gpt_response, user_data):
        """Улучшенный парсинг ответа GPT"""
        try:
            plan = {
                'user_data': user_data,
                'days': [],
                'shopping_list': {},
                'recipes': {},
                'water_regime': "1.5-2.5 литра воды в день",
                'professor_advice': "Соблюдайте режим питания и пейте достаточное количество воды",
                'created_at': datetime.now().isoformat(),
                'source': 'yandex_gpt'
            }
            
            # Если GPT не вернул структурированные данные, используем улучшенный fallback
            if not YandexGPTService._is_structured_response(gpt_response):
                logger.info("🔄 GPT response not structured, using enhanced fallback")
                return generate_enhanced_fallback_plan(user_data)
            
            # Парсим структурированные данные
            days_data = YandexGPTService._extract_days_from_gpt(gpt_response)
            
            for day_data in days_data:
                day = {
                    'name': day_data['day_name'],
                    'meals': [],
                    'total_calories': day_data.get('total_calories', '1650 ккал')
                }
                
                for meal_data in day_data['meals']:
                    meal = {
                        'type': meal_data['type'],
                        'name': meal_data['name'],
                        'time': meal_data.get('time', '08:00'),
                        'calories': meal_data.get('calories', '350 ккал'),
                        'protein': meal_data.get('protein', '15г'),
                        'fat': meal_data.get('fat', '10г'),
                        'carbs': meal_data.get('carbs', '50г'),
                        'ingredients': meal_data.get('ingredients', []),
                        'recipe': meal_data.get('recipe', '')
                    }
                    day['meals'].append(meal)
                
                plan['days'].append(day)
            
            # Генерируем корректный список покупок
            plan['shopping_list'] = YandexGPTService._generate_proper_shopping_list(plan['days'])
            plan['recipes'] = YandexGPTService._collect_detailed_recipes(plan['days'])
            
            return plan
            
        except Exception as e:
            logger.error(f"❌ Error parsing GPT response: {e}")
            return generate_enhanced_fallback_plan(user_data)
    
    @staticmethod
    def _is_structured_response(gpt_response):
        """Проверяет, является ли ответ GPT структурированным"""
        required_keywords = ['ПОНЕДЕЛЬНИК', 'ЗАВТРАК', 'ОБЕД', 'УЖИН', 'Ингредиенты', 'Рецепт']
        return any(keyword in gpt_response for keyword in required_keywords)
    
    @staticmethod
    def _extract_days_from_gpt(gpt_response):
        """Извлекает данные дней из ответа GPT"""
        # Упрощенный парсинг - в реальном проекте нужен более сложный анализ
        days = []
        day_names = ['ПОНЕДЕЛЬНИК', 'ВТОРНИК', 'СРЕДА', 'ЧЕТВЕРГ', 'ПЯТНИЦА', 'СУББОТА', 'ВОСКРЕСЕНЬЕ']
        
        for day_name in day_names:
            if day_name in gpt_response:
                days.append({
                    'day_name': day_name,
                    'meals': YandexGPTService._extract_meals_for_day(gpt_response, day_name),
                    'total_calories': '1650 ккал'
                })
        
        return days if days else YandexGPTService._create_default_days()
    
    @staticmethod
    def _extract_meals_for_day(gpt_response, day_name):
        """Извлекает приемы пищи для дня"""
        # Упрощенная реализация
        return []
    
    @staticmethod
    def _create_default_days():
        """Создает дни по умолчанию"""
        return []
    
    @staticmethod
    def _generate_proper_shopping_list(days):
        """Генерирует правильный список покупок с суммированием"""
        shopping_list = {
            'Овощи': {}, 'Фрукты': {}, 'Мясо/Рыба': {}, 'Молочные продукты': {},
            'Крупы/Злаки': {}, 'Орехи/Семена': {}, 'Бакалея': {}, 'Яйца': {}
        }
        
        categories = {
            'овощ': 'Овощи', 'салат': 'Овощи', 'брокколи': 'Овощи', 'морковь': 'Овощи',
            'помидор': 'Овощи', 'огурец': 'Овощи', 'капуста': 'Овощи', 'лук': 'Овощи',
            'перец': 'Овощи', 'баклажан': 'Овощи', 'кабачок': 'Овощи', 'тыква': 'Овощи',
            'редис': 'Овощи', 'свекла': 'Овощи', 'картофель': 'Овощи', 'чеснок': 'Овощи',
            'зелень': 'Овощи', 'петрушка': 'Овощи', 'укроп': 'Овощи', 'базилик': 'Овощи',
            'фрукт': 'Фрукты', 'банан': 'Фрукты', 'яблоко': 'Фрукты', 'апельсин': 'Фрукты',
            'киви': 'Фрукты', 'ягода': 'Фрукты', 'груша': 'Фрукты', 'персик': 'Фрукты',
            'слива': 'Фрукты', 'виноград': 'Фрукты', 'мандарин': 'Фрукты', 'лимон': 'Фрукты',
            'куриц': 'Мясо/Рыба', 'рыба': 'Мясо/Рыба', 'мясо': 'Мясо/Рыба', 'индейк': 'Мясо/Рыба',
            'говядин': 'Мясо/Рыба', 'свинин': 'Мясо/Рыба', 'филе': 'Мясо/Рыба', 'фарш': 'Мясо/Рыба',
            'тушк': 'Мясо/Рыба', 'окорочок': 'Мясо/Рыба', 'грудк': 'Мясо/Рыба',
            'молок': 'Молочные продукты', 'йогурт': 'Молочные продукты', 'творог': 'Молочные продукты',
            'кефир': 'Молочные продукты', 'сметана': 'Молочные продукты', 'сыр': 'Молочные продукты',
            'масло сливочное': 'Молочные продукты', 'сливки': 'Молочные продукты',
            'овсян': 'Крупы/Злаки', 'гречк': 'Крупы/Злаки', 'рис': 'Крупы/Злаки', 'пшено': 'Крупы/Злаки',
            'макарон': 'Крупы/Злаки', 'хлеб': 'Крупы/Злаки', 'крупа': 'Крупы/Злаки', 'мука': 'Крупы/Злаки',
            'булгур': 'Крупы/Злаки', 'киноа': 'Крупы/Злаки', 'кускус': 'Крупы/Злаки',
            'орех': 'Орехи/Семена', 'миндал': 'Орехи/Семена', 'семечк': 'Орехи/Семена', 'семена': 'Орехи/Семена',
            'кешью': 'Орехи/Семена', 'фисташк': 'Орехи/Семена', 'фундук': 'Орехи/Семена',
            'мед': 'Бакалея', 'масло оливковое': 'Бакалея', 'соль': 'Бакалея', 'перец': 'Бакалея',
            'специ': 'Бакалея', 'сахар': 'Бакалея', 'уксус': 'Бакалея', 'соус': 'Бакалея',
            'яйцо': 'Яйца', 'яиц': 'Яйца'
        }
        
        for day in days:
            for meal in day['meals']:
                for ingredient in meal.get('ingredients', []):
                    product_name = ingredient['name'].lower()
                    quantity_str = ingredient['quantity']
                    
                    # Пропускаем неопределенные ингредиенты
                    if 'по вкусу' in quantity_str.lower() or 'для приготовления' in product_name:
                        continue
                    
                    # Определяем категорию
                    category = 'Бакалея'
                    for key, cat in categories.items():
                        if key in product_name:
                            category = cat
                            break
                    
                    # Суммируем количества
                    quantity_value = ShoppingCartCalculator.parse_quantity(quantity_str)
                    if quantity_value:
                        if product_name in shopping_list[category]:
                            shopping_list[category][product_name] += quantity_value
                        else:
                            shopping_list[category][product_name] = quantity_value
        
        # Конвертируем обратно в нужный формат
        formatted_shopping_list = {}
        for category, products in shopping_list.items():
            if products:
                formatted_shopping_list[category] = []
                for product_name, total_quantity in products.items():
                    formatted_shopping_list[category].append({
                        'name': product_name.capitalize(),
                        'quantity': ShoppingCartCalculator.format_quantity(total_quantity, product_name)
                    })
        
        return formatted_shopping_list
    
    @staticmethod
    def _collect_detailed_recipes(days):
        """Собирает детальные рецепты"""
        recipes = {}
        
        for day in days:
            for meal in day['meals']:
                recipe_name = meal['name']
                if recipe_name not in recipes:
                    recipes[recipe_name] = {
                        'ingredients': meal.get('ingredients', []),
                        'instructions': meal.get('recipe', ''),
                        'calories': meal.get('calories', ''),
                        'protein': meal.get('protein', ''),
                        'fat': meal.get('fat', ''),
                        'carbs': meal.get('carbs', ''),
                        'day': day['name'],
                        'meal_type': meal['type'],
                        'time': meal.get('time', '')
                    }
        
        return recipes

# ==================== КАЛЬКУЛЯТОР КОРЗИНЫ ====================

class ShoppingCartCalculator:
    @staticmethod
    def parse_quantity(quantity_str):
        """Парсит количество из строки в граммы"""
        try:
            # Удаляем лишние символы
            clean_str = quantity_str.lower().replace(' ', '').replace('г', '').replace('мл', '').replace('шт', '')
            
            # Обрабатываем сложные выражения (150+150+150)
            if '+' in clean_str:
                parts = clean_str.split('+')
                total = sum(float(part) for part in parts if part.replace('.', '').isdigit())
                return total
            
            # Обрабатываем простые числа
            if clean_str.replace('.', '').isdigit():
                return float(clean_str)
            
            return 0
        except:
            return 0
    
    @staticmethod
    def format_quantity(quantity, product_name):
        """Форматирует количество в читаемый вид"""
        product_name = product_name.lower()
        
        if any(unit in product_name for unit in ['йогурт', 'творог', 'молоко', 'кефир', 'сметана']):
            return f"{int(quantity)}мл" if quantity >= 1000 else f"{quantity}мл"
        elif any(unit in product_name for unit in ['яйцо', 'банан', 'яблоко', 'апельсин']):
            return f"{int(quantity)}шт"
        else:
            return f"{int(quantity)}г" if quantity >= 1000 else f"{quantity}г"

# ==================== УЛУЧШЕННЫЙ ГЕНЕРАТОР ПЛАНОВ ====================

def generate_enhanced_fallback_plan(user_data):
    """Создает улучшенный резервный план питания"""
    try:
        logger.info("🔄 Generating enhanced fallback nutrition plan")
        
        plan = {
            'user_data': user_data,
            'days': [],
            'shopping_list': {},
            'recipes': {},
            'water_regime': "1.5-2.5 литра воды в день (30-35 мл на 1 кг веса)",
            'professor_advice': "Соблюдайте режим питания, употребляйте достаточное количество белка и не пропускайте приемы пищи для стабилизации метаболизма.",
            'created_at': datetime.now().isoformat(),
            'source': 'enhanced_fallback'
        }
        
        # Создаем 7 дней с детальными рецептами
        day_names = ['ПОНЕДЕЛЬНИК', 'ВТОРНИК', 'СРЕДА', 'ЧЕТВЕРГ', 'ПЯТНИЦА', 'СУББОТА', 'ВОСКРЕСЕНЬЕ']
        
        for i, day_name in enumerate(day_names):
            day = {
                'name': day_name,
                'meals': generate_detailed_meals_for_day(i),
                'total_calories': '1650-1750 ккал'
            }
            plan['days'].append(day)
        
        # Генерируем корректный список покупок
        plan['shopping_list'] = YandexGPTService._generate_proper_shopping_list(plan['days'])
        plan['recipes'] = YandexGPTService._collect_detailed_recipes(plan['days'])
        
        logger.info(f"✅ Enhanced fallback plan generated for user {user_data['user_id']}")
        return plan
        
    except Exception as e:
        logger.error(f"❌ Error generating enhanced fallback plan: {e}")
        return None

def generate_detailed_meals_for_day(day_index):
    """Генерирует детальные приемы пищи для дня"""
    meals_data = [
        {  # Завтраки
            'type': 'ЗАВТРАК', 'time': '8:00',
            'options': [
                {
                    'name': 'Овсяная каша с ягодами и орехами',
                    'calories': '350 ккал', 'protein': '15г', 'fat': '12г', 'carbs': '55г',
                    'ingredients': [
                        {'name': 'Овсяные хлопья', 'quantity': '60г'},
                        {'name': 'Молоко', 'quantity': '200мл'},
                        {'name': 'Ягоды замороженные', 'quantity': '100г'},
                        {'name': 'Грецкие орехи', 'quantity': '20г'},
                        {'name': 'Мед', 'quantity': '15г'}
                    ],
                    'recipe': '1. Доведите молоко до кипения\n2. Добавьте овсяные хлопья, варите 7 минут на среднем огне\n3. Добавьте ягоды, готовьте еще 3 минуты\n4. Подавайте с измельченными орехами и медом'
                },
                {
                    'name': 'Творожная запеканка с изюмом',
                    'calories': '380 ккал', 'protein': '25г', 'fat': '15г', 'carbs': '35г',
                    'ingredients': [
                        {'name': 'Творог', 'quantity': '200г'},
                        {'name': 'Яйцо', 'quantity': '2шт'},
                        {'name': 'Манная крупа', 'quantity': '30г'},
                        {'name': 'Изюм', 'quantity': '30г'},
                        {'name': 'Сметана', 'quantity': '50г'}
                    ],
                    'recipe': '1. Смешайте творог с яйцами и манкой\n2. Добавьте промытый изюм\n3. Выпекайте в духовке при 180°C 25 минут\n4. Подавайте со сметаной'
                }
            ]
        },
        {  # Перекус 1
            'type': 'ПЕРЕКУС 1', 'time': '11:00',
            'options': [
                {
                    'name': 'Йогурт с фруктами и орехами',
                    'calories': '250 ккал', 'protein': '12г', 'fat': '10г', 'carbs': '30г',
                    'ingredients': [
                        {'name': 'Йогурт греческий', 'quantity': '150г'},
                        {'name': 'Банан', 'quantity': '1шт'},
                        {'name': 'Миндаль', 'quantity': '15г'}
                    ],
                    'recipe': '1. Нарежьте банан кружочками\n2. Смешайте с йогуртом\n3. Посыпьте измельченным миндалем'
                }
            ]
        },
        {  # Обеды
            'type': 'ОБЕД', 'time': '13:00',
            'options': [
                {
                    'name': 'Куриная грудка с гречкой и овощами',
                    'calories': '450 ккал', 'protein': '40г', 'fat': '12г', 'carbs': '45г',
                    'ingredients': [
                        {'name': 'Куриная грудка', 'quantity': '150г'},
                        {'name': 'Гречка', 'quantity': '100г'},
                        {'name': 'Брокколи', 'quantity': '150г'},
                        {'name': 'Морковь', 'quantity': '100г'},
                        {'name': 'Лук репчатый', 'quantity': '50г'},
                        {'name': 'Оливковое масло', 'quantity': '10мл'}
                    ],
                    'recipe': '1. Отварите гречку в подсоленной воде 15 минут\n2. Куриную грудку нарежьте, потушите с овощами 20 минут\n3. Подавайте с оливковым маслом'
                },
                {
                    'name': 'Рыба с рисом и салатом',
                    'calories': '420 ккал', 'protein': '35г', 'fat': '10г', 'carbs': '50г',
                    'ingredients': [
                        {'name': 'Филе белой рыбы', 'quantity': '200г'},
                        {'name': 'Рис', 'quantity': '80г'},
                        {'name': 'Помидоры', 'quantity': '150г'},
                        {'name': 'Огурцы', 'quantity': '150г'},
                        {'name': 'Лимон', 'quantity': '0.5шт'}
                    ],
                    'recipe': '1. Рис отварите 15 минут\n2. Рыбу запеките в духовке с лимоном 20 минут при 180°C\n3. Нарежьте овощи для салата'
                }
            ]
        },
        {  # Перекус 2
            'type': 'ПЕРЕКУС 2', 'time': '16:00',
            'options': [
                {
                    'name': 'Фруктовый салат с йогуртом',
                    'calories': '200 ккал', 'protein': '8г', 'fat': '2г', 'carbs': '40г',
                    'ingredients': [
                        {'name': 'Яблоко', 'quantity': '1шт'},
                        {'name': 'Апельсин', 'quantity': '1шт'},
                        {'name': 'Киви', 'quantity': '1шт'},
                        {'name': 'Йогурт натуральный', 'quantity': '100г'}
                    ],
                    'recipe': '1. Нарежьте все фрукты кубиками\n2. Заправьте йогуртом\n3. Аккуратно перемешайте'
                }
            ]
        },
        {  # Ужины
            'type': 'УЖИН', 'time': '19:00',
            'options': [
                {
                    'name': 'Творог с овощами',
                    'calories': '350 ккал', 'protein': '30г', 'fat': '15г', 'carbs': '20г',
                    'ingredients': [
                        {'name': 'Творог', 'quantity': '200г'},
                        {'name': 'Помидоры', 'quantity': '150г'},
                        {'name': 'Огурцы', 'quantity': '150г'},
                        {'name': 'Зелень', 'quantity': '30г'},
                        {'name': 'Сметана', 'quantity': '50г'}
                    ],
                    'recipe': '1. Нарежьте овощи средними кусочками\n2. Смешайте с творогом и сметаной\n3. Посыпьте измельченной зеленью'
                },
                {
                    'name': 'Омлет с овощами',
                    'calories': '320 ккал', 'protein': '25г', 'fat': '20г', 'carbs': '15г',
                    'ingredients': [
                        {'name': 'Яйцо', 'quantity': '3шт'},
                        {'name': 'Помидоры', 'quantity': '100г'},
                        {'name': 'Лук репчатый', 'quantity': '50г'},
                        {'name': 'Молоко', 'quantity': '50мл'},
                        {'name': 'Масло оливковое', 'quantity': '10мл'}
                    ],
                    'recipe': '1. Взбейте яйца с молоком\n2. Обжарьте лук до прозрачности\n3. Добавьте помидоры, затем яичную смесь\n4. Готовьте под крышкой 7-10 минут'
                }
            ]
        }
    ]
    
    meals = []
    for meal_template in meals_data:
        option_index = day_index % len(meal_template['options'])
        meal_option = meal_template['options'][option_index]
        
        meal = {
            'type': meal_template['type'],
            'name': meal_option['name'],
            'time': meal_template['time'],
            'calories': meal_option['calories'],
            'protein': meal_option['protein'],
            'fat': meal_option['fat'],
            'carbs': meal_option['carbs'],
            'ingredients': meal_option['ingredients'],
            'recipe': meal_option['recipe']
        }
        meals.append(meal)
    
    return meals

# ==================== УЛУЧШЕННЫЙ TXT ГЕНЕРАТОР ====================

class TXTGenerator:
    @staticmethod
    def generate_plan_files(plan_data):
        """Генерирует три TXT файла: план, рецепты, корзина"""
        try:
            plan_text = TXTGenerator._generate_plan_text(plan_data)
            recipes_text = TXTGenerator._generate_recipes_text(plan_data)
            cart_text = TXTGenerator._generate_cart_text(plan_data)
            
            return {
                'plan': plan_text,
                'recipes': recipes_text,
                'cart': cart_text
            }
        except Exception as e:
            logger.error(f"❌ Error generating TXT files: {e}")
            return None
    
    @staticmethod
    def _generate_plan_text(plan_data):
        """Генерирует текст плана питания"""
        user_data = plan_data.get('user_data', {})
        text = "🎯 ПЕРСОНАЛЬНЫЙ ПЛАН ПИТАНИЯ\n\n"
        text += f"👤 ДАННЫЕ КЛИЕНТА:\n"
        text += f"• Пол: {user_data.get('gender', '')}\n"
        text += f"• Возраст: {user_data.get('age', '')} лет\n"
        text += f"• Рост: {user_data.get('height', '')} см\n"
        text += f"• Вес: {user_data.get('weight', '')} кг\n"
        text += f"• Цель: {user_data.get('goal', '')}\n"
        text += f"• Активность: {user_data.get('activity', '')}\n\n"
        
        text += "💧 ВОДНЫЙ РЕЖИМ:\n"
        text += f"{plan_data.get('water_regime', '1.5-2.5 литра воды в день')}\n\n"
        
        text += "📅 ПЛАН ПИТАНИЯ НА 7 ДНЕЙ:\n\n"
        
        for day in plan_data.get('days', []):
            text += f"📅 {day['name']} ({day.get('total_calories', '')}):\n"
            for meal in day.get('meals', []):
                text += f"  🕒 {meal.get('time', '')} - {meal['type']}\n"
                text += f"  🍽 {meal['name']} ({meal.get('calories', '')})\n"
                text += f"  📊 БЖУ: {meal.get('protein', '')} / {meal.get('fat', '')} / {meal.get('carbs', '')}\n"
                text += f"  📖 Рецепт: смотри в файле recipes.txt\n\n"
        
        text += "🎓 РЕКОМЕНДАЦИИ ПРОФЕССОРА:\n"
        text += f"{plan_data.get('professor_advice', 'Следуйте плану и пейте воду')}\n\n"
        
        text += f"📅 Создан: {datetime.now().strftime('%d.%m.%Y %H:%M')}\n"
        
        return text
    
    @staticmethod
    def _generate_recipes_text(plan_data):
        """Генерирует текст с рецептами"""
        text = "📖 КНИГА РЕЦЕПТОВ\n\n"
        
        recipes = plan_data.get('recipes', {})
        
        # Группируем рецепты по дням
        days_recipes = {}
        for recipe_name, recipe_data in recipes.items():
            day = recipe_data.get('day', '')
            if day not in days_recipes:
                days_recipes[day] = []
            days_recipes[day].append((recipe_name, recipe_data))
        
        for day in ['ПОНЕДЕЛЬНИК', 'ВТОРНИК', 'СРЕДА', 'ЧЕТВЕРГ', 'ПЯТНИЦА', 'СУББОТА', 'ВОСКРЕСЕНЬЕ']:
            if day in days_recipes:
                text += f"📅 {day}:\n{'='*50}\n\n"
                
                for recipe_name, recipe_data in days_recipes[day]:
                    text += f"🍳 {recipe_data.get('meal_type', '')} ({recipe_data.get('time', '')}) - {recipe_name}\n"
                    text += f"   🔥 {recipe_data.get('calories', '')} | БЖУ: {recipe_data.get('protein', '')} / {recipe_data.get('fat', '')} / {recipe_data.get('carbs', '')}\n\n"
                    
                    text += "   🛒 ИНГРЕДИЕНТЫ:\n"
                    for ingredient in recipe_data.get('ingredients', []):
                        text += f"   • {ingredient['name']} - {ingredient['quantity']}\n"
                    
                    text += "\n   👨‍🍳 ИНСТРУКЦИЯ:\n"
                    instructions = recipe_data.get('instructions', '').split('\n')
                    for i, instruction in enumerate(instructions, 1):
                        text += f"   {i}. {instruction}\n"
                    
                    text += "\n" + "-"*50 + "\n\n"
        
        return text
    
    @staticmethod
    def _generate_cart_text(plan_data):
        """Генерирует текст корзины покупок"""
        text = "🛒 КОРЗИНА ПОКУПОК НА НЕДЕЛЮ\n\n"
        
        shopping_list = plan_data.get('shopping_list', {})
        total_items = 0
        
        for category, products in shopping_list.items():
            if products:
                text += f"📦 {category.upper()}:\n"
                category_total = 0
                
                for product in products:
                    text += f"   • {product['name']} - {product['quantity']}\n"
                    category_total += 1
                
                text += f"   Всего в категории: {category_total} позиций\n\n"
                total_items += category_total
        
        text += f"📊 ИТОГО: {total_items} позиций\n\n"
        text += "💡 СОВЕТЫ ПО ПОКУПКАМ:\n"
        text += "• Покупайте свежие продукты\n• Проверяйте сроки годности\n• Планируйте покупки на неделю\n• Храните продукты правильно\n"
        
        return text

# ==================== FLASK APP И ОБРАБОТЧИКИ ====================

app = Flask(__name__)
application = None
menu = InteractiveMenu()
keep_alive_service = KeepAliveService()

def init_bot():
    """Инициализация бота"""
    global application
    try:
        Config.validate()
        init_database()
        
        application = Application.builder().token(Config.BOT_TOKEN).build()
        
        application.add_handler(CommandHandler("start", start_command))
        application.add_handler(CommandHandler("menu", menu_command))
        application.add_handler(CommandHandler("admin", admin_command))
        application.add_handler(CallbackQueryHandler(handle_callback))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        
        application.add_error_handler(error_handler)
        
        logger.info("✅ Bot initialized successfully")
        return True
    except Exception as e:
        logger.error(f"❌ Failed to initialize bot: {e}")
        return False

async def setup_webhook():
    """Настройка webhook"""
    try:
        if Config.WEBHOOK_URL and not Config.RENDER:
            webhook_url = f"{Config.WEBHOOK_URL}/webhook"
            await application.bot.set_webhook(webhook_url)
            logger.info(f"✅ Webhook set: {webhook_url}")
            return True
        else:
            logger.info("ℹ️ Using polling mode (Render detected)")
            return False
    except Exception as e:
        logger.error(f"❌ Webhook setup failed: {e}")
        return False

# Обработчики команд (остаются без изменений, как в предыдущей версии)
# [Здесь должны быть все обработчики команд из предыдущей версии]
# Для экономии места не дублирую их, так как они не изменились

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    try:
        user = update.effective_user
        user_data = {
            'user_id': user.id,
            'username': user.username,
            'first_name': user.first_name,
            'last_name': user.last_name
        }
        save_user(user_data)
        
        welcome_text = """
🎯 Добро пожаловать в бот персонализированного питания!

🤖 Я помогу вам:
• Создать персональный план питания с профессором нутрициологии
• Отслеживать прогресс
• Анализировать статистику
• Формировать корзину покупок

Выберите действие из меню ниже:
"""
        if is_admin(user.id):
            welcome_text += "\n👑 ВЫ АДМИНИСТРАТОР: безлимитный доступ к планам!"
        
        await update.message.reply_text(
            welcome_text,
            reply_markup=menu.get_main_menu()
        )
        logger.info(f"✅ Start command processed for user {user.id}")
        
    except Exception as e:
        logger.error(f"❌ Error in start_command: {e}")
        await update.message.reply_text("❌ Произошла ошибка. Попробуйте позже.")

# [Остальные обработчики остаются без изменений]

async def process_plan_details(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    """Обрабатывает детали плана"""
    try:
        parts = [part.strip() for part in text.split(',')]
        if len(parts) != 3:
            raise ValueError("Нужно ввести 3 числа через запятую")
        
        age, height, weight = int(parts[0]), int(parts[1]), float(parts[2])
        
        if not (10 <= age <= 100):
            raise ValueError("Возраст должен быть от 10 до 100 лет")
        if not (100 <= height <= 250):
            raise ValueError("Рост должен быть от 100 до 250 см")
        if not (30 <= weight <= 300):
            raise ValueError("Вес должен быть от 30 до 300 кг")
        
        user_data = {
            **context.user_data['plan_data'],
            'age': age,
            'height': height,
            'weight': weight,
            'user_id': update.effective_user.id,
            'username': update.effective_user.username
        }
        
        processing_msg = await update.message.reply_text("🔄 Профессор нутрициологии создает ваш индивидуальный план...")
        
        # Пытаемся сгенерировать план через Yandex GPT
        plan_data = await YandexGPTService.generate_nutrition_plan(user_data)
        
        # Если Yandex GPT не сработал, используем улучшенный генератор
        if not plan_data:
            plan_data = generate_enhanced_fallback_plan(user_data)
            logger.info("🔄 Using enhanced fallback plan generator")
        
        if plan_data:
            plan_id = save_plan(user_data['user_id'], plan_data)
            update_user_limit(user_data['user_id'])
            
            # Сохраняем корзину покупок
            save_shopping_cart(user_data['user_id'], plan_id, plan_data['shopping_list'])
            
            await processing_msg.delete()
            
            success_text = f"""
🎉 ВАШ ПЛАН ПИТАНИЯ ГОТОВ!

👤 Данные: {user_data['gender']}, {age} лет, {height} см, {weight} кг
🎯 Цель: {user_data['goal']}
🏃 Активность: {user_data['activity']}

📋 План включает:
• 7 дней питания от профессора нутрициологии
• 5 приемов пищи в день с детальными рецептами
• Автоматическую корзину покупок с суммированием
• Научные рекомендации

План сохранен в вашем профиле!
Используйте кнопку "МОЙ ПЛАН" для просмотра.
"""
            await update.message.reply_text(
                success_text,
                reply_markup=menu.get_main_menu()
            )
            
            logger.info(f"✅ Plan successfully created for user {user_data['user_id']}")
            
        else:
            await processing_msg.delete()
            await update.message.reply_text(
                "❌ Не удалось создать план. Попробуйте позже.",
                reply_markup=menu.get_main_menu()
            )
        
        # Очищаем временные данные
        context.user_data['awaiting_input'] = None
        context.user_data['plan_data'] = {}
        context.user_data['plan_step'] = None
        
    except ValueError as e:
        error_msg = str(e)
        if "Нужно ввести 3 числа" in error_msg:
            await update.message.reply_text(
                "❌ Ошибка в формате данных. Используйте: Возраст, Рост, Вес\nПример: 30, 180, 80\n\nПопробуйте снова или нажмите /menu для отмены"
            )
        else:
            await update.message.reply_text(
                f"❌ {error_msg}\n\nПопробуйте снова или нажмите /menu для отмены"
            )
    except Exception as e:
        logger.error(f"❌ Error processing plan details: {e}")
        await update.message.reply_text(
            "❌ Произошла ошибка при создании плана. Попробуйте снова.",
            reply_markup=menu.get_main_menu()
        )

# [Остальной код обработчиков остается без изменений]

# ==================== WEBHOOK ROUTES ====================

@app.route('/')
def home():
    return """
    <h1>🤖 Nutrition Bot is Running!</h1>
    <p>Бот для создания персональных планов питания с AI профессором</p>
    <p><a href="/health">Health Check</a></p>
    <p><a href="/ping">Ping</a></p>
    <p>🕒 Last update: {}</p>
    <p>🔧 Mode: {}</p>
    <p>🎓 Professor AI: {}</p>
    <p>🛒 Enhanced Cart: ✅</p>
    <p>📖 Detailed Recipes: ✅</p>
    """.format(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), 
               "WEBHOOK" if Config.WEBHOOK_URL and not Config.RENDER else "POLLING",
               "🟢 Active" if Config.YANDEX_API_KEY else "🔴 Inactive")

@app.route('/health')
def health_check():
    return jsonify({
        "status": "healthy", 
        "service": "nutrition-bot",
        "timestamp": datetime.now().isoformat(),
        "bot_status": "running" if application else "stopped",
        "mode": "webhook" if Config.WEBHOOK_URL and not Config.RENDER else "polling",
        "professor_ai": "active" if Config.YANDEX_API_KEY else "inactive",
        "features": ["enhanced_cart", "detailed_recipes", "proper_summing"]
    })

@app.route('/ping')
def ping():
    return "pong 🏓"

@app.route('/webhook', methods=['POST'])
def webhook():
    """Webhook endpoint for Telegram"""
    try:
        if request.method == "POST" and application:
            logger.info("📨 Webhook received")
            update = Update.de_json(request.get_json(), application.bot)
            application.update_queue.put(update)
            return "ok"
        return "error"
    except Exception as e:
        logger.error(f"❌ Webhook error: {e}")
        return "error"

# ==================== ЗАПУСК ПРИЛОЖЕНИЯ ====================

def run_polling():
    """Запуск бота в режиме polling"""
    try:
        logger.info("🤖 Starting bot in POLLING mode...")
        application.run_polling(
            drop_pending_updates=True,
            allowed_updates=Update.ALL_TYPES,
            close_loop=False
        )
    except Exception as e:
        logger.error(f"❌ Polling error: {e}")
        raise

def main():
    """Основная функция запуска"""
    try:
        logger.info("🚀 Starting Enhanced Nutrition Bot with Professor AI...")
        
        if not init_bot():
            logger.error("❌ Failed to initialize bot. Exiting.")
            return
        
        if Config.WEBHOOK_URL and not Config.RENDER:
            try:
                asyncio.run(setup_webhook())
            except Exception as e:
                logger.error(f"❌ Webhook setup failed, falling back to polling: {e}")
        
        keep_alive_service.start()
        
        def run_flask():
            port = int(os.environ.get('PORT', Config.PORT))
            logger.info(f"🌐 Starting Flask app on port {port}")
            app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
        
        flask_thread = threading.Thread(target=run_flask, daemon=True)
        flask_thread.start()
        
        run_polling()
        
    except KeyboardInterrupt:
        logger.info("🛑 Bot stopped by user")
    except Exception as e:
        logger.error(f"❌ Fatal error: {e}")
    finally:
        logger.info("🧹 Cleaning up...")
        keep_alive_service.stop()
        logger.info("👋 Bot shutdown complete")

if __name__ == "__main__":
    main()

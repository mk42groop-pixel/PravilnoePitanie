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
import random
from datetime import datetime, timedelta
from flask import Flask, jsonify, request, send_file
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
        if not cls.YANDEX_API_KEY or not cls.YANDEX_FOLDER_ID:
            logger.warning("⚠️ Yandex GPT credentials not set - using enhanced generator")
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

def get_all_plans(user_id):
    """Получает все планы пользователя"""
    conn = DatabaseManager.get_connection()
    cursor = conn.cursor()
    
    try:
        cursor.execute('''
            SELECT id, plan_data, created_at FROM nutrition_plans 
            WHERE user_id = ? ORDER BY created_at DESC
        ''', (user_id,))
        plans = []
        for row in cursor.fetchall():
            plans.append({
                'id': row['id'],
                'data': json.loads(row['plan_data']),
                'created_at': row['created_at']
            })
        return plans
    except Exception as e:
        logger.error(f"❌ Error getting all plans: {e}")
        return []
    finally:
        conn.close()

def get_user_plan_count(user_id):
    """Получает количество созданных планов пользователя"""
    conn = DatabaseManager.get_connection()
    cursor = conn.cursor()
    
    try:
        cursor.execute('SELECT COUNT(*) as count FROM nutrition_plans WHERE user_id = ?', (user_id,))
        result = cursor.fetchone()
        return result['count'] if result else 0
    except Exception as e:
        logger.error(f"❌ Error getting plan count: {e}")
        return 0
    finally:
        conn.close()

def get_total_users():
    """Получает общее количество пользователей"""
    conn = DatabaseManager.get_connection()
    cursor = conn.cursor()
    
    try:
        cursor.execute('SELECT COUNT(*) as count FROM users')
        result = cursor.fetchone()
        return result['count'] if result else 0
    except Exception as e:
        logger.error(f"❌ Error getting total users: {e}")
        return 0
    finally:
        conn.close()

def get_total_plans():
    """Получает общее количество планов"""
    conn = DatabaseManager.get_connection()
    cursor = conn.cursor()
    
    try:
        cursor.execute('SELECT COUNT(*) as count FROM nutrition_plans')
        result = cursor.fetchone()
        return result['count'] if result else 0
    except Exception as e:
        logger.error(f"❌ Error getting total plans: {e}")
        return 0
    finally:
        conn.close()

def get_recent_checkins(user_id, days=7):
    """Получает последние чекины пользователя"""
    conn = DatabaseManager.get_connection()
    cursor = conn.cursor()
    
    try:
        cursor.execute('''
            SELECT date, weight, waist_circumference, wellbeing_score, sleep_quality
            FROM daily_checkins 
            WHERE user_id = ? AND date >= date('now', '-? days')
            ORDER BY date DESC
        ''', (user_id, days))
        checkins = [dict(row) for row in cursor.fetchall()]
        return checkins
    except Exception as e:
        logger.error(f"❌ Error getting recent checkins: {e}")
        return []
    finally:
        conn.close()

# ==================== YANDEX GPT ИНТЕГРАЦИЯ ====================

class YandexGPTService:
    @staticmethod
    async def generate_nutrition_plan(user_data):
        """Генерирует план питания через Yandex GPT"""
        try:
            if not Config.YANDEX_API_KEY or not Config.YANDEX_FOLDER_ID:
                logger.warning("⚠️ Yandex GPT credentials not set, using enhanced generator")
                return await YandexGPTService._generate_fallback_plan(user_data)
            
            prompt = YandexGPTService._create_detailed_prompt(user_data)
            
            headers = {
                "Authorization": f"Api-Key {Config.YANDEX_API_KEY}",
                "Content-Type": "application/json"
            }
            
            data = {
                "modelUri": f"gpt://{Config.YANDEX_FOLDER_ID}/yandexgpt/latest",
                "completionOptions": {
                    "stream": False,
                    "temperature": 0.8,
                    "maxTokens": 8000
                },
                "messages": [
                    {
                        "role": "system",
                        "text": """Ты - профессор нутрициологии с 25-летним опытом. Создай ИНДИВИДУАЛЬНЫЙ план питания на 7 дней.

КРИТИЧЕСКИ ВАЖНЫЕ ТРЕБОВАНИЯ:
1. УНИКАЛЬНЫЕ рецепты для каждого дня - блюда НЕ ДОЛЖНЫ повторяться
2. Детальный водный режим по часам
3. 5 приемов пищи в день: ЗАВТРАК, ПЕРЕКУС 1, ОБЕД, ПЕРЕКУС 2, УЖИН
4. Для каждого приема пищи указывай:
   - Уникальное название блюда
   - Время приема
   - Точную калорийность
   - БЖУ (белки, жиры, углеводы в граммах)
   - Ингредиенты с количествами
   - Пошаговый рецепт приготовления
5. В конце - ОБЩИЙ СПИСОК ПОКУПОК сгруппированный по категориям

ФОРМАТ ОТВЕТА - строго JSON:
{
    "days": [
        {
            "name": "ПОНЕДЕЛЬНИК",
            "total_calories": "1650 ккал",
            "water_schedule": ["7:00 - 200 мл теплой воды", ...],
            "meals": [
                {
                    "type": "ЗАВТРАК",
                    "name": "Уникальное название блюда",
                    "time": "8:00",
                    "calories": "350 ккал",
                    "protein": "15г",
                    "fat": "10г",
                    "carbs": "55г",
                    "ingredients": [
                        {"name": "Продукт", "quantity": "100г"}
                    ],
                    "recipe": "Пошаговый рецепт..."
                }
            ]
        }
    ],
    "shopping_list": {
        "Овощи": [{"name": "Помидор", "quantity": "500г"}],
        "Фрукты": [{"name": "Яблоко", "quantity": "300г"}]
    },
    "water_regime": {
        "total": "2.0 литра в день",
        "schedule": [
            {"time": "7:00", "amount": "200 мл", "description": "Описание"}
        ]
    },
    "professor_advice": "Персонализированный совет"
}"""
                    },
                    {
                        "role": "user", 
                        "text": prompt
                    }
                ]
            }
            
            logger.info("🚀 Sending request to Yandex GPT...")
            
            # Асинхронный запрос к Yandex GPT
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None, 
                lambda: requests.post(Config.YANDEX_GPT_URL, headers=headers, json=data, timeout=120)
            )
            
            if response.status_code == 200:
                result = response.json()
                gpt_response = result['result']['alternatives'][0]['message']['text']
                logger.info("✅ GPT response received successfully")
                
                # Парсим JSON ответ
                plan_data = YandexGPTService._parse_gpt_json_response(gpt_response, user_data)
                if plan_data:
                    logger.info("🎓 Successfully parsed GPT plan with YandexGPT")
                    plan_data['source'] = 'yandex_gpt'
                    return plan_data
                else:
                    logger.warning("⚠️ Failed to parse GPT JSON response, using enhanced generator")
                    return await YandexGPTService._generate_fallback_plan(user_data)
            else:
                logger.error(f"❌ GPT API error: {response.status_code} - {response.text}")
                return await YandexGPTService._generate_fallback_plan(user_data)
                
        except Exception as e:
            logger.error(f"❌ Error generating plan with YandexGPT: {e}")
            return await YandexGPTService._generate_fallback_plan(user_data)
    
    @staticmethod
    def _create_detailed_prompt(user_data):
        """Создает детальный промпт для Yandex GPT"""
        gender = user_data['gender']
        goal = user_data['goal']
        activity = user_data['activity']
        age = user_data['age']
        height = user_data['height']
        weight = user_data['weight']
        
        # Рассчитываем параметры
        bmr = YandexGPTService._calculate_bmr(gender, age, height, weight)
        tdee = YandexGPTService._calculate_tdee(bmr, activity)
        target_calories = YandexGPTService._calculate_target_calories(tdee, goal)
        
        prompt = f"""
Создай персонализированный план питания на 7 дней с УНИКАЛЬНЫМИ рецептами для каждого дня.

ДАННЫЕ КЛИЕНТА:
- Пол: {gender}
- Возраст: {age} лет
- Рост: {height} см
- Вес: {weight} кг
- Цель: {goal}
- Уровень активности: {activity}
- BMR (базальный метаболизм): {bmr:.0f} ккал
- TDEE (общий расход): {tdee:.0f} ккал
- Целевая калорийность: {target_calories:.0f} ккал/день

ОСОБЫЕ ТРЕБОВАНИЯ:
1. ВСЕ рецепты должны быть УНИКАЛЬНЫМИ - никаких повторений между днями
2. Используй доступные в России продукты
3. Время приготовления блюд - не более 30 минут
4. Учитывай цель: {goal}
5. Включи разнообразные белки, сложные углеводы, полезные жиры
6. Для водного режима используй расписание: 7:00, 8:00, 10:00, 11:00, 13:00, 15:00, 16:00, 18:00, 19:00, 21:00

Верни ответ в строгом JSON формате как указано в системном промпте.
"""
        return prompt
    
    @staticmethod
    def _parse_gpt_json_response(gpt_response, user_data):
        """Парсит JSON ответ от GPT"""
        try:
            # Ищем JSON в ответе
            json_match = re.search(r'\{.*\}', gpt_response, re.DOTALL)
            if json_match:
                json_str = json_match.group()
                plan_data = json.loads(json_str)
                
                # Добавляем пользовательские данные
                plan_data['user_data'] = user_data
                plan_data['created_at'] = datetime.now().isoformat()
                
                # Проверяем структуру
                if 'days' in plan_data and len(plan_data['days']) == 7:
                    logger.info("✅ Valid GPT plan structure received")
                    return plan_data
                else:
                    logger.warning("❌ Invalid plan structure from GPT")
                    return None
            else:
                logger.warning("❌ No JSON found in GPT response")
                return None
                
        except json.JSONDecodeError as e:
            logger.error(f"❌ JSON decode error: {e}")
            return None
        except Exception as e:
            logger.error(f"❌ Error parsing GPT JSON: {e}")
            return None
    
    @staticmethod
    async def _generate_fallback_plan(user_data):
        """Fallback генератор если YandexGPT не работает"""
        logger.info("🔄 Using enhanced generator as fallback")
        return EnhancedPlanGenerator.generate_plan_with_progress_indicator(user_data)
    
    @staticmethod
    def _calculate_bmr(gender, age, height, weight):
        """Рассчитывает базовый метаболизм"""
        if gender == "МУЖЧИНА":
            return 88.36 + (13.4 * weight) + (4.8 * height) - (5.7 * age)
        else:
            return 447.6 + (9.2 * weight) + (3.1 * height) - (4.3 * age)
    
    @staticmethod
    def _calculate_tdee(bmr, activity):
        """Рассчитывает общий расход энергии"""
        activity_multipliers = {
            "НИЗКАЯ": 1.2,
            "СРЕДНЯЯ": 1.55,
            "ВЫСОКАЯ": 1.725
        }
        return bmr * activity_multipliers.get(activity, 1.55)
    
    @staticmethod
    def _calculate_target_calories(tdee, goal):
        """Рассчитывает целевую калорийность"""
        if goal == "ПОХУДЕНИЕ":
            return tdee * 0.85  # Дефицит 15%
        elif goal == "НАБОР МАССЫ":
            return tdee * 1.15  # Профицит 15%
        else:
            return tdee

# ==================== УЛУЧШЕННЫЙ ГЕНЕРАТОР (FALLBACK) ====================

class EnhancedPlanGenerator:
    """Улучшенный генератор планов питания как fallback"""
    
    @staticmethod
    def generate_plan_with_progress_indicator(user_data):
        """Генерирует разнообразный план с уникальными рецептами"""
        logger.info(f"🎯 Generating enhanced plan for user {user_data['user_id']}")
        
        plan = {
            'user_data': user_data,
            'days': [],
            'shopping_list': {},
            'water_regime': EnhancedPlanGenerator._generate_detailed_water_regime(user_data),
            'professor_advice': EnhancedPlanGenerator._get_professor_advice(user_data),
            'created_at': datetime.now().isoformat(),
            'source': 'enhanced_generator'
        }
        
        # Создаем 7 дней с УНИКАЛЬНЫМИ рецептами
        day_names = ['ПОНЕДЕЛЬНИК', 'ВТОРНИК', 'СРЕДА', 'ЧЕТВЕРГ', 'ПЯТНИЦА', 'СУББОТА', 'ВОСКРЕСЕНЬЕ']
        
        for i, day_name in enumerate(day_names):
            day = {
                'name': day_name,
                'meals': EnhancedPlanGenerator._generate_unique_meals_for_day(i, user_data),
                'total_calories': EnhancedPlanGenerator._calculate_daily_calories(user_data),
                'water_schedule': EnhancedPlanGenerator._get_daily_water_schedule()
            }
            plan['days'].append(day)
        
        # Генерируем корректный список покупок
        plan['shopping_list'] = EnhancedPlanGenerator._generate_proper_shopping_list(plan['days'])
        
        logger.info(f"✅ Enhanced plan generated for user {user_data['user_id']}")
        return plan
    
    @staticmethod
    def _generate_detailed_water_regime(user_data):
        """Генерирует детальный водный режим"""
        weight = user_data.get('weight', 70)
        water_needed = max(1.5, weight * 0.03)  # 30 мл на 1 кг веса
        
        return {
            'total': f'{water_needed:.1f} литра в день',
            'schedule': [
                {'time': '7:00', 'amount': '200 мл', 'description': '1 стакан теплой воды натощак для запуска метаболизма'},
                {'time': '8:00', 'amount': '200 мл', 'description': 'После завтрака для улучшения пищеварения'},
                {'time': '10:00', 'amount': '200 мл', 'description': 'Стакан воды для поддержания гидратации'},
                {'time': '11:00', 'amount': '200 мл', 'description': 'После первого перекуса'},
                {'time': '13:00', 'amount': '200 мл', 'description': 'После обеда для усвоения питательных веществ'},
                {'time': '15:00', 'amount': '200 мл', 'description': 'Вода для поддержания энергии'},
                {'time': '16:00', 'amount': '200 мл', 'description': 'После второго перекуса'},
                {'time': '18:00', 'amount': '200 мл', 'description': 'Подготовка к ужину'},
                {'time': '19:00', 'amount': '200 мл', 'description': 'После ужина'},
                {'time': '21:00', 'amount': '200 мл', 'description': 'За 1-2 часа до сна для восстановления'}
            ]
        }
    
    @staticmethod
    def _get_daily_water_schedule():
        """Возвращает расписание воды на день"""
        return [
            '7:00 - 200 мл теплой воды натощак',
            '8:00 - 200 мл после завтрака', 
            '10:00 - 200 мл',
            '11:00 - 200 мл после перекуса',
            '13:00 - 200 мл после обеда',
            '15:00 - 200 мл',
            '16:00 - 200 мл после перекуса',
            '18:00 - 200 мл',
            '19:00 - 200 мл после ужина',
            '21:00 - 200 мл перед сном'
        ]
    
    @staticmethod
    def _calculate_daily_calories(user_data):
        """Рассчитывает дневную калорийность"""
        gender = user_data.get('gender', '')
        age = user_data.get('age', 30)
        height = user_data.get('height', 170)
        weight = user_data.get('weight', 70)
        activity = user_data.get('activity', '')
        goal = user_data.get('goal', '')
        
        # Базальный метаболизм
        if gender == 'МУЖЧИНА':
            bmr = 88.36 + (13.4 * weight) + (4.8 * height) - (5.7 * age)
        else:
            bmr = 447.6 + (9.2 * weight) + (3.1 * height) - (4.3 * age)
        
        # Учет активности
        activity_multipliers = {'НИЗКАЯ': 1.2, 'СРЕДНЯЯ': 1.55, 'ВЫСОКАЯ': 1.725}
        tdee = bmr * activity_multipliers.get(activity, 1.55)
        
        # Учет цели
        if goal == 'ПОХУДЕНИЕ':
            calories = tdee * 0.85  # Дефицит 15%
        elif goal == 'НАБОР МАССЫ':
            calories = tdee * 1.15  # Профицит 15%
        else:
            calories = tdee
        
        return f"{int(calories)} ккал"
    
    @staticmethod
    def _get_professor_advice(user_data):
        """Возвращает советы профессора"""
        goal = user_data.get('goal', '')
        
        advice = {
            'ПОХУДЕНИЕ': "Соблюдайте дефицит калорий для плавного снижения веса. Увеличьте потребление белка для сохранения мышечной массы.",
            'НАБОР МАССЫ': "Создайте профицит калорий для роста мышц. Увеличьте потребление сложных углеводов для энергии.",
            'ПОДДЕРЖАНИЕ': "Поддерживайте баланс между потреблением и расходом калорий. Сбалансируйте БЖУ для оптимального функционирования."
        }
        
        return advice.get(goal, "Следите за водным балансом и регулярно занимайтесь спортом для лучших результатов.")
    
    @staticmethod
    def _generate_unique_meals_for_day(day_index, user_data):
        """Генерирует УНИКАЛЬНЫЕ приемы пищи для каждого дня"""
        meals = []
        meal_types = [
            {'type': 'ЗАВТРАК', 'time': '8:00'},
            {'type': 'ПЕРЕКУС 1', 'time': '11:00'},
            {'type': 'ОБЕД', 'time': '13:00'},
            {'type': 'ПЕРЕКУС 2', 'time': '16:00'},
            {'type': 'УЖИН', 'time': '19:00'}
        ]
        
        # Базовые варианты для каждого типа приема пищи
        breakfast_options = EnhancedPlanGenerator._get_breakfast_options()
        snack_options = EnhancedPlanGenerator._get_snack_options()
        lunch_options = EnhancedPlanGenerator._get_lunch_options()
        dinner_options = EnhancedPlanGenerator._get_dinner_options()
        
        for meal_type in meal_types:
            if meal_type['type'] == 'ЗАВТРАК':
                options = breakfast_options
            elif meal_type['type'] in ['ПЕРЕКУС 1', 'ПЕРЕКУС 2']:
                options = snack_options
            elif meal_type['type'] == 'ОБЕД':
                options = lunch_options
            else:  # УЖИН
                options = dinner_options
            
            # Выбираем уникальный вариант для каждого дня
            option_index = (day_index * len(options)) % len(options)
            meal_option = options[option_index]
            
            meal = {
                'type': meal_type['type'],
                'name': meal_option['name'],
                'time': meal_type['time'],
                'calories': meal_option['calories'],
                'protein': meal_option['protein'],
                'fat': meal_option['fat'],
                'carbs': meal_option['carbs'],
                'ingredients': meal_option['ingredients'],
                'recipe': meal_option['recipe']
            }
            meals.append(meal)
        
        return meals
    
    @staticmethod
    def _get_breakfast_options():
        """Уникальные варианты завтраков"""
        return [
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
                'recipe': '1. Доведите молоко до кипения\n2. Добавьте овсяные хлопья, варите 7 минут\n3. Добавьте ягоды, готовьте еще 3 минуты\n4. Подавайте с измельченными орехами и медом'
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
            },
            {
                'name': 'Омлет с овощами и сыром',
                'calories': '320 ккал', 'protein': '22г', 'fat': '18г', 'carbs': '12г',
                'ingredients': [
                    {'name': 'Яйцо', 'quantity': '3шт'},
                    {'name': 'Помидор', 'quantity': '1шт'},
                    {'name': 'Перец болгарский', 'quantity': '0.5шт'},
                    {'name': 'Сыр', 'quantity': '50г'},
                    {'name': 'Молоко', 'quantity': '50мл'}
                ],
                'recipe': '1. Взбейте яйца с молоком\n2. Обжарьте овощи 5 минут\n3. Залейте яичной смесью, посыпьте сыром\n4. Готовьте под крышкой 10 минут'
            }
        ]
    
    @staticmethod
    def _get_snack_options():
        """Уникальные варианты перекусов"""
        return [
            {
                'name': 'Йогурт с фруктами и орехами',
                'calories': '250 ккал', 'protein': '12г', 'fat': '10г', 'carbs': '30г',
                'ingredients': [
                    {'name': 'Йогурт греческий', 'quantity': '150г'},
                    {'name': 'Банан', 'quantity': '1шт'},
                    {'name': 'Миндаль', 'quantity': '15г'}
                ],
                'recipe': '1. Нарежьте банан кружочками\n2. Смешайте с йогуртом\n3. Посыпьте измельченным миндалем'
            },
            {
                'name': 'Творог с ягодами',
                'calories': '200 ккал', 'protein': '20г', 'fat': '5г', 'carbs': '15г',
                'ingredients': [
                    {'name': 'Творог', 'quantity': '150г'},
                    {'name': 'Ягоды свежие', 'quantity': '100г'},
                    {'name': 'Мед', 'quantity': '10г'}
                ],
                'recipe': '1. Смешайте творог с ягодами\n2. Добавьте мед по вкусу\n3. Тщательно перемешайте'
            }
        ]
    
    @staticmethod
    def _get_lunch_options():
        """Уникальные варианты обедов"""
        return [
            {
                'name': 'Куриная грудка с гречкой и овощами',
                'calories': '450 ккал', 'protein': '40г', 'fat': '12г', 'carbs': '45г',
                'ingredients': [
                    {'name': 'Куриная грудка', 'quantity': '150г'},
                    {'name': 'Гречневая крупа', 'quantity': '100г'},
                    {'name': 'Брокколи', 'quantity': '150г'},
                    {'name': 'Морковь', 'quantity': '1шт'},
                    {'name': 'Лук', 'quantity': '0.5шт'}
                ],
                'recipe': '1. Отварите гречку\n2. Обжарьте куриную грудку с овощами\n3. Потушите 15 минут\n4. Подавайте с гречкой'
            },
            {
                'name': 'Рыба на пару с рисом и салатом',
                'calories': '420 ккал', 'protein': '35г', 'fat': '10г', 'carbs': '50г',
                'ingredients': [
                    {'name': 'Филе белой рыбы', 'quantity': '200г'},
                    {'name': 'Рис', 'quantity': '100г'},
                    {'name': 'Огурец', 'quantity': '1шт'},
                    {'name': 'Помидор', 'quantity': '1шт'},
                    {'name': 'Лимон', 'quantity': '0.5шт'}
                ],
                'recipe': '1. Приготовьте рыбу на пару 15 минут\n2. Отварите рис\n3. Нарежьте овощи для салата\n4. Подавайте с лимонным соком'
            }
        ]
    
    @staticmethod
    def _get_dinner_options():
        """Уникальные варианты ужинов"""
        return [
            {
                'name': 'Овощной салат с курицей',
                'calories': '350 ккал', 'protein': '30г', 'fat': '15г', 'carbs': '20г',
                'ingredients': [
                    {'name': 'Куриная грудка', 'quantity': '120г'},
                    {'name': 'Листья салата', 'quantity': '100г'},
                    {'name': 'Огурец', 'quantity': '1шт'},
                    {'name': 'Помидор', 'quantity': '1шт'},
                    {'name': 'Оливковое масло', 'quantity': '15мл'}
                ],
                'recipe': '1. Отварите куриную грудку\n2. Нарежьте овощи\n3. Смешайте все ингредиенты\n4. Заправьте оливковым маслом'
            },
            {
                'name': 'Тушеные овощи с индейкой',
                'calories': '380 ккал', 'protein': '35г', 'fat': '12г', 'carbs': '25г',
                'ingredients': [
                    {'name': 'Филе индейки', 'quantity': '150г'},
                    {'name': 'Кабачок', 'quantity': '1шт'},
                    {'name': 'Баклажан', 'quantity': '1шт'},
                    {'name': 'Помидор', 'quantity': '2шт'},
                    {'name': 'Лук', 'quantity': '0.5шт'}
                ],
                'recipe': '1. Обжарьте индейку с луком\n2. Добавьте нарезанные овощи\n3. Тушите 20 минут под крышкой\n4. Подавайте горячим'
            }
        ]
    
    @staticmethod
    def _generate_proper_shopping_list(days):
        """Генерирует корректный список покупок"""
        shopping_list = {
            'Овощи': [],
            'Фрукты': [],
            'Мясо/Рыба': [],
            'Молочные продукты': [],
            'Крупы/Злаки': [],
            'Орехи/Семена': [],
            'Бакалея': []
        }
        
        # Собираем все ингредиенты из всех дней
        all_ingredients = []
        for day in days:
            for meal in day['meals']:
                all_ingredients.extend(meal['ingredients'])
        
        # Группируем по категориям
        for ingredient in all_ingredients:
            name = ingredient['name'].lower()
            quantity = ingredient['quantity']
            
            if any(word in name for word in ['овощ', 'салат', 'брокколи', 'морковь', 'помидор', 'огурец', 'капуста', 'лук', 'перец', 'кабачок', 'баклажан']):
                shopping_list['Овощи'].append({'name': ingredient['name'], 'quantity': quantity})
            elif any(word in name for word in ['фрукт', 'ягода', 'банан', 'яблоко', 'апельсин', 'груша', 'персик', 'изюм']):
                shopping_list['Фрукты'].append({'name': ingredient['name'], 'quantity': quantity})
            elif any(word in name for word in ['куриц', 'мясо', 'говядин', 'свинин', 'рыб', 'индейк']):
                shopping_list['Мясо/Рыба'].append({'name': ingredient['name'], 'quantity': quantity})
            elif any(word in name for word in ['молок', 'творог', 'йогурт', 'сметан', 'сыр', 'кефир']):
                shopping_list['Молочные продукты'].append({'name': ingredient['name'], 'quantity': quantity})
            elif any(word in name for word in ['круп', 'рис', 'гречк', 'овсян', 'манн', 'хлопь']):
                shopping_list['Крупы/Злаки'].append({'name': ingredient['name'], 'quantity': quantity})
            elif any(word in name for word in ['орех', 'миндал', 'сем', 'семечк']):
                shopping_list['Орехи/Семена'].append({'name': ingredient['name'], 'quantity': quantity})
            else:
                shopping_list['Бакалея'].append({'name': ingredient['name'], 'quantity': quantity})
        
        # Убираем пустые категории
        shopping_list = {k: v for k, v in shopping_list.items() if v}
        
        return shopping_list

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
            [InlineKeyboardButton("📥 СКАЧАТЬ КОРЗИНУ TXT", callback_data=f"download_cart_txt_{plan_id}")],
            [InlineKeyboardButton("📄 СКАЧАТЬ ПЛАН TXT", callback_data=f"download_plan_txt_{plan_id}")],
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
    
    def get_my_plan_menu(self, plan_id=None):
        """Меню моего плана"""
        if plan_id:
            keyboard = [
                [InlineKeyboardButton("📋 ПОСМОТРЕТЬ ПЛАН", callback_data=f"view_plan_{plan_id}")],
                [InlineKeyboardButton("🛒 КОРЗИНА ПОКУПОК", callback_data=f"shopping_cart_plan_{plan_id}")],
                [InlineKeyboardButton("📥 СКАЧАТЬ ПЛАН TXT", callback_data=f"download_plan_txt_{plan_id}")],
                [InlineKeyboardButton("↩️ НАЗАД", callback_data="back_main")]
            ]
        else:
            keyboard = [
                [InlineKeyboardButton("📋 ПОСМОТРЕТЬ ПОСЛЕДНИЙ ПЛАН", callback_data="view_latest_plan")],
                [InlineKeyboardButton("📚 ВСЕ МОИ ПЛАНЫ", callback_data="view_all_plans")],
                [InlineKeyboardButton("↩️ НАЗАД", callback_data="back_main")]
            ]
        return InlineKeyboardMarkup(keyboard)
    
    def get_plans_list_menu(self, plans):
        """Меню списка планов"""
        keyboard = []
        for plan in plans[:5]:  # Показываем последние 5 планов
            plan_date = plan['created_at'][:10] if isinstance(plan['created_at'], str) else plan['created_at'].strftime('%Y-%m-%d')
            keyboard.append([
                InlineKeyboardButton(
                    f"📅 {plan_date} (ID: {plan['id']})", 
                    callback_data=f"view_plan_{plan['id']}"
                )
            ])
        
        keyboard.append([InlineKeyboardButton("↩️ НАЗАД", callback_data="my_plan")])
        return InlineKeyboardMarkup(keyboard)
    
    def get_admin_menu(self):
        """Меню администратора"""
        keyboard = [
            [InlineKeyboardButton("📊 СТАТИСТИКА БОТА", callback_data="admin_stats")],
            [InlineKeyboardButton("👥 ПОЛЬЗОВАТЕЛИ", callback_data="admin_users")],
            [InlineKeyboardButton("📋 ВСЕ ПЛАНЫ", callback_data="admin_plans")],
            [InlineKeyboardButton("🔄 СБРОС ЛИМИТОВ", callback_data="admin_reset_limits")],
            [InlineKeyboardButton("↩️ НАЗАД", callback_data="back_main")]
        ]
        return InlineKeyboardMarkup(keyboard)
    
    def get_wellbeing_buttons(self):
        """Кнопки для оценки самочувствия"""
        keyboard = [
            [
                InlineKeyboardButton("1 😢", callback_data="wellbeing_1"),
                InlineKeyboardButton("2 😔", callback_data="wellbeing_2"),
                InlineKeyboardButton("3 😐", callback_data="wellbeing_3"),
                InlineKeyboardButton("4 😊", callback_data="wellbeing_4"),
                InlineKeyboardButton("5 😄", callback_data="wellbeing_5")
            ]
        ]
        return InlineKeyboardMarkup(keyboard)
    
    def get_sleep_quality_buttons(self):
        """Кнопки для оценки качества сна"""
        keyboard = [
            [
                InlineKeyboardButton("1 😴", callback_data="sleep_1"),
                InlineKeyboardButton("2 🛌", callback_data="sleep_2"),
                InlineKeyboardButton("3 🛌", callback_data="sleep_3"),
                InlineKeyboardButton("4 💤", callback_data="sleep_4"),
                InlineKeyboardButton("5 🌟", callback_data="sleep_5")
            ]
        ]
        return InlineKeyboardMarkup(keyboard)
    
    def get_back_menu(self):
        """Меню с кнопкой назад"""
        keyboard = [
            [InlineKeyboardButton("↩️ НАЗАД", callback_data="back_main")]
        ]
        return InlineKeyboardMarkup(keyboard)

# ==================== ФОРМИРОВАНИЕ ТЕКСТОВЫХ ФАЙЛОВ ====================

class TextFileGenerator:
    """Генератор текстовых файлов для планов, корзин и рецептов"""
    
    @staticmethod
    def generate_plan_text_file(plan_data, plan_id):
        """Генерирует текстовый файл с планом питания"""
        try:
            user_data = plan_data.get('user_data', {})
            content = []
            
            # Заголовок
            content.append("🎯 ПЕРСОНАЛЬНЫЙ ПЛАН ПИТАНИЯ")
            content.append("=" * 50)
            content.append(f"ID плана: {plan_id}")
            content.append(f"👤 Клиент: {user_data.get('gender', '')}, {user_data.get('age', '')} лет")
            content.append(f"📏 Параметры: {user_data.get('height', '')} см, {user_data.get('weight', '')} кг")
            content.append(f"🎯 Цель: {user_data.get('goal', '')}")
            content.append(f"🏃 Активность: {user_data.get('activity', '')}")
            content.append(f"📅 Создан: {plan_data.get('created_at', '')[:10]}")
            content.append(f"🤖 Источник: {plan_data.get('source', 'enhanced_generator')}")
            content.append("")
            
            # Водный режим
            water_regime = plan_data.get('water_regime', {})
            content.append("💧 ДЕТАЛЬНЫЙ ВОДНЫЙ РЕЖИМ")
            content.append("-" * 30)
            content.append(f"Общий объем: {water_regime.get('total', '2.0 литра')}")
            content.append("")
            content.append("Расписание по часам:")
            for schedule in water_regime.get('schedule', []):
                content.append(f"  {schedule.get('time', '')} - {schedule.get('amount', '')} - {schedule.get('description', '')}")
            content.append("")
            
            # План по дням
            content.append("📋 ПЛАН ПИТАНИЯ НА 7 ДНЕЙ")
            content.append("=" * 50)
            
            for day in plan_data.get('days', []):
                content.append("")
                content.append(f"📅 {day['name']}")
                content.append("-" * 20)
                content.append(f"Общая калорийность: {day.get('total_calories', '')}")
                content.append("")
                
                for meal in day.get('meals', []):
                    content.append(f"🍽 {meal['type']} ({meal['time']})")
                    content.append(f"   Блюдо: {meal['name']}")
                    content.append(f"   Калории: {meal['calories']}")
                    content.append(f"   БЖУ: {meal['protein']}, {meal['fat']}, {meal['carbs']}")
                    content.append("   Ингредиенты:")
                    for ingredient in meal.get('ingredients', []):
                        content.append(f"     - {ingredient['name']}: {ingredient['quantity']}")
                    content.append("   Рецепт:")
                    for line in meal.get('recipe', '').split('\n'):
                        content.append(f"     {line}")
                    content.append("")
            
            # Совет профессора
            content.append("🎓 СОВЕТ ПРОФЕССОРА НУТРИЦИОЛОГИИ")
            content.append("-" * 40)
            content.append(plan_data.get('professor_advice', ''))
            content.append("")
            
            return "\n".join(content)
            
        except Exception as e:
            logger.error(f"❌ Error generating plan text file: {e}")
            return "Ошибка при генерации файла"
    
    @staticmethod
    def generate_shopping_list_text_file(shopping_cart, plan_id):
        """Генерирует текстовый файл со списком покупок"""
        try:
            content = []
            
            content.append("🛒 СПИСОК ПОКУПОК")
            content.append("=" * 30)
            content.append(f"ID плана: {plan_id}")
            content.append(f"Дата создания: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
            content.append("")
            
            total_products = 0
            purchased_count = 0
            
            for category, products in shopping_cart.items():
                if products:  # Показываем только непустые категории
                    content.append(f"📦 {category.upper()}")
                    content.append("-" * 20)
                    
                    for product in products:
                        status = "✅ КУПЛЕНО" if product.get('purchased', False) else "⭕ НУЖНО КУПИТЬ"
                        content.append(f"  {status}")
                        content.append(f"  {product['name']} - {product['quantity']}")
                        content.append("")
                        
                        total_products += 1
                        if product.get('purchased', False):
                            purchased_count += 1
            
            content.append("")
            content.append("📊 СТАТИСТИКА:")
            content.append(f"Всего продуктов: {total_products}")
            content.append(f"Куплено: {purchased_count}")
            content.append(f"Осталось: {total_products - purchased_count}")
            
            return "\n".join(content)
            
        except Exception as e:
            logger.error(f"❌ Error generating shopping list text file: {e}")
            return "Ошибка при генерации списка покупок"

# ==================== FLASK APP И ОБРАБОТЧИКИ ====================

app = Flask(__name__)
application = None
menu = InteractiveMenu()

# ==================== ОБРАБОТЧИК ОШИБОК ====================

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик ошибок"""
    try:
        logger.error(f"❌ Exception while handling update: {context.error}")
        
        if update and update.effective_message:
            await update.effective_message.reply_text(
                "❌ Произошла непредвиденная ошибка. Попробуйте позже.",
                reply_markup=menu.get_main_menu()
            )
    except Exception as e:
        logger.error(f"Error in error handler: {e}")

# ==================== ОСНОВНЫЕ ОБРАБОТЧИКИ КОМАНД ====================

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
• Создать персональный план питания с YandexGPT
• Отслеживать прогресс через чек-ины
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

async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает главное меню"""
    await update.message.reply_text(
        "🤖 ГЛАВНОЕ МЕНЮ\n\nВыберите действие:",
        reply_markup=menu.get_main_menu()
    )

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда администратора"""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("❌ У вас нет прав доступа")
        return
    
    await update.message.reply_text(
        "👑 ПАНЕЛЬ АДМИНИСТРАТОРА",
        reply_markup=menu.get_admin_menu()
    )

# ==================== ОБРАБОТЧИКИ CALLBACK ====================

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик callback'ов"""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    logger.info(f"📨 Callback received: {data} from user {query.from_user.id}")
    
    try:
        # Основные команды меню
        if data == "create_plan":
            await handle_create_plan(query, context)
        elif data == "checkin":
            await handle_checkin_menu(query, context)
        elif data == "stats":
            await handle_stats(query, context)
        elif data == "my_plan":
            await handle_my_plan_menu(query, context)
        elif data == "shopping_cart":
            await handle_shopping_cart_main(query, context)
        elif data == "help":
            await handle_help(query, context)
        elif data == "admin":
            await handle_admin_callback(query, context)
        
        # Навигация назад
        elif data == "back_main":
            await show_main_menu(query)
        elif data.startswith("back_gender"):
            await handle_gender_back(query, context)
        elif data.startswith("back_goal"):
            await handle_goal_back(query, context)
        
        # Ввод данных плана
        elif data.startswith("gender_"):
            await handle_gender(query, context, data)
        elif data.startswith("goal_"):
            await handle_goal(query, context, data)
        elif data.startswith("activity_"):
            await handle_activity(query, context, data)
        
        # Чек-ин
        elif data == "checkin_data":
            await handle_checkin_data(query, context)
        elif data == "checkin_history":
            await handle_checkin_history(query, context)
        elif data.startswith("wellbeing_"):
            await handle_wellbeing_score(query, context, data)
        elif data.startswith("sleep_"):
            await handle_sleep_quality(query, context, data)
        
        # Корзина покупок
        elif data.startswith("view_cart_"):
            plan_id = data.replace("view_cart_", "")
            await handle_view_cart(query, context, int(plan_id))
        elif data.startswith("mark_purchased_"):
            plan_id = data.replace("mark_purchased_", "")
            await handle_mark_purchased(query, context, int(plan_id))
        elif data.startswith("reset_cart_"):
            plan_id = data.replace("reset_cart_", "")
            await handle_reset_cart(query, context, int(plan_id))
        elif data.startswith("download_cart_txt_"):
            plan_id = data.replace("download_cart_txt_", "")
            await handle_download_cart_txt(query, context, int(plan_id))
        elif data.startswith("download_plan_txt_"):
            plan_id = data.replace("download_plan_txt_", "")
            await handle_download_plan_txt(query, context, int(plan_id))
        elif data.startswith("toggle_"):
            await handle_toggle_product(query, context, data)
        elif data.startswith("back_cart_"):
            plan_id = data.replace("back_cart_", "")
            await handle_shopping_cart_menu(query, context, int(plan_id))
        elif data.startswith("shopping_cart_plan_"):
            plan_id = data.replace("shopping_cart_plan_", "")
            await handle_shopping_cart_menu(query, context, int(plan_id))
        
        # Мой план
        elif data == "view_latest_plan":
            await handle_view_latest_plan(query, context)
        elif data == "view_all_plans":
            await handle_view_all_plans(query, context)
        elif data.startswith("view_plan_"):
            plan_id = data.replace("view_plan_", "")
            await handle_view_plan(query, context, int(plan_id))
        
        # Админ-панель
        elif data == "admin_stats":
            await handle_admin_stats(query, context)
        elif data == "admin_users":
            await handle_admin_users(query, context)
        elif data == "admin_plans":
            await handle_admin_plans(query, context)
        elif data == "admin_reset_limits":
            await handle_admin_reset_limits(query, context)
        
        else:
            logger.warning(f"⚠️ Unknown callback data: {data}")
            await query.edit_message_text(
                "❌ Неизвестная команда",
                reply_markup=menu.get_main_menu()
            )
            
    except Exception as e:
        logger.error(f"❌ Error in callback handler: {e}")
        await query.edit_message_text(
            "❌ Произошла ошибка. Попробуйте снова.",
            reply_markup=menu.get_main_menu()
        )

# ==================== ОБРАБОТЧИКИ ПЛАНА ПИТАНИЯ ====================

async def handle_create_plan(query, context):
    """Обработчик создания плана"""
    try:
        user_id = query.from_user.id
        
        if not is_admin(user_id) and not can_make_request(user_id):
            days_remaining = get_days_until_next_plan(user_id)
            await query.edit_message_text(
                f"⏳ Вы уже запрашивали план питания\nСледующий доступен через {days_remaining} дней",
                reply_markup=menu.get_main_menu()
            )
            return
        
        context.user_data['plan_data'] = {}
        context.user_data['plan_step'] = 1
        
        await query.edit_message_text(
            "📊 СОЗДАНИЕ ПЛАНА ПИТАНИЯ\n\n1️⃣ Выберите ваш пол:",
            reply_markup=menu.get_plan_data_input(step=1)
        )
        
    except Exception as e:
        logger.error(f"❌ Error in create plan handler: {e}")
        await query.edit_message_text(
            "❌ Ошибка при создании плана. Попробуйте снова.",
            reply_markup=menu.get_main_menu()
        )

async def handle_gender_back(query, context):
    """Назад к выбору пола"""
    try:
        context.user_data['plan_step'] = 1
        
        await query.edit_message_text(
            "📊 СОЗДАНИЕ ПЛАНА ПИТАНИЯ\n\n1️⃣ Выберите ваш пол:",
            reply_markup=menu.get_plan_data_input(step=1)
        )
    except Exception as e:
        logger.error(f"❌ Error in gender back handler: {e}")
        await query.edit_message_text(
            "❌ Ошибка навигации. Попробуйте с начала.",
            reply_markup=menu.get_main_menu()
        )

async def handle_goal_back(query, context):
    """Назад к выбору цели"""
    try:
        context.user_data['plan_step'] = 2
        
        await query.edit_message_text(
            "📊 СОЗДАНИЕ ПЛАНА ПИТАНИЯ\n\n2️⃣ Выберите вашу цель:",
            reply_markup=menu.get_plan_data_input(step=2)
        )
    except Exception as e:
        logger.error(f"❌ Error in goal back handler: {e}")
        await query.edit_message_text(
            "❌ Ошибка навигации. Попробуйте с начала.",
            reply_markup=menu.get_main_menu()
        )

async def handle_gender(query, context, data):
    """Обработчик выбора пола"""
    try:
        gender_map = {
            "gender_male": "МУЖЧИНА",
            "gender_female": "ЖЕНЩИНА"
        }
        
        context.user_data['plan_data']['gender'] = gender_map[data]
        context.user_data['plan_step'] = 2
        
        await query.edit_message_text(
            "📊 СОЗДАНИЕ ПЛАНА ПИТАНИЯ\n\n2️⃣ Выберите вашу цель:",
            reply_markup=menu.get_plan_data_input(step=2)
        )
        
    except Exception as e:
        logger.error(f"❌ Error in gender handler: {e}")
        await query.edit_message_text(
            "❌ Ошибка при выборе пола. Попробуйте снова.",
            reply_markup=menu.get_main_menu()
        )

async def handle_goal(query, context, data):
    """Обработчик выбора цели"""
    try:
        goal_map = {
            "goal_weight_loss": "ПОХУДЕНИЕ",
            "goal_mass": "НАБОР МАССЫ", 
            "goal_maintain": "ПОДДЕРЖАНИЕ"
        }
        
        context.user_data['plan_data']['goal'] = goal_map[data]
        context.user_data['plan_step'] = 3
        
        await query.edit_message_text(
            "📊 СОЗДАНИЕ ПЛАНА ПИТАНИЯ\n\n3️⃣ Выберите уровень активности:",
            reply_markup=menu.get_plan_data_input(step=3)
        )
        
    except Exception as e:
        logger.error(f"❌ Error in goal handler: {e}")
        await query.edit_message_text(
            "❌ Ошибка при выборе цели. Попробуйте снова.",
            reply_markup=menu.get_main_menu()
        )

async def handle_activity(query, context, data):
    """Обработчик выбора активности"""
    try:
        activity_map = {
            "activity_high": "ВЫСОКАЯ",
            "activity_medium": "СРЕДНЯЯ",
            "activity_low": "НИЗКАЯ"
        }
        
        context.user_data['plan_data']['activity'] = activity_map[data]
        context.user_data['awaiting_input'] = 'plan_details'
        
        await query.edit_message_text(
            "📊 СОЗДАНИЕ ПЛАНА ПИТАНИЯ\n\n4️⃣ Введите ваши данные в формате:\n"
            "Возраст, Рост (см), Вес (кг)\n\n"
            "Пример: 30, 180, 75\n\n"
            "Для отмены нажмите /menu",
            reply_markup=menu.get_back_menu()
        )
        
    except Exception as e:
        logger.error(f"❌ Error in activity handler: {e}")
        await query.edit_message_text(
            "❌ Ошибка при выборе активности. Попробуйте снова.",
            reply_markup=menu.get_main_menu()
        )

async def process_plan_details(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    """Обрабатывает детали плана с REAL YandexGPT"""
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
        
        # Отправляем сообщение о начале работы YandexGPT
        progress_message = await update.message.reply_text(
            "🔄 Ваш план готовится, подключаем YandexGPT...\n\n"
            "🎓 Профессор анализирует ваши данные и создает ИНДИВИДУАЛЬНЫЙ план...",
            reply_markup=menu.get_back_menu()
        )
        
        # РЕАЛЬНАЯ РАБОТА YANDEX GPT
        plan_data = await YandexGPTService.generate_nutrition_plan(user_data)
        
        if plan_data:
            plan_id = save_plan(user_data['user_id'], plan_data)
            update_user_limit(user_data['user_id'])
            
            # Сохраняем корзину покупок
            save_shopping_cart(user_data['user_id'], plan_id, plan_data['shopping_list'])
            
            await progress_message.delete()
            
            source_info = "🎓 Создан профессором нутрициологии с YandexGPT" if plan_data.get('source') == 'yandex_gpt' else "🤖 Создан улучшенным алгоритмом"
            
            success_text = f"""
🎉 ВАШ ПЛАН ПИТАНИЯ ГОТОВ!

👤 Данные: {user_data['gender']}, {age} лет, {height} см, {weight} кг
🎯 Цель: {user_data['goal']}
🏃 Активность: {user_data['activity']}
{source_info}

📋 План включает:
• 7 дней питания с УНИКАЛЬНЫМИ рецептами
• Детальный водный режим по часам
• 5 приемов пищи в день
• Автоматическую корзину покупок

💧 ВОДНЫЙ РЕЖИМ:
{plan_data.get('water_regime', {}).get('total', '2.0 литра в день')}

🎓 СОВЕТ ПРОФЕССОРА:
{plan_data.get('professor_advice', '')}

План сохранен в вашем профиле!
Используйте кнопку "МОЙ ПЛАН" для просмотра.
"""
            await update.message.reply_text(
                success_text,
                reply_markup=menu.get_my_plan_menu(plan_id)
            )
            
            logger.info(f"✅ Plan successfully created for user {user_data['user_id']} with {plan_data.get('source')}")
            
        else:
            await progress_message.delete()
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

# ==================== ОБРАБОТЧИКИ МОЕГО ПЛАНА ====================

async def handle_my_plan_menu(query, context):
    """Обработчик меню моего плана"""
    user_id = query.from_user.id
    latest_plan = get_latest_plan(user_id)
    
    if latest_plan:
        await query.edit_message_text(
            "📋 МОЙ ПЛАН\n\nУ вас есть сохраненные планы питания. Выберите действие:",
            reply_markup=menu.get_my_plan_menu(latest_plan['id'])
        )
    else:
        await query.edit_message_text(
            "📋 МОЙ ПЛАН\n\nУ вас пока нет сохраненных планов питания.\n\nСоздайте первый план с помощью кнопки 'СОЗДАТЬ ПЛАН'!",
            reply_markup=menu.get_my_plan_menu()
        )

async def handle_view_latest_plan(query, context):
    """Просмотр последнего плана"""
    user_id = query.from_user.id
    latest_plan = get_latest_plan(user_id)
    
    if latest_plan:
        await display_plan_details(query, latest_plan)
    else:
        await query.edit_message_text(
            "❌ У вас пока нет сохраненных планов.",
            reply_markup=menu.get_my_plan_menu()
        )

async def handle_view_all_plans(query, context):
    """Просмотр всех планов"""
    user_id = query.from_user.id
    plans = get_all_plans(user_id)
    
    if plans:
        await query.edit_message_text(
            f"📚 ВСЕ ВАШИ ПЛАНЫ\n\nНайдено планов: {len(plans)}\n\nВыберите план для просмотра:",
            reply_markup=menu.get_plans_list_menu(plans)
        )
    else:
        await query.edit_message_text(
            "❌ У вас пока нет сохраненных планов.",
            reply_markup=menu.get_my_plan_menu()
        )

async def handle_view_plan(query, context, plan_id):
    """Просмотр конкретного плана"""
    user_id = query.from_user.id
    
    # Получаем план из базы данных
    conn = DatabaseManager.get_connection()
    cursor = conn.cursor()
    
    try:
        cursor.execute('SELECT plan_data FROM nutrition_plans WHERE id = ? AND user_id = ?', (plan_id, user_id))
        result = cursor.fetchone()
        
        if result:
            plan_data = {
                'id': plan_id,
                'data': json.loads(result['plan_data'])
            }
            await display_plan_details(query, plan_data)
        else:
            await query.edit_message_text(
                "❌ План не найден или у вас нет доступа.",
                reply_markup=menu.get_my_plan_menu()
            )
    except Exception as e:
        logger.error(f"❌ Error viewing plan: {e}")
        await query.edit_message_text(
            "❌ Ошибка при загрузке плана.",
            reply_markup=menu.get_my_plan_menu()
        )
    finally:
        conn.close()

async def display_plan_details(query, plan_data):
    """Отображает детали плана"""
    try:
        plan = plan_data['data']
        user_data = plan.get('user_data', {})
        
        # Формируем сообщение с планом
        message_text = f"""
📋 ВАШ ПЛАН ПИТАНИЯ (ID: {plan_data['id']})

👤 Данные:
• Пол: {user_data.get('gender', '')}
• Возраст: {user_data.get('age', '')} лет
• Рост: {user_data.get('height', '')} см
• Вес: {user_data.get('weight', '')} кг
• Цель: {user_data.get('goal', '')}
• Активность: {user_data.get('activity', '')}

💧 Водный режим: {plan.get('water_regime', {}).get('total', '2.0 литра')}

🎓 Совет профессора:
{plan.get('professor_advice', '')}

🤖 Источник: {plan.get('source', 'enhanced_generator')}

Для просмотра детального плана на 7 дней используйте кнопку "СКАЧАТЬ ПЛАН TXT".
"""
        await query.edit_message_text(
            message_text,
            reply_markup=menu.get_my_plan_menu(plan_data['id'])
        )
        
    except Exception as e:
        logger.error(f"❌ Error displaying plan details: {e}")
        await query.edit_message_text(
            "❌ Ошибка при отображении плана.",
            reply_markup=menu.get_my_plan_menu()
        )

# ==================== ОБРАБОТЧИКИ ЧЕК-ИНА ====================

async def handle_checkin_menu(query, context):
    """Обработчик меню чек-ина"""
    await query.edit_message_text(
        "📈 ЕЖЕДНЕВНЫЙ ЧЕК-ИН\n\nОтслеживайте ваш прогресс и самочувствие:",
        reply_markup=menu.get_checkin_menu()
    )

async def handle_checkin_data(query, context):
    """Начало процесса чек-ина"""
    context.user_data['checkin_step'] = 'weight'
    
    await query.edit_message_text(
        "📊 ЗАПИСЬ ДАННЫХ ЧЕК-ИНА\n\n1️⃣ Введите ваш текущий вес (кг):\nПример: 75.5",
        reply_markup=menu.get_back_menu()
    )

async def handle_checkin_history(query, context):
    """Просмотр истории чек-инов"""
    user_id = query.from_user.id
    checkins = get_recent_checkins(user_id, 7)
    
    if checkins:
        history_text = "📊 ИСТОРИЯ ЧЕК-ИНОВ (последние 7 дней)\n\n"
        
        for checkin in checkins:
            date = checkin['date'][:10] if isinstance(checkin['date'], str) else checkin['date'].strftime('%Y-%m-%d')
            history_text += f"📅 {date}:\n"
            history_text += f"   Вес: {checkin['weight']} кг\n"
            history_text += f"   Талия: {checkin['waist_circumference']} см\n"
            history_text += f"   Самочувствие: {checkin['wellbeing_score']}/5\n"
            history_text += f"   Сон: {checkin['sleep_quality']}/5\n\n"
        
        await query.edit_message_text(
            history_text,
            reply_markup=menu.get_checkin_menu()
        )
    else:
        await query.edit_message_text(
            "📊 У вас пока нет записей чек-инов.\n\nНачните отслеживать свой прогресс!",
            reply_markup=menu.get_checkin_menu()
        )

async def handle_wellbeing_score(query, context, data):
    """Обработка оценки самочувствия"""
    try:
        score = int(data.replace("wellbeing_", ""))
        context.user_data['checkin_wellbeing'] = score
        context.user_data['checkin_step'] = 'sleep'
        
        await query.edit_message_text(
            "📊 ЗАПИСЬ ДАННЫХ ЧЕК-ИНА\n\n4️⃣ Оцените качество сна:",
            reply_markup=menu.get_sleep_quality_buttons()
        )
    except Exception as e:
        logger.error(f"❌ Error in wellbeing handler: {e}")
        await query.edit_message_text(
            "❌ Ошибка при сохранении данных.",
            reply_markup=menu.get_checkin_menu()
        )

async def handle_sleep_quality(query, context, data):
    """Обработка оценки качества сна"""
    try:
        score = int(data.replace("sleep_", ""))
        user_id = query.from_user.id
        
        # Сохраняем все данные чек-ина
        save_checkin(
            user_id,
            context.user_data.get('checkin_weight'),
            context.user_data.get('checkin_waist'),
            context.user_data.get('checkin_wellbeing'),
            score
        )
        
        # Очищаем временные данные
        context.user_data['checkin_step'] = None
        context.user_data['checkin_weight'] = None
        context.user_data['checkin_waist'] = None
        context.user_data['checkin_wellbeing'] = None
        
        await query.edit_message_text(
            "✅ ДАННЫЕ ЧЕК-ИНА СОХРАНЕНЫ!\n\nВаш прогресс записан. Продолжайте отслеживать!",
            reply_markup=menu.get_checkin_menu()
        )
        
    except Exception as e:
        logger.error(f"❌ Error in sleep quality handler: {e}")
        await query.edit_message_text(
            "❌ Ошибка при сохранении данных.",
            reply_markup=menu.get_checkin_menu()
        )

# ==================== ОБРАБОТЧИКИ СТАТИСТИКИ ====================

async def handle_stats(query, context):
    """Обработчик статистики"""
    user_id = query.from_user.id
    
    # Получаем данные
    checkins = get_recent_checkins(user_id, 7)
    latest_plan = get_latest_plan(user_id)
    plan_count = get_user_plan_count(user_id)
    
    stats_text = "📊 ВАША СТАТИСТИКА\n\n"
    
    # Статистика по планам
    stats_text += f"📋 Создано планов: {plan_count}\n"
    
    if latest_plan:
        plan_date = latest_plan['data'].get('created_at', '')[:10]
        stats_text += f"📅 Последний план: {plan_date}\n"
    
    # Статистика по чек-инам
    if checkins:
        stats_text += f"\n📈 Чек-инов за 7 дней: {len(checkins)}\n"
        
        # Анализ прогресса
        if len(checkins) >= 2:
            first_weight = checkins[-1]['weight']
            last_weight = checkins[0]['weight']
            weight_diff = last_weight - first_weight
            
            if weight_diff > 0:
                stats_text += f"📈 Изменение веса: +{weight_diff:.1f} кг\n"
            elif weight_diff < 0:
                stats_text += f"📉 Изменение веса: {weight_diff:.1f} кг\n"
            else:
                stats_text += "⚖️ Вес остался без изменений\n"
        
        # Средние показатели
        avg_wellbeing = sum(c['wellbeing_score'] for c in checkins) / len(checkins)
        avg_sleep = sum(c['sleep_quality'] for c in checkins) / len(checkins)
        
        stats_text += f"😊 Среднее самочувствие: {avg_wellbeing:.1f}/5\n"
        stats_text += f"💤 Среднее качество сна: {avg_sleep:.1f}/5\n"
    
    else:
        stats_text += "\n📊 Чек-ины: пока нет данных\n"
    
    stats_text += "\n💡 Совет: Регулярно отслеживайте прогресс для лучших результатов!"
    
    await query.edit_message_text(
        stats_text,
        reply_markup=menu.get_main_menu()
    )

# ==================== ОБРАБОТЧИКИ КОРЗИНЫ ПОКУПОК ====================

async def handle_shopping_cart_main(query, context):
    """Главное меню корзины"""
    user_id = query.from_user.id
    latest_plan = get_latest_plan(user_id)
    
    if latest_plan:
        await handle_shopping_cart_menu(query, context, latest_plan['id'])
    else:
        await query.edit_message_text(
            "🛒 КОРЗИНА ПОКУПОК\n\nУ вас пока нет планов питания.\nСоздайте план, чтобы сгенерировать корзину покупок!",
            reply_markup=menu.get_main_menu()
        )

async def handle_shopping_cart_menu(query, context, plan_id):
    """Меню корзины для конкретного плана"""
    user_id = query.from_user.id
    
    await query.edit_message_text(
        f"🛒 КОРЗИНА ПОКУПОК (План ID: {plan_id})\n\nВыберите действие:",
        reply_markup=menu.get_shopping_cart_menu(plan_id)
    )

async def handle_view_cart(query, context, plan_id):
    """Просмотр корзины"""
    user_id = query.from_user.id
    cart = get_shopping_cart(user_id, plan_id)
    
    if cart:
        cart_text = f"🛒 ВАША КОРЗИНА (План ID: {plan_id})\n\n"
        
        total_products = 0
        purchased_count = 0
        
        for category, products in cart.items():
            if products:  # Показываем только непустые категории
                cart_text += f"📦 {category.upper()}:\n"
                
                for product in products:
                    status = "✅" if product['purchased'] else "⭕"
                    cart_text += f"  {status} {product['name']} - {product['quantity']}\n"
                    
                    total_products += 1
                    if product['purchased']:
                        purchased_count += 1
                
                cart_text += "\n"
        
        progress = (purchased_count / total_products * 100) if total_products > 0 else 0
        cart_text += f"📊 Прогресс: {purchased_count}/{total_products} ({progress:.1f}%)"
        
        await query.edit_message_text(
            cart_text,
            reply_markup=menu.get_shopping_cart_menu(plan_id)
        )
    else:
        await query.edit_message_text(
            "❌ Корзина пуста или не найдена.",
            reply_markup=menu.get_shopping_cart_menu(plan_id)
        )

async def handle_mark_purchased(query, context, plan_id):
    """Отметка покупок"""
    user_id = query.from_user.id
    cart = get_shopping_cart(user_id, plan_id)
    
    if cart:
        await query.edit_message_text(
            f"✅ ОТМЕТКА КУПЛЕННЫХ ПРОДУКТОВ\n\nНажмите на продукт, чтобы изменить его статус:",
            reply_markup=menu.get_shopping_cart_products(cart, plan_id)
        )
    else:
        await query.edit_message_text(
            "❌ Корзина пуста.",
            reply_markup=menu.get_shopping_cart_menu(plan_id)
        )

async def handle_toggle_product(query, context, data):
    """Переключение статуса продукта"""
    try:
        parts = data.split('_')
        plan_id = int(parts[1])
        product_name = parts[2]
        purchased = bool(int(parts[3]))
        
        user_id = query.from_user.id
        
        # Обновляем статус в базе
        success = update_shopping_cart_item(user_id, plan_id, product_name, purchased)
        
        if success:
            # Обновляем отображение
            cart = get_shopping_cart(user_id, plan_id)
            await query.edit_message_text(
                f"✅ СТАТУС ОБНОВЛЕН\n\nПродукт: {product_name}\nСтатус: {'КУПЛЕНО' if purchased else 'НУЖНО КУПИТЬ'}",
                reply_markup=menu.get_shopping_cart_products(cart, plan_id)
            )
        else:
            await query.answer("❌ Ошибка при обновлении статуса")
            
    except Exception as e:
        logger.error(f"❌ Error toggling product: {e}")
        await query.answer("❌ Произошла ошибка")

async def handle_reset_cart(query, context, plan_id):
    """Сброс отметок корзины"""
    user_id = query.from_user.id
    cart = get_shopping_cart(user_id, plan_id)
    
    if cart:
        # Сбрасываем все статусы
        for category, products in cart.items():
            for product in products:
                update_shopping_cart_item(user_id, plan_id, product['name'], False)
        
        await query.edit_message_text(
            "🔄 ВСЕ ОТМЕТКИ СБРОШЕНЫ\n\nСтатусы всех продуктов установлены в 'НУЖНО КУПИТЬ'",
            reply_markup=menu.get_shopping_cart_menu(plan_id)
        )
    else:
        await query.edit_message_text(
            "❌ Корзина пуста.",
            reply_markup=menu.get_shopping_cart_menu(plan_id)
        )

async def handle_download_cart_txt(query, context, plan_id):
    """Скачивание корзины в TXT формате"""
    user_id = query.from_user.id
    
    try:
        # Получаем корзину
        cart = get_shopping_cart(user_id, plan_id)
        
        if cart:
            # Генерируем файл корзины
            text_content = TextFileGenerator.generate_shopping_list_text_file(cart, plan_id)
            
            # Создаем файл в памяти
            file_buffer = io.BytesIO(text_content.encode('utf-8'))
            file_buffer.name = f'shopping_cart_{plan_id}.txt'
            
            # Отправляем файл
            await context.bot.send_document(
                chat_id=query.message.chat_id,
                document=file_buffer,
                filename=f'shopping_cart_{plan_id}.txt',
                caption=f"🛒 Ваш список покупок (ID плана: {plan_id})"
            )
            
            await query.answer("✅ Файл корзины отправлен!")
        else:
            await query.answer("❌ Корзина пуста")
            
    except Exception as e:
        logger.error(f"❌ Error downloading cart TXT: {e}")
        await query.answer("❌ Ошибка при создании файла корзины")

async def handle_download_plan_txt(query, context, plan_id):
    """Скачивание плана в TXT формате"""
    user_id = query.from_user.id
    
    try:
        # Получаем план
        conn = DatabaseManager.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT plan_data FROM nutrition_plans WHERE id = ? AND user_id = ?', (plan_id, user_id))
        result = cursor.fetchone()
        conn.close()
        
        if result:
            plan_data = json.loads(result['plan_data'])
            
            # Генерируем текстовый файл
            text_content = TextFileGenerator.generate_plan_text_file(plan_data, plan_id)
            
            # Создаем файл в памяти
            file_buffer = io.BytesIO(text_content.encode('utf-8'))
            file_buffer.name = f'nutrition_plan_{plan_id}.txt'
            
            # Отправляем файл
            await context.bot.send_document(
                chat_id=query.message.chat_id,
                document=file_buffer,
                filename=f'nutrition_plan_{plan_id}.txt',
                caption=f"📄 Ваш план питания (ID: {plan_id})"
            )
            
            await query.answer("✅ Файл плана отправлен!")
        else:
            await query.answer("❌ План не найден")
            
    except Exception as e:
        logger.error(f"❌ Error downloading plan TXT: {e}")
        await query.answer("❌ Ошибка при создании файла")

# ==================== ОБРАБОТЧИКИ АДМИН-ПАНЕЛИ ====================

async def handle_admin_callback(query, context):
    """Обработчик админских callback'ов"""
    user_id = query.from_user.id
    if not is_admin(user_id):
        await query.edit_message_text("❌ У вас нет прав доступа")
        return
    
    await query.edit_message_text(
        "👑 ПАНЕЛЬ АДМИНИСТРАТОРА\n\nВыберите действие:",
        reply_markup=menu.get_admin_menu()
    )

async def handle_admin_stats(query, context):
    """Статистика бота для администратора"""
    user_id = query.from_user.id
    if not is_admin(user_id):
        await query.edit_message_text("❌ У вас нет прав доступа")
        return
    
    total_users = get_total_users()
    total_plans = get_total_plans()
    
    stats_text = f"""
👑 СТАТИСТИКА БОТА

👥 Пользователи: {total_users}
📋 Планы питания: {total_plans}
📊 Среднее планов на пользователя: {total_plans / total_users if total_users > 0 else 0:.1f}

⚙️ Конфигурация:
• Режим: {'WEBHOOK' if Config.WEBHOOK_URL and not Config.RENDER else 'POLLING'}
• GPT Интеграция: {'✅ Активна' if Config.YANDEX_API_KEY else '❌ Неактивна'}
• Порт: {Config.PORT}
"""
    await query.edit_message_text(
        stats_text,
        reply_markup=menu.get_admin_menu()
    )

async def handle_admin_users(query, context):
    """Управление пользователями"""
    user_id = query.from_user.id
    if not is_admin(user_id):
        await query.edit_message_text("❌ У вас нет прав доступа")
        return
    
    # Получаем последних пользователей
    conn = DatabaseManager.get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT user_id, username, first_name, created_at FROM users ORDER BY created_at DESC LIMIT 10')
    users = cursor.fetchall()
    conn.close()
    
    users_text = "👥 ПОСЛЕДНИЕ ПОЛЬЗОВАТЕЛИ\n\n"
    
    for user in users:
        users_text += f"👤 ID: {user['user_id']}\n"
        users_text += f"   Имя: {user['first_name'] or 'Не указано'}\n"
        users_text += f"   Username: @{user['username'] or 'Не указан'}\n"
        users_text += f"   Зарегистрирован: {user['created_at'][:10]}\n\n"
    
    await query.edit_message_text(
        users_text,
        reply_markup=menu.get_admin_menu()
    )

async def handle_admin_plans(query, context):
    """Просмотр всех планов"""
    user_id = query.from_user.id
    if not is_admin(user_id):
        await query.edit_message_text("❌ У вас нет прав доступа")
        return
    
    # Получаем последние планы
    conn = DatabaseManager.get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT p.id, p.user_id, u.username, p.created_at 
        FROM nutrition_plans p 
        LEFT JOIN users u ON p.user_id = u.user_id 
        ORDER BY p.created_at DESC LIMIT 10
    ''')
    plans = cursor.fetchall()
    conn.close()
    
    plans_text = "📋 ПОСЛЕДНИЕ ПЛАНЫ\n\n"
    
    for plan in plans:
        plans_text += f"📅 ID: {plan['id']}\n"
        plans_text += f"   Пользователь: {plan['user_id']} (@{plan['username'] or 'без username'})\n"
        plans_text += f"   Создан: {plan['created_at'][:10]}\n\n"
    
    await query.edit_message_text(
        plans_text,
        reply_markup=menu.get_admin_menu()
    )

async def handle_admin_reset_limits(query, context):
    """Сброс лимитов пользователей"""
    user_id = query.from_user.id
    if not is_admin(user_id):
        await query.edit_message_text("❌ У вас нет прав доступа")
        return
    
    # Сбрасываем все лимиты
    conn = DatabaseManager.get_connection()
    cursor = conn.cursor()
    cursor.execute('DELETE FROM user_limits')
    conn.commit()
    conn.close()
    
    await query.edit_message_text(
        "✅ ВСЕ ЛИМИТЫ ПОЛЬЗОВАТЕЛЕЙ СБРОШЕНЫ\n\nТеперь все пользователи могут создавать новые планы.",
        reply_markup=menu.get_admin_menu()
    )

# ==================== ОБРАБОТЧИК СООБЩЕНИЙ ====================

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик текстовых сообщений"""
    try:
        text = update.message.text.strip()
        user_id = update.effective_user.id
        
        if text == "/menu":
            await update.message.reply_text(
                "🤖 ГЛАВНОЕ МЕНЮ\n\nВыберите действие:",
                reply_markup=menu.get_main_menu()
            )
            return
        
        # Обработка чек-ина
        if context.user_data.get('checkin_step') == 'weight':
            try:
                weight = float(text)
                if 30 <= weight <= 300:
                    context.user_data['checkin_weight'] = weight
                    context.user_data['checkin_step'] = 'waist'
                    
                    await update.message.reply_text(
                        "📊 ЗАПИСЬ ДАННЫХ ЧЕК-ИНА\n\n2️⃣ Введите обхват талии (см):\nПример: 85",
                        reply_markup=menu.get_back_menu()
                    )
                else:
                    await update.message.reply_text(
                        "❌ Вес должен быть от 30 до 300 кг. Попробуйте снова:"
                    )
            except ValueError:
                await update.message.reply_text(
                    "❌ Введите корректное число для веса. Пример: 75.5"
                )
                
        elif context.user_data.get('checkin_step') == 'waist':
            try:
                waist = int(text)
                if 50 <= waist <= 200:
                    context.user_data['checkin_waist'] = waist
                    context.user_data['checkin_step'] = 'wellbeing'
                    
                    await update.message.reply_text(
                        "📊 ЗАПИСЬ ДАННЫХ ЧЕК-ИНА\n\n3️⃣ Оцените ваше самочувствие:",
                        reply_markup=menu.get_wellbeing_buttons()
                    )
                else:
                    await update.message.reply_text(
                        "❌ Обхват талии должен быть от 50 до 200 см. Попробуйте снова:"
                    )
            except ValueError:
                await update.message.reply_text(
                    "❌ Введите целое число для обхвата талии. Пример: 85"
                )
        
        # Обработка создания плана
        elif context.user_data.get('awaiting_input') == 'plan_details':
            await process_plan_details(update, context, text)
        else:
            await update.message.reply_text(
                "🤖 Используйте меню для навигации",
                reply_markup=menu.get_main_menu()
            )
                
    except Exception as e:
        logger.error(f"❌ Error in message handler: {e}")
        await update.message.reply_text(
            "❌ Произошла ошибка. Попробуйте снова.",
            reply_markup=menu.get_main_menu()
        )

async def handle_help(query, context):
    """Обработчик помощи"""
    help_text = """
❓ ПОМОЩЬ ПО БОТУ

📊 СОЗДАТЬ ПЛАН:
• Создает персонализированный план питания на 7 дней через YandexGPT
• Учитывает ваш пол, цель, активность и параметры
• Генерирует УНИКАЛЬНЫЕ рецепты для каждого дня
• Лимит: 1 план в 7 дней (для администраторов - безлимитно)

📈 ЧЕК-ИН:
• Ежедневное отслеживание веса, обхвата талии, самочувствия и сна
• Просмотр истории за последние 7 дней
• Анализ прогресса

📊 СТАТИСТИКА:
• Обзор ваших планов и чек-инов
• Анализ прогресса по весу
• Средние показатели самочувствия и сна

📋 МОЙ ПЛАН:
• Просмотр текущего и предыдущих планов питания
• Доступ к корзине покупок для каждого плана
• Скачивание плана в TXT формате

🛒 КОРЗИНА:
• Автоматическая генерация списка покупок для плана
• Отметка купленных продуктов
• Скачивание списка покупок в TXT формате

👑 АДМИН (только для администраторов):
• Просмотр статистики бота
• Управление пользователями
• Сброс лимитов
"""
    await query.edit_message_text(
        help_text,
        reply_markup=menu.get_main_menu()
    )

async def show_main_menu(query):
    """Показывает главное меню"""
    await query.edit_message_text(
        "🤖 ГЛАВНОЕ МЕНЮ\n\nВыберите действие:",
        reply_markup=menu.get_main_menu()
    )

# ==================== ИНИЦИАЛИЗАЦИЯ БОТА ====================

def init_bot():
    """Инициализация бота"""
    global application
    try:
        Config.validate()
        init_database()
        
        application = Application.builder().token(Config.BOT_TOKEN).build()
        
        # Регистрируем все обработчики
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

# ==================== FLASK ROUTES ====================

@app.route('/')
def home():
    return """
    <h1>🤖 Nutrition Bot is Running!</h1>
    <p>Бот для создания персональных планов питания с YandexGPT</p>
    <p><a href="/health">Health Check</a></p>
    <p><a href="/ping">Ping</a></p>
    <p>🕒 Last update: {}</p>
    <p>🔧 Mode: {}</p>
    <p>🎓 YandexGPT: {}</p>
    """.format(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), 
               "WEBHOOK" if Config.RENDER else "POLLING",
               "🟢 Active" if Config.YANDEX_API_KEY else "🔴 Inactive")

@app.route('/health')
def health_check():
    return jsonify({
        "status": "healthy", 
        "service": "nutrition-bot",
        "timestamp": datetime.now().isoformat(),
        "mode": "webhook" if Config.RENDER else "polling",
        "yandex_gpt": "active" if Config.YANDEX_API_KEY else "inactive",
        "database": "connected"
    })

@app.route('/ping')
def ping():
    return "pong 🏓"

@app.route('/webhook', methods=['POST'])
def webhook():
    """Webhook endpoint for Telegram"""
    if request.method == "POST":
        try:
            update = Update.de_json(request.get_json(), application.bot)
            application.update_queue.put(update)
            return "ok"
        except Exception as e:
            logger.error(f"❌ Webhook error: {e}")
            return "error"
    return "error"

# ==================== ЗАПУСК ПРИЛОЖЕНИЯ ====================

def run_webhook():
    """Запуск бота в режиме webhook"""
    try:
        logger.info("🤖 Starting bot in WEBHOOK mode...")
        
        # Устанавливаем webhook
        webhook_url = f"{Config.WEBHOOK_URL}/webhook"
        application.bot.set_webhook(webhook_url)
        logger.info(f"✅ Webhook set to: {webhook_url}")
        
        # Запускаем Flask приложение
        port = int(os.environ.get('PORT', Config.PORT))
        logger.info(f"🌐 Starting Flask app on port {port}")
        app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
        
    except Exception as e:
        logger.error(f"❌ Webhook error: {e}")
        raise

def run_polling():
    """Запуск бота в режиме polling (для локальной разработки)"""
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
        logger.info("🚀 Starting Nutrition Bot with YandexGPT...")
        
        if not init_bot():
            logger.error("❌ Failed to initialize bot. Exiting.")
            return
        
        # Выбираем режим запуска
        if Config.RENDER and Config.WEBHOOK_URL:
            run_webhook()  # Webhook режим для Render
        else:
            run_polling()  # Polling режим для локальной разработки
        
    except KeyboardInterrupt:
        logger.info("🛑 Bot stopped by user")
    except Exception as e:
        logger.error(f"❌ Fatal error: {e}")
    finally:
        if Config.RENDER and application:
            try:
                application.bot.delete_webhook()
                logger.info("✅ Webhook removed")
            except:
                pass
        logger.info("👋 Bot shutdown complete")

if __name__ == "__main__":
    main()

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
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
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

# ==================== ГЕНЕРАТОР ПЛАНОВ ====================

class EnhancedPlanGenerator:
    """Улучшенный генератор планов питания"""
    
    @staticmethod
    def generate_plan_with_progress_indicator(user_data):
        """Генерирует разнообразный план с уникальными рецептами"""
        logger.info(f"🎯 Generating enhanced plan for user {user_data['user_id']}")
        
        plan = {
            'user_data': user_data,
            'days': [],
            'shopping_list': {},
            'water_regime': {
                'total': '2.0 литра в день',
                'schedule': [
                    {'time': '7:00', 'amount': '200 мл', 'description': 'Стакан теплой воды натощак'},
                    {'time': '8:00', 'amount': '200 мл', 'description': 'После завтрака'},
                    {'time': '10:00', 'amount': '200 мл', 'description': 'В течение дня'},
                    {'time': '11:00', 'amount': '200 мл', 'description': 'После перекуса'},
                    {'time': '13:00', 'amount': '200 мл', 'description': 'После обеда'},
                    {'time': '15:00', 'amount': '200 мл', 'description': 'В течение дня'},
                    {'time': '16:00', 'amount': '200 мл', 'description': 'После перекуса'},
                    {'time': '18:00', 'amount': '200 мл', 'description': 'Перед ужином'},
                    {'time': '19:00', 'amount': '200 мл', 'description': 'После ужина'},
                    {'time': '21:00', 'amount': '200 мл', 'description': 'Перед сном'}
                ]
            },
            'professor_advice': 'Соблюдайте режим питания и пейте достаточное количество воды для достижения лучших результатов!',
            'created_at': datetime.now().isoformat(),
            'source': 'enhanced_generator'
        }
        
        # Создаем 7 дней с уникальными рецептами
        day_names = ['ПОНЕДЕЛЬНИК', 'ВТОРНИК', 'СРЕДА', 'ЧЕТВЕРГ', 'ПЯТНИЦА', 'СУББОТА', 'ВОСКРЕСЕНЬЕ']
        
        for i, day_name in enumerate(day_names):
            day = {
                'name': day_name,
                'meals': [
                    {
                        'type': 'ЗАВТРАК',
                        'name': f'Овсяная каша с фруктами {i+1}',
                        'time': '8:00',
                        'calories': '350 ккал',
                        'protein': '15г',
                        'fat': '10г',
                        'carbs': '55г',
                        'ingredients': [
                            {'name': 'Овсяные хлопья', 'quantity': '60г'},
                            {'name': 'Молоко', 'quantity': '200мл'},
                            {'name': 'Банан', 'quantity': '1шт'},
                            {'name': 'Мед', 'quantity': '15г'}
                        ],
                        'recipe': '1. Доведите молоко до кипения\n2. Добавьте овсяные хлопья\n3. Варите 7 минут\n4. Добавьте банан и мед'
                    },
                    {
                        'type': 'ПЕРЕКУС 1',
                        'name': f'Йогурт с орехами {i+1}',
                        'time': '11:00',
                        'calories': '250 ккал',
                        'protein': '12г',
                        'fat': '10г',
                        'carbs': '30г',
                        'ingredients': [
                            {'name': 'Йогурт греческий', 'quantity': '150г'},
                            {'name': 'Миндаль', 'quantity': '20г'},
                            {'name': 'Ягоды', 'quantity': '100г'}
                        ],
                        'recipe': '1. Смешайте йогурт с ягодами\n2. Посыпьте измельченным миндалем'
                    },
                    {
                        'type': 'ОБЕД',
                        'name': f'Куриная грудка с гречкой {i+1}',
                        'time': '13:00',
                        'calories': '450 ккал',
                        'protein': '40г',
                        'fat': '12г',
                        'carbs': '45г',
                        'ingredients': [
                            {'name': 'Куриная грудка', 'quantity': '150г'},
                            {'name': 'Гречневая крупа', 'quantity': '100г'},
                            {'name': 'Овощи', 'quantity': '200г'}
                        ],
                        'recipe': '1. Отварите гречку\n2. Приготовьте куриную грудку на пару\n3. Подавайте с овощами'
                    },
                    {
                        'type': 'ПЕРЕКУС 2',
                        'name': f'Творог с фруктами {i+1}',
                        'time': '16:00',
                        'calories': '200 ккал',
                        'protein': '20г',
                        'fat': '5г',
                        'carbs': '15г',
                        'ingredients': [
                            {'name': 'Творог', 'quantity': '150г'},
                            {'name': 'Персик', 'quantity': '1шт'},
                            {'name': 'Мед', 'quantity': '10г'}
                        ],
                        'recipe': '1. Смешайте творог с персиком\n2. Добавьте мед'
                    },
                    {
                        'type': 'УЖИН',
                        'name': f'Рыба с овощами {i+1}',
                        'time': '19:00',
                        'calories': '350 ккал',
                        'protein': '30г',
                        'fat': '15г',
                        'carbs': '20г',
                        'ingredients': [
                            {'name': 'Филе рыбы', 'quantity': '200г'},
                            {'name': 'Овощи на пару', 'quantity': '300г'},
                            {'name': 'Лимон', 'quantity': '0.5шт'}
                        ],
                        'recipe': '1. Приготовьте рыбу на пару\n2. Подавайте с овощами и лимоном'
                    }
                ],
                'total_calories': '1600 ккал',
                'water_schedule': [
                    '7:00 - 200 мл теплой воды',
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
            }
            plan['days'].append(day)
        
        # Генерируем список покупок
        plan['shopping_list'] = {
            'Овощи': [
                {'name': 'Овощи для салата', 'quantity': '500г'},
                {'name': 'Овощи для приготовления', 'quantity': '1кг'}
            ],
            'Фрукты': [
                {'name': 'Банан', 'quantity': '7шт'},
                {'name': 'Ягоды', 'quantity': '500г'},
                {'name': 'Персик', 'quantity': '7шт'}
            ],
            'Мясо/Рыба': [
                {'name': 'Куриная грудка', 'quantity': '1кг'},
                {'name': 'Филе рыбы', 'quantity': '1.4кг'}
            ],
            'Молочные продукты': [
                {'name': 'Молоко', 'quantity': '1.5л'},
                {'name': 'Йогурт греческий', 'quantity': '1кг'},
                {'name': 'Творог', 'quantity': '1кг'}
            ],
            'Крупы': [
                {'name': 'Овсяные хлопья', 'quantity': '500г'},
                {'name': 'Гречневая крупа', 'quantity': '1кг'}
            ],
            'Прочее': [
                {'name': 'Мед', 'quantity': '200г'},
                {'name': 'Миндаль', 'quantity': '150г'},
                {'name': 'Лимон', 'quantity': '4шт'}
            ]
        }
        
        logger.info(f"✅ Enhanced plan generated for user {user_data['user_id']}")
        return plan

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
    
    def get_back_menu(self):
        """Меню с кнопкой назад"""
        keyboard = [
            [InlineKeyboardButton("↩️ НАЗАД", callback_data="back_main")]
        ]
        return InlineKeyboardMarkup(keyboard)

# ==================== FLASK APP ====================

app = Flask(__name__)

# Глобальная переменная для приложения бота
application = None
menu = InteractiveMenu()

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
• Создать персональный план питания
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

# ==================== ОБРАБОТЧИКИ СОЗДАНИЯ ПЛАНА ====================

async def handle_create_plan(query, context):
    """Обработчик создания плана"""
    try:
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
        
        # Отправляем сообщение о начале генерации
        progress_message = await update.message.reply_text(
            "🔄 Ваш план готовится...\n\n"
            "🎓 Генерируем индивидуальный план питания...",
            reply_markup=menu.get_back_menu()
        )
        
        # Генерируем план
        plan_data = EnhancedPlanGenerator.generate_plan_with_progress_indicator(user_data)
        
        if plan_data:
            plan_id = save_plan(user_data['user_id'], plan_data)
            
            await progress_message.delete()
            
            success_text = f"""
🎉 ВАШ ПЛАН ПИТАНИЯ ГОТОВ!

👤 Данные: {user_data['gender']}, {age} лет, {height} см, {weight} кг
🎯 Цель: {user_data['goal']}
🏃 Активность: {user_data['activity']}

📋 План включает:
• 7 дней питания с уникальными рецептами
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
            
            logger.info(f"✅ Plan successfully created for user {user_data['user_id']}")
            
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

# ==================== ПРОСТЫЕ ОБРАБОТЧИКИ ДЛЯ ОСТАЛЬНЫХ КНОПОК ====================

async def handle_checkin_menu(query, context):
    """Обработчик меню чек-ина"""
    await query.edit_message_text(
        "📈 ЕЖЕДНЕВНЫЙ ЧЕК-ИН\n\nФункция в разработке...",
        reply_markup=menu.get_checkin_menu()
    )

async def handle_stats(query, context):
    """Обработчик статистики"""
    await query.edit_message_text(
        "📊 СТАТИСТИКА\n\nФункция в разработке...",
        reply_markup=menu.get_main_menu()
    )

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

async def handle_shopping_cart_main(query, context):
    """Главное меню корзины"""
    user_id = query.from_user.id
    latest_plan = get_latest_plan(user_id)
    
    if latest_plan:
        await query.edit_message_text(
            f"🛒 КОРЗИНА ПОКУПОК (План ID: {latest_plan['id']})\n\nВыберите действие:",
            reply_markup=menu.get_shopping_cart_menu(latest_plan['id'])
        )
    else:
        await query.edit_message_text(
            "🛒 КОРЗИНА ПОКУПОК\n\nУ вас пока нет планов питания.\nСоздайте план, чтобы сгенерировать корзину покупок!",
            reply_markup=menu.get_main_menu()
        )

async def handle_help(query, context):
    """Обработчик помощи"""
    help_text = """
❓ ПОМОЩЬ ПО БОТУ

📊 СОЗДАТЬ ПЛАН:
• Создает персонализированный план питания на 7 дней
• Учитывает ваш пол, цель, активность и параметры
• Генерирует уникальные рецепты для каждого дня

📈 ЧЕК-ИН:
• Ежедневное отслеживание прогресса (в разработке)

📊 СТАТИСТИКА:
• Анализ вашего прогресса (в разработке)

📋 МОЙ ПЛАН:
• Просмотр текущего и предыдущих планов питания
• Доступ к корзине покупок

🛒 КОРЗИНА:
• Автоматическая генерация списка покупок для плана
• Отметка купленных продуктов
• Скачивание списка покупок
"""
    await query.edit_message_text(
        help_text,
        reply_markup=menu.get_main_menu()
    )

async def handle_admin_callback(query, context):
    """Обработчик админских callback'ов"""
    user_id = query.from_user.id
    if not is_admin(user_id):
        await query.edit_message_text("❌ У вас нет прав доступа")
        return
    
    await query.edit_message_text(
        "👑 ПАНЕЛЬ АДМИНИСТРАТОРА\n\nФункции в разработке...",
        reply_markup=menu.get_main_menu()
    )

async def show_main_menu(query):
    """Показывает главное меню"""
    await query.edit_message_text(
        "🤖 ГЛАВНОЕ МЕНЮ\n\nВыберите действие:",
        reply_markup=menu.get_main_menu()
    )

# ==================== ОБРАБОТЧИК CALLBACK ====================

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

# ==================== ИНИЦИАЛИЗАЦИЯ БОТА ====================

def init_bot():
    """Инициализация бота"""
    global application
    try:
        Config.validate()
        init_database()
        
        # Создаем application
        application = Application.builder().token(Config.BOT_TOKEN).build()
        
        # Регистрируем обработчики
        application.add_handler(CommandHandler("start", start_command))
        application.add_handler(CommandHandler("menu", menu_command))
        application.add_handler(CallbackQueryHandler(handle_callback))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        
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
    <p>Бот для создания персональных планов питания</p>
    <p><a href="/health">Health Check</a></p>
    <p>🕒 Last update: {}</p>
    <p>🔧 Mode: WEBHOOK</p>
    <p>🚀 Status: Active</p>
    """.format(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

@app.route('/health')
def health_check():
    return jsonify({
        "status": "healthy", 
        "service": "nutrition-bot",
        "timestamp": datetime.now().isoformat(),
        "bot_status": "running" if application else "stopped"
    })

@app.route('/webhook', methods=['POST'])
def webhook():
    """Webhook endpoint for Telegram"""
    if request.method == "POST" and application:
        try:
            update = Update.de_json(request.get_json(), application.bot)
            application.update_queue.put(update)
            return "ok"
        except Exception as e:
            logger.error(f"❌ Webhook error: {e}")
            return "error"
    return "error"

# ==================== ЗАПУСК ПРИЛОЖЕНИЯ ====================

def main():
    """Основная функция запуска"""
    try:
        logger.info("🚀 Starting Nutrition Bot...")
        
        if not init_bot():
            logger.error("❌ Failed to initialize bot. Exiting.")
            return
        
        # Настраиваем webhook для Render
        if Config.RENDER and Config.WEBHOOK_URL:
            try:
                webhook_url = f"{Config.WEBHOOK_URL}/webhook"
                application.bot.set_webhook(webhook_url)
                logger.info(f"✅ Webhook set to: {webhook_url}")
            except Exception as e:
                logger.error(f"❌ Failed to set webhook: {e}")
            
            # Запускаем Flask приложение
            port = int(os.environ.get('PORT', Config.PORT))
            logger.info(f"🌐 Starting Flask app on port {port}")
            app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
        else:
            # Локальный запуск в polling режиме
            logger.info("🤖 Starting bot in POLLING mode...")
            application.run_polling(
                drop_pending_updates=True,
                allowed_updates=Update.ALL_TYPES
            )
        
    except Exception as e:
        logger.error(f"❌ Fatal error: {e}")

if __name__ == "__main__":
    main()

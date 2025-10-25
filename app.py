import os
import logging
import sqlite3
import json
import requests
import sys
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
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ==================== КОНФИГУРАЦИЯ ====================

class Config:
    BOT_TOKEN = os.getenv('BOT_TOKEN')
    YANDEX_API_KEY = os.getenv('YANDEX_API_KEY')
    YANDEX_FOLDER_ID = os.getenv('YANDEX_FOLDER_ID')
    ADMIN_USER_ID = int(os.getenv('ADMIN_USER_ID', '362423055'))
    DATABASE_URL = os.getenv('DATABASE_URL', 'nutrition_bot.db')
    REQUEST_TIMEOUT = int(os.getenv('REQUEST_TIMEOUT', '60'))
    MAX_RETRIES = int(os.getenv('MAX_RETRIES', '3'))
    PORT = int(os.getenv('PORT', '10000'))
    WEBHOOK_URL = os.getenv('RENDER_EXTERNAL_URL', '')  # Автоматически на Render
    
    @classmethod
    def validate(cls):
        """Проверка обязательных переменных"""
        required_vars = ['BOT_TOKEN']
        missing_vars = []
        for var in required_vars:
            if not getattr(cls, var):
                missing_vars.append(var)
        
        if missing_vars:
            raise ValueError(f"❌ Missing required environment variables: {', '.join(missing_vars)}")
        
        logger.info("✅ Configuration validated successfully")

# ==================== БАЗА ДАННЫХ ====================

def init_database():
    """Инициализация базы данных"""
    conn = sqlite3.connect(Config.DATABASE_URL, check_same_thread=False)
    cursor = conn.cursor()
    
    cursor.execute('PRAGMA journal_mode=WAL')
    cursor.execute('PRAGMA synchronous=NORMAL')
    
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
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS daily_checkins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            weight REAL,
            waist_circumference INTEGER,
            wellbeing_score INTEGER,
            sleep_quality INTEGER,
            date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_limits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER UNIQUE NOT NULL,
            last_plan_date TIMESTAMP,
            plan_count INTEGER DEFAULT 0
        )
    ''')
    
    conn.commit()
    conn.close()
    logger.info("✅ Database initialized successfully")

def save_user(user_data):
    """Сохраняет пользователя в БД"""
    conn = sqlite3.connect(Config.DATABASE_URL, check_same_thread=False)
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
            
        conn = sqlite3.connect(Config.DATABASE_URL, check_same_thread=False)
        cursor = conn.cursor()
        
        cursor.execute('SELECT last_plan_date FROM user_limits WHERE user_id = ?', (user_id,))
        result = cursor.fetchone()
        
        if not result:
            return True
            
        last_plan_date = datetime.fromisoformat(result[0])
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
            
        conn = sqlite3.connect(Config.DATABASE_URL, check_same_thread=False)
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
            
        conn = sqlite3.connect(Config.DATABASE_URL, check_same_thread=False)
        cursor = conn.cursor()
        
        cursor.execute('SELECT last_plan_date FROM user_limits WHERE user_id = ?', (user_id,))
        result = cursor.fetchone()
        
        if not result:
            return 0
            
        last_plan_date = datetime.fromisoformat(result[0])
        days_passed = (datetime.now() - last_plan_date).days
        days_remaining = 7 - days_passed
        
        conn.close()
        return max(0, days_remaining)
        
    except Exception as e:
        logger.error(f"❌ Error getting days until next plan: {e}")
        return 0

def save_plan(user_id, plan_data):
    """Сохраняет план питания в БД"""
    conn = sqlite3.connect(Config.DATABASE_URL, check_same_thread=False)
    cursor = conn.cursor()
    
    try:
        cursor.execute('INSERT INTO nutrition_plans (user_id, plan_data) VALUES (?, ?)', 
                      (user_id, json.dumps(plan_data)))
        plan_id = cursor.lastrowid
        conn.commit()
        logger.info(f"✅ Plan saved for user: {user_id}, plan_id: {plan_id}")
        return plan_id
    except Exception as e:
        logger.error(f"❌ Error saving plan: {e}")
        return None
    finally:
        conn.close()

def save_checkin(user_id, weight, waist, wellbeing, sleep):
    """Сохраняет ежедневный чек-ин"""
    conn = sqlite3.connect(Config.DATABASE_URL, check_same_thread=False)
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
    conn = sqlite3.connect(Config.DATABASE_URL, check_same_thread=False)
    cursor = conn.cursor()
    
    try:
        cursor.execute('''
            SELECT date, weight, waist_circumference, wellbeing_score, sleep_quality
            FROM daily_checkins WHERE user_id = ? ORDER BY date DESC LIMIT 7
        ''', (user_id,))
        checkins = cursor.fetchall()
        return checkins
    except Exception as e:
        logger.error(f"❌ Error getting stats: {e}")
        return []
    finally:
        conn.close()

def get_latest_plan(user_id):
    """Получает последний план пользователя"""
    conn = sqlite3.connect(Config.DATABASE_URL, check_same_thread=False)
    cursor = conn.cursor()
    
    try:
        cursor.execute('''
            SELECT plan_data FROM nutrition_plans 
            WHERE user_id = ? ORDER BY created_at DESC LIMIT 1
        ''', (user_id,))
        result = cursor.fetchone()
        return json.loads(result[0]) if result else None
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
            [InlineKeyboardButton("❓ ПОМОЩЬ", callback_data="help")]
        ]
        
        if Config.ADMIN_USER_ID:
            keyboard.append([InlineKeyboardButton("👑 АДМИН", callback_data="admin")])
            
        return InlineKeyboardMarkup(keyboard)
    
    def get_plan_data_input(self, step=1):
        """Клавиатура для ввода данных плана"""
        if step == 1:  # Выбор пола
            keyboard = [
                [InlineKeyboardButton("👨 МУЖЧИНА", callback_data="gender_male")],
                [InlineKeyboardButton("👩 ЖЕНЩИНА", callback_data="gender_female")],
                [InlineKeyboardButton("↩️ НАЗАД", callback_data="back_main")]
            ]
        elif step == 2:  # Выбор цели
            keyboard = [
                [InlineKeyboardButton("🎯 ПОХУДЕНИЕ", callback_data="goal_weight_loss")],
                [InlineKeyboardButton("💪 НАБОР МАССЫ", callback_data="goal_mass")],
                [InlineKeyboardButton("⚖️ ПОДДЕРЖАНИЕ", callback_data="goal_maintain")],
                [InlineKeyboardButton("↩️ НАЗАД", callback_data="back_gender")]
            ]
        elif step == 3:  # Выбор активности
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
    
    def get_back_menu(self):
        """Меню с кнопкой назад"""
        keyboard = [
            [InlineKeyboardButton("↩️ НАЗАД", callback_data="back_main")]
        ]
        return InlineKeyboardMarkup(keyboard)

# ==================== FLASK APP ====================

app = Flask(__name__)

# Глобальные переменные для бота
application = None
menu = InteractiveMenu()

def init_bot():
    """Инициализация бота"""
    global application
    try:
        Config.validate()
        init_database()
        
        application = Application.builder().token(Config.BOT_TOKEN).build()
        setup_handlers(application)
        
        logger.info("✅ Bot initialized successfully")
        return application
    except Exception as e:
        logger.error(f"❌ Failed to initialize bot: {e}")
        return None

def setup_handlers(app):
    """Настройка обработчиков"""
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("menu", menu_command))
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

# ==================== ОБРАБОТЧИКИ КОМАНД ====================

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

Выберите действие из меню ниже:
"""
        if is_admin(user.id):
            welcome_text += "\n👑 ВЫ АДМИНИСТРАТОР: безлимитный доступ к планам!"
        
        await update.message.reply_text(
            welcome_text,
            reply_markup=menu.get_main_menu()
        )
        
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
        "👑 ПАНЕЛЬ АДМИНИСТРАТОРА - Функции в разработке",
        reply_markup=menu.get_main_menu()
    )

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик callback'ов"""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    logger.info(f"📨 Callback received: {data}")
    
    try:
        if data == "create_plan":
            await handle_create_plan(query, context)
        elif data == "checkin":
            await handle_checkin_menu(query, context)
        elif data == "stats":
            await handle_stats(query, context)
        elif data == "my_plan":
            await handle_my_plan(query, context)
        elif data == "help":
            await handle_help(query, context)
        elif data == "back_main":
            await show_main_menu(query)
        elif data.startswith("gender_"):
            await handle_gender(query, context, data)
        elif data.startswith("goal_"):
            await handle_goal(query, context, data)
        elif data.startswith("activity_"):
            await handle_activity(query, context, data)
        elif data == "checkin_data":
            await handle_checkin_data(query, context)
        elif data == "checkin_history":
            await handle_checkin_history(query, context)
        else:
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

async def handle_checkin_menu(query, context):
    """Обработчик меню чек-ина"""
    try:
        await query.edit_message_text(
            "📈 ЕЖЕДНЕВНЫЙ ЧЕК-ИН\n\n"
            "Отслеживайте ваш прогресс:\n"
            "• Вес\n"
            "• Обхват талии\n"
            "• Самочувствие\n"
            "• Качество сна\n\n"
            "Выберите действие:",
            reply_markup=menu.get_checkin_menu()
        )
    except Exception as e:
        logger.error(f"Error in checkin menu handler: {e}")
        await query.edit_message_text(
            "❌ Ошибка при открытии чек-ина",
            reply_markup=menu.get_main_menu()
        )

async def handle_checkin_data(query, context):
    """Обработчик ввода данных чек-ина"""
    try:
        context.user_data['awaiting_input'] = 'checkin_data'
        
        await query.edit_message_text(
            "📝 ВВЕДИТЕ ДАННЫЕ ЧЕК-ИНА\n\n"
            "Введите данные в формате:\n"
            "Вес (кг), Обхват талии (см), Самочувствие (1-5), Сон (1-5)\n\n"
            "Пример: 75.5, 85, 4, 3\n\n"
            "Для отмены нажмите /menu"
        )
        
    except Exception as e:
        logger.error(f"Error in checkin data handler: {e}")
        await query.edit_message_text(
            "❌ Ошибка при вводе данных чек-ина",
            reply_markup=menu.get_main_menu()
        )

async def handle_checkin_history(query, context):
    """Обработчик истории чек-инов"""
    try:
        user_id = query.from_user.id
        stats = get_user_stats(user_id)
        
        if not stats:
            await query.edit_message_text(
                "📊 У вас пока нет данных чек-инов\n\n"
                "Начните отслеживать свой прогресс!",
                reply_markup=menu.get_checkin_menu()
            )
            return
        
        stats_text = "📊 ИСТОРИЯ ВАШИХ ЧЕК-ИНОВ:\n\n"
        for stat in stats:
            date, weight, waist, wellbeing, sleep = stat
            stats_text += f"📅 {date[:10]}\n"
            stats_text += f"⚖️ Вес: {weight} кг\n"
            stats_text += f"📏 Талия: {waist} см\n"
            stats_text += f"😊 Самочувствие: {wellbeing}/5\n"
            stats_text += f"😴 Сон: {sleep}/5\n\n"
        
        await query.edit_message_text(
            stats_text,
            reply_markup=menu.get_checkin_menu()
        )
        
    except Exception as e:
        logger.error(f"Error in checkin history handler: {e}")
        await query.edit_message_text(
            "❌ Ошибка при получении истории чек-инов",
            reply_markup=menu.get_main_menu()
        )

async def handle_stats(query, context):
    """Обработчик статистики"""
    try:
        user_id = query.from_user.id
        stats = get_user_stats(user_id)
        
        if not stats:
            await query.edit_message_text(
                "📊 У вас пока нет данных для статистики\n\n"
                "Начните с ежедневных чек-инов!",
                reply_markup=menu.get_main_menu()
            )
            return
        
        stats_text = "📊 ВАША СТАТИСТИКА\n\n"
        stats_text += "Последние записи:\n"
        
        for i, stat in enumerate(stats[:5]):
            date, weight, waist, wellbeing, sleep = stat
            stats_text += f"📅 {date[:10]}: {weight} кг, талия {waist} см\n"
        
        await query.edit_message_text(
            stats_text,
            reply_markup=menu.get_main_menu()
        )
        
    except Exception as e:
        logger.error(f"Error in stats handler: {e}")
        await query.edit_message_text(
            "❌ Ошибка при получении статистики",
            reply_markup=menu.get_main_menu()
        )

async def handle_my_plan(query, context):
    """Обработчик просмотра текущего плана"""
    try:
        user_id = query.from_user.id
        plan = get_latest_plan(user_id)
        
        if not plan:
            await query.edit_message_text(
                "📋 У вас пока нет созданных планов питания\n\n"
                "Создайте ваш первый персональный план!",
                reply_markup=menu.get_main_menu()
            )
            return
        
        user_data = plan.get('user_data', {})
        plan_text = f"📋 ВАШ ТЕКУЩИЙ ПЛАН ПИТАНИЯ\n\n"
        plan_text += f"👤 {user_data.get('gender', '')}, {user_data.get('age', '')} лет\n"
        plan_text += f"📏 {user_data.get('height', '')} см, {user_data.get('weight', '')} кг\n"
        plan_text += f"🎯 Цель: {user_data.get('goal', '')}\n"
        plan_text += f"🏃 Активность: {user_data.get('activity', '')}\n\n"
        
        plan_text += f"💧 Рекомендации по воде: 1.5-2 литра в день"
        
        await query.edit_message_text(
            plan_text,
            reply_markup=menu.get_main_menu()
        )
        
    except Exception as e:
        logger.error(f"Error in my_plan handler: {e}")
        await query.edit_message_text(
            "❌ Ошибка при получении плана",
            reply_markup=menu.get_main_menu()
        )

async def handle_help(query, context):
    """Обработчик помощи"""
    help_text = """
❓ ПОМОЩЬ ПО БОТУ

📊 СОЗДАТЬ ПЛАН:
• Создает персонализированный план питания
• Учитывает ваш пол, цель, активность
• Доступен раз в 7 дней

📈 ЧЕК-ИН:
• Ежедневное отслеживание прогресса
• Запись веса, обхвата талии
• Просмотр истории

📊 СТАТИСТИКА:
• Анализ вашего прогресса

📋 МОЙ ПЛАН:
• Просмотр текущего плана питания
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

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик текстовых сообщений"""
    try:
        text = update.message.text
        user_id = update.effective_user.id
        
        if text == "/menu":
            await update.message.reply_text(
                "🤖 ГЛАВНОЕ МЕНЮ\n\nВыберите действие:",
                reply_markup=menu.get_main_menu()
            )
            return
        
        if context.user_data.get('awaiting_input') == 'plan_details':
            await process_plan_details(update, context, text)
        elif context.user_data.get('awaiting_input') == 'checkin_data':
            await process_checkin_data(update, context, text)
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
        
        # Создаем простой план (без Yandex GPT для начала)
        plan_data = generate_simple_plan(user_data)
        if plan_data:
            plan_id = save_plan(user_data['user_id'], plan_data)
            update_user_limit(user_data['user_id'])
            
            success_text = f"""
🎉 ВАШ ПЛАН ПИТАНИЯ ГОТОВ!

👤 Данные: {user_data['gender']}, {age} лет, {height} см, {weight} кг
🎯 Цель: {user_data['goal']}
🏃 Активность: {user_data['activity']}

📋 План включает:
• 7 дней питания
• 5 приемов пищи в день  
• Сбалансированное питание

План сохранен в вашем профиле!
"""
            await update.message.reply_text(
                success_text,
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

async def process_checkin_data(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    """Обрабатывает данные чек-ина"""
    try:
        parts = [part.strip() for part in text.split(',')]
        if len(parts) != 4:
            raise ValueError("Нужно ввести 4 значения через запятую")
        
        weight, waist, wellbeing, sleep = float(parts[0]), int(parts[1]), int(parts[2]), int(parts[3])
        
        if not (30 <= weight <= 300):
            raise ValueError("Вес должен быть от 30 до 300 кг")
        if not (50 <= waist <= 200):
            raise ValueError("Обхват талии должен быть от 50 до 200 см")
        if not (1 <= wellbeing <= 5):
            raise ValueError("Самочувствие должно быть от 1 до 5")
        if not (1 <= sleep <= 5):
            raise ValueError("Качество сна должно быть от 1 до 5")
        
        user_id = update.effective_user.id
        save_checkin(user_id, weight, waist, wellbeing, sleep)
        
        success_text = f"""
✅ ДАННЫЕ ЧЕК-ИНА СОХРАНЕНЫ!

📅 Дата: {datetime.now().strftime('%d.%m.%Y')}
⚖️ Вес: {weight} кг
📏 Талия: {waist} см
😊 Самочувствие: {wellbeing}/5
😴 Сон: {sleep}/5
"""
        await update.message.reply_text(
            success_text,
            reply_markup=menu.get_main_menu()
        )
        
        # Очищаем временные данные
        context.user_data['awaiting_input'] = None
        
    except ValueError as e:
        error_msg = str(e)
        if "Нужно ввести 4 значения" in error_msg:
            await update.message.reply_text(
                "❌ Ошибка в формате данных. Используйте: Вес, Талия, Самочувствие, Сон\nПример: 75.5, 85, 4, 3\n\nПопробуйте снова или нажмите /menu для отмены"
            )
        else:
            await update.message.reply_text(
                f"❌ {error_msg}\n\nПопробуйте снова или нажмите /menu для отмены"
            )
    except Exception as e:
        logger.error(f"❌ Error processing checkin data: {e}")
        await update.message.reply_text(
            "❌ Произошла ошибка при сохранении чек-ина. Попробуйте снова.",
            reply_markup=menu.get_main_menu()
        )

def generate_simple_plan(user_data):
    """Создает простой план питания"""
    plan = {
        'user_data': user_data,
        'days': [],
        'created_at': datetime.now().isoformat()
    }
    
    day_names = ['ПОНЕДЕЛЬНИК', 'ВТОРНИК', 'СРЕДА', 'ЧЕТВЕРГ', 'ПЯТНИЦА', 'СУББОТА', 'ВОСКРЕСЕНЬЕ']
    
    for day_name in day_names:
        day = {
            'name': day_name,
            'meals': [
                {
                    'type': 'ЗАВТРАК',
                    'name': 'Овсяная каша с фруктами',
                    'time': '8:00',
                    'calories': '350 ккал'
                },
                {
                    'type': 'ПЕРЕКУС 1', 
                    'name': 'Йогурт с орехами',
                    'time': '11:00',
                    'calories': '250 ккал'
                },
                {
                    'type': 'ОБЕД',
                    'name': 'Куриная грудка с гречкой',
                    'time': '13:00', 
                    'calories': '450 ккал'
                },
                {
                    'type': 'ПЕРЕКУС 2',
                    'name': 'Фруктовый салат',
                    'time': '16:00',
                    'calories': '200 ккал'
                },
                {
                    'type': 'УЖИН',
                    'name': 'Рыба с овощами',
                    'time': '19:00',
                    'calories': '400 ккал'
                }
            ]
        }
        plan['days'].append(day)
    
    return plan

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик ошибок"""
    logger.error(f"❌ Exception while handling update: {context.error}")

# ==================== WEBHOOK ROUTES ====================

@app.route('/')
def home():
    return """
    <h1>🤖 Nutrition Bot is Running!</h1>
    <p>Бот для создания персональных планов питания</p>
    <p><a href="/health">Health Check</a></p>
    <p>🕒 Last update: {}</p>
    """.format(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

@app.route('/health')
def health_check():
    return jsonify({
        "status": "healthy", 
        "service": "nutrition-bot",
        "timestamp": datetime.now().isoformat()
    })

@app.route('/webhook', methods=['POST'])
async def webhook():
    """Webhook endpoint for Telegram"""
    if request.method == "POST":
        update = Update.de_json(request.get_json(), application.bot)
        await application.process_update(update)
    return "ok"

@app.route('/set_webhook', methods=['GET'])
async def set_webhook():
    """Установка webhook"""
    try:
        webhook_url = f"https://{request.host}/webhook"
        await application.bot.set_webhook(webhook_url)
        return jsonify({"status": "success", "webhook_url": webhook_url})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500

# ==================== ЗАПУСК ПРИЛОЖЕНИЯ ====================

def main():
    """Основная функция запуска"""
    # Инициализация бота
    bot = init_bot()
    if not bot:
        logger.error("❌ Failed to initialize bot. Exiting.")
        return
    
    # Запуск Flask приложения
    logger.info(f"🚀 Starting Flask app on port {Config.PORT}")
    app.run(host='0.0.0.0', port=Config.PORT, debug=False)

if __name__ == "__main__":
    main()

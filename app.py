import os
import logging
import sqlite3
import json
import asyncio
import threading
import time
import requests
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
    
    # Включаем WAL mode для лучшей производительности
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
    
    # Создаем индексы для улучшения производительности
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_users_user_id ON users(user_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_plans_user_id ON nutrition_plans(user_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_checkins_user_id ON daily_checkins(user_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_checkins_date ON daily_checkins(date)')
    
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
            SELECT plan_data FROM nutrition_plans 
            WHERE user_id = ? ORDER BY created_at DESC LIMIT 1
        ''', (user_id,))
        result = cursor.fetchone()
        return json.loads(result['plan_data']) if result else None
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
                
                # Ждем 4 минуты до следующего пинга (меньше 5 минут Render timeout)
                for _ in range(240):  # 4 минуты в секундах
                    if not self.is_running:
                        break
                    time.sleep(1)
                    
            except Exception as e:
                logger.error(f"❌ Keep-alive worker error: {e}")
                time.sleep(60)

# ==================== FLASK APP ====================

app = Flask(__name__)

# Глобальные переменные для бота
application = None
menu = InteractiveMenu()
keep_alive_service = KeepAliveService()

def init_bot():
    """Инициализация бота"""
    global application
    try:
        Config.validate()
        init_database()
        
        # Создаем приложение бота
        application = Application.builder().token(Config.BOT_TOKEN).build()
        
        # Настраиваем обработчики
        application.add_handler(CommandHandler("start", start_command))
        application.add_handler(CommandHandler("menu", menu_command))
        application.add_handler(CommandHandler("admin", admin_command))
        application.add_handler(CallbackQueryHandler(handle_callback))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        
        # Обработчик ошибок
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

🤖 Я помогу вам:
• Создать персональный план питания
• Отслеживать прогресс
• Анализировать статистику

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
    
    # Статистика для админа
    conn = DatabaseManager.get_connection()
    cursor = conn.cursor()
    
    try:
        cursor.execute('SELECT COUNT(*) as total_users FROM users')
        total_users = cursor.fetchone()['total_users']
        
        cursor.execute('SELECT COUNT(*) as total_plans FROM nutrition_plans')
        total_plans = cursor.fetchone()['total_plans']
        
        cursor.execute('SELECT COUNT(*) as total_checkins FROM daily_checkins')
        total_checkins = cursor.fetchone()['total_checkins']
        
        admin_text = f"""
👑 ПАНЕЛЬ АДМИНИСТРАТОРА

📊 Статистика:
• Пользователей: {total_users}
• Создано планов: {total_plans}
• Чек-инов: {total_checkins}
• Сервис: {"🟢 Онлайн" if application else "🔴 Офлайн"}

Доступные команды:
/menu - Главное меню
"""
        await update.message.reply_text(admin_text)
        
    except Exception as e:
        logger.error(f"❌ Error in admin command: {e}")
        await update.message.reply_text("❌ Ошибка при получении статистики")
    finally:
        conn.close()

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик callback'ов"""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    logger.info(f"📨 Callback received: {data} from user {query.from_user.id}")
    
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
        elif data == "admin":
            await handle_admin_callback(query, context)
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

async def handle_admin_callback(query, context):
    """Обработчик админских callback'ов"""
    user_id = query.from_user.id
    if not is_admin(user_id):
        await query.edit_message_text("❌ У вас нет прав доступа")
        return
    
    await admin_command(await _get_update_from_query(query), context)

async def _get_update_from_query(query):
    """Создает Update объект из query"""
    return Update(update_id=query.id, callback_query=query)

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
            "• Самочувствие (1-5)\n"
            "• Качество сна (1-5)\n\n"
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
            "📊 Шкала оценок:\n"
            "• Самочувствие: 1(плохо) - 5(отлично)\n"
            "• Сон: 1(бессонница) - 5(отлично выспался)\n\n"
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
        for stat in stats[:5]:  # Показываем только последние 5 записей
            date_str = stat['date'][:10] if isinstance(stat['date'], str) else stat['date'].strftime('%Y-%m-%d')
            stats_text += f"📅 {date_str}\n"
            stats_text += f"⚖️ Вес: {stat['weight']} кг\n"
            stats_text += f"📏 Талия: {stat['waist_circumference']} см\n"
            stats_text += f"😊 Самочувствие: {stat['wellbeing_score']}/5\n"
            stats_text += f"😴 Сон: {stat['sleep_quality']}/5\n\n"
        
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
        
        # Анализ прогресса
        if len(stats) >= 2:
            latest_weight = stats[0]['weight']
            oldest_weight = stats[-1]['weight']
            weight_diff = latest_weight - oldest_weight
            
            if weight_diff < 0:
                progress_text = f"📉 Потеря веса: {abs(weight_diff):.1f} кг"
            elif weight_diff > 0:
                progress_text = f"📈 Набор веса: {weight_diff:.1f} кг"
            else:
                progress_text = "⚖️ Вес стабилен"
        else:
            progress_text = "📈 Записей пока мало для анализа прогресса"
        
        stats_text = f"📊 ВАША СТАТИСТИКА\n\n{progress_text}\n\n"
        stats_text += "Последние записи:\n"
        
        for i, stat in enumerate(stats[:3]):
            date_str = stat['date'][:10] if isinstance(stat['date'], str) else stat['date'].strftime('%Y-%m-%d')
            stats_text += f"📅 {date_str}: {stat['weight']} кг, талия {stat['waist_circumference']} см\n"
        
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
        
        # Показываем первый день плана
        if plan.get('days'):
            first_day = plan['days'][0]
            plan_text += f"📅 {first_day['name']}:\n"
            for meal in first_day.get('meals', [])[:3]:  # Показываем первые 3 приема пищи
                plan_text += f"• {meal['time']} - {meal['name']}\n"
            plan_text += f"\n🍽️ Всего приемов пищи: 5 в день"
        
        plan_text += f"\n\n💧 Рекомендации: 1.5-2 литра воды в день"
        
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
• Создает персонализированный план питания на 7 дней
• Учитывает ваш пол, цель, активность и параметры
• Доступен раз в 7 дней (админам - безлимитно)

📈 ЧЕК-ИН:
• Ежедневное отслеживание прогресса
• Запись веса, обхвата талии, самочувствия
• Просмотр истории и статистики

📊 СТАТИСТИКА:
• Анализ вашего прогресса  
• Графики изменений параметров

📋 МОЙ ПЛАН:
• Просмотр текущего плана питания
• Рекомендации и списки покупок

💡 Советы:
• Вводите данные точно
• Следуйте плану питания
• Регулярно делайте чек-ин
• Пейте достаточное количество воды

👑 АДМИН:
• Статистика использования бота
• Мониторинг состояния системы
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
        text = update.message.text.strip()
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
        
        processing_msg = await update.message.reply_text("🔄 Создаем ваш персональный план питания...")
        
        # Создаем план
        plan_data = generate_simple_plan(user_data)
        if plan_data:
            plan_id = save_plan(user_data['user_id'], plan_data)
            update_user_limit(user_data['user_id'])
            
            await processing_msg.delete()
            
            success_text = f"""
🎉 ВАШ ПЛАН ПИТАНИЯ ГОТОВ!

👤 Данные: {user_data['gender']}, {age} лет, {height} см, {weight} кг
🎯 Цель: {user_data['goal']}
🏃 Активность: {user_data['activity']}

📋 План включает:
• 7 дней питания
• 5 приемов пищи в день  
• Сбалансированное питание
• Рекомендации по воде

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

Продолжайте отслеживать ваш прогресс!
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
    try:
        plan = {
            'user_data': user_data,
            'days': [],
            'shopping_list': "Куриная грудка, рыба, овощи, фрукты, крупы, яйца, творог, молоко",
            'water_regime': "1.5-2 литра воды в день",
            'general_recommendations': "Сбалансированное питание и регулярная физическая активность",
            'created_at': datetime.now().isoformat()
        }
        
        day_names = ['ПОНЕДЕЛЬНИК', 'ВТОРНИК', 'СРЕДА', 'ЧЕТВЕРГ', 'ПЯТНИЦА', 'СУББОТА', 'ВОСКРЕСЕНЬЕ']
        meal_templates = [
            {'type': 'ЗАВТРАК', 'time': '8:00', 'base_calories': 350},
            {'type': 'ПЕРЕКУС 1', 'time': '11:00', 'base_calories': 250},
            {'type': 'ОБЕД', 'time': '13:00', 'base_calories': 450},
            {'type': 'ПЕРЕКУС 2', 'time': '16:00', 'base_calories': 200},
            {'type': 'УЖИН', 'time': '19:00', 'base_calories': 400}
        ]
        
        meal_names = {
            'ЗАВТРАК': ['Овсяная каша с фруктами', 'Творог с ягодами', 'Яичница с овощами', 'Гречневая каша'],
            'ПЕРЕКУС 1': ['Йогурт с орехами', 'Фруктовый салат', 'Протеиновый коктейль', 'Творожная запеканка'],
            'ОБЕД': ['Куриная грудка с гречкой', 'Рыба с рисом', 'Индейка с овощами', 'Тефтели с пастой'],
            'ПЕРЕКУС 2': ['Фруктовый салат', 'Орехи и сухофрукты', 'Сэндвич с авокадо', 'Йогурт'],
            'УЖИН': ['Рыба с овощами', 'Курица с салатом', 'Творог', 'Омлет с зеленью']
        }
        
        for day_name in day_names:
            day_calories = 0
            meals = []
            
            for meal_template in meal_templates:
                meal_type = meal_template['type']
                meal_name = meal_names[meal_type][hash(f"{day_name}{meal_type}") % len(meal_names[meal_type])]
                calories = meal_template['base_calories']
                day_calories += calories
                
                meal = {
                    'type': meal_type,
                    'name': meal_name,
                    'time': meal_template['time'],
                    'calories': f"{calories} ккал"
                }
                meals.append(meal)
            
            day = {
                'name': day_name,
                'meals': meals,
                'total_calories': f"{day_calories} ккал"
            }
            plan['days'].append(day)
        
        logger.info(f"✅ Plan generated for user {user_data['user_id']}")
        return plan
        
    except Exception as e:
        logger.error(f"❌ Error generating plan: {e}")
        return None

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик ошибок"""
    try:
        logger.error(f"❌ Exception while handling update: {context.error}")
        
        # Логируем дополнительную информацию
        if update:
            logger.error(f"Update: {update}")
        if context:
            logger.error(f"Context: {context}")
            
        # Отправляем сообщение пользователю
        if update and update.effective_message:
            await update.effective_message.reply_text(
                "❌ Произошла непредвиденная ошибка. Попробуйте позже.",
                reply_markup=menu.get_main_menu()
            )
    except Exception as e:
        logger.error(f"Error in error handler: {e}")

# ==================== WEBHOOK ROUTES ====================

@app.route('/')
def home():
    return """
    <h1>🤖 Nutrition Bot is Running!</h1>
    <p>Бот для создания персональных планов питания</p>
    <p><a href="/health">Health Check</a></p>
    <p><a href="/ping">Ping</a></p>
    <p>🕒 Last update: {}</p>
    <p>🔧 Mode: {}</p>
    """.format(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), 
               "WEBHOOK" if Config.WEBHOOK_URL and not Config.RENDER else "POLLING")

@app.route('/health')
def health_check():
    return jsonify({
        "status": "healthy", 
        "service": "nutrition-bot",
        "timestamp": datetime.now().isoformat(),
        "bot_status": "running" if application else "stopped",
        "mode": "webhook" if Config.WEBHOOK_URL and not Config.RENDER else "polling"
    })

@app.route('/ping')
def ping():
    return "pong 🏓"

@app.route('/status')
def status():
    return jsonify({
        "status": "operational",
        "service": "nutrition-bot",
        "timestamp": datetime.now().isoformat(),
        "version": "2.0",
        "environment": "production"
    })

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

@app.route('/set_webhook', methods=['GET'])
def set_webhook():
    """Установка webhook"""
    try:
        if application and Config.WEBHOOK_URL and not Config.RENDER:
            webhook_url = f"{Config.WEBHOOK_URL}/webhook"
            
            # Используем отдельный event loop для webhook
            async def set_webhook_async():
                await application.bot.set_webhook(webhook_url)
                return True
                
            success = asyncio.run(set_webhook_async())
            
            if success:
                return jsonify({
                    "status": "success", 
                    "message": "Webhook set successfully",
                    "webhook_url": webhook_url
                })
        else:
            return jsonify({
                "status": "info", 
                "message": "Using polling mode (Render environment)"
            })
    except Exception as e:
        logger.error(f"❌ Webhook setup error: {e}")
        return jsonify({"status": "error", "error": str(e)}), 500

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
        logger.info("🚀 Starting Nutrition Bot...")
        
        # Инициализация бота
        if not init_bot():
            logger.error("❌ Failed to initialize bot. Exiting.")
            return
        
        # Настройка webhook (только если не на Render)
        if Config.WEBHOOK_URL and not Config.RENDER:
            try:
                asyncio.run(setup_webhook())
            except Exception as e:
                logger.error(f"❌ Webhook setup failed, falling back to polling: {e}")
        
        # Запуск keep-alive service
        keep_alive_service.start()
        
        # Запуск Flask приложения в отдельном потоке
        def run_flask():
            port = int(os.environ.get('PORT', Config.PORT))
            logger.info(f"🌐 Starting Flask app on port {port}")
            app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
        
        flask_thread = threading.Thread(target=run_flask, daemon=True)
        flask_thread.start()
        
        # Запуск бота в режиме polling
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

import os
import json
import re
import logging
import asyncio
import random
import requests
from typing import Dict, Any, List, Tuple
from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, InputFile
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, ConversationHandler
from threading import Thread
from datetime import datetime, timedelta

load_dotenv()

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Конфигурация
BOT_TOKEN = os.getenv('BOT_TOKEN')
YANDEX_FOLDER_ID = os.getenv('YANDEX_FOLDER_ID')
YANDEX_API_KEY = os.getenv('YANDEX_API_KEY')
WEBHOOK_URL = os.getenv('WEBHOOK_URL')
ADMIN_CHAT_ID = os.getenv('ADMIN_CHAT_ID')
CHANNEL_USERNAME = "@ppsupershef"
PORT = int(os.environ.get('PORT', 5000))

# Состояния беседы
(
    SUBSCRIPTION_CHECK, START, GOAL, DIET, ALLERGIES, GENDER, AGE, HEIGHT, WEIGHT, ACTIVITY,
    CONFIRMATION, SELECT_PACKAGE, PAYMENT_METHOD, AWAITING_PAYMENT, PROCESSING
) = range(15)

# Пакеты услуг
SERVICE_PACKAGES = {
    'basic': {
        'name': '🎯 Базовый пакет',
        'description': 'План питания на 7 дней + расчет БЖУ',
        'price': 100,
        'features': [
            '📅 План питания на 7 дней',
            '📊 Расчет БЖУ и калорий', 
            '💧 Рекомендации по водному режиму'
        ]
    },
    'standard': {
        'name': '🚀 Стандартный пакет',
        'description': 'План + рецепты на 7 дней',
        'price': 200,
        'features': [
            '📅 План питания на 7 дней',
            '📊 Расчет БЖУ и калорий',
            '💧 Рекомендации по водному режиму',
            '👨‍🍳 Подробные рецепты на 7 дней'
        ]
    },
    'premium': {
        'name': '👑 Премиум пакет',
        'description': 'Полный комплект + бонусы',
        'price': 300,
        'features': [
            '📅 План питания на 7 дней',
            '📊 Расчет БЖУ и калорий',
            '👨‍🍳 Подробные рецепты на 7 дней',
            '🛒 Умный список покупок',
            '💧 Детальный водный режим',
            '🎁 Бонус: гайд по ПП'
        ]
    }
}

class SubscriptionChecker:
    async def check_subscription(self, user_id: int, bot) -> bool:
        """Проверка подписки пользователя на канал"""
        try:
            chat_member = await bot.get_chat_member(
                chat_id=CHANNEL_USERNAME, 
                user_id=user_id
            )
            
            valid_statuses = ['member', 'administrator', 'creator']
            return chat_member.status in valid_statuses
            
        except Exception as e:
            logger.error(f"Ошибка проверки подписки: {e}")
            return False

class NutritionProfessor:
    def __init__(self):
        self.required_days = ['понедельник', 'вторник', 'среда', 'четверг', 'пятница', 'суббота', 'воскресенье']
        self.meals = ['завтрак', 'перекус', 'обед', 'перекус', 'ужин']
    
    def calculate_bmi(self, height: int, weight: int) -> float:
        """Расчет индекса массы тела"""
        return round(weight / ((height / 100) ** 2), 1)
    
    def calculate_calories(self, user_data: Dict[str, Any]) -> int:
        """Расчет суточной нормы калорий по формуле Миффлина-Сан Жеора"""
        age = int(user_data['age'])
        height = int(user_data['height'])
        weight = int(user_data['weight'])
        gender = user_data['gender']
        activity = user_data['activity']
        
        if gender == 'мужской':
            bmr = 10 * weight + 6.25 * height - 5 * age + 5
        else:
            bmr = 10 * weight + 6.25 * height - 5 * age - 161
        
        activity_multipliers = {
            'сидячий': 1.2,
            'умеренная': 1.375,
            'активный': 1.55,
            'очень активный': 1.725
        }
        
        maintenance = bmr * activity_multipliers.get(activity, 1.375)
        
        goal = user_data['goal']
        if goal == 'похудение':
            return int(maintenance * 0.85)
        elif goal == 'набор мышечной массы':
            return int(maintenance * 1.15)
        else:
            return int(maintenance)
    
    def calculate_bju(self, user_data: Dict[str, Any], calories: int) -> Dict[str, float]:
        """Расчет БЖУ"""
        goal = user_data['goal']
        
        if goal == 'похудение':
            protein_ratio = 0.30
            fat_ratio = 0.25
            carb_ratio = 0.45
        elif goal == 'набор мышечной массы':
            protein_ratio = 0.35
            fat_ratio = 0.25
            carb_ratio = 0.40
        else:
            protein_ratio = 0.25
            fat_ratio = 0.25
            carb_ratio = 0.50
        
        protein = (calories * protein_ratio) / 4
        fat = (calories * fat_ratio) / 9
        carbs = (calories * carb_ratio) / 4
        
        return {
            'protein': round(protein),
            'fat': round(fat),
            'carbs': round(carbs)
        }
    
    def calculate_water_intake(self, weight: int) -> Dict[str, Any]:
        """Расчет водного режима"""
        min_water = weight * 30
        max_water = weight * 40
        avg_water = (min_water + max_water) // 2
        
        water_schedule = [
            {"time": "07:00", "amount": 250, "description": "Стакан теплой воды натощак для запуска метаболизма"},
            {"time": "08:30", "amount": 200, "description": "После завтрака - способствует пищеварению"},
            {"time": "10:00", "amount": 200, "description": "Между завтраком и перекусом - поддержание гидратации"},
            {"time": "11:30", "amount": 200, "description": "Перед обедом - подготовка ЖКТ к приему пищи"},
            {"time": "13:30", "amount": 200, "description": "После обеда - через 30 минут после еды"},
            {"time": "15:00", "amount": 200, "description": "Во второй половине дня - поддержание энергии"},
            {"time": "17:00", "amount": 200, "description": "Перед ужином - снижение аппетита"},
            {"time": "19:00", "amount": 200, "description": "После ужина - завершение дневной нормы"}
        ]
        
        return {
            "min_water": min_water,
            "max_water": max_water,
            "avg_water": avg_water,
            "schedule": water_schedule,
            "recommendations": [
                "Пейте воду комнатной температуры",
                "Не пейте во время еды - только за 30 минут до и через 1 час после",
                "Увеличьте потребление воды при физических нагрузках",
                "Ограничьте потребление жидкости за 2 часа до сна"
            ]
        }

class AdminPanel:
    def __init__(self):
        self.orders = {}
        self.admin_chat_id = ADMIN_CHAT_ID
    
    async def notify_admin_new_order(self, order_id: str, user_data: dict, bot):
        """Уведомление администратора о новом заказе"""
        if not self.admin_chat_id:
            return
            
        order_info = (
            f"🆕 НОВЫЙ ЗАКАЗ!\n\n"
            f"👤 Клиент: {user_data.get('user_name', 'Не указано')}\n"
            f"📞 User ID: {user_data.get('user_id')}\n"
            f"📦 Пакет: {user_data.get('selected_package')}\n"
            f"💵 Сумма: {SERVICE_PACKAGES[user_data.get('selected_package')]['price']}₽\n"
            f"📋 Номер: #{order_id}\n\n"
            f"⚡ Действия:\n"
            f"/confirm_{order_id} - Подтвердить оплату\n"
            f"/send_{order_id} - Отправить план\n"
            f"/cancel_{order_id} - Отменить заказ"
        )
        
        await bot.send_message(
            chat_id=self.admin_chat_id,
            text=order_info
        )
    
    async def send_plan_to_client(self, order_id: str, bot, files_info: list):
        """Отправка плана КОНКРЕТНОМУ клиенту"""
        order = self.orders.get(order_id)
        if not order:
            return False
        
        user_id = order['user_id']
        
        try:
            await bot.send_message(
                chat_id=user_id,
                text="🎉 Ваш план питания готов! Присылаю файлы..."
            )
            
            for file_info in files_info:
                with open(file_info['path'], 'rb') as f:
                    await bot.send_document(
                        chat_id=user_id,
                        document=InputFile(f, filename=file_info['filename']),
                        caption=file_info['description']
                    )
            
            await bot.send_message(
                chat_id=user_id,
                text=(
                    "📦 Вы получили полный комплект документов!\n\n"
                    "💡 Для продолжения journey подпишитесь на канал:\n"
                    "👉 @ppsupershef\n\n"
                    "Спасибо за заказ! ❤️\n"
                    "Хотите создать еще один план?"
                ),
                reply_markup=ReplyKeyboardMarkup([
                    ["🆕 Новый план"], 
                    ["📢 Наш канал"]
                ], resize_keyboard=True)
            )
            
            order['status'] = 'delivered'
            return True
            
        except Exception as e:
            logger.error(f"Ошибка отправки клиенту {user_id}: {e}")
            return False

class PaymentManager:
    def __init__(self):
        self.pending_orders = {}
    
    def create_order(self, user_data: dict, package: str) -> str:
        """Создание заказа и блокировка формирования плана"""
        order_id = f"nutrition{random.randint(10000, 99999)}"
        
        self.pending_orders[order_id] = {
            'user_data': user_data.copy(),
            'package': package,
            'status': 'pending',
            'created_at': datetime.now(),
            'user_id': user_data.get('user_id')
        }
        
        return order_id
    
    def confirm_payment(self, order_id: str) -> bool:
        """Подтверждение оплаты (вызывается администратором)"""
        if order_id in self.pending_orders:
            self.pending_orders[order_id]['status'] = 'paid'
            return True
        return False
    
    def can_generate_plan(self, order_id: str) -> bool:
        """Проверка можно ли формировать план"""
        order = self.pending_orders.get(order_id)
        return order and order['status'] == 'paid'

class ReminderSystem:
    def __init__(self):
        self.follow_ups = {}
    
    async def schedule_follow_up(self, user_id: int, context: ContextTypes.DEFAULT_TYPE):
        """Планирование напоминания через 3 дня"""
        self.follow_ups[user_id] = datetime.now() + timedelta(days=3)
        
        context.job_queue.run_once(
            callback=self.send_follow_up,
            when=timedelta(days=3),
            data=user_id,
            name=f"follow_up_{user_id}"
        )
    
    async def send_follow_up(self, context: ContextTypes.DEFAULT_TYPE):
        """Отправка напоминания через 3 дня"""
        user_id = context.job.data
        
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text="📊 Как ваши успехи с планом питания?\n\n"
                     "• Следуете рекомендациям?\n"
                     "• Есть вопросы?\n" 
                     "• Нужна корректировка?",
                reply_markup=ReplyKeyboardMarkup([
                    ["✅ Все отлично", "❓ Есть вопросы"],
                    ["🔄 Нужна корректировка", "🆕 Новый план"]
                ], resize_keyboard=True)
            )
            
            if user_id in self.follow_ups:
                del self.follow_ups[user_id]
                
        except Exception as e:
            logger.error(f"Ошибка отправки напоминания {user_id}: {e}")

class KnowledgeBase:
    def __init__(self):
        self.articles = {
            "bju": {
                "title": "📚 Что такое БЖУ?",
                "content": (
                    "🔍 *БЖУ* - это Белки, Жиры, Углеводы - основные нутриенты:\n\n"
                    "🥚 *Белки:* 1 г = 4 ккал\n"
                    "• Строительный материал для мышц\n"
                    "• Источники: мясо, рыба, яйца, творог\n\n"
                    "🥑 *Жиры:* 1 г = 9 ккал\n" 
                    "• Энергия и гормональная система\n"
                    "• Источники: орехи, авокадо, масла\n\n"
                    "🌾 *Углеводы:* 1 г = 4 ккал\n"
                    "• Основной источник энергии\n"
                    "• Источники: крупы, фрукты, овощи"
                )
            },
            "water": {
                "title": "💧 Водный режим", 
                "content": (
                    "💧 *Правильный водный режим:*\n\n"
                    "• 30-40 мл на 1 кг веса в день\n"
                    "• Пить за 30 минут ДО еды\n"
                    "• Не пить во время еды\n"
                    "• Пить через 1 час ПОСЛЕ еды\n"
                    "• Лучше вода комнатной температуры"
                )
            }
        }

class LoyaltySystem:
    def __init__(self):
        self.client_weeks = {}
    
    def add_week_plan(self, user_id: int):
        """Добавление недельного плана в историю"""
        if user_id not in self.client_weeks:
            self.client_weeks[user_id] = 0
        self.client_weeks[user_id] += 1
    
    def check_discount_eligibility(self, user_id: int) -> bool:
        """Проверка права на скидку (4 недели)"""
        return self.client_weeks.get(user_id, 0) >= 4
    
    async def offer_discount(self, user_id: int, bot):
        """Предложение скидки лояльному клиенту"""
        if self.check_discount_eligibility(user_id):
            await bot.send_message(
                chat_id=user_id,
                text="🎁 Вы получили скидку 15% на следующий план!\n\n"
                     "Спасибо за вашу лояльность! ❤️",
                reply_markup=ReplyKeyboardMarkup([
                    ["🆕 Новый план со скидкой"],
                    ["📢 Наш канал"]
                ], resize_keyboard=True)
            )
            return True
        return False

async def send_channel_notification(order_id: str, user_data: dict, bot):
    """Отправка уведомления в канал о новом плане"""
    try:
        message_text = (
            "🎯 *Внимание! Создан новый план индивидуального питания*\n\n"
            f"👤 *Клиент:* {user_data.get('user_name', 'Новый клиент')}\n"
            f"⚡ *Статус:* План сформирован и отправлен\n\n"
            f"#{order_id} #новыйклиент #планпитания"
        )
        
        await bot.send_message(
            chat_id=CHANNEL_USERNAME,
            text=message_text,
            parse_mode='Markdown'
        )
        logger.info(f"Уведомление отправлено в канал для заказа {order_id}")
        
    except Exception as e:
        logger.error(f"Ошибка отправки в канал: {e}")

# Инициализация классов
nutrition_professor = NutritionProfessor()
subscription_checker = SubscriptionChecker()
admin_panel = AdminPanel()
payment_manager = PaymentManager()
reminder_system = ReminderSystem()
knowledge_base = KnowledgeBase()
loyalty_system = LoyaltySystem()

# Создание приложения Telegram
application = Application.builder().token(BOT_TOKEN).build()

# Клавиатуры
def create_goal_keyboard(show_back: bool = False):
    keyboard = [["похудение", "поддержание веса"], ["набор мышечной массы"]]
    if show_back:
        keyboard.append(["◀️ Назад"])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def create_diet_keyboard(show_back: bool = False):
    keyboard = [["стандарт", "вегетарианская"], ["веганская", "безглютеновая"], ["низкоуглеводная"]]
    if show_back:
        keyboard.append(["◀️ Назад"])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def create_allergies_keyboard(show_back: bool = False):
    keyboard = [["нет", "орехи"], ["молочные продукты", "яйца"], ["рыба/морепродукты"]]
    if show_back:
        keyboard.append(["◀️ Назад"])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def create_gender_keyboard(show_back: bool = False):
    keyboard = [["мужской", "женский"]]
    if show_back:
        keyboard.append(["◀️ Назад"])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def create_activity_keyboard(show_back: bool = False):
    keyboard = [["сидячий", "умеренная"], ["активный", "очень активный"]]
    if show_back:
        keyboard.append(["◀️ Назад"])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def create_confirmation_keyboard():
    keyboard = [["✅ Да, все верно", "✏️ Редактировать параметры"]]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def create_edit_keyboard():
    keyboard = [
        ["🎯 Цель", "🥗 Тип диеты"],
        ["⚠️ Аллергии", "👤 Пол"],
        ["🎂 Возраст", "📏 Рост"],
        ["⚖️ Вес", "🏃‍♂️ Активность"],
        ["✅ Завершить редактирование"]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def create_packages_keyboard():
    keyboard = [
        ["🎯 Базовый - 100₽", "🚀 Стандартный - 200₽"],
        ["👑 Премиум - 300₽", "❓ Сравнить пакеты"],
        ["◀️ Назад к анкете"]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def create_payment_keyboard():
    keyboard = [
        ["💳 Оплатить картой", "📱 СБП перевод"],
        ["📲 Реквизиты для банка", "◀️ Выбрать другой пакет"]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def create_subscription_keyboard():
    keyboard = [
        ["✅ Проверить подписку", "📢 Перейти в канал"],
        ["🏠 Главное меню"]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def create_new_plan_keyboard():
    keyboard = [
        ["🆕 Новый план"],
        ["📢 Наш канал"]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_progress_text(user_data: Dict[str, Any], current_step: str = None) -> str:
    """Генерация текста с прогрессом и параметрами"""
    progress = "📊 ВВЕДЕННЫЕ ПАРАМЕТРЫ:\n"
    
    steps = {
        'goal': '🎯 Цель',
        'diet': '🥗 Тип диеты', 
        'allergies': '⚠️ Аллергии',
        'gender': '👤 Пол',
        'age': '🎂 Возраст',
        'height': '📏 Рост',
        'weight': '⚖️ Вес',
        'activity': '🏃‍♂️ Активность'
    }
    
    for key, description in steps.items():
        if key in user_data:
            value = user_data[key]
            if current_step == key:
                progress += f"   {description}: {value} ✅\n"
            else:
                progress += f"   {description}: {value}\n"
        else:
            if current_step == key:
                progress += f"   {description}: ... 🔄\n"
            else:
                progress += f"   {description}: ❌\n"
    
    return progress

async def show_subscription_required(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Показ сообщения о необходимости подписки"""
    subscription_text = (
        "👋 Добро пожаловать в бот профессора нутрициологии!\n\n"
        "📢 Для использования бота необходимо быть подписанным на наш канал с полезными материалами по правильному питанию.\n\n"
        "✅ Проверяем подписку...\n\n"
        "❌ Подписка не активна\n\n"
        "Пожалуйста, подпишитесь на канал:\n"
        f"👉 {CHANNEL_USERNAME}\n\n"
        "После подписки нажмите кнопку «Проверить подписку»"
    )
    
    await update.message.reply_text(
        subscription_text,
        reply_markup=create_subscription_keyboard(),
        disable_web_page_preview=False
    )
    return SUBSCRIPTION_CHECK

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Начало работы с проверкой подписки"""
    user_id = update.effective_user.id
    
    is_subscribed = await subscription_checker.check_subscription(user_id, context.bot)
    
    if not is_subscribed:
        await show_subscription_required(update, context)
        return SUBSCRIPTION_CHECK
    
    context.user_data.clear()
    await update.message.reply_text(
        "✅ Отлично! Подписка активна.\n\n"
        "Теперь я создам для вас индивидуальный план питания на 7 дней с учетом:\n"
        "• 🎯 Ваших целей и параметров\n"  
        "• 🥗 Диетических предпочтений\n"
        "• 💧 Водного режима\n"
        "• 📊 Баланса БЖУ\n\n"
        "Давайте начнем! Выберите вашу цель:",
        reply_markup=create_goal_keyboard()
    )
    return GOAL

async def process_subscription_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработка проверки подписки"""
    text = update.message.text
    user_id = update.effective_user.id
    
    if text == "📢 Перейти в канал":
        channel_link = f"https://t.me/{CHANNEL_USERNAME.replace('@', '')}"
        await update.message.reply_text(
            f"📢 Перейдите по ссылке и подпишитесь на канал:\n{channel_link}\n\n"
            "После подписки нажмите «Проверить подписку»",
            reply_markup=ReplyKeyboardMarkup([["✅ Проверить подписку"]], resize_keyboard=True)
        )
        return SUBSCRIPTION_CHECK
    
    elif text == "✅ Проверить подписку":
        is_subscribed = await subscription_checker.check_subscription(user_id, context.bot)
        
        if is_subscribed:
            await update.message.reply_text(
                "✅ Отлично! Подписка активна.\n\n"
                "Теперь я создам для вас индивидуальный план питания...",
                reply_markup=ReplyKeyboardRemove()
            )
            return await start(update, context)
        else:
            await update.message.reply_text(
                "❌ Подписка все еще не активна.\n\n"
                "Пожалуйста, убедитесь, что вы подписались на канал и нажмите «Проверить подписку» еще раз.",
                reply_markup=create_subscription_keyboard()
            )
            return SUBSCRIPTION_CHECK
    
    return SUBSCRIPTION_CHECK

# Обработчики состояний анкеты
async def process_goal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработка выбора цели"""
    text = update.message.text
    
    if text == "◀️ Назад":
        await update.message.reply_text(
            "Начинаем заново! Выберите вашу цель:",
            reply_markup=create_goal_keyboard()
        )
        return GOAL
    
    if text not in ['похудение', 'поддержание веса', 'набор мышечной массы']:
        await update.message.reply_text("Пожалуйста, выберите цель из предложенных вариантов:")
        return GOAL
    
    context.user_data['goal'] = text
    
    progress_text = get_progress_text(context.user_data, 'goal')
    await update.message.reply_text(
        f"{progress_text}\n\n"
        "🥗 Теперь выберите тип диеты:",
        reply_markup=create_diet_keyboard(show_back=True)
    )
    return DIET

async def process_diet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработка выбора диеты"""
    text = update.message.text
    
    if text == "◀️ Назад":
        progress_text = get_progress_text(context.user_data, 'goal')
        await update.message.reply_text(
            f"{progress_text}\n\n"
            "Выберите вашу цель:",
            reply_markup=create_goal_keyboard(show_back=True)
        )
        return GOAL
    
    if text not in ['стандарт', 'вегетарианская', 'веганская', 'безглютеновая', 'низкоуглеводная']:
        await update.message.reply_text("Пожалуйста, выберите тип диеты из предложенных вариантов:")
        return DIET
    
    context.user_data['diet'] = text
    
    progress_text = get_progress_text(context.user_data, 'diet')
    await update.message.reply_text(
        f"{progress_text}\n\n"
        "⚠️ Есть ли у вас аллергии или непереносимости?",
        reply_markup=create_allergies_keyboard(show_back=True)
    )
    return ALLERGIES

async def process_allergies(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработка выбора аллергий"""
    text = update.message.text
    
    if text == "◀️ Назад":
        progress_text = get_progress_text(context.user_data, 'diet')
        await update.message.reply_text(
            f"{progress_text}\n\n"
            "Выберите тип диеты:",
            reply_markup=create_diet_keyboard(show_back=True)
        )
        return DIET
    
    if text not in ['нет', 'орехи', 'молочные продукты', 'яйца', 'рыба/морепродукты']:
        await update.message.reply_text("Пожалуйста, выберите вариант из предложенных:")
        return ALLERGIES
    
    context.user_data['allergies'] = text
    
    progress_text = get_progress_text(context.user_data, 'allergies')
    await update.message.reply_text(
        f"{progress_text}\n\n"
        "👤 Укажите ваш пол:",
        reply_markup=create_gender_keyboard(show_back=True)
    )
    return GENDER

async def process_gender(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработка выбора пола"""
    text = update.message.text
    
    if text == "◀️ Назад":
        progress_text = get_progress_text(context.user_data, 'allergies')
        await update.message.reply_text(
            f"{progress_text}\n\n"
            "Выберите аллергии:",
            reply_markup=create_allergies_keyboard(show_back=True)
        )
        return ALLERGIES
    
    if text not in ['мужской', 'женский']:
        await update.message.reply_text("Пожалуйста, выберите пол из предложенных вариантов:")
        return GENDER
    
    context.user_data['gender'] = text
    
    progress_text = get_progress_text(context.user_data, 'gender')
    await update.message.reply_text(
        f"{progress_text}\n\n"
        "🎂 Укажите ваш возраст (полных лет, от 10 до 100):",
        reply_markup=ReplyKeyboardRemove()
    )
    return AGE

async def process_age(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработка возраста"""
    text = update.message.text
    
    if text == "◀️ Назад":
        progress_text = get_progress_text(context.user_data, 'gender')
        await update.message.reply_text(
            f"{progress_text}\n\n"
            "Выберите пол:",
            reply_markup=create_gender_keyboard(show_back=True)
        )
        return GENDER
    
    try:
        age = int(text)
        if age < 10 or age > 100:
            await update.message.reply_text("Пожалуйста, введите реальный возраст (10-100 лет):")
            return AGE
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите число:")
        return AGE
    
    context.user_data['age'] = age
    
    progress_text = get_progress_text(context.user_data, 'age')
    await update.message.reply_text(
        f"{progress_text}\n\n"
        "📏 Укажите ваш рост (в см, от 100 до 250):"
    )
    return HEIGHT

async def process_height(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработка роста"""
    text = update.message.text
    
    if text == "◀️ Назад":
        progress_text = get_progress_text(context.user_data, 'age')
        await update.message.reply_text(
            f"{progress_text}\n\n"
            "Введите возраст:"
        )
        return AGE
    
    try:
        height = int(text)
        if height < 100 or height > 250:
            await update.message.reply_text("Пожалуйста, введите реальный рост (100-250 см):")
            return HEIGHT
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите число:")
        return HEIGHT
    
    context.user_data['height'] = height
    
    progress_text = get_progress_text(context.user_data, 'height')
    await update.message.reply_text(
        f"{progress_text}\n\n"
        "⚖️ Укажите ваш вес (в кг, от 30 до 300):"
    )
    return WEIGHT

async def process_weight(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработка веса"""
    text = update.message.text
    
    if text == "◀️ Назад":
        progress_text = get_progress_text(context.user_data, 'height')
        await update.message.reply_text(
            f"{progress_text}\n\n"
            "Введите рост:"
        )
        return HEIGHT
    
    try:
        weight = int(text)
        if weight < 30 or weight > 300:
            await update.message.reply_text("Пожалуйста, введите реальный вес (30-300 кг):")
            return WEIGHT
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите число:")
        return WEIGHT
    
    context.user_data['weight'] = weight
    
    progress_text = get_progress_text(context.user_data, 'weight')
    await update.message.reply_text(
        f"{progress_text}\n\n"
        "🏃‍♂️ Укажите ваш уровень физической активности:",
        reply_markup=create_activity_keyboard(show_back=True)
    )
    return ACTIVITY

async def process_activity(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработка уровня активности"""
    text = update.message.text
    
    if text == "◀️ Назад":
        progress_text = get_progress_text(context.user_data, 'weight')
        await update.message.reply_text(
            f"{progress_text}\n\n"
            "Введите вес:"
        )
        return WEIGHT
    
    if text not in ['сидячий', 'умеренная', 'активный', 'очень активный']:
        await update.message.reply_text("Пожалуйста, выберите уровень активности из предложенных вариантов:")
        return ACTIVITY
    
    context.user_data['activity'] = text
    
    # Показываем все введенные параметры для подтверждения
    professor = NutritionProfessor()
    calories = professor.calculate_calories(context.user_data)
    bju = professor.calculate_bju(context.user_data, calories)
    water = professor.calculate_water_intake(int(context.user_data['weight']))
    
    progress_text = get_progress_text(context.user_data, 'activity')
    confirmation_text = (
        f"{progress_text}\n\n"
        "📊 РАСЧЕТНЫЕ ПОКАЗАТЕЛИ:\n"
        f"   • 🔥 Суточная норма калорий: {calories} ккал\n"
        f"   • 🥚 Белки: {bju['protein']}г | 🥑 Жиры: {bju['fat']}г | 🌾 Углеводы: {bju['carbs']}г\n"
        f"   • 💧 Норма воды: {water['avg_water']} мл/день\n\n"
        "✅ Все данные введены! Проверьте правильность и подтвердите:"
    )
    
    await update.message.reply_text(
        confirmation_text,
        reply_markup=create_confirmation_keyboard()
    )
    return CONFIRMATION

async def process_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработка подтверждения параметров"""
    text = update.message.text
    
    if text == "✅ Да, все верно":
        user_data = context.user_data
        
        professor = NutritionProfessor()
        calories = professor.calculate_calories(user_data)
        bju = professor.calculate_bju(user_data, calories)
        water = professor.calculate_water_intake(int(user_data['weight']))
        
        package_selection_text = (
            "📦 ВЫБЕРИТЕ ПАКЕТ УСЛУГ:\n\n"
            "🎯 **Базовый пакет** - 100₽\n"
            "• План питания на 7 дней\n"
            "• Расчет БЖУ и калорий\n"
            "• Рекомендации по водному режиму\n\n"
            "🚀 **Стандартный пакет** - 200₽\n"  
            "• Всё из Базового +\n"
            "• Подробные рецепты на 7 дней\n\n"
            "👑 **Премиум пакет** - 300₽\n"
            "• Всё из Стандартного +\n"
            "• Умный список покупок\n"
            "• Детальный водный режим\n"
            "• Бонус: гайд по ПП\n\n"
            "Выберите подходящий вариант:"
        )
        
        await update.message.reply_text(
            package_selection_text,
            reply_markup=create_packages_keyboard()
        )
        return SELECT_PACKAGE
        
    elif text == "✏️ Редактировать параметры":
        await update.message.reply_text(
            "✏️ Выберите параметр для редактирования:",
            reply_markup=create_edit_keyboard()
        )
        return EDIT_PARAMS
    
    else:
        await update.message.reply_text("Пожалуйста, выберите вариант из предложенных:")
        return CONFIRMATION

async def process_edit_params(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработка редактирования параметров"""
    text = update.message.text
    
    edit_handlers = {
        "🎯 Цель": (GOAL, create_goal_keyboard(show_back=True), "Выберите цель:"),
        "🥗 Тип диеты": (DIET, create_diet_keyboard(show_back=True), "Выберите тип диеты:"),
        "⚠️ Аллергии": (ALLERGIES, create_allergies_keyboard(show_back=True), "Выберите аллергии:"),
        "👤 Пол": (GENDER, create_gender_keyboard(show_back=True), "Выберите пол:"),
        "🎂 Возраст": (AGE, ReplyKeyboardRemove(), "Введите возраст:"),
        "📏 Рост": (HEIGHT, ReplyKeyboardRemove(), "Введите рост:"),
        "⚖️ Вес": (WEIGHT, ReplyKeyboardRemove(), "Введите вес:"),
        "🏃‍♂️ Активность": (ACTIVITY, create_activity_keyboard(show_back=True), "Выберите активность:")
    }
    
    if text in edit_handlers:
        next_state, keyboard, message = edit_handlers[text]
        await update.message.reply_text(message, reply_markup=keyboard)
        return next_state
    
    elif text == "✅ Завершить редактирование":
        # Возвращаемся к подтверждению
        professor = NutritionProfessor()
        calories = professor.calculate_calories(context.user_data)
        bju = professor.calculate_bju(context.user_data, calories)
        water = professor.calculate_water_intake(int(context.user_data['weight']))
        
        progress_text = get_progress_text(context.user_data)
        confirmation_text = (
            f"{progress_text}\n\n"
            "📊 РАСЧЕТНЫЕ ПОКАЗАТЕЛИ:\n"
            f"   • 🔥 Суточная норма калорий: {calories} ккал\n"
            f"   • 🥚 Белки: {bju['protein']}г | 🥑 Жиры: {bju['fat']}г | 🌾 Углеводы: {bju['carbs']}г\n"
            f"   • 💧 Норма воды: {water['avg_water']} мл/день\n\n"
            "✅ Все данные введены! Проверьте правильность и подтвердите:"
        )
        
        await update.message.reply_text(
            confirmation_text,
            reply_markup=create_confirmation_keyboard()
        )
        return CONFIRMATION
    
    else:
        await update.message.reply_text("Пожалуйста, выберите параметр для редактирования:")
        return EDIT_PARAMS

async def process_package_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработка выбора пакета"""
    text = update.message.text
    
    if text == "◀️ Назад к анкете":
        return await show_confirmation(update, context)
    
    if text == "❓ Сравнить пакеты":
        comparison_text = (
            "📊 СРАВНЕНИЕ ПАКЕТОВ:\n\n"
            "🎯 БАЗОВЫЙ (100₽):\n"
            "• План питания на 7 дней\n"
            "• Расчет БЖУ и калорий\n"
            "• Рекомендации по водному режиму\n\n"
            "🚀 СТАНДАРТНЫЙ (200₽):\n"
            "• Всё из Базового +\n"
            "• Подробные рецепты на 7 дней\n\n"
            "👑 ПРЕМИУМ (300₽):\n"
            "• Всё из Стандартного +\n"
            "• Умный список покупок\n"
            "• Детальный водный режим\n"
            "• Бонус: гайд по ПП"
        )
        await update.message.reply_text(comparison_text)
        return SELECT_PACKAGE
    
    package_map = {
        "🎯 Базовый - 100₽": "basic",
        "🚀 Стандартный - 200₽": "standard", 
        "👑 Премиум - 300₽": "premium"
    }
    
    if text in package_map:
        package_key = package_map[text]
        context.user_data['selected_package'] = package_key
        
        package = SERVICE_PACKAGES[package_key]
        
        package_info = (
            f"📦 Вы выбрали: {package['name']}\n"
            f"💵 Стоимость: {package['price']}₽\n\n"
            f"📋 Что входит:\n"
        )
        
        for feature in package['features']:
            package_info += f"   ✅ {feature}\n"
            
        package_info += f"\nВыберите способ оплаты:"
        
        await update.message.reply_text(
            package_info,
            reply_markup=create_payment_keyboard()
        )
        return PAYMENT_METHOD
    
    await update.message.reply_text("Пожалуйста, выберите пакет из предложенных:")
    return SELECT_PACKAGE

async def show_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать подтверждение анкеты"""
    professor = NutritionProfessor()
    calories = professor.calculate_calories(context.user_data)
    bju = professor.calculate_bju(context.user_data, calories)
    water = professor.calculate_water_intake(int(context.user_data['weight']))
    
    progress_text = get_progress_text(context.user_data)
    confirmation_text = (
        f"{progress_text}\n\n"
        "📊 РАСЧЕТНЫЕ ПОКАЗАТЕЛИ:\n"
        f"   • 🔥 Суточная норма калорий: {calories} ккал\n"
        f"   • 🥚 Белки: {bju['protein']}г | 🥑 Жиры: {bju['fat']}г | 🌾 Углеводы: {bju['carbs']}г\n"
        f"   • 💧 Норма воды: {water['avg_water']} мл/день\n\n"
        "✅ Все данные введены! Проверьте правильность и подтвердите:"
    )
    
    await update.message.reply_text(
        confirmation_text,
        reply_markup=create_confirmation_keyboard()
    )

async def process_payment_method(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработка выбора способа оплаты"""
    text = update.message.text
    
    if text == "◀️ Выбрать другой пакет":
        return await show_package_selection(update, context)
    
    user_data = context.user_data
    user_id = update.effective_user.id
    
    user_data['user_name'] = update.effective_user.first_name
    user_data['user_id'] = user_id
    
    order_id = payment_manager.create_order(user_data, user_data['selected_package'])
    
    await admin_panel.notify_admin_new_order(order_id, user_data, context.bot)
    
    await update.message.reply_text(
        f"🎉 Заказ принят!\n\n"
        f"📋 Ваш заказ:\n"
        f"• Пакет: {SERVICE_PACKAGES[user_data['selected_package']]['name']}\n"
        f"• Сумма: {SERVICE_PACKAGES[user_data['selected_package']]['price']}₽\n"
        f"• Номер: #{order_id}\n\n"
        f"⏱ Срок формирования плана: 24 часа\n"
        f"📬 План придет в этот чат\n\n"
        f"💡 Пока ждете, посетите наш канал:\n"
        f"👉 @ppsupershef\n\n"
        f"Статус: ❌ Ожидает подтверждения оплаты",
        reply_markup=create_new_plan_keyboard()
    )
    
    context.user_data.clear()
    return ConversationHandler.END

async def show_package_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать выбор пакетов"""
    package_selection_text = (
        "📦 ВЫБЕРИТЕ ПАКЕТ УСЛУГ:\n\n"
        "🎯 **Базовый пакет** - 100₽\n"
        "• План питания на 7 дней\n"
        "• Расчет БЖУ и калорий\n"
        "• Рекомендации по водному режиму\n\n"
        "🚀 **Стандартный пакет** - 200₽\n"  
        "• Всё из Базового +\n"
        "• Подробные рецепты на 7 дней\n\n"
        "👑 **Премиум пакет** - 300₽\n"
        "• Всё из Стандартного +\n"
        "• Умный список покупок\n"
        "• Детальный водный режим\n"
        "• Бонус: гайд по ПП\n\n"
        "Выберите подходящий вариант:"
    )
    
    await update.message.reply_text(
        package_selection_text,
        reply_markup=create_packages_keyboard()
    )

async def handle_new_plan_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка запроса на новый план"""
    text = update.message.text
    
    if text == "🆕 Новый план":
        return await start(update, context)
    
    elif text == "📢 Наш канал":
        await update.message.reply_text(
            "📢 Подпишитесь на наш канал с полезными материалами:\n"
            "👉 @ppsupershef\n\n"
            "Там вы найдете:\n"
            "• Советы по питанию\n"
            "• Рецепты ПП\n" 
            "• Мотивационные истории\n"
            "• Ответы на вопросы",
            reply_markup=create_new_plan_keyboard()
        )

async def handle_follow_up_response(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка ответов на напоминания"""
    text = update.message.text
    
    if text == "✅ Все отлично":
        await update.message.reply_text(
            "🎉 Отлично! Рады, что план работает!\n\n"
            "Хотите заказать план на следующую неделю?",
            reply_markup=create_new_plan_keyboard()
        )
        
    elif text == "❓ Есть вопросы":
        await update.message.reply_text(
            "📞 Напишите ваш вопрос, и мы обязательно поможем!",
            reply_markup=create_new_plan_keyboard()
        )
        
    elif text == "🔄 Нужна корректировка":
        await update.message.reply_text(
            "🔄 Расскажите, что нужно изменить в плане?\n\n"
            "Мы скорректируем его бесплатно!",
            reply_markup=create_new_plan_keyboard()
        )

async def handle_knowledge_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка запросов к базе знаний"""
    text = update.message.text
    
    if text == "📚 Что такое БЖУ?":
        await send_article(update, "bju")
    elif text == "💧 Водный режим":
        await send_article(update, "water")

async def send_article(update: Update, article_key: str):
    """Отправка статьи из базы знаний"""
    article = knowledge_base.articles.get(article_key)
    if article:
        await update.message.reply_text(
            article["content"],
            parse_mode='Markdown',
            reply_markup=ReplyKeyboardMarkup([
                ["📚 Что такое БЖУ?", "💧 Водный режим"],
                ["🆕 Новый план", "📢 Наш канал"]
            ], resize_keyboard=True)
        )

# Обработчики команд админа
async def handle_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка команд администратора"""
    text = update.message.text
    
    if str(update.effective_user.id) != ADMIN_CHAT_ID:
        return
    
    if text.startswith('/confirm_'):
        order_id = text.replace('/confirm_', '')
        await confirm_payment(update, context, order_id)
    
    elif text.startswith('/send_'):
        order_id = text.replace('/send_', '')
        await send_plan_to_client(update, context, order_id)

async def confirm_payment(update: Update, context: ContextTypes.DEFAULT_TYPE, order_id: str):
    """Подтверждение оплаты администратором"""
    order = payment_manager.pending_orders.get(order_id)
    if order:
        payment_manager.confirm_payment(order_id)
        
        await context.bot.send_message(
            chat_id=order['user_id'],
            text=(
                "✅ Оплата подтверждена! Ваш план поставлен в очередь на формирование.\n\n"
                "⏱ План будет готов в течение 24 часов\n"
                "📬 Вы получите уведомление в этом чате\n\n"
                "📢 Подписывайтесь на наш канал:\n"
                "👉 @ppsupershef"
            )
        )
        
        await update.message.reply_text(f"✅ Оплата для заказа #{order_id} подтверждена")

async def send_plan_to_client(update: Update, context: ContextTypes.DEFAULT_TYPE, order_id: str):
    """Отправка плана клиенту"""
    order = payment_manager.pending_orders.get(order_id)
    if not order:
        await update.message.reply_text("❌ Заказ не найден")
        return
    
    # Здесь должна быть ваша логика формирования плана
    # Для примера создаем тестовые файлы
    files_info = [
        {
            'path': f"plans/{order_id}_plan.pdf",
            'filename': f"План_питания_{order_id}.pdf",
            'description': "📅 Ваш индивидуальный план питания на 7 дней"
        },
        {
            'path': f"plans/{order_id}_recipes.pdf", 
            'filename': f"Рецепты_{order_id}.pdf",
            'description': "👨‍🍳 Подробные рецепты всех блюд"
        }
    ]
    
    success = await admin_panel.send_plan_to_client(order_id, context.bot, files_info)
    
    if success:
        await update.message.reply_text(f"✅ План для заказа #{order_id} отправлен клиенту")
        
        # Отправляем уведомление в канал
        await send_channel_notification(order_id, order['user_data'], context.bot)
        
        # Запускаем напоминание через 3 дня
        await reminder_system.schedule_follow_up(order['user_id'], context)
        
        # Добавляем в систему лояльности
        loyalty_system.add_week_plan(order['user_id'])
        await loyalty_system.offer_discount(order['user_id'], context.bot)
        
    else:
        await update.message.reply_text(f"❌ Ошибка отправки плана для заказа #{order_id}")

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Отмена диалога"""
    await update.message.reply_text(
        "Диалог отменен. Чтобы начать заново, нажмите «🆕 Новый план»",
        reply_markup=create_new_plan_keyboard()
    )
    
    context.user_data.clear()
    return ConversationHandler.END

# Настройка обработчиков
conv_handler = ConversationHandler(
    entry_points=[MessageHandler(filters.TEXT & ~filters.COMMAND, start)],
    states={
        SUBSCRIPTION_CHECK: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_subscription_check)],
        GOAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_goal)],
        DIET: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_diet)],
        ALLERGIES: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_allergies)],
        GENDER: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_gender)],
        AGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_age)],
        HEIGHT: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_height)],
        WEIGHT: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_weight)],
        ACTIVITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_activity)],
        CONFIRMATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_confirmation)],
        EDIT_PARAMS: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_edit_params)],
        SELECT_PACKAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_package_selection)],
        PAYMENT_METHOD: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_payment_method)],
    },
    fallbacks=[CommandHandler('cancel', cancel)]
)

application.add_handler(conv_handler)
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_new_plan_request))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_follow_up_response))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_knowledge_request))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_admin_command))

def run_polling():
    """Запуск через polling"""
    print("🔍 Запуск в режиме polling...")
    application.run_polling()

if __name__ == '__main__':
    run_polling()

import os
import json
import re
import logging
from typing import Dict, Any, List
from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, InputFile
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, ConversationHandler
from flask import Flask, request
import requests
import asyncio
from threading import Thread

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
WEBHOOK_URL = os.getenv('WEBHOOK_URL')  # Ваш URL для вебхука

# Состояния беседы
GOAL, DIET, ALLERGIES, GENDER, AGE, HEIGHT, WEIGHT, ACTIVITY, GENERATING = range(9)

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
        
        # Базовый метаболизм
        if gender == 'мужской':
            bmr = 10 * weight + 6.25 * height - 5 * age + 5
        else:
            bmr = 10 * weight + 6.25 * height - 5 * age - 161
        
        # Коэффициент активности
        activity_multipliers = {
            'сидячий': 1.2,
            'умеренная': 1.375,
            'активный': 1.55,
            'очень активный': 1.725
        }
        
        maintenance = bmr * activity_multipliers.get(activity, 1.375)
        
        # Корректировка по цели
        goal = user_data['goal']
        if goal == 'похудение':
            return int(maintenance * 0.85)
        elif goal == 'набор мышечной массы':
            return int(maintenance * 1.15)
        else:  # поддержание веса
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
        else:  # поддержание веса
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
    
    def create_professor_prompt(self, user_data: Dict[str, Any]) -> str:
        """Создание промпта для профессора нутрициологии"""
        calories = self.calculate_calories(user_data)
        bju = self.calculate_bju(user_data, calories)
        bmi = self.calculate_bmi(int(user_data['height']), int(user_data['weight']))
        
        return f"""
        Ты профессор нутрициологии с 40-летним опытом. Составь индивидуальный план питания на 7 дней.

        ПАРАМЕТРЫ КЛИЕНТА:
        - Пол: {user_data['gender']}
        - Возраст: {user_data['age']} лет
        - Рост: {user_data['height']} см
        - Вес: {user_data['weight']} кг
        - ИМТ: {bmi}
        - Уровень активности: {user_data['activity']}
        - Цель: {user_data['goal']}
        - Тип диеты: {user_data['diet']}
        - Аллергии: {user_data['allergies']}

        РАСЧЕТНЫЕ ПОКАЗАТЕЛИ:
        - Суточная норма калорий: {calories} ккал
        - Рекомендуемое БЖУ: {bju['protein']}г белка, {bju['fat']}г жиров, {bju['carbs']}г углеводов

        ТРЕБОВАНИЯ К ПЛАНУ:
        1. 5 приемов пищи ежедневно: завтрак, перекус, обед, перекус, ужин
        2. Сбалансированное БЖУ согласно принципам нутрициологии
        3. Учет циркадных ритмов и времени приема пищи
        4. Разнообразие блюд без повторений в течение недели
        5. Практическая реализуемость рецептов
        6. Учет диетических ограничений и аллергий

        ФОРМАТ ОТВЕТА - STRICT JSON:
        {{
            "понедельник": {{
                "завтрак": {{
                    "name": "Название блюда",
                    "calories": 350,
                    "protein": 20,
                    "carbs": 40,
                    "fat": 10,
                    "ingredients": [
                        {{"name": "ингредиент", "quantity": 100, "unit": "гр"}}
                    ],
                    "recipe": "Подробный рецепт приготовления"
                }},
                "перекус": {{...}},
                "обед": {{...}},
                "перекус": {{...}},
                "ужин": {{...}},
                "total_calories": {calories},
                "total_protein": {bju['protein']},
                "total_carbs": {bju['carbs']},
                "total_fat": {bju['fat']}
            }},
            "вторник": {{...}},
            "среда": {{...}},
            "четверг": {{...}},
            "пятница": {{...}},
            "суббота": {{...}},
            "воскресенье": {{...}}
        }}

        Соблюдай общую калорийность {calories} ±100 ккал в день и баланс БЖУ.
        Верни ТОЛЬКО JSON без дополнительного текста.
        """

class YandexGPTClient:
    def __init__(self):
        self.folder_id = YANDEX_FOLDER_ID
        self.api_key = YANDEX_API_KEY
        self.url = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"
    
    def get_completion(self, prompt: str) -> str:
        """Получение ответа от Yandex GPT"""
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Api-Key {self.api_key}",
            "x-folder-id": self.folder_id
        }
        
        data = {
            "modelUri": f"gpt://{self.folder_id}/yandexgpt-lite",
            "completionOptions": {
                "stream": False,
                "temperature": 0.7,
                "maxTokens": 4000
            },
            "messages": [
                {
                    "role": "system",
                    "text": "Ты профессор нутрициологии с 40-летним опытом."
                },
                {
                    "role": "user",
                    "text": prompt
                }
            ]
        }
        
        try:
            response = requests.post(self.url, headers=headers, json=data, timeout=60)
            response.raise_for_status()
            result = response.json()
            return result['result']['alternatives'][0]['message']['text']
        except Exception as e:
            logger.error(f"Ошибка Yandex GPT: {e}")
            return ""

class NutritionPlanParser:
    def __init__(self):
        self.validator = NutritionValidator()
    
    def parse_gpt_response(self, response_text: str, user_data: Dict[str, Any]) -> Dict[str, Any]:
        """Парсинг ответа от Yandex GPT"""
        try:
            cleaned_text = self._clean_response_text(response_text)
            nutrition_plan = json.loads(cleaned_text)
            
            if self.validator.validate_plan_structure(nutrition_plan):
                if self.validator.validate_nutrition_values(nutrition_plan, user_data):
                    return nutrition_plan
            
            return self._create_fallback_plan(user_data)
            
        except Exception as e:
            logger.error(f"Ошибка парсинга: {e}")
            return self._create_fallback_plan(user_data)
    
    def _clean_response_text(self, text: str) -> str:
        """Очистка текста ответа"""
        text = re.sub(r'```json\s*', '', text)
        text = re.sub(r'\s*```', '', text)
        return text.strip()
    
    def _create_fallback_plan(self, user_data: Dict[str, Any]) -> Dict[str, Any]:
        """Создание резервного плана питания"""
        plan = {}
        for day in ['понедельник', 'вторник', 'среда', 'четверг', 'пятница', 'суббота', 'воскресенье']:
            plan[day] = {
                'завтрак': self._create_fallback_meal('завтрак'),
                'перекус': self._create_fallback_meal('перекус'),
                'обед': self._create_fallback_meal('обед'),
                'перекус': self._create_fallback_meal('перекус'),
                'ужин': self._create_fallback_meal('ужин'),
                'total_calories': 2000,
                'total_protein': 100,
                'total_carbs': 250,
                'total_fat': 65
            }
        return plan
    
    def _create_fallback_meal(self, meal_type: str) -> Dict[str, Any]:
        """Создание резервного приема пищи"""
        meals = {
            'завтрак': {'name': 'Овсяная каша с ягодами', 'calories': 350},
            'перекус': {'name': 'Йогурт натуральный', 'calories': 150},
            'обед': {'name': 'Куриная грудка с гречкой', 'calories': 450},
            'ужин': {'name': 'Рыба на пару с овощами', 'calories': 400}
        }
        
        meal = meals.get(meal_type, {'name': 'Блюдо', 'calories': 300})
        return {
            'name': meal['name'],
            'calories': meal['calories'],
            'protein': 20,
            'carbs': 40,
            'fat': 10,
            'ingredients': [{'name': 'продукт', 'quantity': 100, 'unit': 'гр'}],
            'recipe': 'Рецепт приготовления блюда'
        }

class NutritionValidator:
    def validate_plan_structure(self, plan: Dict[str, Any]) -> bool:
        """Валидация структуры плана питания"""
        required_days = ['понедельник', 'вторник', 'среда', 'четверг', 'пятница', 'суббота', 'воскресенье']
        required_meals = ['завтрак', 'перекус', 'обед', 'перекус', 'ужин']
        required_meal_fields = ['name', 'calories', 'protein', 'carbs', 'fat', 'ingredients', 'recipe']
        
        try:
            for day in required_days:
                if day not in plan:
                    return False
                
                for meal in required_meals:
                    if meal not in plan[day]:
                        return False
                    
                    for field in required_meal_fields:
                        if field not in plan[day][meal]:
                            return False
            
            return True
        except:
            return False
    
    def validate_nutrition_values(self, plan: Dict[str, Any], user_data: Dict[str, Any]) -> bool:
        """Валидация нутриционных значений"""
        professor = NutritionProfessor()
        target_calories = professor.calculate_calories(user_data)
        tolerance = 100
        
        try:
            for day in plan.values():
                day_calories = day.get('total_calories', 0)
                if abs(day_calories - target_calories) > tolerance:
                    return False
            return True
        except:
            return False

class ShoppingListGenerator:
    def generate_shopping_list(self, nutrition_plan: Dict[str, Any]) -> Dict[str, Any]:
        """Генерация списка покупок"""
        shopping_list = {}
        
        for day in nutrition_plan.values():
            for meal_type in ['завтрак', 'перекус', 'обед', 'перекус', 'ужин']:
                meal = day[meal_type]
                ingredients = meal.get('ingredients', [])
                self._aggregate_ingredients(shopping_list, ingredients)
        
        return self._categorize_ingredients(shopping_list)
    
    def _aggregate_ingredients(self, shopping_list: Dict[str, Any], ingredients: List[Dict]):
        """Агрегация ингредиентов"""
        for ingredient in ingredients:
            name = ingredient['name'].lower().strip()
            quantity = self._parse_quantity(ingredient['quantity'])
            unit = ingredient.get('unit', 'гр')
            
            key = f"{name}_{unit}"
            
            if key in shopping_list:
                shopping_list[key]['quantity'] += quantity
            else:
                shopping_list[key] = {
                    'name': name,
                    'quantity': quantity,
                    'unit': unit
                }
    
    def _parse_quantity(self, quantity) -> float:
        """Парсинг количества ингредиента"""
        if isinstance(quantity, (int, float)):
            return float(quantity)
        elif isinstance(quantity, str):
            numbers = re.findall(r'\d+\.?\d*', quantity)
            return float(numbers[0]) if numbers else 100.0
        else:
            return 100.0
    
    def _categorize_ingredients(self, shopping_list: Dict[str, Any]) -> Dict[str, List]:
        """Категоризация ингредиентов"""
        categories = {
            'Овощи и фрукты': [],
            'Мясо и птица': [],
            'Рыба и морепродукты': [],
            'Молочные продукты': [],
            'Крупы и злаки': [],
            'Бакалея': [],
            'Прочее': []
        }
        
        category_keywords = {
            'Овощи и фрукты': ['помидор', 'огурец', 'яблоко', 'банан', 'апельсин', 'морковь', 'лук', 'картофель', 'капуста', 'салат', 'зелень'],
            'Мясо и птица': ['куриц', 'говядин', 'свинин', 'индейк', 'фарш', 'грудк', 'мясо'],
            'Рыба и морепродукты': ['рыба', 'лосось', 'тунец', 'креветк', 'кальмар', 'миди'],
            'Молочные продукты': ['молоко', 'йогурт', 'творог', 'сыр', 'кефир', 'сметана', 'масло сливочное'],
            'Крупы и злаки': ['рис', 'гречк', 'овсян', 'пшено', 'макарон', 'хлеб', 'мука'],
            'Бакалея': ['масло оливковое', 'масло растительное', 'соль', 'сахар', 'перец', 'специи', 'соус']
        }
        
        for item_key, item_data in shopping_list.items():
            name = item_data['name']
            categorized = False
            
            for category, keywords in category_keywords.items():
                if any(keyword in name for keyword in keywords):
                    categories[category].append(item_data)
                    categorized = True
                    break
            
            if not categorized:
                categories['Прочее'].append(item_data)
        
        return categories

class FileExporter:
    def export_complete_plan(self, nutrition_plan: Dict[str, Any], shopping_list: Dict[str, Any], user_id: int):
        """Экспорт полного пакета документов"""
        self._export_nutrition_plan(nutrition_plan, user_id)
        self._export_shopping_list(shopping_list, user_id)
        self._export_recipes(nutrition_plan, user_id)
    
    def _export_nutrition_plan(self, plan: Dict[str, Any], user_id: int):
        """Экспорт плана питания"""
        filename = f"nutrition_plan_{user_id}.txt"
        with open(filename, 'w', encoding='utf-8') as f:
            f.write("🎯 ИНДИВИДУАЛЬНЫЙ ПЛАН ПИТАНИЯ ОТ ПРОФЕССОРА НУТРИЦИОЛОГИИ\n\n")
            
            for day, meals_data in plan.items():
                f.write(f"📅 {day.upper()}\n")
                f.write("=" * 50 + "\n")
                
                total_day_calories = 0
                total_day_protein = 0
                total_day_carbs = 0
                total_day_fat = 0
                
                for meal_type in ['завтрак', 'перекус', 'обед', 'перекус', 'ужин']:
                    meal = meals_data[meal_type]
                    f.write(f"\n🍽️ {meal_type.upper()}: {meal['name']}\n")
                    f.write(f"   🔥 Калории: {meal['calories']} ккал\n")
                    f.write(f"   🥚 Белки: {meal['protein']}г | 🥑 Жиры: {meal['fat']}г | 🌾 Углеводы: {meal['carbs']}г\n")
                    
                    total_day_calories += meal['calories']
                    total_day_protein += meal['protein']
                    total_day_carbs += meal['carbs']
                    total_day_fat += meal['fat']
                
                f.write(f"\n📊 ИТОГИ ДНЯ:\n")
                f.write(f"   🔥 Общие калории: {total_day_calories} ккал\n")
                f.write(f"   🥚 Белки: {total_day_protein}г | 🥑 Жиры: {total_day_fat}г | 🌾 Углеводы: {total_day_carbs}г\n")
                f.write("\n" + "=" * 50 + "\n\n")
    
    def _export_shopping_list(self, shopping_list: Dict[str, Any], user_id: int):
        """Экспорт списка покупок"""
        filename = f"shopping_list_{user_id}.txt"
        with open(filename, 'w', encoding='utf-8') as f:
            f.write("🛒 СПИСОК ПОКУПОК НА НЕДЕЛЮ\n\n")
            
            for category, items in shopping_list.items():
                if items:
                    f.write(f"📦 {category.upper()}:\n")
                    for item in items:
                        f.write(f"   ✅ {item['name'].title()}: {item['quantity']} {item['unit']}\n")
                    f.write("\n")
    
    def _export_recipes(self, plan: Dict[str, Any], user_id: int):
        """Экспорт рецептов"""
        filename = f"recipes_{user_id}.txt"
        with open(filename, 'w', encoding='utf-8') as f:
            f.write("👨‍🍳 РЕЦЕПТЫ ОТ ПРОФЕССОРА НУТРИЦИОЛОГИИ\n\n")
            
            for day, meals_data in plan.items():
                f.write(f"📅 {day.upper()}\n")
                f.write("=" * 60 + "\n")
                
                for meal_type in ['завтрак', 'перекус', 'обед', 'перекус', 'ужин']:
                    meal = meals_data[meal_type]
                    f.write(f"\n🍽️ {meal_type.upper()}: {meal['name']}\n")
                    f.write(f"📝 Рецепт: {meal['recipe']}\n")
                    f.write("📋 Ингредиенты:\n")
                    
                    for ingredient in meal.get('ingredients', []):
                        f.write(f"   • {ingredient['name']}: {ingredient['quantity']} {ingredient.get('unit', 'гр')}\n")
                    
                    f.write("\n" + "-" * 40 + "\n")
                
                f.write("\n")

# Инициализация классов
nutrition_professor = NutritionProfessor()
yandex_gpt = YandexGPTClient()
plan_parser = NutritionPlanParser()
shopping_generator = ShoppingListGenerator()
file_exporter = FileExporter()

# Создание приложения Telegram
application = Application.builder().token(BOT_TOKEN).build()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Начало работы с ботом"""
    await update.message.reply_text(
        "👋 Добро пожаловать в бот профессора нутрициологии!\n\n"
        "Я создам для вас индивидуальный план питания на 7 дней с учетом всех ваших параметров.\n\n"
        "📝 Для начала выберите вашу цель:",
        reply_markup=create_goal_keyboard()
    )
    return GOAL

def create_goal_keyboard():
    """Клавиатура для выбора цели"""
    keyboard = [
        ["похудение", "поддержание веса"],
        ["набор мышечной массы"]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def create_diet_keyboard():
    """Клавиатура для выбора типа диеты"""
    keyboard = [
        ["стандарт", "вегетарианская"],
        ["веганская", "безглютеновая"],
        ["низкоуглеводная"]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def create_allergies_keyboard():
    """Клавиатура для выбора аллергий"""
    keyboard = [
        ["нет", "орехи"],
        ["молочные продукты", "яйца"],
        ["рыба/морепродукты"]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def create_gender_keyboard():
    """Клавиатура для выбора пола"""
    keyboard = [
        ["мужской", "женский"]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def create_activity_keyboard():
    """Клавиатура для выбора уровня активности"""
    keyboard = [
        ["сидячий", "умеренная"],
        ["активный", "очень активный"]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

async def process_goal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработка выбора цели"""
    goal = update.message.text
    if goal not in ['похудение', 'поддержание веса', 'набор мышечной массы']:
        await update.message.reply_text("Пожалуйста, выберите цель из предложенных вариантов:")
        return GOAL
    
    context.user_data['goal'] = goal
    await update.message.reply_text(
        "🥗 Выберите тип диеты:",
        reply_markup=create_diet_keyboard()
    )
    return DIET

async def process_diet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработка выбора диеты"""
    diet = update.message.text
    if diet not in ['стандарт', 'вегетарианская', 'веганская', 'безглютеновая', 'низкоуглеводная']:
        await update.message.reply_text("Пожалуйста, выберите тип диеты из предложенных вариантов:")
        return DIET
    
    context.user_data['diet'] = diet
    await update.message.reply_text(
        "⚠️ Есть ли у вас аллергии или непереносимости?",
        reply_markup=create_allergies_keyboard()
    )
    return ALLERGIES

async def process_allergies(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработка выбора аллергий"""
    allergies = update.message.text
    if allergies not in ['нет', 'орехи', 'молочные продукты', 'яйца', 'рыба/морепродукты']:
        await update.message.reply_text("Пожалуйста, выберите вариант из предложенных:")
        return ALLERGIES
    
    context.user_data['allergies'] = allergies
    await update.message.reply_text(
        "👤 Укажите ваш пол:",
        reply_markup=create_gender_keyboard()
    )
    return GENDER

async def process_gender(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработка выбора пола"""
    gender = update.message.text
    if gender not in ['мужской', 'женский']:
        await update.message.reply_text("Пожалуйста, выберите пол из предложенных вариантов:")
        return GENDER
    
    context.user_data['gender'] = gender
    await update.message.reply_text(
        "🎂 Укажите ваш возраст (полных лет):",
        reply_markup=ReplyKeyboardRemove()
    )
    return AGE

async def process_age(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработка возраста"""
    try:
        age = int(update.message.text)
        if age < 10 or age > 100:
            await update.message.reply_text("Пожалуйста, введите реальный возраст (10-100 лет):")
            return AGE
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите число:")
        return AGE
    
    context.user_data['age'] = age
    await update.message.reply_text("📏 Укажите ваш рост (в см):")
    return HEIGHT

async def process_height(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработка роста"""
    try:
        height = int(update.message.text)
        if height < 100 or height > 250:
            await update.message.reply_text("Пожалуйста, введите реальный рост (100-250 см):")
            return HEIGHT
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите число:")
        return HEIGHT
    
    context.user_data['height'] = height
    await update.message.reply_text("⚖️ Укажите ваш вес (в кг):")
    return WEIGHT

async def process_weight(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработка веса"""
    try:
        weight = int(update.message.text)
        if weight < 30 or weight > 300:
            await update.message.reply_text("Пожалуйста, введите реальный вес (30-300 кг):")
            return WEIGHT
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите число:")
        return WEIGHT
    
    context.user_data['weight'] = weight
    await update.message.reply_text(
        "🏃‍♂️ Укажите ваш уровень физической активности:",
        reply_markup=create_activity_keyboard()
    )
    return ACTIVITY

async def process_activity(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработка уровня активности"""
    activity = update.message.text
    if activity not in ['сидячий', 'умеренная', 'активный', 'очень активный']:
        await update.message.reply_text("Пожалуйста, выберите уровень активности из предложенных вариантов:")
        return ACTIVITY
    
    context.user_data['activity'] = activity
    
    # Показываем прогресс
    user_data = context.user_data
    progress_text = (
        "🔄 Собираю ваши данные...\n"
        "✅ Все параметры получены!\n\n"
        "📊 Ваши параметры:\n"
        f"• Цель: {user_data['goal']}\n"
        f"• Диета: {user_data['diet']}\n"
        f"• Аллергии: {user_data['allergies']}\n"
        f"• Пол: {user_data['gender']}\n"
        f"• Возраст: {user_data['age']} лет\n"
        f"• Рост: {user_data['height']} см\n"
        f"• Вес: {user_data['weight']} кг\n"
        f"• Активность: {user_data['activity']}\n\n"
        "🎓 Обращаюсь к профессору нутрициологии..."
    )
    
    progress_message = await update.message.reply_text(progress_text)
    context.user_data['progress_message'] = progress_message
    
    # Запускаем генерацию плана в отдельном потоке
    thread = Thread(target=generate_plan_wrapper, args=(update, context, user_data))
    thread.start()
    
    return GENERATING

def generate_plan_wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, user_data: dict):
    """Обертка для запуска генерации плана в отдельном потоке"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(generate_plan(update, context, user_data))
    loop.close()

async def generate_plan(update: Update, context: ContextTypes.DEFAULT_TYPE, user_data: dict):
    """Генерация плана питания"""
    try:
        progress_message = context.user_data.get('progress_message')
        
        # Обновляем прогресс
        await progress_message.edit_text(
            progress_message.text + "\n📝 Формирую индивидуальный план..."
        )
        
        # Создаем промпт для GPT
        prompt = nutrition_professor.create_professor_prompt(user_data)
        
        # Обновляем прогресс
        await progress_message.edit_text(
            progress_message.text + "\n🤖 Получаю ответ от AI..."
        )
        
        # Отправляем запрос к Yandex GPT
        gpt_response = yandex_gpt.get_completion(prompt)
        
        if not gpt_response:
            await update.message.reply_text("❌ Ошибка при обращении к AI. Пожалуйста, попробуйте позже.")
            return ConversationHandler.END
        
        # Обновляем прогресс
        await progress_message.edit_text(
            progress_message.text + "\n📊 Анализирую данные..."
        )
        
        # Парсим ответ
        nutrition_plan = plan_parser.parse_gpt_response(gpt_response, user_data)
        
        # Обновляем прогресс
        await progress_message.edit_text(
            progress_message.text + "\n🛒 Формирую список покупок..."
        )
        
        # Генерируем список покупок
        shopping_list = shopping_generator.generate_shopping_list(nutrition_plan)
        
        # Обновляем прогресс
        await progress_message.edit_text(
            progress_message.text + "\n📁 Сохраняю файлы..."
        )
        
        # Экспортируем файлы
        file_exporter.export_complete_plan(nutrition_plan, shopping_list, update.effective_user.id)
        
        # Отправляем файлы пользователю
        await update.message.reply_document(
            document=InputFile(f"nutrition_plan_{update.effective_user.id}.txt"),
            caption="📅 Ваш индивидуальный план питания на 7 дней"
        )
        
        await update.message.reply_document(
            document=InputFile(f"shopping_list_{update.effective_user.id}.txt"),
            caption="🛒 Список покупок на неделю"
        )
        
        await update.message.reply_document(
            document=InputFile(f"recipes_{update.effective_user.id}.txt"),
            caption="👨‍🍳 Рецепты для всех блюд"
        )
        
        await update.message.reply_text(
            "🎉 Готово! Ваш индивидуальный план питания создан!\n\n"
            "📋 Что вы получили:\n"
            "• 📅 План питания на 7 дней с 5 приемами пищи\n"
            "• 🛒 Оптимизированный список покупок\n"
            "• 👨‍🍳 Подробные рецепты всех блюд\n\n"
            "Чтобы начать заново, отправьте /start",
            reply_markup=ReplyKeyboardRemove()
        )
        
    except Exception as e:
        logger.error(f"Ошибка при генерации плана: {e}")
        await update.message.reply_text(
            "❌ Произошла ошибка при генерации плана питания. "
            "Пожалуйста, попробуйте позже или обратитесь в поддержку.",
            reply_markup=ReplyKeyboardRemove()
        )
    
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Отмена диалога"""
    await update.message.reply_text(
        "Диалог отменен. Чтобы начать заново, отправьте /start",
        reply_markup=ReplyKeyboardRemove()
    )
    return ConversationHandler.END

# Настройка обработчиков
conv_handler = ConversationHandler(
    entry_points=[CommandHandler('start', start)],
    states={
        GOAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_goal)],
        DIET: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_diet)],
        ALLERGIES: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_allergies)],
        GENDER: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_gender)],
        AGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_age)],
        HEIGHT: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_height)],
        WEIGHT: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_weight)],
        ACTIVITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_activity)],
        GENERATING: [MessageHandler(filters.TEXT & ~filters.COMMAND, lambda u, c: None)]  # Игнорируем ввод во время генерации
    },
    fallbacks=[CommandHandler('cancel', cancel)]
)

application.add_handler(conv_handler)

# Flask приложение для вебхуков
app = Flask(__name__)

@app.route('/webhook', methods=['POST'])
def webhook():
    """Обработка вебхуков от Telegram"""
    update = Update.de_json(request.get_json(), application.bot)
    application.update_queue.put(update)
    return 'ok'

@app.route('/')
def index():
    return 'Bot is running!'

def run_flask():
    """Запуск Flask сервера"""
    app.run(host='0.0.0.0', port=5000)

if __name__ == '__main__':
    # Запуск в режиме polling (для разработки)
    print("Бот запущен в режиме polling...")
    application.run_polling()
    
    # Для продакшена с вебхуками раскомментируйте:
    # thread = Thread(target=run_flask)
    # thread.start()
    # application.run_webhook(
    #     listen="0.0.0.0",
    #     port=5000,
    #     webhook_url=WEBHOOK_URL
    # )

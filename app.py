import os
import json
import re
import logging
import asyncio
from typing import Dict, Any, List
from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, InputFile
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, ConversationHandler
from flask import Flask, request
import requests
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
ADMIN_USER_ID = int(os.getenv('ADMIN_USER_ID', '362423055'))
PORT = int(os.getenv('PORT', 10000))

# Базовый URL для вебхука
RENDER_DOMAIN = os.getenv('RENDER_EXTERNAL_URL', 'https://pravilnoepitanie.onrender.com')
WEBHOOK_URL = f"{RENDER_DOMAIN}/webhook"

# Состояния беседы
(
    START, GOAL, DIET, ALLERGIES, GENDER, AGE, HEIGHT, WEIGHT, ACTIVITY,
    CONFIRMATION, EDIT_PARAMS, GENERATING
) = range(12)

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
    
    def calculate_water_intake(self, weight: int) -> Dict[str, Any]:
        """Расчет водного режима (30-40 мл на 1 кг веса)"""
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
    
    def create_professor_prompt(self, user_data: Dict[str, Any]) -> str:
        """Создание промпта для профессора нутрициологии"""
        calories = self.calculate_calories(user_data)
        bju = self.calculate_bju(user_data, calories)
        bmi = self.calculate_bmi(int(user_data['height']), int(user_data['weight']))
        water = self.calculate_water_intake(int(user_data['weight']))
        
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
        - Норма воды: {water['avg_water']} мл в день

        ТРЕБОВАНИЯ К ПЛАНУ:
        1. 5 приемов пищи ежедневно: завтрак, перекус, обед, перекус, ужин
        2. Сбалансированное БЖУ согласно принципам нутрициологии
        3. Учет циркадных ритмов и времени приема пищи
        4. Разнообразие блюд без повторений в течение недели
        5. Практическая реализуемость рецептов
        6. Учет диетических ограничений и аллергий
        7. Включи рекомендации по водному режиму

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
                    "recipe": "Подробный рецепт приготовления",
                    "water_recommendation": "Выпейте стакан воды за 30 минут до еды"
                }},
                "перекус": {{...}},
                "обед": {{...}},
                "перекус": {{...}},
                "ужин": {{...}},
                "total_calories": {calories},
                "total_protein": {bju['protein']},
                "total_carbs": {bju['carbs']},
                "total_fat": {bju['fat']},
                "water_notes": "Рекомендации по водному режиму на день"
            }},
            "вторник": {{...}},
            "среда": {{...}},
            "четверг": {{...}},
            "пятница": {{...}},
            "суббота": {{...}},
            "воскресенье": {{...}},
            "water_regime": {{
                "daily_total": {water['avg_water']},
                "schedule": [
                    {{"time": "07:00", "amount": 250, "description": "Стакан теплой воды натощак"}},
                    {{"time": "08:30", "amount": 200, "description": "После завтрака"}}
                ],
                "general_recommendations": [
                    "Пейте воду за 30 минут до еды",
                    "Не пейте во время приема пищи",
                    "Увеличьте потребление при физических нагрузках"
                ]
            }}
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
                    "text": "Ты профессор нутрициологии с 40-летним опытом. Составляешь индивидуальные планы питания."
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
        professor = NutritionProfessor()
        water = professor.calculate_water_intake(int(user_data['weight']))
        
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
                'total_fat': 65,
                'water_notes': 'Пейте воду за 30 минут до каждого приема пищи'
            }
        
        plan['water_regime'] = water
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
            'recipe': 'Рецепт приготовления блюда',
            'water_recommendation': 'Выпейте стакан воды за 30 минут до еды'
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
                if isinstance(day, dict) and 'total_calories' in day:
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
        
        for day_name, day_data in nutrition_plan.items():
            if day_name == 'water_regime':
                continue
                
            for meal_type in ['завтрак', 'перекус', 'обед', 'перекус', 'ужин']:
                meal = day_data[meal_type]
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
            'Напитки': [],
            'Прочее': []
        }
        
        category_keywords = {
            'Овощи и фрукты': ['помидор', 'огурец', 'яблоко', 'банан', 'апельсин', 'морковь', 'лук', 'картофель', 'капуста', 'салат', 'зелень', 'ягода'],
            'Мясо и птица': ['куриц', 'говядин', 'свинин', 'индейк', 'фарш', 'грудк', 'мясо', 'телятин'],
            'Рыба и морепродукты': ['рыба', 'лосось', 'тунец', 'креветк', 'кальмар', 'миди', 'треска', 'окунь'],
            'Молочные продукты': ['молоко', 'йогурт', 'творог', 'сыр', 'кефир', 'сметана', 'масло сливочное', 'ряженка'],
            'Крупы и злаки': ['рис', 'гречк', 'овсян', 'пшено', 'макарон', 'хлеб', 'мука', 'крупа', 'отруб'],
            'Бакалея': ['масло оливковое', 'масло растительное', 'соль', 'сахар', 'перец', 'специи', 'соус', 'уксус', 'мед'],
            'Напитки': ['вода', 'чай', 'кофе', 'сок', 'компот', 'морс']
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
        self._export_water_regime(nutrition_plan, user_id)
    
    def _export_nutrition_plan(self, plan: Dict[str, Any], user_id: int):
        """Экспорт плана питания"""
        filename = f"nutrition_plan_{user_id}.txt"
        with open(filename, 'w', encoding='utf-8') as f:
            f.write("🎯 ИНДИВИДУАЛЬНЫЙ ПЛАН ПИТАНИЯ ОТ ПРОФЕССОРА НУТРИЦИОЛОГИИ\n\n")
            
            for day, meals_data in plan.items():
                if day == 'water_regime':
                    continue
                    
                f.write(f"📅 {day.upper()}\n")
                f.write("=" * 60 + "\n")
                
                total_day_calories = 0
                total_day_protein = 0
                total_day_carbs = 0
                total_day_fat = 0
                
                for meal_type in ['завтрак', 'перекус', 'обед', 'перекус', 'ужин']:
                    meal = meals_data[meal_type]
                    f.write(f"\n🍽️ {meal_type.upper()}: {meal['name']}\n")
                    f.write(f"   🔥 Калории: {meal['calories']} ккал\n")
                    f.write(f"   🥚 Белки: {meal['protein']}г | 🥑 Жиры: {meal['fat']}г | 🌾 Углеводы: {meal['carbs']}г\n")
                    if 'water_recommendation' in meal:
                        f.write(f"   💧 {meal['water_recommendation']}\n")
                    
                    total_day_calories += meal['calories']
                    total_day_protein += meal['protein']
                    total_day_carbs += meal['carbs']
                    total_day_fat += meal['fat']
                
                f.write(f"\n📊 ИТОГИ ДНЯ:\n")
                f.write(f"   🔥 Общие калории: {total_day_calories} ккал\n")
                f.write(f"   🥚 Белки: {total_day_protein}г | 🥑 Жиры: {total_day_fat}г | 🌾 Углеводы: {total_day_carbs}г\n")
                if 'water_notes' in meals_data:
                    f.write(f"   💧 {meals_data['water_notes']}\n")
                f.write("\n" + "=" * 60 + "\n\n")
    
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
                if day == 'water_regime':
                    continue
                    
                f.write(f"📅 {day.upper()}\n")
                f.write("=" * 70 + "\n")
                
                for meal_type in ['завтрак', 'перекус', 'обед', 'перекус', 'ужин']:
                    meal = meals_data[meal_type]
                    f.write(f"\n🍽️ {meal_type.upper()}: {meal['name']}\n")
                    f.write(f"📝 Рецепт: {meal['recipe']}\n")
                    if 'water_recommendation' in meal:
                        f.write(f"💧 Рекомендация: {meal['water_recommendation']}\n")
                    f.write("📋 Ингредиенты:\n")
                    
                    for ingredient in meal.get('ingredients', []):
                        f.write(f"   • {ingredient['name']}: {ingredient['quantity']} {ingredient.get('unit', 'гр')}\n")
                    
                    f.write("\n" + "-" * 50 + "\n")
                
                f.write("\n")
    
    def _export_water_regime(self, plan: Dict[str, Any], user_id: int):
        """Экспорт водного режима"""
        if 'water_regime' not in plan:
            return
            
        water = plan['water_regime']
        filename = f"water_regime_{user_id}.txt"
        with open(filename, 'w', encoding='utf-8') as f:
            f.write("💧 ВОДНЫЙ РЕЖИМ ОТ ПРОФЕССОРА НУТРИЦИОЛОГИИ\n\n")
            
            f.write(f"📊 СУТОЧНАЯ НОРМА ВОДЫ:\n")
            f.write(f"   • Минимальная: {water['min_water']} мл\n")
            f.write(f"   • Рекомендуемая: {water['avg_water']} мл\n")
            f.write(f"   • Максимальная: {water['max_water']} мл\n\n")
            
            f.write("🕒 ГРАФИК ПРИЕМА ВОДЫ В ТЕЧЕНИЕ ДНЯ:\n")
            for schedule in water['schedule']:
                f.write(f"   ⏰ {schedule['time']} - {schedule['amount']} мл\n")
                f.write(f"      {schedule['description']}\n")
            f.write("\n")
            
            f.write("💡 ОБЩИЕ РЕКОМЕНДАЦИИ:\n")
            for i, recommendation in enumerate(water['recommendations'], 1):
                f.write(f"   {i}. {recommendation}\n")
            f.write("\n")
            
            f.write("📝 ВАЖНЫЕ ПРИНЦИПЫ:\n")
            f.write("   • Пейте воду за 30 минут ДО еды\n")
            f.write("   • Не пейте во время приема пищи\n")
            f.write("   • Пейте через 1 час ПОСЛЕ еды\n")
            f.write("   • Увеличьте норму при физических нагрузках\n")
            f.write("   • Ограничьте жидкость за 2 часа до сна\n")

# Инициализация классов
nutrition_professor = NutritionProfessor()
yandex_gpt = YandexGPTClient()
plan_parser = NutritionPlanParser()
shopping_generator = ShoppingListGenerator()
file_exporter = FileExporter()

# Создание приложения Telegram
application = Application.builder().token(BOT_TOKEN).build()

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

def create_goal_keyboard(show_back: bool = False):
    """Клавиатура для выбора цели"""
    keyboard = [["похудение", "поддержание веса"], ["набор мышечной массы"]]
    if show_back:
        keyboard.append(["◀️ Назад"])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def create_diet_keyboard(show_back: bool = False):
    """Клавиатура для выбора типа диеты"""
    keyboard = [["стандарт", "вегетарианская"], ["веганская", "безглютеновая"], ["низкоуглеводная"]]
    if show_back:
        keyboard.append(["◀️ Назад"])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def create_allergies_keyboard(show_back: bool = False):
    """Клавиатура для выбора аллергий"""
    keyboard = [["нет", "орехи"], ["молочные продукты", "яйца"], ["рыба/морепродукты"]]
    if show_back:
        keyboard.append(["◀️ Назад"])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def create_gender_keyboard(show_back: bool = False):
    """Клавиатура для выбора пола"""
    keyboard = [["мужской", "женский"]]
    if show_back:
        keyboard.append(["◀️ Назад"])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def create_activity_keyboard(show_back: bool = False):
    """Клавиатура для выбора уровня активности"""
    keyboard = [["сидячий", "умеренная"], ["активный", "очень активный"]]
    if show_back:
        keyboard.append(["◀️ Назад"])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def create_confirmation_keyboard():
    """Клавиатура для подтверждения параметров"""
    keyboard = [["✅ Да, все верно", "✏️ Редактировать параметры"]]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def create_edit_keyboard():
    """Клавиатура для редактирования параметров"""
    keyboard = [
        ["🎯 Цель", "🥗 Тип диеты"],
        ["⚠️ Аллергии", "👤 Пол"],
        ["🎂 Возраст", "📏 Рост"],
        ["⚖️ Вес", "🏃‍♂️ Активность"],
        ["✅ Завершить редактирование"]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Начало работы с ботом"""
    context.user_data.clear()
    
    await update.message.reply_text(
        "👋 Добро пожаловать в бот профессора нутрициологии!\n\n"
        "Я создам для вас индивидуальный план питания на 7 дней с учетом:\n"
        "• 🎯 Ваших целей и параметров\n"  
        "• 🥗 Диетических предпочтений\n"
        "• 💧 Водного режима\n"
        "• 📊 Баланса БЖУ\n\n"
        "Давайте начнем! Выберите вашу цель:",
        reply_markup=create_goal_keyboard()
    )
    return GOAL

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
        # Запускаем генерацию плана
        user_data = context.user_data
        
        progress_message = await update.message.reply_text(
            "🎓 Обращаюсь к профессору нутрициологии...\n"
            "📝 Формирую индивидуальный план питания..."
        )
        context.user_data['progress_message'] = progress_message
        
        # Запускаем генерацию плана в отдельном потоке
        thread = Thread(target=generate_plan_wrapper, args=(update, context, user_data))
        thread.start()
        
        return GENERATING
        
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
            progress_message.text + "\n🤖 Получаю ответ от AI..."
        )
        
        # Создаем промпт для GPT
        prompt = nutrition_professor.create_professor_prompt(user_data)
        
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
        
        await update.message.reply_document(
            document=InputFile(f"water_regime_{update.effective_user.id}.txt"),
            caption="💧 Детальный водный режим"
        )
        
        # Расчеты для итогового сообщения
        professor = NutritionProfessor()
        calories = professor.calculate_calories(user_data)
        water = professor.calculate_water_intake(int(user_data['weight']))
        
        await update.message.reply_text(
            f"🎉 Готово! Ваш индивидуальный план питания создан!\n\n"
            f"📋 Что вы получили:\n"
            f"• 📅 План питания на 7 дней с 5 приемами пищи\n"
            f"• 🛒 Оптимизированный список покупок\n"
            f"• 👨‍🍳 Подробные рецепты всех блюд\n"
            f"• 💧 Детальный водный режим\n\n"
            f"📊 Ваши показатели:\n"
            f"• 🔥 Суточная норма: {calories} ккал\n"
            f"• 💧 Норма воды: {water['avg_water']} мл/день\n"
            f"• 🕒 8 приемов воды по графику\n\n"
            f"Чтобы начать заново, отправьте /start",
            reply_markup=ReplyKeyboardRemove()
        )
        
        # Очищаем прогресс
        if 'progress_message' in context.user_data:
            del context.user_data['progress_message']
        
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
    
    # Очищаем данные
    context.user_data.clear()
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
        CONFIRMATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_confirmation)],
        EDIT_PARAMS: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_edit_params)],
        GENERATING: [MessageHandler(filters.TEXT & ~filters.COMMAND, lambda u, c: None)]
    },
    fallbacks=[CommandHandler('cancel', cancel)]
)

application.add_handler(conv_handler)

# Flask приложение для вебхуков
app = Flask(__name__)

@app.route('/webhook', methods=['POST'])
def webhook():
    """Обработка вебхуков от Telegram"""
    try:
        update = Update.de_json(request.get_json(), application.bot)
        
        # Создаем и запускаем новую event loop для каждого запроса
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        async def process():
            await application.process_update(update)
        
        loop.run_until_complete(process())
        loop.close()
        
        return 'ok'
    except Exception as e:
        logger.error(f"Ошибка в webhook: {e}")
        return 'error', 500

@app.route('/')
def index():
    return '🚀 Бот профессора нутрициологии работает!'

@app.route('/health')
def health():
    return '✅ OK'

# Инициализация вебхука при запуске
async def init_webhook():
    """Инициализация вебхука"""
    try:
        # Удаляем старый вебхук если есть
        await application.bot.delete_webhook(drop_pending_updates=True)
        
        # Устанавливаем новый вебхук
        await application.bot.set_webhook(
            url=WEBHOOK_URL,
            drop_pending_updates=True,
            allowed_updates=Update.ALL_TYPES
        )
        print(f"✅ Webhook установлен: {WEBHOOK_URL}")
        
        # Инициализируем приложение
        await application.initialize()
        await application.start()
        print("✅ Приложение Telegram инициализировано")
        
    except Exception as e:
        print(f"❌ Ошибка инициализации: {e}")

if __name__ == '__main__':
    print("🚀 Запуск бота профессора нутрициологии...")
    
    # Инициализируем вебхук
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(init_webhook())
    
    # Запускаем Flask
    print(f"🌐 Flask запущен на порту {PORT}")
    app.run(host='0.0.0.0', port=PORT, debug=False, use_reloader=False)

import os
import json
import asyncio
from collections import deque
from datetime import datetime, timedelta
import requests
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message

# Конфигурация
MOEX_TOKEN = ''
TELEGRAM_BOT_TOKEN = ''
CHANNEL_ID =   # Числовой ID канала

# Инициализация бота
bot = Bot(token=TELEGRAM_BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)


# Состояния для FSM
class AlertStates(StatesGroup):
    waiting_for_interval = State()


# Глобальные переменные
known_alerts = set()  # Хранит ID всех обработанных алертов
check_interval = 300  # Интервал проверки в секундах (по умолчанию 5 минут)


def get_alert_description(alert_type):
    """Возвращает понятное описание типа алерта"""
    descriptions = {
        'vol_s_99_9_pctl': 'Крупная продажа',
        'vol_b_99_9_pctl': 'Крупная покупка',
        'vol_s_99_pctl': 'Большой объем продаж',
        'vol_b_99_pctl': 'Большой объем покупок',
        'vol_99_9_pctl': 'Крупная сделка',
        'vol_s_95_pctl': 'Повышенный объем продаж',
        'vol_b_95_pctl': 'Повышенный объем покупок',
        'net_vol_99_9_pctl-': 'Большой объем торгов',
        'pr_change_99_9_pctl-': 'Сильное падение цены',
        'net_vol_99_9_pctl+': 'Крупная покупка',
        'pr_change_99_9_pctl+': 'Сильное изменение цены',
        'vol_max': 'Максимальный объем',
        'vol_s_max': 'Максимальная продажа',
        'pr_change_min': 'Максимальное падение цены',
        'pr_change_max': 'Максимальный рост цены',
        'net_vol_max': 'Максимальный объем(net)',
        'vol_b_max': 'Максимальная покупка',
        'pr_low_min': 'Минимальная цена',
        'net_vol_min': 'Крупная продажа(net)',
        'pr_high_max': 'Максимальная цена'
    }
    return descriptions.get(alert_type, alert_type)


def format_value(value, alert_type):
    """Форматирует значение в зависимости от типа алерта"""
    try:
        value = float(value)
        if alert_type in ['pr_low_min', 'pr_high_max']:
            return f"{value:.2f} ₽"
        elif 'change' in alert_type:
            return f"{value:.2f}%"
        else:
            return f"{int(value)} лот"
    except (ValueError, TypeError):
        return str(value)


def format_probability(m_15_data):
    """Форматирует данные вероятности"""
    if not m_15_data or len(m_15_data) < 5:
        return ""

    try:
        change_value = m_15_data[4]
        if change_value is None:
            change_percent = 0.0
        else:
            change_percent = float(change_value)

        formatted_percent = f"{change_percent:.2f}%"

        if formatted_percent.endswith(".00%"):
            formatted_percent = formatted_percent.replace(".00%", "%")
        elif formatted_percent.endswith("0%"):
            formatted_percent = formatted_percent.replace("0%", "%")

    except (ValueError, TypeError, IndexError):
        formatted_percent = "0%"

    up = m_15_data[2] if len(m_15_data) > 2 and m_15_data[2] is not None else 0
    down = m_15_data[3] if len(m_15_data) > 3 and m_15_data[3] is not None else 0

    return f"{formatted_percent} ↑{up} ↓{down}"


def fetch_moex_alerts():
    """Запрашивает данные аномалий с MOEX API"""
    current_date = datetime.now().strftime('%Y-%m-%d')
    api_url = f'https://apim.moex.com/iss/datashop/algopack/eq/alerts.json?date={current_date}'

    headers = {
        'Authorization': f'Bearer {MOEX_TOKEN}',
        'Accept': 'application/json'
    }

    try:
        response = requests.get(api_url, headers=headers, timeout=10)
        response.raise_for_status()

        if response.status_code == 200:
            data = response.json()
            if 'data' in data and 'data' in data['data']:
                return data['data']['data']
            else:
                print("Неожиданная структура ответа API")
                return None
        else:
            print(f"HTTP ошибка: {response.status_code}")
            return None

    except Exception as e:
        print(f"Ошибка при запросе к API: {str(e)}")
        return None


def parse_alert(alert):
    """Парсит данные одного алерта"""
    try:
        alert_datetime = datetime.strptime(f"{alert[0]} {alert[1]}", "%Y-%m-%d %H:%M:%S")
        alert_data = {
            'date': alert[0],
            'time': alert[1],
            'ticker': alert[2],
            'alert_type': alert[3],
            'threshold': alert[4],
            'value': alert[5],
            'processed_time': alert[7],
            'datetime': alert_datetime
        }

        details = json.loads(alert[6])
        if isinstance(details, list) and len(details) > 0:
            details = details[0]

        m_15 = details.get('m_15', [])

        alert_data.update({
            'm_15': m_15,
            'vol_b': details.get('vol_b', 0),
            'vol_s': details.get('vol_s', 0),
            'change_percent': m_15[4] if m_15 and len(m_15) > 4 else "0",
            'up_count': m_15[2] if m_15 and len(m_15) > 2 else 0,
            'down_count': m_15[3] if m_15 and len(m_15) > 3 else 0
        })

        return alert_data
    except Exception as e:
        print(f"Ошибка парсинга алерта: {str(e)}")
        print("Сырые данные:", alert)
        return None


async def send_alert_to_channel(alert):
    """Форматирует и отправляет алерт в канал"""
    alert_desc = get_alert_description(alert['alert_type'])
    prob_str = format_probability(alert['m_15'])
    value_str = format_value(alert['value'], alert['alert_type'])
    threshold_str = format_value(alert['threshold'], alert['alert_type'])

    message = (
        f"🚨 <b>{alert_desc}</b>\n"
        f"📊 <b>Тикер:</b> {alert['ticker']}\n"
        f"⏰ <b>Время:</b> {alert['time']}\n"
        f"📈 <b>Значение:</b> {value_str} (порог: {threshold_str})\n"
        f"📊 <b>Статистика 15 мин:</b> {prob_str}\n"
        f"🔍 <b>Продажи:</b> {alert['vol_s']} лот | <b>Покупки:</b> {alert['vol_b']} лот"
    )

    try:
        await bot.send_message(
            chat_id=CHANNEL_ID,
            text=message,
            parse_mode='HTML'
        )
        print(f"Отправлен алерт для {alert['ticker']}")
    except Exception as e:
        print(f"Ошибка при отправке сообщения: {str(e)}")


async def check_new_alerts():
    """Проверяет новые алерты и отправляет только свежие (за последний час)"""
    global known_alerts

    current_time = datetime.now()
    one_hour_ago = current_time - timedelta(hours=1)
    print(f"Проверка новых алертов за период с {one_hour_ago} по {current_time}")

    alerts = fetch_moex_alerts()
    if not alerts:
        return

    new_alerts = []
    for alert_data in alerts:
        alert = parse_alert(alert_data)
        if not alert:
            continue

        alert_id = f"{alert['ticker']}_{alert['time']}_{alert['alert_type']}"

        # Проверяем что алерт свежий (не старше 1 часа) и еще не был обработан
        if alert['datetime'] >= one_hour_ago and alert_id not in known_alerts:
            known_alerts.add(alert_id)
            new_alerts.append(alert)

    if new_alerts:
        print(f"Найдено {len(new_alerts)} новых алертов за последний час")
        # Сортируем по времени перед отправкой
        for alert in sorted(new_alerts, key=lambda x: x['datetime']):
            await send_alert_to_channel(alert)
    else:
        print("Новых алертов за последний час не найдено")


async def scheduled_checker():
    """Периодическая проверка новых алертов"""
    while True:
        await check_new_alerts()
        await asyncio.sleep(check_interval)


# Обработчики команд
@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer("Бот для мониторинга свежих алертов MOEX запущен. Используйте /help для списка команд.")


@dp.message(Command("help"))
async def cmd_help(message: Message):
    help_text = (
        "📌 <b>Доступные команды:</b>\n"
        "/start - Запустить бота\n"
        "/help - Показать это сообщение\n"
        "/check_now - Проверить алерты сейчас\n"
        "/set_interval - Установить интервал проверки (в минутах)\n"
        "/current_interval - Показать текущий интервал проверки"
    )
    await message.answer(help_text, parse_mode='HTML')


@dp.message(Command("check_now"))
async def cmd_check_now(message: Message):
    await message.answer("Проверяю свежие алерты...")
    await check_new_alerts()
    await message.answer("Проверка завершена")


@dp.message(Command("current_interval"))
async def cmd_current_interval(message: Message):
    minutes = check_interval // 60
    await message.answer(f"Текущий интервал проверки: {minutes} минут")


@dp.message(Command("set_interval"))
async def cmd_set_interval(message: Message, state: FSMContext):
    await state.set_state(AlertStates.waiting_for_interval)
    await message.answer("Введите новый интервал проверки в минутах (1-60):")


@dp.message(AlertStates.waiting_for_interval)
async def process_interval(message: Message, state: FSMContext):
    try:
        minutes = int(message.text)
        if 1 <= minutes <= 60:
            global check_interval
            check_interval = minutes * 60
            await message.answer(f"Интервал проверки установлен на {minutes} минут")
        else:
            await message.answer("Пожалуйста, введите число от 1 до 60")
    except ValueError:
        await message.answer("Пожалуйста, введите целое число")
    await state.clear()


async def on_startup():
    """Действия при запуске бота"""
    asyncio.create_task(scheduled_checker())
    print("Бот запущен и начал мониторинг свежих алертов (за последний час)")


async def main():
    await on_startup()
    await dp.start_polling(bot)


if __name__ == '__main__':
    asyncio.run(main())
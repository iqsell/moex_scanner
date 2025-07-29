import os
import json
from aiogram.types import Message
import asyncio
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
import requests
import random
import string
import sqlite3

# Конфигурация
MOEX_TOKEN = ''
TELEGRAM_BOT_TOKEN = ''
ALERTS_CHANNEL_ID =   # Числовой ID канала
ADMIN_ID =   # Ваш ID в Telegram
PAYMENT_PHONE = '+79998887766'  # Номер для оплаты
TRIAL_PERIOD_HOURS = 24  # Продолжительность триального периода

# Инициализация бота
bot = Bot(token=TELEGRAM_BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# Глобальные переменные
known_alerts = set()  # Хранит ID всех обработанных алертов
check_interval = 60  # Интервал проверки в секундах (по умолчанию 5 минут)


# === БАЗА ДАННЫХ ===
def init_db():
    conn = sqlite3.connect('alerts_bot.db')
    cursor = conn.cursor()

    cursor.execute('''
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        full_name TEXT,
        registration_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        trial_start_date TIMESTAMP NULL,
        banned BOOLEAN DEFAULT FALSE
    )
    ''')

    cursor.execute('''
    CREATE TABLE IF NOT EXISTS subscriptions (
        subscription_id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        start_date TIMESTAMP,
        end_date TIMESTAMP,
        status TEXT DEFAULT 'active',
        FOREIGN KEY (user_id) REFERENCES users (user_id)
    )
    ''')

    cursor.execute('''
    CREATE TABLE IF NOT EXISTS payments (
        payment_id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        payment_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        amount REAL,
        comment TEXT,
        status TEXT DEFAULT 'pending',
        FOREIGN KEY (user_id) REFERENCES users (user_id)
    )
    ''')

    conn.commit()
    conn.close()


init_db()


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

async def unban_user(user_id: int):
    """Удаляет пользователя из черного списка канала"""
    try:
        await bot.unban_chat_member(
            chat_id=ALERTS_CHANNEL_ID,
            user_id=user_id,
            only_if_banned=True  # Разбаниваем только если был забанен
        )
        return True
    except Exception as e:
        print(f"Ошибка при разбане пользователя {user_id}: {e}")
        return False


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
            chat_id=ALERTS_CHANNEL_ID,
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


def add_user(user_id, username, full_name):
    conn = sqlite3.connect('alerts_bot.db')
    cursor = conn.cursor()
    cursor.execute('''
    INSERT OR IGNORE INTO users (user_id, username, full_name, trial_start_date) 
    VALUES (?, ?, ?, ?)
    ''', (user_id, username, full_name, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
    conn.commit()
    conn.close()


def check_trial_period(user_id):
    """Проверяет, активен ли триальный период у пользователя"""
    conn = sqlite3.connect('alerts_bot.db')
    cursor = conn.cursor()
    cursor.execute('SELECT trial_start_date, banned FROM users WHERE user_id = ?', (user_id,))
    result = cursor.fetchone()
    conn.close()

    # Если нет даты начала или пользователь забанен или уже использовал триал
    if not result or not result[0] or result[1]:
        return False

    trial_start = datetime.strptime(result[0], '%Y-%m-%d %H:%M:%S')
    return (datetime.now() - trial_start) < timedelta(hours=TRIAL_PERIOD_HOURS)


def check_user_subscription(user_id):
    """Проверяет подписку пользователя и возвращает дату окончания или None"""
    conn = sqlite3.connect('alerts_bot.db')
    cursor = conn.cursor()

    # Проверяем, не забанен ли пользователь
    cursor.execute('SELECT banned FROM users WHERE user_id = ?', (user_id,))
    banned = cursor.fetchone()
    if banned and banned[0]:
        conn.close()
        return None

    # Проверяем триальный период
    cursor.execute('SELECT trial_start_date FROM users WHERE user_id = ?', (user_id,))
    trial_start = cursor.fetchone()
    if trial_start and trial_start[0]:
        trial_end = datetime.strptime(trial_start[0], '%Y-%m-%d %H:%M:%S') + timedelta(hours=TRIAL_PERIOD_HOURS)
        if datetime.now() < trial_end:
            conn.close()
            return trial_end

    # Проверяем активные подписки
    current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    cursor.execute('''
    SELECT end_date FROM subscriptions 
    WHERE user_id = ? AND status = 'active' AND datetime(end_date) > datetime(?)
    ORDER BY end_date DESC LIMIT 1
    ''', (user_id, current_time))
    active_sub = cursor.fetchone()

    # Если активной подписки нет, помечаем истекшие как expired
    if not active_sub:
        cursor.execute('''
        SELECT subscription_id, end_date FROM subscriptions 
        WHERE user_id = ? AND status = 'active' AND datetime(end_date) <= datetime(?)
        ''', (user_id, current_time))
        expired_subs = cursor.fetchall()

        for sub_id, end_date in expired_subs:
            cursor.execute('''
            UPDATE subscriptions SET status = 'expired' 
            WHERE subscription_id = ?
            ''', (sub_id,))
            conn.commit()

            # Уведомляем пользователя
            asyncio.create_task(notify_subscription_expired(user_id, end_date))

            # Баним в канале
            asyncio.create_task(
                bot.ban_chat_member(
                    chat_id=ALERTS_CHANNEL_ID,
                    user_id=user_id
                )
            )

            # Помечаем как забаненного
            cursor.execute('UPDATE users SET banned = TRUE WHERE user_id = ?', (user_id,))
            conn.commit()

    conn.close()
    return active_sub[0] if active_sub else None


async def notify_subscription_expired(user_id, end_date):
    """Уведомляет пользователя об истечении подписки"""
    try:
        await bot.send_message(
            user_id,
            f"❌ Ваша подписка истекла {end_date}. Доступ к каналу закрыт.\n"
            "Для возобновления доступа оформите подписку снова."
        )
    except Exception as e:
        print(f"Ошибка при отправке уведомления пользователю {user_id}: {e}")

def add_subscription(user_id, days):
    """Добавляет подписку на указанное количество дней"""
    conn = sqlite3.connect('alerts_bot.db')
    cursor = conn.cursor()

    # Снимаем бан в базе данных
    cursor.execute('UPDATE users SET banned = FALSE WHERE user_id = ?', (user_id,))

    start_date = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    end_date = (datetime.now() + timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')

    cursor.execute('''
    INSERT INTO subscriptions (user_id, start_date, end_date) 
    VALUES (?, ?, ?)
    ''', (user_id, start_date, end_date))

    conn.commit()
    conn.close()

    # Пытаемся разбанить пользователя в канале
    asyncio.create_task(unban_user(user_id))

    return end_date

def generate_payment_code():
    """Генерирует уникальный код для оплаты"""
    chars = string.ascii_uppercase + string.digits
    return ''.join(random.choice(chars) for _ in range(6))


def add_payment_request(user_id, comment):
    """Добавляет запрос на оплату"""
    conn = sqlite3.connect('alerts_bot.db')
    cursor = conn.cursor()
    cursor.execute('''
    INSERT INTO payments (user_id, comment, status) 
    VALUES (?, ?, ?)
    ''', (user_id, comment, 'pending'))
    payment_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return payment_id


# === КОМАНДЫ БОТА ===
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    username = message.from_user.username
    full_name = message.from_user.full_name

    add_user(user_id, username, full_name)
    subscription_end = check_user_subscription(user_id)  # Используем новую функцию

    keyboard = InlineKeyboardBuilder()

    if subscription_end:
        try:
            invite_link = await bot.create_chat_invite_link(
                chat_id=ALERTS_CHANNEL_ID,
                member_limit=1
            )
            keyboard.add(InlineKeyboardButton(
                text="Перейти в канал",
                url=invite_link.invite_link
            ))

            # Проверяем триальный период
            if check_trial_period(user_id):
                msg = "🎉 Вам доступен триальный период на 24 часа!"
            else:
                msg = f"✅ Ваша подписка активна до {subscription_end}"

            await message.answer(msg, reply_markup=keyboard.as_markup())

        except Exception as e:
            print(f"Error creating invite link: {e}")
            await message.answer("⚠️ Не удалось создать ссылку на канал. Пожалуйста, сообщите администратору.")
    else:
        keyboard.add(InlineKeyboardButton(
            text="🎁 Активировать триал",
            callback_data="activate_trial"
        ))
        keyboard.add(InlineKeyboardButton(
            text="💳 Купить подписку",
            callback_data="buy_subscription"
        ))

        await message.answer(
            "❌ Для доступа к алертам необходимо оформить подписку.\n"
            "Вам доступен триальный период на 24 часа после активации.",
            reply_markup=keyboard.as_markup()
        )

@dp.callback_query(F.data == "activate_trial")
async def activate_trial(callback: types.CallbackQuery):
    user_id = callback.from_user.id

    # Check if trial was already used
    conn = sqlite3.connect('alerts_bot.db')
    cursor = conn.cursor()
    cursor.execute('SELECT trial_start_date FROM users WHERE user_id = ?', (user_id,))
    result = cursor.fetchone()

    if result and result[0]:  # Trial already exists
        trial_start = datetime.strptime(result[0], '%Y-%m-%d %H:%M:%S')
        trial_end = trial_start + timedelta(hours=TRIAL_PERIOD_HOURS)

        if datetime.now() < trial_end:
            await callback.answer("Вы уже используете триальный период", show_alert=True)
        else:
            await callback.answer("❌ Вы уже использовали триальный период. Доступно только одно пробное использование.",
                                  show_alert=True)
        conn.close()
        return

    # Activate trial
    try:
        cursor.execute('''
        UPDATE users SET trial_start_date = ?, banned = FALSE 
        WHERE user_id = ?
        ''', (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), user_id))
        conn.commit()

        # Create channel invite
        invite_link = await bot.create_chat_invite_link(
            chat_id=ALERTS_CHANNEL_ID,
            member_limit=1
        )

        keyboard = InlineKeyboardBuilder()
        keyboard.add(InlineKeyboardButton(
            text="Перейти в канал",
            url=invite_link.invite_link
        ))

        await callback.message.edit_text(
            "🎉 Вам активирован триальный период на 24 часа!\n\n",
            reply_markup=keyboard.as_markup()
        )
    except Exception as e:
        await callback.answer("Ошибка при активации триала. Пожалуйста, попробуйте позже.", show_alert=True)
        print(f"Error activating trial: {e}")
    finally:
        conn.close()

    await callback.answer()


@dp.callback_query(F.data == "buy_subscription")
async def buy_subscription(callback: types.CallbackQuery):
    payment_code = generate_payment_code()
    payment_id = add_payment_request(callback.from_user.id, payment_code)

    keyboard = InlineKeyboardBuilder()
    keyboard.add(InlineKeyboardButton(
        text="✅ Я оплатил",
        callback_data=f"payment_done_{payment_id}"
    ))
    keyboard.add(InlineKeyboardButton(
        text="⬅️ Назад",
        callback_data="back_to_start"
    ))

    await callback.message.edit_text(
        f"💳 Для оплаты подписки:\n"
        f"1. Переведите 100 руб на номер {PAYMENT_PHONE}\n"
        f"2. В комментарии укажите код: {payment_code}\n\n"
        "3. После оплаты нажмите кнопку ниже",
        reply_markup=keyboard.as_markup()
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("payment_done_"))
async def payment_done(callback: types.CallbackQuery):
    payment_id = int(callback.data.split('_')[2])

    # Уведомляем админа
    admin_keyboard = InlineKeyboardBuilder()
    admin_keyboard.add(InlineKeyboardButton(
        text="✅ Подтвердить",
        callback_data=f"confirm_payment_{payment_id}"
    ))
    admin_keyboard.add(InlineKeyboardButton(
        text="❌ Отклонить",
        callback_data=f"reject_payment_{payment_id}"
    ))

    await bot.send_message(
        ADMIN_ID,
        f"🔔 Новый платеж от @{callback.from_user.username}\n"
        f"ID платежа: {payment_id}\n"
        f"Подтвердите оплату:",
        reply_markup=admin_keyboard.as_markup()
    )

    keyboard = InlineKeyboardBuilder()
    keyboard.add(InlineKeyboardButton(
        text="⬅️ Назад",
        callback_data="back_to_start"
    ))

    await callback.message.edit_text(
        "🕒 Ваш платеж отправлен на проверку. Ожидайте подтверждения.",
        reply_markup=keyboard.as_markup()
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("confirm_payment_"))
async def confirm_payment(callback: types.CallbackQuery):
    payment_id = int(callback.data.split('_')[2])

    # Получаем информацию о платеже
    conn = sqlite3.connect('alerts_bot.db')
    cursor = conn.cursor()
    cursor.execute('SELECT user_id FROM payments WHERE payment_id = ?', (payment_id,))
    user_id = cursor.fetchone()[0]

    # Добавляем подписку (30 дней) - функция сама разбанит пользователя
    end_date = add_subscription(user_id, 30)

    # Обновляем статус платежа
    cursor.execute('UPDATE payments SET status = "confirmed" WHERE payment_id = ?', (payment_id,))
    conn.commit()
    conn.close()

    # Создаем ссылку на канал
    invite_link = await bot.create_chat_invite_link(
        chat_id=ALERTS_CHANNEL_ID,
        member_limit=1
    )

    keyboard = InlineKeyboardBuilder()
    keyboard.add(InlineKeyboardButton(
        text="Перейти в канал",
        url=invite_link.invite_link
    ))

    await bot.send_message(
        user_id,
        f"🎉 Ваша подписка активирована до {end_date}!\n\n",
        reply_markup=keyboard.as_markup()
    )

    await callback.message.edit_text(f"✅ Платеж #{payment_id} подтвержден")
    await callback.answer()


@dp.callback_query(F.data.startswith("reject_payment_"))
async def reject_payment(callback: types.CallbackQuery):
    payment_id = int(callback.data.split('_')[2])

    # Обновляем статус платежа
    conn = sqlite3.connect('alerts_bot.db')
    cursor = conn.cursor()
    cursor.execute('UPDATE payments SET status = "rejected" WHERE payment_id = ?', (payment_id,))
    conn.commit()
    conn.close()

    await callback.message.edit_text(f"❌ Платеж #{payment_id} отклонен")
    await callback.answer()


@dp.callback_query(F.data == "back_to_start")
async def back_to_start(callback: types.CallbackQuery):
    await cmd_start(callback.message)
    await callback.answer()


# === ПРОВЕРКА ПОДПИСОК И УДАЛЕНИЕ ИЗ КАНАЛА ===
async def subscription_checker():
    """Периодическая проверка подписок"""
    while True:
        try:
            # Получаем всех пользователей с подписками
            conn = sqlite3.connect('alerts_bot.db')
            cursor = conn.cursor()
            cursor.execute('SELECT DISTINCT user_id FROM subscriptions WHERE status = "active"')
            users = cursor.fetchall()
            conn.close()

            # Проверяем подписку для каждого пользователя
            for (user_id,) in users:
                check_user_subscription(user_id)

            await asyncio.sleep(60)  # Проверка каждую минуту

        except Exception as e:
            print(f"Ошибка в subscription_checker: {e}")
            await asyncio.sleep(60)

async def check_expired_subscriptions():
    """Check and remove users with expired subscriptions"""
    conn = sqlite3.connect('alerts_bot.db')
    cursor = conn.cursor()

    # Find users with expired subscriptions or trials
    cursor.execute('''
    SELECT u.user_id, s.subscription_id
    FROM users u
    LEFT JOIN subscriptions s ON u.user_id = s.user_id AND s.status = 'active'
    WHERE 
        (u.trial_start_date IS NOT NULL AND 
         datetime(u.trial_start_date, '+24 hours') <= datetime('now')) OR
        (s.end_date IS NOT NULL AND 
         datetime(s.end_date) <= datetime('now'))
    AND u.banned = FALSE
    ''')

    expired_users = cursor.fetchall()

    for user_id, subscription_id in expired_users:
        try:
            # Ban from channel
            await bot.ban_chat_member(
                chat_id=ALERTS_CHANNEL_ID,
                user_id=user_id
            )

            # Mark as banned in DB and update subscription status
            cursor.execute('UPDATE users SET banned = TRUE WHERE user_id = ?', (user_id,))
            if subscription_id:
                cursor.execute('UPDATE subscriptions SET status = "expired" WHERE subscription_id = ?', (subscription_id,))

            # Notify user
            await bot.send_message(
                user_id,
                "❌ Ваша подписка истекла. Доступ к каналу закрыт.\n"
                "Для возобновления доступа оформите подписку снова."
            )
        except Exception as e:
            print(f"Ошибка при удалении пользователя {user_id}: {e}")

    conn.commit()
    conn.close()


# === АДМИН КОМАНДЫ ===
@dp.message(Command("admin"))
async def admin_panel(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("Доступ запрещен")
        return

    # Получаем список всех пользователей
    conn = sqlite3.connect('alerts_bot.db')
    cursor = conn.cursor()

    cursor.execute('''
    SELECT u.user_id, u.username, u.full_name, 
           CASE 
               WHEN datetime(u.trial_start_date, '+24 hours') > datetime('now') THEN 'Trial'
               WHEN s.end_date IS NOT NULL AND datetime(s.end_date) > datetime('now') THEN 'Subscribed'
               ELSE 'No subscription'
           END as status
    FROM users u
    LEFT JOIN subscriptions s ON u.user_id = s.user_id AND s.status = 'active'
    ORDER BY u.registration_date DESC
    ''')

    users = cursor.fetchall()
    conn.close()

    if not users:
        await message.answer("В базе данных нет пользователей")
        return

    # Формируем сообщение со списком пользователей
    users_list = "📊 Список пользователей:\n\n"
    for user in users:
        user_id, username, full_name, status = user
        users_list += f"🆔 ID: {user_id}\n👤 Имя: {full_name}\n📛 Username: @{username}\n🔹 Статус: {status}\n\n"

    await message.answer(users_list)


@dp.message(Command("grant_sub"))
async def grant_subscription(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("Доступ запрещен")
        return

    try:
        # Формат команды: /grant_sub user_id days
        args = message.text.split()
        if len(args) != 3:
            raise ValueError("Неверный формат команды")

        user_id = int(args[1])
        days = int(args[2])

        # Добавляем подписку - функция сама разбанит пользователя
        end_date = add_subscription(user_id, days)

        # Создаем ссылку на канал
        invite_link = await bot.create_chat_invite_link(
            chat_id=ALERTS_CHANNEL_ID,
            member_limit=1
        )

        keyboard = InlineKeyboardBuilder()
        keyboard.add(InlineKeyboardButton(
            text="Перейти в канал",
            url=invite_link.invite_link
        ))

        # Уведомляем пользователя
        await bot.send_message(
            user_id,
            f"🎉 Администратор активировал вам подписку на {days} дней (до {end_date})!\n\n",
            reply_markup=keyboard.as_markup()
        )

        await message.answer(f"✅ Пользователю {user_id} выдана подписка на {days} дней (до {end_date})")

    except Exception as e:
        await message.answer(f"Ошибка: {str(e)}\n\nИспользуйте формат: /grant_sub user_id days")


@dp.message(Command("revoke_sub"))
async def revoke_subscription(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("Доступ запрещен")
        return

    try:
        # Формат команды: /revoke_sub user_id
        args = message.text.split()
        if len(args) != 2:
            raise ValueError("Неверный формат команды")

        user_id = int(args[1])

        # Удаляем подписки пользователя
        conn = sqlite3.connect('alerts_bot.db')
        cursor = conn.cursor()
        cursor.execute('DELETE FROM subscriptions WHERE user_id = ?', (user_id,))
        cursor.execute('UPDATE users SET banned = TRUE WHERE user_id = ?', (user_id,))
        conn.commit()
        conn.close()

        # Пытаемся удалить из канала
        try:
            await bot.ban_chat_member(
                chat_id=ALERTS_CHANNEL_ID,
                user_id=user_id
            )
        except Exception as e:
            print(f"Ошибка при бане пользователя {user_id}: {e}")

        # Уведомляем пользователя
        await bot.send_message(
            user_id,
            "❌ Ваша подписка была отменена администратором. Доступ к каналу закрыт."
        )

        await message.answer(f"✅ Подписка пользователя {user_id} отменена")

    except Exception as e:
        await message.answer(f"Ошибка: {str(e)}\n\nИспользуйте формат: /revoke_sub user_id")


# === ЗАПУСК БОТА ===
async def on_startup():
    # Start background tasks
    asyncio.create_task(scheduled_checker())  # For alerts
    asyncio.create_task(subscription_checker())  # For subscriptions

    # Initial check of subscriptions
    await check_expired_subscriptions()
    print("Бот запущен")


async def main():
    await on_startup()
    await dp.start_polling(bot)


if __name__ == '__main__':
    asyncio.run(main())
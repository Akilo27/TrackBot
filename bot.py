# -*- coding: utf-8 -*-
"""
Karma Tracker Bot — v3.2 "max pack ++"
Что нового по сравнению с твоей версией:
- Полноценные челленджи:
  • статус (active/finished), цель как число (target_count), дедлайн (deadline, ISO)
  • участие/выход, список «мои челленджи»
  • инлайн-детали челленджа, +1 прогресс кнопкой
  • авто-завершение при достижении цели или после дедлайна
  • распределение призового фонда среди победителей (виртуально), комиссия COMMISSION_PCT
- Премиум со сроком: premium_until (30 дней), is_premium синхронизируется
- Мягкие миграции под новые поля
- Улучшенные проверки и UX-сообщения
"""

import os
import re
import sys
import json
import random
import sqlite3
from datetime import datetime, timedelta

import telebot
from telebot import types

# ===================== НАСТРОЙКИ =====================

# Рекомендуется хранить в ENV:
# export BOT_TOKEN="XXX:YYYY"
# export PROVIDER_TOKEN="123456:LIVE:..."


def _get_env_value(name: str, default: str = "") -> str:
    """Возвращает значение переменной окружения без лишних пробелов."""

    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip()
    return value or default


BOT_TOKEN = "6396361025:AAFUBxVyMDOIK5IxfVdBUp8PbpTRLmObWE8"
PROVIDER_TOKEN = ""
PAYMENTS_AVAILABLE = bool(PROVIDER_TOKEN)

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

DB_PATH = "karma_bot.db"
REF_BONUS = 50
DAILY_MIN, DAILY_MAX = 10, 50
KARMA_PER_HABIT_COMPLETE = 50
KARMA_FOR_PUBLIC_JOIN = 10
LEVEL_BORDER = 100
COMMISSION_PCT = 15  # комиссия с призового фонда (виртуально)

user_states = {}  # простая FSM

# ===================== УТИЛИТЫ БД =====================

def db():
    return sqlite3.connect(DB_PATH)

def init_db():
    conn = db()
    c = conn.cursor()

    # базовые таблицы
    c.execute("""
    CREATE TABLE IF NOT EXISTS users(
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        karma INTEGER DEFAULT 0,
        level INTEGER DEFAULT 1,
        is_premium INTEGER DEFAULT 0,
        created_date TEXT,
        referrer_id INTEGER,
        last_daily_claim TEXT,
        premium_until TEXT
    )""")

    c.execute("""
    CREATE TABLE IF NOT EXISTS habits(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        name TEXT,
        target INTEGER,
        current_progress INTEGER DEFAULT 0,
        created_date TEXT,
        is_done INTEGER DEFAULT 0
    )""")

    c.execute("""
    CREATE TABLE IF NOT EXISTS challenges(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        description TEXT,
        creator_id INTEGER,
        target TEXT,                        -- legacy (оставляем)
        prize_pool INTEGER DEFAULT 0,
        participants TEXT,                  -- JSON: {user_id: progress_int}
        is_premium INTEGER DEFAULT 0,
        is_public INTEGER DEFAULT 1,
        created_at TEXT,
        status TEXT,                        -- 'active' | 'finished'
        target_count INTEGER,               -- числовая цель
        deadline TEXT,                      -- ISO дата/время дедлайна
        winners TEXT                        -- JSON: [user_id,...]
    )""")

    c.execute("""
    CREATE TABLE IF NOT EXISTS achievements(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        title TEXT,
        description TEXT,
        earned_date TEXT
    )""")

    c.execute("""
    CREATE TABLE IF NOT EXISTS payments(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        amount INTEGER,
        description TEXT,
        status TEXT,
        created_at TEXT
    )""")

    # мягкие миграции (на случай старых баз)
    safe_add_column(c, "users", "premium_until", "TEXT")
    safe_add_column(c, "challenges", "status", "TEXT")
    safe_add_column(c, "challenges", "target_count", "INTEGER")
    safe_add_column(c, "challenges", "deadline", "TEXT")
    safe_add_column(c, "challenges", "winners", "TEXT")
    safe_add_column(c, "challenges", "is_public", "INTEGER DEFAULT 1")
    safe_add_column(c, "challenges", "created_at", "TEXT")

    conn.commit()
    conn.close()

def safe_add_column(cursor, table, column, ddl_type):
    cursor.execute("PRAGMA table_info(%s)" % table)
    cols = [row[1] for row in cursor.fetchall()]
    if column not in cols:
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}")

init_db()

# ===================== ХЕЛПЕРЫ =====================

def now_iso():
    return datetime.now().isoformat()

def dt_from_iso(s):
    try:
        return datetime.fromisoformat(s) if s else None
    except Exception:
        return None

def fetch_one(query, args=()):
    conn = db(); c = conn.cursor()
    c.execute(query, args)
    row = c.fetchone()
    conn.close()
    return row

def fetch_all(query, args=()):
    conn = db(); c = conn.cursor()
    c.execute(query, args)
    rows = c.fetchall()
    conn.close()
    return rows

def execute(query, args=()):
    conn = db(); c = conn.cursor()
    c.execute(query, args)
    conn.commit()
    conn.close()

def json_load(s, default):
    try:
        return json.loads(s) if s else default
    except Exception:
        return default

def ensure_user(message):
    user_id = message.from_user.id
    username = message.from_user.username or message.from_user.first_name or "noname"
    execute(
        "INSERT OR IGNORE INTO users (user_id, username, created_date) VALUES (?, ?, ?)",
        (user_id, username, now_iso())
    )
    return user_id, username

def compute_is_premium(user_id) -> bool:
    row = fetch_one("SELECT is_premium, premium_until FROM users WHERE user_id=?", (user_id,))
    if not row:
        return False
    flag, until = row
    if until:
        active = datetime.now() < dt_from_iso(until)
        # синхронизируем флаг
        execute("UPDATE users SET is_premium=? WHERE user_id=?", (1 if active else 0, user_id))
        return active
    return bool(flag)


def ensure_payments_enabled(chat_id) -> bool:
    """Проверяет наличие провайдера платежей и сообщает пользователю, если он не настроен."""

    if PAYMENTS_AVAILABLE:
        return True
    bot.send_message(
        chat_id,
        "🚫 Платёжный провайдер не настроен. Оплата временно недоступна."
    )
    return False


def add_karma(user_id: int, amount: int, reason: str = ""):
    mult = 2 if compute_is_premium(user_id) else 1
    add = amount * mult
    conn = db(); c = conn.cursor()

    c.execute("UPDATE users SET karma = COALESCE(karma,0) + ? WHERE user_id=?", (add, user_id))
    c.execute("SELECT karma, level FROM users WHERE user_id=?", (user_id,))
    karma, level = c.fetchone()
    while karma >= LEVEL_BORDER:
        level += 1
        karma -= LEVEL_BORDER
    c.execute("UPDATE users SET karma=?, level=? WHERE user_id=?", (karma, level, user_id))
    conn.commit(); conn.close()

def add_achievement(user_id, title, desc):
    execute(
        "INSERT INTO achievements (user_id, title, description, earned_date) VALUES (?, ?, ?, ?)",
        (user_id, title, desc, now_iso())
    )

def parse_referrer(start_text: str):
    if not start_text:
        return None
    m = re.search(r"ref[_]?(\d+)", start_text)
    return int(m.group(1)) if m else None

# ===================== КЛАВИАТУРЫ =====================

def main_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(types.KeyboardButton('🎯 Мои привычки'), types.KeyboardButton('🏆 Челленджи'))
    kb.add(types.KeyboardButton('📊 Статистика'), types.KeyboardButton('🎖 Ачивки'))
    kb.add(types.KeyboardButton('🎁 Ежедневная награда'), types.KeyboardButton('👥 Пригласить друга'))
    kb.add(types.KeyboardButton('🏅 Лидеры'), types.KeyboardButton('🛒 Магазин'))
    kb.add(types.KeyboardButton('🧩 Мои челленджи'))
    return kb

def challenge_inline(chl_id, joined: bool, is_creator: bool, status: str):
    kb = types.InlineKeyboardMarkup()
    if status != "finished":
        kb.add(types.InlineKeyboardButton("🔎 Детали", callback_data=f"chl:details:{chl_id}"))
        if joined:
            kb.add(types.InlineKeyboardButton("➕ Прогресс +1", callback_data=f"chl:prog_inc:{chl_id}"))
            kb.add(types.InlineKeyboardButton("🚪 Выйти", callback_data=f"chl:leave:{chl_id}"))
        else:
            kb.add(types.InlineKeyboardButton("✅ Участвовать", callback_data=f"chl:join:{chl_id}"))
        if is_creator:
            kb.add(types.InlineKeyboardButton("⏹ Завершить", callback_data=f"chl:finish:{chl_id}"))
    else:
        kb.add(types.InlineKeyboardButton("🔎 Итоги", callback_data=f"chl:details:{chl_id}"))
    return kb

# ===================== СТАРТ / РЕФЕРАЛЫ =====================

@bot.message_handler(commands=['start'])
def start_cmd(message):
    user_id, username = ensure_user(message)

    ref_id = parse_referrer(message.text or "")
    if ref_id and ref_id != user_id:
        row = fetch_one("SELECT referrer_id FROM users WHERE user_id=?", (user_id,))
        if row and row[0] is None:
            execute("UPDATE users SET referrer_id=? WHERE user_id=?", (ref_id, user_id))
            add_karma(ref_id, REF_BONUS, reason="referral")
            try:
                bot.send_message(ref_id, f"🎉 По твоей ссылке пришёл новый друг! +{REF_BONUS} кармы.")
            except Exception:
                pass

    bot.send_message(
        message.chat.id,
        f"👋 Привет, <b>{username}</b>!\n"
        f"Я помогу прокачать привычки, участвовать в челленджах и копить карму.\n\n"
        f"Используй меню ниже 👇",
        reply_markup=main_menu()
    )

# ===================== ПРИВЫЧКИ =====================

@bot.message_handler(func=lambda m: m.text == '🎯 Мои привычки')
def show_habits(message):
    user_id, _ = ensure_user(message)
    rows = fetch_all("SELECT id, name, current_progress, target, is_done FROM habits WHERE user_id=?", (user_id,))

    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("➕ Добавить привычку", callback_data="habit:add"))
    if rows:
        kb.add(types.InlineKeyboardButton("📈 Отметить прогресс", callback_data="habit:track"))

    if not rows:
        bot.send_message(message.chat.id, "😅 У тебя пока нет привычек.", reply_markup=main_menu())
        bot.send_message(message.chat.id, "Нажми «➕ Добавить привычку» ниже:", reply_markup=kb)
        return

    text = "📋 <b>Твои привычки</b>:\n\n"
    for _id, name, cur, tgt, done in rows:
        status = "✅" if done else "🔄"
        text += f"{status} <b>{name}</b> — {cur}/{tgt}\n"
    bot.send_message(message.chat.id, text, reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data == "habit:add")
def habit_add_callback(call):
    user_states[call.from_user.id] = {"state": "habit_wait_name"}
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, "📝 Введи название новой привычки (например: «Бег по утрам»):")

@bot.message_handler(func=lambda m: user_states.get(m.from_user.id, {}).get("state") == "habit_wait_name")
def habit_get_name(message):
    user_states[message.from_user.id] = {"state": "habit_wait_target", "habit_name": message.text.strip()}
    bot.send_message(message.chat.id, "🎯 Какая цель? Введи число (сколько раз):")

@bot.message_handler(func=lambda m: user_states.get(m.from_user.id, {}).get("state") == "habit_wait_target")
def habit_save(message):
    user_id = message.from_user.id
    st = user_states.get(user_id, {})
    habit_name = st.get("habit_name", "Без названия")
    try:
        target = int(re.findall(r"\d+", message.text)[0])
    except Exception:
        bot.send_message(message.chat.id, "❌ Введи число для цели (например: 30).")
        return
    execute(
        "INSERT INTO habits (user_id, name, target, current_progress, created_date) VALUES (?, ?, ?, 0, ?)",
        (user_id, habit_name, target, now_iso())
    )
    user_states.pop(user_id, None)
    bot.send_message(message.chat.id, f"✅ Привычка «{habit_name}» добавлена! Цель: {target}.")

@bot.callback_query_handler(func=lambda c: c.data == "habit:track")
def habit_track_select(call):
    user_id = call.from_user.id
    rows = fetch_all("SELECT id, name FROM habits WHERE user_id=? AND is_done=0", (user_id,))
    if not rows:
        bot.answer_callback_query(call.id, "Нет активных привычек.")
        return
    kb = types.InlineKeyboardMarkup()
    for _id, name in rows:
        kb.add(types.InlineKeyboardButton(name, callback_data=f"habit:progress:{_id}"))
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, "Выбери привычку:", reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data.startswith("habit:progress:"))
def habit_mark_progress(call):
    habit_id = int(call.data.split(":")[-1])
    row = fetch_one("SELECT name, current_progress, target, user_id FROM habits WHERE id=?", (habit_id,))
    if not row:
        bot.answer_callback_query(call.id, "Привычка не найдена.")
        return
    name, cur, tgt, user_id = row
    cur += 1
    done = 1 if cur >= tgt else 0
    execute("UPDATE habits SET current_progress=?, is_done=? WHERE id=?", (cur, done, habit_id))

    if done:
        add_achievement(user_id, "🧘 Настойчивость", f"Завершил привычку «{name}».")
        add_karma(user_id, KARMA_PER_HABIT_COMPLETE, reason="habit_complete")
        bot.answer_callback_query(call.id, "🎉 Привычка выполнена!")
        bot.send_message(call.message.chat.id, f"🎉 Поздравляю! Ты выполнил привычку «{name}»!")
    else:
        bot.answer_callback_query(call.id, "Готово!")
        bot.send_message(call.message.chat.id, f"✅ Прогресс по «{name}»: {cur}/{tgt}")

# ===================== ЧЕЛЛЕНДЖИ =====================

def finalize_challenge(chl_id):
    """Пытаемся завершить челлендж: по дедлайну или если есть победители"""
    row = fetch_one("""SELECT id, name, target_count, prize_pool, participants, status, deadline
                       FROM challenges WHERE id=?""", (chl_id,))
    if not row:
        return False, "Челлендж не найден."
    _id, name, target_count, pool, participants_json, status, deadline = row
    if status == "finished":
        return False, "Уже завершён."

    participants = json_load(participants_json, {})
    winners = [int(uid) for uid, prog in participants.items() if target_count and int(prog) >= int(target_count)]

    # дедлайн
    ddl = dt_from_iso(deadline) if deadline else None
    deadline_passed = ddl and datetime.now() >= ddl

    # если никто не достиг цели, но дедлайн прошёл — победителей нет
    if not winners and not deadline_passed:
        return False, "Условия завершения ещё не выполнены."

    # распределяем приз
    net_pool = int(pool * (100 - COMMISSION_PCT) / 100) if pool else 0
    per_winner = int(net_pool / max(1, len(winners))) if winners else 0

    if winners and per_winner > 0:
        for uid in winners:
            add_karma(uid, per_winner, reason="challenge_win")

    execute("UPDATE challenges SET status='finished', winners=? WHERE id=?",
            (json.dumps(winners, ensure_ascii=False), chl_id))
    return True, f"Челлендж «{name}» завершён. Победителей: {len(winners)}. Награда каждому: {per_winner}."

@bot.message_handler(func=lambda m: m.text == '🏆 Челленджи')
def challenges_menu(message):
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("🎪 Публичные челленджи", callback_data="chl:public"))
    kb.add(types.InlineKeyboardButton("⚡ Создать свой (Премиум)", callback_data="chl:create"))
    bot.send_message(message.chat.id, "🏆 Выбери категорию челленджей:", reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data == "chl:public")
def show_public_challenges(call):
    # показываем только активные
    rows = fetch_all("""SELECT id, name, description, prize_pool, target_count, deadline
                        FROM challenges WHERE is_public=1 AND COALESCE(status,'active')='active'
                        ORDER BY id DESC LIMIT 20""")
    if not rows:
        samples = [
            ("💧 2 литра воды", "Пей воду каждый день 7 дней подряд", 7),
            ("📚 15 страниц в день", "Читай по 15 страниц ежедневно неделю", 7),
            ("🏃 5 км в день", "Бегай/ходи по 5 км ежедневно 7 дней", 7),
        ]
        kb = types.InlineKeyboardMarkup()
        for i, (name, desc, _) in enumerate(samples):
            kb.add(types.InlineKeyboardButton(name, callback_data=f"chl:sample_join:{i}"))
        bot.edit_message_text("🎯 Доступные челленджи (примеры):", call.message.chat.id, call.message.message_id, reply_markup=kb)
        return

    text = "<b>Публичные челленджи:</b>\n\n"
    kb = types.InlineKeyboardMarkup()
    for _id, name, desc, pool, tcount, ddl in rows:
        ddl_txt = f"до {ddl.split('T')[0]}" if ddl else "без дедлайна"
        text += f"• <b>{name}</b> — {desc}\n   Цель: {tcount} | Приз: {pool} 💰 | {ddl_txt}\n"
        kb.add(types.InlineKeyboardButton(f"Участвовать: {name}", callback_data=f"chl:join:{_id}"))
        kb.add(types.InlineKeyboardButton(f"Детали: {name}", callback_data=f"chl:details:{_id}"))
    try:
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=kb)
    except Exception:
        bot.send_message(call.message.chat.id, text, reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data.startswith("chl:details:"))
def chl_details(call):
    chl_id = int(call.data.split(":")[-1])
    row = fetch_one("""SELECT id, name, description, creator_id, prize_pool, participants, is_public,
                              COALESCE(status,'active'), target_count, deadline, winners
                       FROM challenges WHERE id=?""", (chl_id,))
    if not row:
        bot.answer_callback_query(call.id, "Челлендж не найден."); return
    _id, name, desc, creator_id, pool, participants_json, is_public, status, tcount, deadline, winners_json = row
    participants = json_load(participants_json, {})
    winners = json_load(winners_json, [])
    user_id = call.from_user.id
    joined = str(user_id) in participants
    is_creator = (user_id == creator_id)
    ddl_txt = deadline.split('T')[0] if deadline else "—"

    text = (f"<b>{name}</b>\n"
            f"{desc}\n\n"
            f"Статус: <b>{status}</b>\n"
            f"Цель: <b>{tcount}</b>\n"
            f"Дедлайн: <b>{ddl_txt}</b>\n"
            f"Призовой фонд: <b>{pool}</b> (комиссия {COMMISSION_PCT}%)\n"
            f"Участников: <b>{len(participants)}</b>\n")
    if status == "finished":
        text += f"\nПобедители: {', '.join('@'+fetch_one('SELECT username FROM users WHERE user_id=?',(w,))[0] or 'user' for w in winners) if winners else '—'}"

    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, text, reply_markup=challenge_inline(chl_id, joined, is_creator, status))

@bot.callback_query_handler(func=lambda c: c.data.startswith("chl:sample_join:"))
def sample_join(call):
    add_karma(call.from_user.id, KARMA_FOR_PUBLIC_JOIN, reason="join_sample")
    bot.answer_callback_query(call.id, "🎉 Ты присоединился! +10 кармы")
    bot.send_message(call.message.chat.id, "✅ Участие подтверждено. Проверь прогресс в статистике!")

@bot.callback_query_handler(func=lambda c: c.data.startswith("chl:join:"))
def join_real_challenge(call):
    user_id = call.from_user.id
    chl_id = int(call.data.split(":")[-1])
    row = fetch_one("SELECT participants, COALESCE(status,'active') FROM challenges WHERE id=?", (chl_id,))
    if not row:
        bot.answer_callback_query(call.id, "Челлендж не найден."); return
    participants = json_load(row[0], {})
    status = row[1]
    if status != "active":
        bot.answer_callback_query(call.id, "Челлендж уже завершён."); return
    if str(user_id) in participants:
        bot.answer_callback_query(call.id, "Ты уже участвуешь."); return
    participants[str(user_id)] = 0
    execute("UPDATE challenges SET participants=? WHERE id=?", (json.dumps(participants, ensure_ascii=False), chl_id))
    add_karma(user_id, KARMA_FOR_PUBLIC_JOIN, reason="join_public")
    bot.answer_callback_query(call.id, "🎉 Ты в деле! +10 кармы")
    bot.send_message(call.message.chat.id, "✅ Участие подтверждено. Открывай детали челленджа и жми «Прогресс +1».")

@bot.callback_query_handler(func=lambda c: c.data.startswith("chl:leave:"))
def chl_leave(call):
    user_id = call.from_user.id
    chl_id = int(call.data.split(":")[-1])
    row = fetch_one("SELECT participants, COALESCE(status,'active') FROM challenges WHERE id=?", (chl_id,))
    if not row:
        bot.answer_callback_query(call.id, "Челлендж не найден."); return
    participants = json_load(row[0], {})
    status = row[1]
    if status != "active":
        bot.answer_callback_query(call.id, "Челлендж завершён."); return
    if str(user_id) not in participants:
        bot.answer_callback_query(call.id, "Ты не участник."); return
    participants.pop(str(user_id), None)
    execute("UPDATE challenges SET participants=? WHERE id=?", (json.dumps(participants, ensure_ascii=False), chl_id))
    bot.answer_callback_query(call.id, "Готово.")
    bot.send_message(call.message.chat.id, "🚪 Ты вышел из челленджа.")

@bot.callback_query_handler(func=lambda c: c.data.startswith("chl:prog_inc:"))
def chl_progress_inc(call):
    user_id = call.from_user.id
    chl_id = int(call.data.split(":")[-1])
    row = fetch_one("""SELECT participants, target_count, COALESCE(status,'active')
                       FROM challenges WHERE id=?""", (chl_id,))
    if not row:
        bot.answer_callback_query(call.id, "Челлендж не найден."); return
    participants = json_load(row[0], {})
    tcount = int(row[1] or 0)
    status = row[2]
    if status != "active":
        bot.answer_callback_query(call.id, "Челлендж завершён."); return
    if str(user_id) not in participants:
        bot.answer_callback_query(call.id, "Сначала вступи."); return

    participants[str(user_id)] = int(participants[str(user_id)]) + 1
    execute("UPDATE challenges SET participants=? WHERE id=?", (json.dumps(participants, ensure_ascii=False), chl_id))

    # авто-проверка завершения
    done = int(participants[str(user_id)]) >= tcount if tcount else False
    msg = f"👍 Прогресс: {participants[str(user_id)]}/{tcount}" if tcount else f"👍 Прогресс: {participants[str(user_id)]}"
    bot.answer_callback_query(call.id, msg)

    # Если кто-то достиг цели — завершаем и распределяем приз
    if done:
        ok, info = finalize_challenge(chl_id)
        bot.send_message(call.message.chat.id, f"🏁 {info}")

@bot.callback_query_handler(func=lambda c: c.data.startswith("chl:finish:"))
def chl_finish(call):
    user_id = call.from_user.id
    chl_id = int(call.data.split(":")[-1])
    row = fetch_one("SELECT creator_id FROM challenges WHERE id=?", (chl_id,))
    if not row:
        bot.answer_callback_query(call.id, "Челлендж не найден."); return
    if row[0] != user_id:
        bot.answer_callback_query(call.id, "Только создатель может завершить."); return
    ok, info = finalize_challenge(chl_id)
    bot.answer_callback_query(call.id, "Готово." if ok else "Не удалось.")
    bot.send_message(call.message.chat.id, f"🏁 {info}")

@bot.message_handler(func=lambda m: m.text == '🧩 Мои челленджи')
def my_challenges(message):
    user_id, _ = ensure_user(message)
    rows = fetch_all("""SELECT id, name, participants, creator_id, COALESCE(status,'active')
                        FROM challenges ORDER BY id DESC""")
    mine = []
    for _id, name, participants_json, creator_id, status in rows:
        participants = json_load(participants_json, {})
        if str(user_id) in participants or user_id == creator_id:
            mine.append((_id, name, participants, creator_id, status))

    if not mine:
        bot.send_message(message.chat.id, "У тебя пока нет челленджей.")
        return

    text = "<b>Мои челленджи</b>:\n\n"
    for _id, name, participants, creator_id, status in mine:
        prog = participants.get(str(user_id), 0)
        role = "создатель" if user_id == creator_id else "участник"
        text += f"• <b>{name}</b> — {role}, статус: {status}, мой прогресс: {prog}\n"
        kb = challenge_inline(_id, str(user_id) in participants, user_id == creator_id, status)
        bot.send_message(message.chat.id, text.splitlines()[-1], reply_markup=kb)
    # отправим общий заголовок
    bot.send_message(message.chat.id, "\n".join(text.splitlines()[:2]))

# === Создание челленджа (премиум) ===

@bot.callback_query_handler(func=lambda c: c.data == "chl:create")
def create_challenge_start(call):
    user_id = call.from_user.id
    if not compute_is_premium(user_id):
        bot.answer_callback_query(call.id, "🚫 Только для премиум-пользователей.")
        bot.send_message(call.message.chat.id,
                         "🌟 Хочешь создать свой челлендж?\nКупи премиум и получи доступ!\n\nКоманда: /premium")
        return
    user_states[user_id] = {"state": "chl_name"}
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, "Придумай название челленджа:")

@bot.message_handler(func=lambda m: user_states.get(m.from_user.id, {}).get("state") == "chl_name")
def chl_set_name(message):
    st = user_states.setdefault(message.from_user.id, {})
    st["name"] = message.text.strip()
    st["state"] = "chl_desc"
    bot.send_message(message.chat.id, "Добавь описание челленджа:")

@bot.message_handler(func=lambda m: user_states.get(m.from_user.id, {}).get("state") == "chl_desc")
def chl_set_desc(message):
    st = user_states.setdefault(message.from_user.id, {})
    st["description"] = message.text.strip()
    st["state"] = "chl_target_count"
    bot.send_message(message.chat.id, "Укажи числовую цель (например: 7):")

@bot.message_handler(func=lambda m: user_states.get(m.from_user.id, {}).get("state") == "chl_target_count")
def chl_set_target_count(message):
    st = user_states.setdefault(message.from_user.id, {})
    try:
        st["target_count"] = int(re.findall(r"\d+", message.text)[0])
    except Exception:
        bot.send_message(message.chat.id, "Нужно число. Попробуй ещё раз.")
        return
    st["state"] = "chl_deadline_days"
    bot.send_message(message.chat.id, "Через сколько дней дедлайн? (например: 7). 0 — без дедлайна:")

@bot.message_handler(func=lambda m: user_states.get(m.from_user.id, {}).get("state") == "chl_deadline_days")
def chl_set_deadline(message):
    st = user_states.setdefault(message.from_user.id, {})
    try:
        days = int(re.findall(r"\d+", message.text)[0])
    except Exception:
        bot.send_message(message.chat.id, "Нужно число (0 или больше)."); return
    st["deadline"] = (datetime.now() + timedelta(days=days)).isoformat() if days > 0 else None
    st["state"] = "chl_pool"
    bot.send_message(message.chat.id, "Размер призового фонда? Введи число (виртуальные монеты/баллы):")

@bot.message_handler(func=lambda m: user_states.get(m.from_user.id, {}).get("state") == "chl_pool")
def chl_set_pool(message):
    user_id = message.from_user.id
    st = user_states.setdefault(user_id, {})
    try:
        pool = int(re.findall(r"\d+", message.text)[0])
    except Exception:
        bot.send_message(message.chat.id, "Нужно число. Попробуй ещё раз.")
        return
    st["prize_pool"] = max(0, pool)
    st["state"] = "chl_public"
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("Сделать публичным", callback_data="chl:finalize:public"))
    kb.add(types.InlineKeyboardButton("Сделать приватным", callback_data="chl:finalize:private"))
    bot.send_message(message.chat.id, "Публичный или приватный челлендж?", reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data.startswith("chl:finalize:"))
def chl_finalize(call):
    user_id = call.from_user.id
    st = user_states.get(user_id, {})
    if not st:
        bot.answer_callback_query(call.id, "Сессия создания потеряна. Начни заново."); return
    is_public = 1 if call.data.endswith("public") else 0
    execute(
        "INSERT INTO challenges (name, description, creator_id, target, prize_pool, participants, is_premium, "
        "is_public, created_at, status, target_count, deadline, winners) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            st.get("name", "Без названия"),
            st.get("description", ""),
            user_id,
            f"{st.get('target_count')} раз(а)",  # legacy текстовая цель
            st.get("prize_pool", 0),
            json.dumps({}, ensure_ascii=False),
            1,
            is_public,
            now_iso(),
            "active",
            st.get("target_count", 0),
            st.get("deadline", None),
            json.dumps([], ensure_ascii=False)
        )
    )
    user_states.pop(user_id, None)
    bot.answer_callback_query(call.id, "Создано!")
    bot.send_message(call.message.chat.id, "✅ Челлендж создан! Участники могут присоединиться в «Публичные челленджи».")

# ===================== СТАТИСТИКА / АЧИВКИ / ЛИДЕРЫ =====================

@bot.message_handler(func=lambda m: m.text == '📊 Статистика')
def stats(message):
    user_id, _ = ensure_user(message)
    row = fetch_one("SELECT karma, level, is_premium, premium_until FROM users WHERE user_id=?", (user_id,))
    row2 = fetch_one("SELECT COUNT(*) FROM habits WHERE user_id=?", (user_id,))
    karma, level, prem_flag, prem_until = row or (0, 1, 0, None)
    # актуализируем премиум
    prem_active = compute_is_premium(user_id)
    lvl_progress = "🟢" * min(level, 10) + "⚪" * max(0, 10 - min(level, 10))
    pm = "Да" if prem_active else "Нет"
    pm_until = dt_from_iso(prem_until).strftime("%Y-%m-%d") if prem_until else "—"
    habits = row2[0] if row2 else 0
    bot.send_message(
        message.chat.id,
        f"📊 <b>Статистика</b>\n\n"
        f"✨ Карма: <b>{karma}</b>\n"
        f"🎯 Уровень: <b>{level}</b>\n"
        f"🌟 Премиум: <b>{pm}</b> (до: {pm_until})\n"
        f"📈 Привычек: <b>{habits}</b>\n\n"
        f"Прогресс уровней:\n{lvl_progress}"
    )

@bot.message_handler(func=lambda m: m.text == '🎖 Ачивки')
def achivs(message):
    rows = fetch_all("SELECT title, description FROM achievements WHERE user_id=?", (message.from_user.id,))
    if not rows:
        bot.send_message(message.chat.id, "😅 У тебя пока нет ачивок. Всё впереди!")
        return
    text = "🏅 <b>Твои достижения:</b>\n\n"
    for t, d in rows:
        text += f"• {t} — {d}\n"
    bot.send_message(message.chat.id, text)

@bot.message_handler(commands=['leaderboard', 'top'])
@bot.message_handler(func=lambda m: m.text == '🏅 Лидеры')
def leaderboard(message):
    rows = fetch_all("SELECT username, karma, level FROM users ORDER BY level DESC, karma DESC LIMIT 10")
    if not rows:
        bot.send_message(message.chat.id, "Таблица лидеров пока пуста."); return
    text = "🏅 <b>Топ-10</b>\n\n"
    for i, (username, karma, level) in enumerate(rows, 1):
        text += f"{i}. @{username or 'user'} — {karma}✨ / lvl {level}\n"
    bot.send_message(message.chat.id, text)

# ===================== ЕЖЕДНЕВНАЯ НАГРАДА =====================

@bot.message_handler(func=lambda m: m.text == '🎁 Ежедневная награда')
def daily_reward(message):
    user_id, _ = ensure_user(message)
    row = fetch_one("SELECT last_daily_claim FROM users WHERE user_id=?", (user_id,))
    last = dt_from_iso(row[0]) if row and row[0] else None

    now = datetime.now()
    if last and now - last < timedelta(hours=24):
        left = timedelta(hours=24) - (now - last)
        hours = int(left.total_seconds() // 3600)
        bot.send_message(message.chat.id, f"⏰ Уже получал сегодня! Возвращайся через ~{hours} ч.")
        return

    reward = random.randint(DAILY_MIN, DAILY_MAX)
    add_karma(user_id, reward, reason="daily")
    execute("UPDATE users SET last_daily_claim=? WHERE user_id=?", (now_iso(), user_id))
    bot.send_message(message.chat.id, f"🎉 Ты получил <b>{reward}</b> кармы за ежедневный вход!")

# ===================== РЕФЕРАЛЫ =====================

@bot.message_handler(func=lambda m: m.text == '👥 Пригласить друга')
def invite_friend(message):
    link = f"https://t.me/{bot.get_me().username}?start=ref{message.from_user.id}"
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("📤 Поделиться", url=f"https://t.me/share/url?url={link}"))
    bot.send_message(
        message.chat.id,
        f"👥 Пригласи друзей!\nТвоя ссылка:\n<code>{link}</code>\n\n"
        f"Каждый приглашённый = +{REF_BONUS} кармы 🎁",
        reply_markup=kb
    )

# ===================== МАГАЗИН / ПЛАТЕЖИ =====================

@bot.message_handler(func=lambda m: m.text == '🛒 Магазин')
@bot.message_handler(commands=['shop'])
def open_shop(message):
    if not ensure_payments_enabled(message.chat.id):
        return
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("🌟 Премиум 30 дней — 199 ₽", callback_data='shop:buy_premium'))
    kb.add(types.InlineKeyboardButton("💰 +100 кармы — 99 ₽", callback_data='shop:buy_karma'))
    bot.send_message(message.chat.id, "🛒 Магазин улучшений:", reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data == "shop:buy_premium")
def shop_buy_premium(call):
    prices = [types.LabeledPrice(label='🌟 Премиум-доступ на 30 дней', amount=19900)]
    bot.answer_callback_query(call.id)
    if not ensure_payments_enabled(call.message.chat.id):
        return
    bot.send_invoice(
        chat_id=call.message.chat.id,
        title="Премиум-доступ",
        description="Приватные челленджи, участие в призовых и x2 карма!",
        provider_token=PROVIDER_TOKEN,
        currency='RUB',
        prices=prices,
        start_parameter='premium-subscription',
        invoice_payload='premium_payment'
    )

@bot.callback_query_handler(func=lambda c: c.data == "shop:buy_karma")
def shop_buy_karma(call):
    prices = [types.LabeledPrice(label='💰 +100 кармы', amount=9900)]
    bot.answer_callback_query(call.id)
    if not ensure_payments_enabled(call.message.chat.id):
        return
    bot.send_invoice(
        chat_id=call.message.chat.id,
        title="+100 кармы",
        description="Мгновенное пополнение кармы на 100 очков.",
        provider_token=PROVIDER_TOKEN,
        currency='RUB',
        prices=prices,
        start_parameter='add-karma',
        invoice_payload='add_karma_100'
    )

@bot.message_handler(commands=['premium'])
def premium_cmd(message):
    if not ensure_payments_enabled(message.chat.id):
        return
    prices = [types.LabeledPrice(label='🌟 Премиум-доступ на 30 дней', amount=19900)]
    bot.send_invoice(
        chat_id=message.chat.id,
        title="Премиум-доступ",
        description="Создавай приватные челленджи, участвуй в призовых и получай x2 карму!",
        provider_token=PROVIDER_TOKEN,
        currency='RUB',
        prices=prices,
        start_parameter='premium-subscription',
        invoice_payload='premium_payment'
    )

@bot.pre_checkout_query_handler(func=lambda q: True)
def pre_checkout(pre_checkout_query):
    bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

@bot.message_handler(content_types=['successful_payment'])
def got_payment(message):
    user_id = message.from_user.id
    amount_rub = message.successful_payment.total_amount // 100
    payload = message.successful_payment.invoice_payload

    execute(
        "INSERT INTO payments (user_id, amount, description, status, created_at) VALUES (?, ?, ?, ?, ?)",
        (user_id, amount_rub, payload, 'success', now_iso())
    )

    if payload == "premium_payment":
        until = (datetime.now() + timedelta(days=30)).isoformat()
        execute("UPDATE users SET is_premium=1, premium_until=? WHERE user_id=?", (until, user_id))
        bot.send_message(
            message.chat.id,
            "✅ Оплата прошла успешно!\n"
            "Теперь у тебя <b>Премиум-доступ</b> 🌟 на 30 дней.\n"
            "Создавай свои челленджи, получай x2 карму и доступ к приватным заданиям!"
        )
    elif payload == "add_karma_100":
        add_karma(user_id, 100, reason="shop_buy")
        bot.send_message(message.chat.id, "✅ +100 кармы зачислены! Спасибо за поддержку 🙌")
    else:
        bot.send_message(message.chat.id, "✅ Платёж получен!")

# ===================== HELP / FALLBACK =====================

@bot.message_handler(commands=['help'])
def help_cmd(message):
    bot.send_message(
        message.chat.id,
        "🧭 Команды:\n"
        "/start — запуск и реферал\n"
        "/help — помощь\n"
        "/shop — магазин\n"
        "/premium — купить премиум (30 дней)\n"
        "/leaderboard — топ пользователей\n\n"
        "Ещё:\n"
        "• «🏆 Челленджи» — каталог и создание\n"
        "• «🧩 Мои челленджи» — участие/прогресс/завершение\n"
        "• «🎯 Мои привычки» — управление привычками\n"
        "• «🎁 Ежедневная награда» — бонус каждый день\n"
        "• «👥 Пригласить друга» — реферальная ссылка\n"
    )

@bot.message_handler(content_types=['text'])
def fallback(message):
    if message.text.startswith("/start"):
        return
    bot.send_message(message.chat.id, "Не понял команду 🤔\nВыбери действие из меню.", reply_markup=main_menu())

# ===================== ЗАПУСК =====================

@bot.callback_query_handler(func=lambda call: True)
def fallback_callback(call):
    # если какой-то callback не поймали более специфичные хэндлеры — хотя бы ответим,
    # и увидим, что именно пришло
    try:
        bot.answer_callback_query(call.id, cache_time=1)
    except Exception:
        pass
    print(f"[DEBUG] callback_query: from={call.from_user.id} data={call.data!r}")



if __name__ == '__main__':
    if not BOT_TOKEN or BOT_TOKEN.startswith("PASTE_"):
        print("❌ Установи переменную окружения BOT_TOKEN перед запуском.")
        sys.exit(1)

    if not PAYMENTS_AVAILABLE:
        print("⚠️ PROVIDER_TOKEN не задан — платёжные функции будут отключены.")

    print("Бот запущен...")
    try:
        # важно: явно подписываемся и на message, и на callback_query
        bot.polling(
            none_stop=True,
            interval=0,
            timeout=60,
            allowed_updates=['message', 'callback_query']
        )
    except KeyboardInterrupt:
        print("Выключение бота...")


import os
import re
import sys
import json
import logging
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import asyncio
from zoneinfo import ZoneInfo
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.error import BadRequest as TgBadRequest
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler
)
from database import get_db
from datetime import datetime, date, timedelta
from weather import get_weather, format_weather_full, danger_level

KYIV_TZ = ZoneInfo("Europe/Kyiv")

def now_kyiv() -> datetime:
    """Current datetime in Europe/Kyiv timezone (naive)."""
    return datetime.now(KYIV_TZ).replace(tzinfo=None)

def today_kyiv() -> date:
    """Current date in Europe/Kyiv timezone."""
    return datetime.now(KYIV_TZ).date()

REGISTER_NAME = 0
DAYS_RU = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
DAYS_SHORT = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
PRIORITY_EMOJI = {"high": "🔴", "normal": "🟡", "low": "🟢"}
PRIORITY_RU = {"high": "Высокий", "normal": "Средний", "low": "Низкий"}

async def safe_edit(query, text: str, reply_markup=None, parse_mode: str | None = None):
    """
    Edit an inline-keyboard message's text.
    Falls back to delete + send_message when the message is a media message
    (photo/video) that cannot be edited with edit_message_text.
    Swallows "message is not modified" — treat it as success.
    """
    try:
        return await query.edit_message_text(
            text, reply_markup=reply_markup, parse_mode=parse_mode
        )
    except TgBadRequest as exc:
        err = str(exc).lower()
        if "message is not modified" in err:
            return  # idempotent — already showing the same text
        if "there is no text in the message" in err or "message can't be edited" in err:
            # Media message — delete it and send a fresh text message
            try:
                await query.message.delete()
            except Exception:
                pass
            return await query.message.chat.send_message(
                text, reply_markup=reply_markup, parse_mode=parse_mode
            )
        raise


QUICK_MESSAGES = [
    ("🌪 Шторм",         "🌪 Шторм! Убрать всё с пляжа, укрыть инвентарь."),
    ("🏠 Хостел",        "🏠 Подойдите на хостел."),
    ("⛱ Закрыть зонты", "⛱ Закрываем зонты."),
    ("⚠️ Сильный ветер", "⚠️ Сильный ветер. Будьте осторожны."),
    ("🌧 Дождь",         "🌧 Возможен дождь. Готовьтесь."),
    ("🚨 Всем собраться","🚨 Всем собраться у главного входа."),
    ("📍 Управляющий",   "📍 Подойти к управляющему."),
    ("📦 Разгрузка",     "📦 Разгрузка. Подойдите помочь."),
    ("🍽 Обед",          "🍽 Обед. Перерыв 30 минут."),
    ("🧹 Уборка",        "🧹 Уборка территории."),
]

URGENT_MESSAGES = [
    ("🌪 Шторм",         "🚨 СРОЧНО! 🌪 Шторм! Немедленно убрать всё с пляжа!"),
    ("⛱ Закрыть зонты", "🚨 СРОЧНО! ⛱ Немедленно закрываем все зонты!"),
    ("🏠 Хостел",        "🚨 СРОЧНО! 🏠 Всем подойти на хостел!"),
    ("🚨 Всем собраться","🚨 СРОЧНО! Всем немедленно собраться у главного входа!"),
    ("📍 Управляющий",   "🚨 СРОЧНО! Подойти к управляющему немедленно!"),
]

SOS_REASONS = [
    "🆘 Нужна помощь",
    "😡 Конфликт с гостями",
    "🏥 Медицинская ситуация",
    "🔧 Проблема с оборудованием",
    "❓ Другое",
]

logger = logging.getLogger(__name__)
UPLOAD_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static", "uploads", "reports")


async def send_with_retry(bot, chat_id: int, text: str, parse_mode=None, reply_markup=None,
                          retries: int = 2, retry_delay: float = 1.5) -> bool:
    """Send a Telegram message with automatic retry on failure. Returns True on success."""
    for attempt in range(retries + 1):
        try:
            await bot.send_message(chat_id=chat_id, text=text,
                                   parse_mode=parse_mode, reply_markup=reply_markup)
            return True
        except Exception as exc:
            if attempt < retries:
                logger.warning("Send attempt %d/%d to %d failed: %s — retrying in %.1fs",
                               attempt + 1, retries + 1, chat_id, exc, retry_delay)
                await asyncio.sleep(retry_delay)
            else:
                logger.error("Send FAILED after %d attempts to %d: %s", retries + 1, chat_id, exc)
    return False


async def _do_broadcast(bot, employees, msg_text: str, is_urgent: bool = False,
                        label: str = "broadcast") -> tuple[int, int, int, list]:
    """Send message to employees with retry. Returns (delivered, failed, total, recipient_names)."""
    delivered = 0
    failed = 0
    recipient_names: list[str] = []
    for emp in employees:
        ok = await send_with_retry(bot, emp["telegram_id"], msg_text)
        if ok:
            delivered += 1
            recipient_names.append(emp["name"])
            logger.info("Broadcast [%s] → %-20s ✓", label, emp["name"])
        else:
            failed += 1
            logger.error("Broadcast [%s] → %-20s ✗ (all retries failed)", label, emp["name"])

    total = len(employees)
    conn = get_db()
    conn.execute("INSERT INTO broadcasts (message, recipient, total, delivered) VALUES (?, ?, ?, ?)",
                 (msg_text, "urgent" if is_urgent else "today", total, delivered))
    conn.commit()
    conn.close()
    return delivered, failed, total, recipient_names


def _get_today_workers() -> list:
    """Return employees (telegram_id, name) who have a shift today."""
    conn = get_db()
    today_str = today_kyiv().isoformat()
    emps = conn.execute("""
        SELECT DISTINCT e.telegram_id, e.name
        FROM employees e
        JOIN schedules s ON s.employee_id = e.id
        WHERE s.date = ? AND e.is_active = 1 AND e.telegram_id IS NOT NULL
    """, (today_str,)).fetchall()
    conn.close()
    return emps


def _deposit_bar(amount: float, goal: float = 5000.0, length: int = 10) -> str:
    """Progress bar, e.g. '█████░░░░░ 50%'."""
    filled = min(int(amount / goal * length), length) if goal > 0 else 0
    pct = min(int(amount / goal * 100), 100) if goal > 0 else 0
    return "█" * filled + "░" * (length - filled) + f" {pct}%"

# ──────────────────────────────────────────────────────────────────────────────
# OWNER: set OWNER_TELEGRAM_ID env var → that user auto-gets admin role
# ──────────────────────────────────────────────────────────────────────────────
try:
    OWNER_TELEGRAM_ID = int(os.environ.get("OWNER_TELEGRAM_ID", "0"))
except ValueError:
    OWNER_TELEGRAM_ID = 0


# ─── DB helpers ───────────────────────────────────────────────────────────────

def get_employee(telegram_id):
    conn = get_db()
    emp = conn.execute(
        "SELECT * FROM employees WHERE telegram_id=? AND is_active=1", (telegram_id,)
    ).fetchone()
    conn.close()
    return emp


def is_admin(emp) -> bool:
    return emp is not None and emp["is_bot_admin"] == 1


def auto_promote_if_owner(telegram_id: int, emp_id: int):
    """If OWNER_TELEGRAM_ID is set and matches, promote to admin automatically."""
    if OWNER_TELEGRAM_ID and telegram_id == OWNER_TELEGRAM_ID:
        conn = get_db()
        conn.execute("UPDATE employees SET is_bot_admin=1 WHERE id=?", (emp_id,))
        conn.commit()
        conn.close()
        return True
    return False


# ─── Registration ─────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    emp = get_employee(user.id)

    if emp:
        # Auto-promote owner if not yet admin
        if not is_admin(emp):
            if auto_promote_if_owner(user.id, emp["id"]):
                emp = get_employee(user.id)

        context.user_data.clear()
        if is_admin(emp):
            await show_admin_menu(update, context, emp)
        else:
            await show_worker_menu(update, context, emp)
    else:
        await update.message.reply_text(
            "👋 Добро пожаловать в *BeachManager*!\n\n"
            "Введите ваше *полное имя* для регистрации\n"
            f"_(ваш Telegram ID: `{user.id}`)_",
            parse_mode="Markdown"
        )
        return REGISTER_NAME


async def register_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    if len(name) < 2:
        await update.message.reply_text("Пожалуйста, введите корректное имя.")
        return REGISTER_NAME

    conn = get_db()
    employee = conn.execute(
        "SELECT * FROM employees WHERE telegram_id IS NULL AND name=? AND is_active=1", (name,)
    ).fetchone()

    if employee:
        conn.execute("UPDATE employees SET telegram_id=? WHERE id=?",
                     (update.effective_user.id, employee["id"]))
        conn.commit()
        conn.close()

        auto_promote_if_owner(update.effective_user.id, employee["id"])
        emp = get_employee(update.effective_user.id)

        await update.message.reply_text(
            f"✅ Добро пожаловать, *{emp['name']}*!\n"
            f"{'👑 Роль: Администратор' if is_admin(emp) else '👤 Роль: Сотрудник'}",
            parse_mode="Markdown"
        )
        if is_admin(emp):
            await show_admin_menu(update, context, emp)
        else:
            await show_worker_menu(update, context, emp)
    else:
        conn.close()
        await update.message.reply_text(
            "❌ Сотрудник с таким именем не найден.\n"
            "Обратитесь к администратору или проверьте написание имени.\n\n"
            f"_Ваш Telegram ID: `{update.effective_user.id}`_",
            parse_mode="Markdown"
        )
        return REGISTER_NAME

    return ConversationHandler.END


# ─── WORKER MENU ─────────────────────────────────────────────────────────────

async def show_worker_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, emp=None):
    if emp is None:
        emp = get_employee(update.effective_user.id)
    if not emp:
        return

    keyboard = [
        [InlineKeyboardButton("📋 Мои задачи", callback_data="my_tasks"),
         InlineKeyboardButton("✅ Я на месте", callback_data="check_in")],
        [InlineKeyboardButton("🏁 Сдать смену", callback_data="shift_handover"),
         InlineKeyboardButton("📝 Сдать отчёт", callback_data="submit_report")],
        [InlineKeyboardButton("📅 Мой график", callback_data="my_schedule"),
         InlineKeyboardButton("🕐 Смена сегодня", callback_data="today_shift")],
        [InlineKeyboardButton("🔄 Обмен сменами", callback_data="exch_start"),
         InlineKeyboardButton("💵 Моя зарплата", callback_data="my_salary")],
        [InlineKeyboardButton("🌦 Погода", callback_data="bot_weather"),
         InlineKeyboardButton("📊 Статистика", callback_data="my_stats")],
        [InlineKeyboardButton("💬 Помощь", callback_data="help"),
         InlineKeyboardButton("💰 Залог", callback_data="deposit_menu")],
        [InlineKeyboardButton("🚨 SOS", callback_data="sos_start")],
    ]
    text = (
        f"🏖️ *BeachManager*\n"
        f"Привет, *{emp['name']}*! 👋\n\n"
        f"👤 Роль: Сотрудник\n"
        f"Что хотите сделать?"
    )
    markup = InlineKeyboardMarkup(keyboard)
    if update.callback_query:
        await safe_edit(update.callback_query, text, reply_markup=markup, parse_mode="Markdown")
    else:
        await update.message.reply_text(text, reply_markup=markup, parse_mode="Markdown")


# ─── ADMIN MENU ──────────────────────────────────────────────────────────────

async def show_admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, emp=None):
    if emp is None:
        emp = get_employee(update.effective_user.id)
    if not emp or not is_admin(emp):
        return

    conn = get_db()
    pending_reports = conn.execute("SELECT COUNT(*) FROM task_reports WHERE status='pending'").fetchone()[0]
    pending_exchanges = conn.execute("SELECT COUNT(*) FROM shift_exchanges WHERE status='pending_admin'").fetchone()[0]
    conn.close()

    rpt_label = f"📸 Отчёты ({pending_reports} ❗)" if pending_reports else "📸 Отчёты"
    exch_label = f"🔄 Обмены ({pending_exchanges} ❗)" if pending_exchanges else "🔄 Обмены"

    keyboard = [
        [InlineKeyboardButton("🏖 Сегодня на пляже", callback_data="adm_today")],
        [InlineKeyboardButton("📅 График", callback_data="adm_sched"),
         InlineKeyboardButton("📋 Задания", callback_data="adm_tasks")],
        [InlineKeyboardButton("📨 Рассылка", callback_data="adm_bcast"),
         InlineKeyboardButton("🚨 Срочная", callback_data="adm_urgent")],
        [InlineKeyboardButton(rpt_label, callback_data="adm_rpts"),
         InlineKeyboardButton("👥 Сотрудники", callback_data="adm_staff")],
        [InlineKeyboardButton("📊 Статистика", callback_data="adm_stats"),
         InlineKeyboardButton(exch_label, callback_data="adm_exchanges")],
        [InlineKeyboardButton("🌦 Погода", callback_data="bot_weather"),
         InlineKeyboardButton("⚙️ Настройки", callback_data="adm_settings")],
        [InlineKeyboardButton("⚙️ Состояние системы", callback_data="adm_sys_status")],
    ]
    text = (
        f"🏖️ *BeachManager*\n"
        f"*{emp['name']}* 👑 Администратор\n\n"
        f"Выберите раздел:"
    )
    alerts = []
    if pending_reports:
        alerts.append(f"{pending_reports} отчётов на проверке")
    if pending_exchanges:
        alerts.append(f"{pending_exchanges} обменов сменами")
    if alerts:
        text += f"\n\n⚠️ _Ожидают: {', '.join(alerts)}_"

    markup = InlineKeyboardMarkup(keyboard)
    if update.callback_query:
        await safe_edit(update.callback_query, text, reply_markup=markup, parse_mode="Markdown")
    else:
        await update.message.reply_text(text, reply_markup=markup, parse_mode="Markdown")


# ─── ADMIN: Schedule ─────────────────────────────────────────────────────────

async def admin_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    offset = context.user_data.get("sched_offset", 0)
    today = date.today()
    monday = today - timedelta(days=today.weekday()) + timedelta(weeks=offset)
    week_dates = [monday + timedelta(days=i) for i in range(7)]

    conn = get_db()
    shifts = conn.execute("""
        SELECT s.*, e.name FROM schedules s
        JOIN employees e ON s.employee_id = e.id
        WHERE s.date >= ? AND s.date <= ?
        ORDER BY s.date, s.shift_start
    """, (week_dates[0].isoformat(), week_dates[-1].isoformat())).fetchall()
    conn.close()

    shift_map: dict[str, list] = {}
    for s in shifts:
        d = s["date"]
        if d not in shift_map:
            shift_map[d] = []
        shift_map[d].append(s)

    week_label = f"{week_dates[0].strftime('%d.%m')} – {week_dates[-1].strftime('%d.%m.%Y')}"
    text = f"📅 *График смен*\n_{week_label}_\n\n"

    for i, d in enumerate(week_dates):
        d_str = d.isoformat()
        marker = " ◀ сегодня" if d == today else ""
        day_shifts = shift_map.get(d_str, [])
        if day_shifts:
            names = ", ".join(f"{s['name'].split()[0]} {s['shift_start'][:5]}–{s['shift_end'][:5]}"
                              for s in day_shifts)
            text += f"*{DAYS_SHORT[i]} {d.strftime('%d.%m')}*{marker}: {names}\n"
        else:
            text += f"*{DAYS_SHORT[i]} {d.strftime('%d.%m')}*{marker}: —\n"

    text += "\n_Редактирование графика — в веб-панели_"

    keyboard = [
        [InlineKeyboardButton("◀ Прошлая", callback_data="adm_sched_prev"),
         InlineKeyboardButton("▶ Следующая", callback_data="adm_sched_next")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="adm_menu")],
    ]
    await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def admin_schedule_nav(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "adm_sched_prev":
        context.user_data["sched_offset"] = context.user_data.get("sched_offset", 0) - 1
    else:
        context.user_data["sched_offset"] = context.user_data.get("sched_offset", 0) + 1
    await admin_schedule(update, context)


# ─── ADMIN: Tasks ─────────────────────────────────────────────────────────────

async def admin_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    conn = get_db()
    tasks = conn.execute("""
        SELECT t.*, e.name as emp_name FROM tasks t
        LEFT JOIN employees e ON t.assigned_to = e.id
        WHERE t.status NOT IN ('completed')
        ORDER BY
          CASE t.status WHEN 'pending_review' THEN 1 WHEN 'in_progress' THEN 2 ELSE 3 END,
          CASE t.priority WHEN 'high' THEN 1 WHEN 'normal' THEN 2 ELSE 3 END
        LIMIT 15
    """).fetchall()
    total_completed = conn.execute("SELECT COUNT(*) FROM tasks WHERE status='completed'").fetchone()[0]
    conn.close()

    status_ru = {"pending": "⏳", "in_progress": "🔄", "pending_review": "🔍"}
    text = f"📋 *Активные задания* ({len(tasks)})\n\n"

    if tasks:
        for t in tasks:
            p = PRIORITY_EMOJI.get(t["priority"], "⚪")
            s = status_ru.get(t["status"], "❓")
            emp = t["emp_name"] or "Не назначен"
            text += f"{p}{s} *{t['title']}*\n   👤 {emp} | ID #{t['id']}\n"
    else:
        text += "_Нет активных заданий_\n"

    text += f"\n✅ Выполнено всего: {total_completed}"

    keyboard = [
        [InlineKeyboardButton("➕ Создать задание", callback_data="adm_new_task")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="adm_menu")],
    ]
    await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def admin_new_task_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 1: pick employee."""
    query = update.callback_query
    await query.answer()

    conn = get_db()
    employees = conn.execute(
        "SELECT id, name FROM employees WHERE is_active=1 AND telegram_id IS NOT NULL ORDER BY name"
    ).fetchall()
    conn.close()

    if not employees:
        await safe_edit(query, 
            "Нет сотрудников с привязанным Telegram для назначения задания.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="adm_tasks")]])
        )
        return

    keyboard = []
    for emp in employees:
        keyboard.append([InlineKeyboardButton(emp["name"], callback_data=f"adm_task_emp_{emp['id']}")])
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="adm_tasks")])

    context.user_data["adm"] = {"flow": "create_task", "step": "employee"}
    await safe_edit(query, 
        "📋 *Новое задание*\n\nВыберите исполнителя:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )


async def admin_task_emp_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 2: employee selected, ask for title."""
    query = update.callback_query
    await query.answer()

    emp_id = int(query.data.split("_")[3])
    conn = get_db()
    emp = conn.execute("SELECT id, name FROM employees WHERE id=?", (emp_id,)).fetchone()
    conn.close()

    context.user_data["adm"] = {
        "flow": "create_task",
        "step": "title",
        "emp_id": emp_id,
        "emp_name": emp["name"],
        "title": "",
        "description": "",
        "priority": "normal",
        "photo_file_ids": [],
        "due_date": "",
    }

    keyboard = [[InlineKeyboardButton("❌ Отмена", callback_data="adm_tasks")]]
    await safe_edit(query, 
        f"📋 *Новое задание*\n"
        f"👤 Исполнитель: *{emp['name']}*\n\n"
        f"Шаг 1/4 — Введите *название* задания:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )


async def admin_task_priority(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Final step: priority selected → create task."""
    query = update.callback_query
    await query.answer()

    adm = context.user_data.get("adm", {})
    priority = query.data.split("_")[3]  # adm_task_prio_high
    adm["priority"] = priority
    context.user_data["adm"] = adm

    file_ids = adm.get("photo_file_ids", [])
    due_date = adm.get("due_date") or None

    conn = get_db()
    cur = conn.execute(
        "INSERT INTO tasks (title, description, assigned_to, priority, due_date, photo_file_ids) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (adm["title"], adm["description"], adm["emp_id"], priority,
         due_date, json.dumps(file_ids))
    )
    task_id = cur.lastrowid
    conn.execute("INSERT INTO notifications (employee_id, message) VALUES (?, ?)",
                 (adm["emp_id"], f"📋 Назначена задача: {adm['title']}"))
    emp_row = conn.execute("SELECT telegram_id FROM employees WHERE id=?", (adm["emp_id"],)).fetchone()
    conn.commit()
    conn.close()

    # Send Telegram notification to employee (with photos if any)
    if emp_row and emp_row["telegram_id"]:
        tg_id = emp_row["telegram_id"]
        try:
            msg = (
                f"📋 *Вам назначено новое задание!*\n\n"
                f"*{adm['title']}*\n"
            )
            if adm["description"]:
                msg += f"📝 _{adm['description']}_\n"
            msg += f"\n🎯 Приоритет: {PRIORITY_EMOJI[priority]} {PRIORITY_RU[priority]}"
            if due_date:
                msg += f"\n📅 Срок: {due_date}"
            if file_ids:
                msg += f"\n📸 {len(file_ids)} фото прикреплено"
            bot = query.get_bot()
            if file_ids:
                if len(file_ids) == 1:
                    await bot.send_photo(chat_id=tg_id, photo=file_ids[0],
                                         caption=msg, parse_mode="Markdown")
                else:
                    media = [InputMediaPhoto(fid) for fid in file_ids]
                    await bot.send_media_group(chat_id=tg_id, media=media)
                    await bot.send_message(chat_id=tg_id, text=msg, parse_mode="Markdown")
            else:
                await bot.send_message(chat_id=tg_id, text=msg, parse_mode="Markdown")
        except Exception as e:
            logger.warning("Task notify failed: %s", e)

    context.user_data.pop("adm", None)
    logger.info("Task #%d created: '%s' → %s (priority=%s, photos=%d)",
                task_id, adm["title"], adm["emp_name"], priority, len(file_ids))

    keyboard = [
        [InlineKeyboardButton("📋 К заданиям", callback_data="adm_tasks")],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="adm_menu")],
    ]
    photo_line = f"\n📸 {len(file_ids)} фото" if file_ids else ""
    due_line = f"\n📅 {due_date}" if due_date else ""
    await safe_edit(query, 
        f"✅ *Задание создано!*\n\n"
        f"📋 {adm['title']}\n"
        f"👤 {adm['emp_name']}\n"
        f"🎯 {PRIORITY_EMOJI[priority]} {PRIORITY_RU[priority]}"
        f"{due_line}{photo_line}\n\n"
        f"Сотрудник уведомлён в Telegram.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )


# ─── ADMIN: Today at the Beach ───────────────────────────────────────────────

async def adm_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """🏖 Today at the beach — full dashboard for admins."""
    from weather import get_weather, danger_level, weather_emoji, weather_label as wlabel

    query = update.callback_query
    await query.answer()

    now       = now_kyiv()
    today     = today_kyiv()
    today_str = today.isoformat()

    conn = get_db()

    # ── Staff on shift with attendance status ──────────────────────────────
    staff_rows = conn.execute("""
        SELECT e.id, e.name, e.is_bot_admin, s.shift_start, s.shift_end,
               a.check_in_time, a.minutes_late
        FROM schedules s
        JOIN employees e ON s.employee_id = e.id
        LEFT JOIN attendance a ON a.employee_id = e.id AND a.date = ?
        WHERE s.date = ? AND e.is_active = 1
        ORDER BY s.shift_start, e.name
    """, (today_str, today_str)).fetchall()

    # ── Reports today ──────────────────────────────────────────────────────
    rpts = conn.execute("""
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN status='pending'  THEN 1 ELSE 0 END) as pending,
            SUM(CASE WHEN status='rejected' THEN 1 ELSE 0 END) as rejected
        FROM task_reports
        WHERE DATE(submitted_at) = ?
    """, (today_str,)).fetchone()

    # ── Tasks ──────────────────────────────────────────────────────────────
    tasks_stat = conn.execute("""
        SELECT
            SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) as done,
            SUM(CASE WHEN status NOT IN ('completed') THEN 1 ELSE 0 END) as active
        FROM tasks
    """).fetchone()

    incomplete = conn.execute("""
        SELECT t.title, t.priority, e.name as emp_name
        FROM tasks t
        LEFT JOIN employees e ON t.assigned_to = e.id
        WHERE t.status NOT IN ('completed')
        ORDER BY CASE t.priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END
        LIMIT 6
    """).fetchall()

    # ── Deposits among today's workers (admins excluded) ───────────────────
    today_worker_ids = [r["id"] for r in staff_rows if not r["is_bot_admin"]]
    if today_worker_ids:
        ph = ",".join("?" * len(today_worker_ids))
        dep_paid = conn.execute(
            f"SELECT COUNT(DISTINCT employee_id) FROM deposit_requests "
            f"WHERE DATE(requested_at)=? AND employee_id IN ({ph})",
            [today_str] + today_worker_ids
        ).fetchone()[0]
        dep_not_paid = len(today_worker_ids) - dep_paid
    else:
        dep_paid = dep_not_paid = 0

    conn.close()

    # ── Weather ────────────────────────────────────────────────────────────
    try:
        w = await asyncio.to_thread(get_weather)
    except Exception:
        w = None

    # ── Build message ──────────────────────────────────────────────────────
    time_str = now.strftime("%d.%m.%Y") + " 🕐 " + now.strftime("%H:%M")
    lines = [f"🏖 *Сегодня на пляже*", f"📅 {time_str}", ""]

    # Weather block
    if w:
        tod       = w.get("today", {})
        wind_max  = tod.get("wind_max")  or 0
        gusts_max = tod.get("wind_gusts_max") or 0
        rain_pct  = tod.get("rain_pct")  or 0
        temp_max  = tod.get("temp_max")
        temp_min  = tod.get("temp_min")
        code      = tod.get("code")

        ce = weather_emoji(code)
        cl = wlabel(code)
        temp_str = f"+{temp_max:.0f}°C" if temp_max is not None else "—"

        lines.append(f"🌦 {ce} {cl}, {temp_str}")
        lines.append(f"💨 {wind_max:.0f} км/ч  🌪 {gusts_max:.0f} км/ч  🌧 {rain_pct:.0f}%")

        lv, lv_label, lv_rec = danger_level(wind_max, gusts_max)
        if lv in ("yellow", "red"):
            lines.append(f"{lv_label}")
            if lv_rec:
                lines.append(lv_rec)

        alerts = w.get("alerts", [])
        if alerts:
            for a in alerts:
                lines.append(f"{a['emoji']} {a['text']}")

        lines.append("")

    # Staff on shift — split admins and workers
    admin_rows  = [r for r in staff_rows if r["is_bot_admin"]]
    worker_rows = [r for r in staff_rows if not r["is_bot_admin"]]
    n_total = len(staff_rows)
    n_workers = len(worker_rows)

    lines.append(f"👷 *Сотрудники на смене сегодня ({n_total}):*")
    if not staff_rows:
        lines.append("_(нет смен на сегодня)_")
    else:
        # Admins shown first without attendance status
        for row in admin_rows:
            lines.append(f"👑 {row['name']} — Администратор")

        # Workers with attendance status
        for row in worker_rows:
            name = row["name"]
            if row["check_in_time"]:
                lines.append(f"🟢 {name} — {row['check_in_time']}")
            else:
                try:
                    h, m   = map(int, row["shift_start"].split(":"))
                    sh_dt  = datetime(today.year, today.month, today.day, h, m)
                    diff   = int((now - sh_dt).total_seconds() / 60)
                except Exception:
                    diff = 0

                if diff > 15:
                    lines.append(f"🔴 {name} — опоздание {diff} мин")
                else:
                    lines.append(f"🟡 {name} — не отметился")

    lines.append("")

    # Reports
    r_total = rpts["total"] or 0
    r_pend  = rpts["pending"] or 0
    r_rej   = rpts["rejected"] or 0
    lines.append("📸 *Отчёты сегодня:*")
    lines.append(f"📸 Сдано: *{r_total}*  ⏳ Ожидают: *{r_pend}*  ❌ Отклонено: *{r_rej}*")
    lines.append("")

    # Tasks
    t_done   = tasks_stat["done"]   or 0
    t_active = tasks_stat["active"] or 0
    lines.append("📋 *Задания:*")
    lines.append(f"✅ Выполнено: *{t_done}*  ⏳ Активных: *{t_active}*")
    if incomplete:
        for t in incomplete:
            p    = PRIORITY_EMOJI.get(t["priority"], "⚪")
            who  = f" ({t['emp_name']})" if t["emp_name"] else ""
            lines.append(f"  {p} {t['title']}{who}")
    lines.append("")

    # Deposits
    lines.append("💰 *Залоги:*")
    lines.append(f"💰 Сдали: *{dep_paid}*  ⚠️ Не сдали: *{dep_not_paid}*")

    text = "\n".join(lines)

    keyboard = [
        [InlineKeyboardButton("📨 Рассылка на смене", callback_data="adm_today_bcast"),
         InlineKeyboardButton("🚨 Срочная",           callback_data="adm_urgent")],
        [InlineKeyboardButton("📸 Отчёты",            callback_data="adm_rpts"),
         InlineKeyboardButton("📋 Задания",            callback_data="adm_tasks")],
        [InlineKeyboardButton("🌦 Погода",             callback_data="bot_weather"),
         InlineKeyboardButton("🔄 Обновить",           callback_data="adm_today")],
        [InlineKeyboardButton("⬅️ Меню",              callback_data="adm_menu")],
    ]

    await safe_edit(query, 
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )


async def adm_today_bcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Prompt admin for a message to broadcast to today's on-duty workers."""
    query = update.callback_query
    await query.answer()

    workers = _get_today_workers()
    context.user_data["adm"] = {"flow": "today_bcast", "step": "text"}

    keyboard = [[InlineKeyboardButton("❌ Отмена", callback_data="adm_today")]]
    await safe_edit(query, 
        f"📨 *Рассылка сотрудникам на смене сегодня*\n\n"
        f"Сотрудников на смене: *{len(workers)}*\n\n"
        f"Введите текст сообщения:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )


# ─── ADMIN: Broadcast ────────────────────────────────────────────────────────

async def admin_broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show quick broadcast menu."""
    query = update.callback_query
    await query.answer()

    today_workers = _get_today_workers()
    conn = get_db()
    total_active = conn.execute("SELECT COUNT(*) FROM employees WHERE is_active=1 AND telegram_id IS NOT NULL").fetchone()[0]
    conn.close()
    on_shift = len(today_workers)
    day_off = total_active - on_shift

    keyboard = []
    for i in range(0, len(QUICK_MESSAGES), 2):
        row = [InlineKeyboardButton(QUICK_MESSAGES[i][0], callback_data=f"adm_bcast_q_{i}")]
        if i + 1 < len(QUICK_MESSAGES):
            row.append(InlineKeyboardButton(QUICK_MESSAGES[i + 1][0], callback_data=f"adm_bcast_q_{i+1}"))
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("✏️ Своя рассылка", callback_data="adm_bcast_custom")])
    keyboard.append([InlineKeyboardButton("⬅️ Меню", callback_data="adm_menu")])

    await safe_edit(query, 
        f"📨 *Быстрая рассылка*\n\n"
        f"🟢 Работают сегодня: *{on_shift}* чел.\n"
        f"⛱ Выходной: *{day_off}* чел.\n\n"
        f"Сообщение получат только работающие сегодня:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )


async def admin_broadcast_quick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send a pre-defined quick message ONLY to employees working today."""
    query = update.callback_query
    await query.answer("Отправляем…")

    idx = int(query.data.split("_")[-1])
    label, msg_text = QUICK_MESSAGES[idx]

    employees = _get_today_workers()
    conn = get_db()
    total_active = conn.execute("SELECT COUNT(*) FROM employees WHERE is_active=1 AND telegram_id IS NOT NULL").fetchone()[0]
    conn.close()
    day_off_count = total_active - len(employees)

    delivered, failed, total, recipients = await _do_broadcast(query.get_bot(), employees, msg_text, label=label)

    keyboard = [
        [InlineKeyboardButton("📨 Ещё рассылка", callback_data="adm_bcast")],
        [InlineKeyboardButton("⬅️ Меню", callback_data="adm_menu")],
    ]
    result = f"✅ Получили: {delivered}\n⛱ Выходной сегодня: {day_off_count}"
    if failed:
        result += f"\n❌ Ошибка: {failed}"
    if recipients:
        result += "\n\n👥 *Список получателей:*\n" + "\n".join(f"• {n}" for n in recipients)
    await safe_edit(query, 
        f"📨 *Рассылка отправлена!*\n\n_{msg_text}_\n\n{result}",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )


async def admin_broadcast_custom(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Switch to custom text broadcast input."""
    query = update.callback_query
    await query.answer()

    conn = get_db()
    count = conn.execute("SELECT COUNT(*) FROM employees WHERE is_active=1 AND telegram_id IS NOT NULL").fetchone()[0]
    conn.close()

    context.user_data["adm"] = {"flow": "broadcast", "step": "text"}
    keyboard = [[InlineKeyboardButton("⬅️ Назад", callback_data="adm_bcast")]]
    await safe_edit(query, 
        f"✏️ *Своя рассылка*\n\n"
        f"Подключено {count} сотрудников.\n\n"
        f"Введите текст сообщения:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )


async def admin_broadcast_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin confirms custom broadcast."""
    query = update.callback_query
    await query.answer()

    adm = context.user_data.get("adm", {})
    msg_text = adm.get("text", "")
    if not msg_text:
        await show_admin_menu(update, context)
        return

    employees = _get_today_workers()
    conn = get_db()
    total_active = conn.execute("SELECT COUNT(*) FROM employees WHERE is_active=1 AND telegram_id IS NOT NULL").fetchone()[0]
    conn.close()
    day_off_count = total_active - len(employees)

    delivered, failed, total, recipients = await _do_broadcast(query.get_bot(), employees, msg_text, label="custom")

    context.user_data.pop("adm", None)

    keyboard = [
        [InlineKeyboardButton("📨 Ещё рассылка", callback_data="adm_bcast")],
        [InlineKeyboardButton("⬅️ Главное меню", callback_data="adm_menu")],
    ]
    result = f"✅ Получили: {delivered}\n⛱ Выходной сегодня: {day_off_count}"
    if failed:
        result += f"\n❌ Ошибка: {failed}"
    if recipients:
        result += "\n\n👥 *Список получателей:*\n" + "\n".join(f"• {n}" for n in recipients)
    await safe_edit(query, 
        f"📨 *Рассылка отправлена!*\n\n{result}",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )


# ─── ADMIN: Reports ───────────────────────────────────────────────────────────

async def admin_reports(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    conn = get_db()
    reports = conn.execute("""
        SELECT tr.*, t.title as task_title, e.name as emp_name
        FROM task_reports tr
        JOIN tasks t ON tr.task_id = t.id
        JOIN employees e ON tr.employee_id = e.id
        WHERE tr.status = 'pending'
        ORDER BY tr.submitted_at DESC
        LIMIT 10
    """).fetchall()
    conn.close()

    if not reports:
        keyboard = [[InlineKeyboardButton("⬅️ Назад", callback_data="adm_menu")]]
        await safe_edit(
            query,
            "📸 *Отчёты на проверку*\n\n✅ Нет отчётов, ожидающих проверки!",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return

    text = f"📸 *Отчёты на проверку* ({len(reports)})\n\n"
    keyboard = []

    for r in reports:
        try:
            file_ids = json.loads(r["photo_file_ids"] or "[]")
        except Exception:
            file_ids = []
        try:
            photos = json.loads(r["photos"] or "[]")
        except Exception:
            photos = []
        n_photos = len(file_ids) or len(photos)
        comment_preview = (r["comment"] or "—")[:40]
        time_str = (r["submitted_at"] or "")[:16].replace("T", " ")
        text += (
            f"📋 *{r['task_title']}*\n"
            f"   👤 {r['emp_name']} | 🕐 {time_str}\n"
            f"   💬 {comment_preview}"
            f"{'...' if len(r['comment'] or '') > 40 else ''}"
        )
        if n_photos:
            text += f" | 📸 {n_photos} фото"
        text += "\n\n"
        btn_row = [
            InlineKeyboardButton(f"✅ #{r['id']}", callback_data=f"adm_rpt_ok_{r['id']}"),
            InlineKeyboardButton(f"❌ #{r['id']}", callback_data=f"adm_rpt_no_{r['id']}"),
        ]
        if n_photos:
            btn_row.append(InlineKeyboardButton(f"👁 #{r['id']}", callback_data=f"adm_rpt_view_{r['id']}"))
        keyboard.append(btn_row)

    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="adm_menu")])
    await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def admin_report_view_photos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Re-send stored photos (before + after) so admin can review them."""
    query = update.callback_query
    await query.answer("Загружаем фото…")

    report_id = int(query.data.split("_")[-1])
    conn = get_db()
    report = conn.execute(
        "SELECT tr.*, t.title, t.photo_file_ids as task_photo_file_ids, e.name as emp_name "
        "FROM task_reports tr "
        "JOIN tasks t ON tr.task_id=t.id "
        "JOIN employees e ON tr.employee_id=e.id WHERE tr.id=?",
        (report_id,)
    ).fetchone()
    conn.close()

    if not report:
        await query.answer("Отчёт не найден", show_alert=True)
        return

    try:
        after_ids = json.loads(report["photo_file_ids"] or "[]")
    except Exception:
        after_ids = []

    try:
        before_ids = json.loads(report["task_photo_file_ids"] or "[]")
    except Exception:
        before_ids = []

    if not after_ids and not before_ids:
        await query.answer("Фотографий нет", show_alert=True)
        return

    submitted_at = (report["submitted_at"] or "")[:16].replace("T", " ")
    caption = (
        f"📸 *Отчёт #{report_id}*\n\n"
        f"👷 Сотрудник: *{report['emp_name']}*\n"
        f"📋 Задача: _{report['title']}_\n"
        f"🕒 {submitted_at}\n"
        f"💬 Комментарий: {report['comment'] or '—'}"
    )
    approve_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Подтвердить", callback_data=f"adm_rpt_ok_{report_id}"),
        InlineKeyboardButton("❌ Отклонить",   callback_data=f"adm_rpt_no_{report_id}"),
    ]])

    bot = query.get_bot()
    chat_id = query.message.chat_id
    try:
        # Send BEFORE photos (task photos from admin)
        if before_ids:
            if len(before_ids) == 1:
                await bot.send_photo(chat_id=chat_id, photo=before_ids[0],
                                     caption="📸 *ДО выполнения* (фото задания)",
                                     parse_mode="Markdown")
            else:
                await bot.send_media_group(
                    chat_id=chat_id,
                    media=[InputMediaPhoto(fid) for fid in before_ids]
                )
                await bot.send_message(chat_id=chat_id,
                                       text="📸 *ДО выполнения* (фото задания)",
                                       parse_mode="Markdown")

        # Send AFTER photos (employee report) + caption + buttons
        if after_ids:
            if len(after_ids) == 1:
                await bot.send_photo(chat_id=chat_id, photo=after_ids[0],
                                     caption=caption, parse_mode="Markdown",
                                     reply_markup=approve_kb)
            else:
                await bot.send_media_group(
                    chat_id=chat_id,
                    media=[InputMediaPhoto(fid) for fid in after_ids]
                )
                await bot.send_message(chat_id=chat_id, text=caption,
                                       parse_mode="Markdown", reply_markup=approve_kb)
        else:
            await bot.send_message(chat_id=chat_id, text=caption,
                                   parse_mode="Markdown", reply_markup=approve_kb)
    except Exception as exc:
        logger.warning("View report photos failed: %s", exc)
        await query.answer("Не удалось загрузить фото (возможно, файл устарел)", show_alert=True)


async def admin_report_approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    report_id = int(query.data.split("_")[3])
    conn = get_db()
    report = conn.execute(
        "SELECT tr.*, t.title, e.telegram_id, e.name as emp_name FROM task_reports tr "
        "JOIN tasks t ON tr.task_id=t.id JOIN employees e ON tr.employee_id=e.id WHERE tr.id=?",
        (report_id,)
    ).fetchone()

    if report:
        conn.execute(
            "UPDATE task_reports SET status='approved', reviewed_at=? WHERE id=?",
            (datetime.now().isoformat(), report_id)
        )
        conn.execute(
            "UPDATE tasks SET status='completed', completed_at=? WHERE id=?",
            (datetime.now().isoformat(), report["task_id"])
        )
        conn.commit()

        if report["telegram_id"]:
            try:
                await query.get_bot().send_message(
                    chat_id=report["telegram_id"],
                    text=(
                        f"✅ *Ваш отчёт принят!*\n\n"
                        f"Задача «{report['title']}» подтверждена.\n"
                        f"Отличная работа, {report['emp_name'].split()[0]}! 🎉"
                    ),
                    parse_mode="Markdown"
                )
            except Exception:
                pass
        logger.info("Admin approved report #%d for task: %s", report_id, report["title"])
    conn.close()
    await admin_reports(update, context)


async def admin_report_reject_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    report_id = int(query.data.split("_")[3])
    context.user_data["adm"] = {"flow": "reject_report", "step": "reason", "report_id": report_id}

    keyboard = [
        [InlineKeyboardButton("🚫 Без причины", callback_data="adm_rpt_no_noreason")],
        [InlineKeyboardButton("❌ Отмена", callback_data="adm_rpts")],
    ]
    await safe_edit(query, 
        f"❌ *Отклонить отчёт #{report_id}*\n\n"
        f"Введите причину отклонения (сотрудник получит уведомление):\n"
        f"_Или нажмите «Без причины»_",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )


async def admin_report_reject_noreason(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    adm = context.user_data.get("adm", {})
    report_id = adm.get("report_id")
    if not report_id:
        await show_admin_menu(update, context)
        return

    await _do_reject_report(query, report_id, "")
    context.user_data.pop("adm", None)
    await admin_reports(update, context)


async def _do_reject_report(query_or_message, report_id: int, reason: str):
    conn = get_db()
    report = conn.execute(
        "SELECT tr.*, t.title, e.telegram_id, e.name as emp_name FROM task_reports tr "
        "JOIN tasks t ON tr.task_id=t.id JOIN employees e ON tr.employee_id=e.id WHERE tr.id=?",
        (report_id,)
    ).fetchone()
    if report:
        conn.execute(
            "UPDATE task_reports SET status='rejected', admin_comment=?, reviewed_at=? WHERE id=?",
            (reason, datetime.now().isoformat(), report_id)
        )
        conn.execute("UPDATE tasks SET status='in_progress' WHERE id=?", (report["task_id"],))
        conn.commit()
        if report["telegram_id"]:
            try:
                reason_line = f"\n\n💬 *Причина:* {reason}" if reason else ""
                bot = query_or_message.get_bot()
                await bot.send_message(
                    chat_id=report["telegram_id"],
                    text=(
                        f"🔄 *Отчёт отклонён*\n\n"
                        f"Задача «{report['title']}» требует доработки.{reason_line}\n\n"
                        f"Вы можете отправить новый отчёт после исправления."
                    ),
                    parse_mode="Markdown"
                )
            except Exception:
                pass
        logger.info("Admin rejected report #%d, reason: %s", report_id, reason)
    conn.close()


# ─── ADMIN: Staff ─────────────────────────────────────────────────────────────

async def admin_staff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    conn = get_db()
    employees = conn.execute(
        "SELECT *, (SELECT COUNT(*) FROM tasks WHERE assigned_to=employees.id AND status!='completed') as active_tasks "
        "FROM employees WHERE is_active=1 ORDER BY name"
    ).fetchall()
    conn.close()

    text = "👥 *Список сотрудников*\n\n"
    keyboard = []

    for emp in employees:
        tg = "✅" if emp["telegram_id"] else "⚠️"
        role_badge = " 👑" if emp["is_bot_admin"] else ""
        tasks_info = f" | {emp['active_tasks']} задач" if emp["active_tasks"] else ""
        text += f"{tg} *{emp['name']}*{role_badge}{tasks_info}\n"
        role_btn = "👤 Снять admin" if emp["is_bot_admin"] else "👑 Сделать admin"
        keyboard.append([InlineKeyboardButton(f"{role_btn}: {emp['name'].split()[0]}", callback_data=f"adm_toggle_{emp['id']}")])

    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="adm_menu")])
    await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def admin_toggle_role(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    emp_id = int(query.data.split("_")[2])
    conn = get_db()
    emp = conn.execute("SELECT * FROM employees WHERE id=?", (emp_id,)).fetchone()
    if emp:
        new_val = 0 if emp["is_bot_admin"] else 1
        conn.execute("UPDATE employees SET is_bot_admin=? WHERE id=?", (new_val, emp_id))
        conn.commit()
        action = "назначен администратором 👑" if new_val else "снят с роли администратора"
        logger.info("Role changed: %s → is_bot_admin=%d", emp["name"], new_val)

        # Notify the employee if they have Telegram
        if emp["telegram_id"]:
            try:
                msg = (f"👑 *Ваша роль изменена!*\n\nВы назначены *администратором* бота BeachManager."
                       if new_val
                       else "👤 Ваша роль изменена: вы сотрудник.")
                await query.get_bot().send_message(chat_id=emp["telegram_id"], text=msg, parse_mode="Markdown")
            except Exception:
                pass
    conn.close()
    await admin_staff(update, context)


# ─── ADMIN: Stats ─────────────────────────────────────────────────────────────

async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    today = date.today().isoformat()
    conn = get_db()
    income = conn.execute("SELECT COALESCE(SUM(amount),0) FROM income WHERE date=?", (today,)).fetchone()[0]
    expenses = conn.execute("SELECT COALESCE(SUM(amount),0) FROM expenses WHERE date=?", (today,)).fetchone()[0]
    profit = income - expenses
    tasks_done = conn.execute("SELECT COUNT(*) FROM tasks WHERE status='completed' AND date(completed_at)=?", (today,)).fetchone()[0]
    tasks_review = conn.execute("SELECT COUNT(*) FROM tasks WHERE status='pending_review'").fetchone()[0]
    tasks_progress = conn.execute("SELECT COUNT(*) FROM tasks WHERE status='in_progress'").fetchone()[0]
    tasks_pending = conn.execute("SELECT COUNT(*) FROM tasks WHERE status='pending'").fetchone()[0]
    staff_total = conn.execute("SELECT COUNT(*) FROM employees WHERE is_active=1").fetchone()[0]
    staff_linked = conn.execute("SELECT COUNT(*) FROM employees WHERE is_active=1 AND telegram_id IS NOT NULL").fetchone()[0]
    pending_reports = conn.execute("SELECT COUNT(*) FROM task_reports WHERE status='pending'").fetchone()[0]

    recent = conn.execute("""
        SELECT t.title, e.name FROM tasks t JOIN employees e ON t.assigned_to=e.id
        WHERE t.status='completed' AND date(t.completed_at)=?
        ORDER BY t.completed_at DESC LIMIT 5
    """, (today,)).fetchall()
    conn.close()

    text = (
        f"📊 *Статистика за сегодня*\n_{date.today().strftime('%d.%m.%Y')}_\n\n"
        f"💰 *Финансы:*\n"
        f"  💵 Доход: {income:,.0f} ₽\n"
        f"  💸 Расходы: {expenses:,.0f} ₽\n"
        f"  {'📈' if profit >= 0 else '📉'} Прибыль: {profit:,.0f} ₽\n\n"
        f"📋 *Задачи:*\n"
        f"  ✅ Выполнено сегодня: {tasks_done}\n"
        f"  🔍 На проверке: {tasks_review}\n"
        f"  🔄 В работе: {tasks_progress}\n"
        f"  ⏳ Ожидают: {tasks_pending}\n\n"
        f"👥 *Сотрудники:* {staff_total} (Telegram: {staff_linked})\n"
    )
    if pending_reports:
        text += f"\n⚠️ Отчётов на проверке: {pending_reports}"
    if recent:
        text += "\n\n✅ *Выполнено сегодня:*\n"
        for t in recent:
            text += f"  • {t['title']} — {t['name']}\n"

    keyboard = [[InlineKeyboardButton("⬅️ Назад", callback_data="adm_menu")]]
    await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


# ─── ADMIN: Settings ──────────────────────────────────────────────────────────

async def admin_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    owner_status = "✅ Установлен" if OWNER_TELEGRAM_ID else "⚠️ Не установлен"
    is_owner = OWNER_TELEGRAM_ID and user_id == OWNER_TELEGRAM_ID

    text = (
        f"⚙️ *Настройки бота*\n\n"
        f"🆔 *Ваш Telegram ID:* `{user_id}`\n\n"
        f"👑 *Владелец бота (OWNER\\_TELEGRAM\\_ID):* {owner_status}\n"
        f"{'✅ Вы являетесь владельцем' if is_owner else ''}\n\n"
        f"*Как задать владельца:*\n"
        f"1. Скопируйте ваш ID: `{user_id}`\n"
        f"2. Добавьте в Replit Secrets:\n"
        f"   Ключ: `OWNER_TELEGRAM_ID`\n"
        f"   Значение: `{user_id}`\n"
        f"3. Перезапустите бот-воркфлоу\n\n"
        f"_После этого вы всегда будете автоматически получать роль admin_"
    )

    keyboard = [[InlineKeyboardButton("⬅️ Назад", callback_data="adm_menu")]]
    await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


# ─── ADMIN: System Status ─────────────────────────────────────────────────────

_SYS_BASE      = os.path.dirname(os.path.dirname(__file__))
_SYS_LOG_FILE  = os.path.join(_SYS_BASE, "bot.log")
_SYS_RUNS_FILE = os.path.join(_SYS_BASE, "bot_run_count.txt")
_SYS_CRASH_FILE= os.path.join(_SYS_BASE, "bot_crash_reason.txt")
_SYS_TS_RE     = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


def _sys_tail_log(n: int = 2000) -> list:
    try:
        with open(_SYS_LOG_FILE, encoding="utf-8", errors="replace") as f:
            return f.readlines()[-n:]
    except Exception:
        return []


def _sys_parse_ts(line: str):
    m = _SYS_TS_RE.match(line)
    if m:
        try:
            return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
        except Exception:
            pass
    return None


async def admin_system_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show live system status to admin."""
    query = update.callback_query
    await query.answer()
    emp = get_employee(query.from_user.id)
    if not emp or not is_admin(emp):
        return

    lines = _sys_tail_log(2000)
    today_str = today_kyiv().strftime("%Y-%m-%d")

    # ── Run count ──────────────────────────────────────────────────────────────
    run_count = 1
    try:
        run_count = int(open(_SYS_RUNS_FILE).read().strip())
    except Exception:
        pass

    # ── Crash reason ──────────────────────────────────────────────────────────
    crash_reason = None
    try:
        with open(_SYS_CRASH_FILE, encoding="utf-8") as f:
            crash_reason = f.read().strip() or None
    except Exception:
        pass

    # ── Last startup & uptime ──────────────────────────────────────────────────
    last_start_dt = None
    for line in reversed(lines):
        if "Bot starting (run #" in line:
            last_start_dt = _sys_parse_ts(line)
            break

    uptime_str = "—"
    if last_start_dt:
        sec = int((datetime.now() - last_start_dt).total_seconds())
        h, rem = divmod(sec, 3600)
        m2, s2  = divmod(rem, 60)
        uptime_str = (f"{h}ч {m2}м" if h else f"{m2}м {s2}с" if m2 else f"{s2}с")

    # ── Bot alive ─────────────────────────────────────────────────────────────
    last_log_dt = None
    for line in reversed(lines):
        dt = _sys_parse_ts(line)
        if dt:
            last_log_dt = dt
            break

    bot_alive = False
    bot_seen  = "никогда"
    if last_log_dt:
        age = (datetime.now() - last_log_dt).total_seconds()
        bot_alive = age < 180
        bot_seen  = (f"{int(age)}с назад" if age < 60
                     else f"{int(age // 60)}м назад" if age < 3600
                     else last_log_dt.strftime("%H:%M"))

    # ── Scheduler ─────────────────────────────────────────────────────────────
    scheduler_ok = False
    for line in reversed(lines):
        if "heartbeat_60s" in line and "executed successfully" in line:
            dt = _sys_parse_ts(line)
            if dt and (datetime.now() - dt).total_seconds() < 300:
                scheduler_ok = True
            break

    # ── DB ────────────────────────────────────────────────────────────────────
    db_ok = False
    try:
        conn = get_db()
        conn.execute("SELECT 1").fetchone()
        conn.close()
        db_ok = True
    except Exception:
        pass

    # ── Stats today ───────────────────────────────────────────────────────────
    msgs_today = sum(
        1 for l in lines
        if today_str in l and "sendMessage" in l and "200 OK" in l
    )
    reports_today = tasks_today = 0
    try:
        conn = get_db()
        reports_today = conn.execute(
            "SELECT COUNT(*) FROM task_reports WHERE date(submitted_at)=?", (today_str,)
        ).fetchone()[0]
        tasks_today = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE date(created_at)=?", (today_str,)
        ).fetchone()[0]
        conn.close()
    except Exception:
        pass

    # ── Last error ────────────────────────────────────────────────────────────
    last_err_text = last_err_time = last_err_type = None
    for line in reversed(lines):
        if " - ERROR - " in line or " - CRITICAL - " in line:
            m = re.match(
                r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ - .+ - (ERROR|CRITICAL) - (.+)",
                line
            )
            if m:
                last_err_time = m.group(1)
                last_err_type = m.group(2)
                last_err_text = m.group(3)[:200]
            break

    # ── Overall status ────────────────────────────────────────────────────────
    recent = lines[-300:]
    recent_crashes = sum(1 for l in recent if "💥 Bot crashed" in l or "FATAL ERROR in run_polling" in l)
    recent_errors  = sum(1 for l in recent if " - ERROR - " in l or " - CRITICAL - " in l)

    if not db_ok or recent_crashes >= 3:
        status_icon, status_text = "🔴", "Требуется вмешательство"
    elif recent_crashes >= 2 or recent_errors >= 5 or not bot_alive or (not scheduler_ok and bot_alive):
        status_icon, status_text = "🟡", "Есть предупреждения"
    else:
        status_icon, status_text = "🟢", "Стабильно"

    # ── Build message ─────────────────────────────────────────────────────────
    sep = "━━━━━━━━━━━━━━━━━━━"
    parts = [
        f"⚙️ *Состояние системы*\n",
        f"{status_icon} *{status_text}*",
        sep,
        f"🤖 Бот: {'🟢 Работает' if bot_alive else '🔴 Не работает'}  _{bot_seen}_",
        f"💾 БД: {'🟢 Подключена' if db_ok else '🔴 Нет соединения'}",
        f"🔔 Планировщик: {'🟢 Активен' if scheduler_ok else '🔴 Нет данных'}",
        f"🌐 Admin Panel: 🟢 Работает\n",
        sep,
        f"📊 *Статистика*",
        f"🔄 Запусков всего: *{run_count}*",
        f"⏳ Аптайм: *{uptime_str}*",
        f"📨 Сообщений сегодня: *{msgs_today}*",
        f"📸 Отчётов сегодня: *{reports_today}*",
        f"📋 Заданий сегодня: *{tasks_today}*",
    ]

    if last_err_text:
        parts += [
            f"\n{sep}",
            f"⚠️ *Последняя ошибка*",
            f"🕐 {last_err_time}",
            f"🏷 {last_err_type or 'ERROR'}",
            f"`{last_err_text}`",
        ]
        if crash_reason:
            short_crash = crash_reason.split("\n")[0][:130]
            parts.append(f"🔁 _Причина перезапуска:_\n`{short_crash}`")
    elif crash_reason:
        short_crash = crash_reason.split("\n")[0][:150]
        parts += [f"\n{sep}", f"🔁 *Последний перезапуск:*\n`{short_crash}`"]
    else:
        parts.append(f"\n✅ _Ошибок не зафиксировано_")

    text = "\n".join(parts)
    keyboard = [[InlineKeyboardButton("🔄 Обновить", callback_data="adm_sys_status"),
                 InlineKeyboardButton("⬅️ Назад", callback_data="adm_menu")]]
    await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


# ─── WORKER: My Tasks ────────────────────────────────────────────────────────

async def my_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    emp = get_employee(query.from_user.id)
    if not emp:
        return

    conn = get_db()
    tasks = conn.execute(
        "SELECT * FROM tasks WHERE assigned_to=? AND status!='completed' ORDER BY priority DESC, due_date ASC",
        (emp["id"],)
    ).fetchall()
    conn.close()

    if not tasks:
        keyboard = [[InlineKeyboardButton("⬅️ Назад", callback_data="worker_menu")]]
        await safe_edit(query, "🎉 Нет активных задач!", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    status_emoji = {"pending": "⏳", "in_progress": "🔄", "pending_review": "🔍"}
    status_ru = {"pending": "Ожидает", "in_progress": "В работе", "pending_review": "На проверке"}

    text = f"📋 *Мои задания* ({len(tasks)})\n\n"
    keyboard = []
    for t in tasks:
        p = PRIORITY_EMOJI.get(t["priority"], "⚪")
        s = status_emoji.get(t["status"], "❓")
        text += f"{p}{s} *{t['title']}*"
        if t["due_date"]:
            text += f" | 📅 {t['due_date']}"
        text += "\n"
        title_label = t["title"][:32] + ("…" if len(t["title"]) > 32 else "")
        keyboard.append([InlineKeyboardButton(f"{p} {title_label}", callback_data=f"task_detail_{t['id']}")])

    text += "\n_Нажмите на задание — подробности и фото._"
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="worker_menu")])
    await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def worker_task_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show full task detail with admin photos to the worker."""
    query = update.callback_query
    await query.answer()

    task_id = int(query.data.split("_")[-1])
    emp = get_employee(query.from_user.id)
    if not emp:
        return

    conn = get_db()
    task = conn.execute(
        "SELECT * FROM tasks WHERE id=? AND assigned_to=?", (task_id, emp["id"])
    ).fetchone()
    conn.close()

    if not task:
        await safe_edit(query, "Задание не найдено.")
        return

    try:
        file_ids = json.loads(task["photo_file_ids"] or "[]")
    except Exception:
        file_ids = []

    p = PRIORITY_EMOJI.get(task["priority"], "⚪")
    priority_label = PRIORITY_RU.get(task["priority"], task["priority"])
    status_map = {
        "pending": "⏳ Ожидает",
        "in_progress": "🔄 В работе",
        "pending_review": "🔍 На проверке",
        "completed": "✅ Выполнено",
    }

    detail = f"📋 *{task['title']}*\n\n"
    if task["description"]:
        detail += f"📝 _{task['description']}_\n\n"
    detail += f"🎯 Приоритет: {p} {priority_label}\n"
    if task["due_date"]:
        detail += f"📅 Срок: {task['due_date']}\n"
    detail += f"📊 {status_map.get(task['status'], task['status'])}"

    keyboard_rows = []
    if task["status"] not in ("completed", "pending_review"):
        keyboard_rows.append([InlineKeyboardButton("📸 Сдать отчёт", callback_data="submit_report")])
    keyboard_rows.append([InlineKeyboardButton("⬅️ К заданиям", callback_data="my_tasks")])

    bot = query.get_bot()
    chat_id = query.message.chat_id

    if file_ids:
        try:
            if len(file_ids) == 1:
                await bot.send_photo(
                    chat_id=chat_id, photo=file_ids[0],
                    caption=detail, parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(keyboard_rows)
                )
            else:
                await bot.send_media_group(
                    chat_id=chat_id,
                    media=[InputMediaPhoto(fid) for fid in file_ids]
                )
                await bot.send_message(
                    chat_id=chat_id, text=detail, parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(keyboard_rows)
                )
            try:
                await query.delete_message()
            except Exception:
                pass
        except Exception as exc:
            logger.warning("Task detail photo send failed: %s", exc)
            await safe_edit(query, 
                detail, reply_markup=InlineKeyboardMarkup(keyboard_rows), parse_mode="Markdown"
            )
    else:
        await safe_edit(query, 
            detail, reply_markup=InlineKeyboardMarkup(keyboard_rows), parse_mode="Markdown"
        )


# ─── WORKER: Report submission ────────────────────────────────────────────────

async def submit_report_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    emp = get_employee(query.from_user.id)
    if not emp:
        return

    conn = get_db()
    tasks = conn.execute(
        "SELECT * FROM tasks WHERE assigned_to=? AND status NOT IN ('completed','pending_review') ORDER BY priority DESC",
        (emp["id"],)
    ).fetchall()
    conn.close()

    if not tasks:
        back = "adm_menu" if is_admin(emp) else "worker_menu"
        keyboard = [[InlineKeyboardButton("⬅️ Назад", callback_data=back)]]
        await safe_edit(query, 
            "Нет активных заданий для отчёта.\n_(Задания «На проверке» уже ожидают подтверждения)_",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return

    keyboard = []
    for t in tasks:
        p = PRIORITY_EMOJI.get(t["priority"], "⚪")
        keyboard.append([InlineKeyboardButton(f"{p} {t['title']}", callback_data=f"rpt_{t['id']}")])
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="worker_menu")])

    await safe_edit(query, 
        "📝 *Сдать отчёт*\n\nВыберите задание:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )


async def report_task_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    task_id = int(query.data.split("_")[1])
    emp = get_employee(query.from_user.id)
    if not emp:
        return

    conn = get_db()
    task = conn.execute("SELECT * FROM tasks WHERE id=? AND assigned_to=?", (task_id, emp["id"])).fetchone()
    conn.close()
    if not task:
        await safe_edit(query, "Задание не найдено.")
        return

    context.user_data["report"] = {
        "state": "photos", "task_id": task_id,
        "task_title": task["title"], "comment": None, "photos": [], "photo_file_ids": [],
    }

    keyboard = [[InlineKeyboardButton("❌ Отмена", callback_data="report_cancel")]]
    await safe_edit(query, 
        f"📸 *{task['title']}*\n\n"
        f"Шаг 1/3 — Прикрепите фотографии выполненной работы.\n\n"
        f"⚠️ *Фотографии обязательны.* Пришлите хотя бы одну.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )


async def report_submit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    report = context.user_data.get("report")
    if not report:
        await safe_edit(query, "Сессия истекла. Начните заново через /start.")
        return

    emp = get_employee(query.from_user.id)
    if not emp:
        return

    # Photos are required
    after_ids = report.get("photo_file_ids", [])
    if not after_ids:
        await query.answer(
            "⚠️ Для этого задания необходимо прикрепить хотя бы одну фотографию.",
            show_alert=True
        )
        return

    conn = get_db()
    cur = conn.execute(
        "INSERT INTO task_reports (task_id, employee_id, comment, photos, photo_file_ids) VALUES (?, ?, ?, ?, ?)",
        (report["task_id"], emp["id"], report.get("comment") or "",
         json.dumps(report["photos"]),
         json.dumps(after_ids))
    )
    report_id = cur.lastrowid
    conn.execute("UPDATE tasks SET status='pending_review' WHERE id=?", (report["task_id"],))
    admins = conn.execute(
        "SELECT telegram_id FROM employees WHERE is_bot_admin=1 AND telegram_id IS NOT NULL AND is_active=1"
    ).fetchall()
    task_row = conn.execute(
        "SELECT photo_file_ids FROM tasks WHERE id=?", (report["task_id"],)
    ).fetchone()
    conn.commit()
    conn.close()

    try:
        before_ids = json.loads((task_row["photo_file_ids"] if task_row else None) or "[]")
    except Exception:
        before_ids = []

    now = now_kyiv()
    comment_display = report.get("comment") or "отсутствует"
    caption = (
        f"📸 *Новый отчёт на проверку*\n\n"
        f"👷 Сотрудник: *{emp['name']}*\n"
        f"📅 {now.strftime('%d.%m.%Y')} 🕒 *{now.strftime('%H:%M')}*\n"
        f"📋 Задача: _{report['task_title']}_\n"
        f"📝 Комментарий: {comment_display}"
    )
    approve_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Подтвердить", callback_data=f"adm_rpt_ok_{report_id}"),
        InlineKeyboardButton("❌ Отклонить",   callback_data=f"adm_rpt_no_{report_id}"),
    ]])
    bot = query.get_bot()
    for admin in admins:
        tg_id = admin["telegram_id"]
        try:
            # Send BEFORE photos (task photos set by admin)
            if before_ids:
                if len(before_ids) == 1:
                    await bot.send_photo(chat_id=tg_id, photo=before_ids[0],
                                         caption="📸 *ДО выполнения* (фото задания)",
                                         parse_mode="Markdown")
                else:
                    await bot.send_media_group(
                        chat_id=tg_id,
                        media=[InputMediaPhoto(fid) for fid in before_ids]
                    )
                    await bot.send_message(chat_id=tg_id,
                                           text="📸 *ДО выполнения* (фото задания)",
                                           parse_mode="Markdown")
            # Send AFTER photos (employee report) + caption + approve buttons
            if len(after_ids) == 1:
                await bot.send_photo(chat_id=tg_id, photo=after_ids[0],
                                     caption=caption, parse_mode="Markdown",
                                     reply_markup=approve_kb)
            else:
                await bot.send_media_group(
                    chat_id=tg_id,
                    media=[InputMediaPhoto(fid) for fid in after_ids]
                )
                await bot.send_message(chat_id=tg_id, text=caption,
                                       parse_mode="Markdown", reply_markup=approve_kb)
        except Exception as exc:
            logger.warning("Report notify admin failed: %s", exc)

    logger.info("Report submitted: task_id=%d emp=%s report_id=%d photos=%d",
                report["task_id"], emp["name"], report_id, len(after_ids))
    context.user_data.pop("report", None)

    keyboard = [[InlineKeyboardButton("⬅️ В главное меню", callback_data="worker_menu")]]
    await safe_edit(query, 
        f"✅ *Отчёт отправлен!*\n\n"
        f"Задача «{report['task_title']}» ожидает проверки администратора.\n"
        f"Вы получите уведомление о результате.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )


async def report_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.pop("report", None)
    emp = get_employee(query.from_user.id)
    if is_admin(emp):
        await show_admin_menu(update, context, emp)
    else:
        await show_worker_menu(update, context, emp)


async def _show_report_confirm(query, report: dict):
    """Show the final confirm screen before submitting the report."""
    count = len(report.get("photo_file_ids", []))
    comment = report.get("comment")
    comment_str = f"_{comment}_" if comment else "_отсутствует_"
    text = (
        f"📋 *Проверьте отчёт перед отправкой*\n\n"
        f"📸 Фотографий: *{count}*\n"
        f"📝 Комментарий: {comment_str}\n\n"
        f"Нажмите «Отправить», чтобы передать отчёт администратору."
    )
    keyboard = [
        [InlineKeyboardButton("📤 Отправить отчёт", callback_data="report_submit")],
        [InlineKeyboardButton("❌ Отмена", callback_data="report_cancel")],
    ]
    await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def report_photos_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Employee pressed 'Continue' after uploading photos — move to comment step."""
    query = update.callback_query
    await query.answer()
    report = context.user_data.get("report")
    if not report or not report.get("photo_file_ids"):
        await query.answer(
            "⚠️ Для этого задания необходимо прикрепить хотя бы одну фотографию.",
            show_alert=True
        )
        return

    report["state"] = "comment"
    context.user_data["report"] = report
    count = len(report["photo_file_ids"])

    keyboard = [
        [InlineKeyboardButton("⏭ Пропустить", callback_data="rpt_skip_comment")],
        [InlineKeyboardButton("❌ Отмена", callback_data="report_cancel")],
    ]
    await safe_edit(query, 
        f"✅ *{count} фото добавлено!*\n\n"
        f"Шаг 2/3 — Добавьте комментарий _(необязательно)_.\n"
        f"Напишите что сделано, или нажмите «Пропустить».",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )


async def report_skip_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Employee chose to skip the comment — go straight to confirm screen."""
    query = update.callback_query
    await query.answer()
    report = context.user_data.get("report")
    if not report:
        await safe_edit(query, "Сессия истекла. Начните заново через /start.")
        return

    report["comment"] = None
    report["state"] = "ready"
    context.user_data["report"] = report
    await _show_report_confirm(query, report)


# ─── WEATHER (bot) ────────────────────────────────────────────────────────────

async def bot_weather(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show full weather card with danger level."""
    query = update.callback_query
    await query.answer()

    emp = get_employee(query.from_user.id)
    back = "adm_menu" if is_admin(emp) else "worker_menu"

    try:
        w = await asyncio.to_thread(get_weather)
    except Exception as exc:
        logger.warning("bot_weather fetch error: %s", exc)
        w = None

    if not w:
        keyboard = [[InlineKeyboardButton("⬅️ Назад", callback_data=back)]]
        await safe_edit(query, 
            "⚠️ Не удалось получить данные о погоде.\nПопробуйте позже.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    text = format_weather_full(w)

    # Append today's alerts if any
    alerts = w.get("alerts", [])
    if alerts:
        text += "\n\n⚠️ *Предупреждения на сегодня:*"
        for a in alerts:
            text += f"\n{a['emoji']} {a['text']}"

    keyboard = [
        [InlineKeyboardButton("🔄 Обновить", callback_data="bot_weather")],
        [InlineKeyboardButton("⬅️ Назад", callback_data=back)],
    ]
    await safe_edit(query, 
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )


# ─── WORKER: Stats ────────────────────────────────────────────────────────────

async def my_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    emp = get_employee(query.from_user.id)
    if not emp:
        return

    today = today_kyiv()
    month_start = today.replace(day=1).isoformat()

    conn = get_db()
    total = conn.execute("SELECT COUNT(*) FROM tasks WHERE assigned_to=?", (emp["id"],)).fetchone()[0]
    completed = conn.execute("SELECT COUNT(*) FROM tasks WHERE assigned_to=? AND status='completed'", (emp["id"],)).fetchone()[0]
    pending = conn.execute("SELECT COUNT(*) FROM tasks WHERE assigned_to=? AND status='pending'", (emp["id"],)).fetchone()[0]
    in_progress = conn.execute("SELECT COUNT(*) FROM tasks WHERE assigned_to=? AND status='in_progress'", (emp["id"],)).fetchone()[0]
    on_review = conn.execute("SELECT COUNT(*) FROM tasks WHERE assigned_to=? AND status='pending_review'", (emp["id"],)).fetchone()[0]
    total_reports = conn.execute("SELECT COUNT(*) FROM task_reports WHERE employee_id=?", (emp["id"],)).fetchone()[0]
    approved_reports = conn.execute("SELECT COUNT(*) FROM task_reports WHERE employee_id=? AND status='approved'", (emp["id"],)).fetchone()[0]

    # Attendance this month
    att_records = conn.execute(
        "SELECT status FROM attendance WHERE employee_id=? AND date>=?",
        (emp["id"], month_start)
    ).fetchall()
    shifts_this_month = conn.execute(
        "SELECT COUNT(*) FROM schedules WHERE employee_id=? AND date>=? AND date<=?",
        (emp["id"], month_start, today.isoformat())
    ).fetchone()[0]

    # Streak: consecutive shifts with check-in (from most recent backwards)
    past_shifts = conn.execute(
        "SELECT date FROM schedules WHERE employee_id=? AND date<=? ORDER BY date DESC LIMIT 30",
        (emp["id"], today.isoformat())
    ).fetchall()
    att_dates = set(r["date"] for r in conn.execute(
        "SELECT date FROM attendance WHERE employee_id=?", (emp["id"],)
    ).fetchall())
    conn.close()

    streak = 0
    for s in past_shifts:
        if s["date"] in att_dates:
            streak += 1
        else:
            break

    att_on_time = sum(1 for r in att_records if r["status"] == "on_time")
    att_minor = sum(1 for r in att_records if r["status"] == "minor_late")
    att_major = sum(1 for r in att_records if r["status"] == "major_late")
    total_att = len(att_records)

    # Rating (0–100)
    task_score = (completed / total * 40) if total > 0 else 0
    att_score = (att_on_time / shifts_this_month * 40) if shifts_this_month > 0 else 0
    rep_score = (approved_reports / total_reports * 20) if total_reports > 0 else 0
    rating = int(task_score + att_score + rep_score)
    rating_emoji = "🏆" if rating >= 80 else "⭐" if rating >= 60 else "📈" if rating >= 40 else "📉"

    pct = round((completed / total * 100) if total > 0 else 0)
    bar = "🟩" * int(pct / 10) + "⬜" * (10 - int(pct / 10))
    role_ru = "👑 Администратор" if is_admin(emp) else "👤 Сотрудник"
    back = "adm_menu" if is_admin(emp) else "worker_menu"

    text = (
        f"📊 *Статистика*\n\n"
        f"👤 *{emp['name']}* — {role_ru}\n\n"
        f"📋 *Задачи:*\n"
        f"  ✅ Выполнено: {completed} | 🔄 В работе: {in_progress}\n"
        f"  🔍 На проверке: {on_review} | ⏳ Ожидают: {pending}\n"
        f"  📝 Отчётов: {total_reports} | Принято: {approved_reports}\n\n"
        f"📅 *Смены этого месяца:* {shifts_this_month}\n"
    )
    if total_att > 0:
        text += (
            f"✅ *Посещаемость:*\n"
            f"  🟢 Вовремя: {att_on_time} | 🟡 Небольшое: {att_minor} | 🔴 Серьёзное: {att_major}\n"
        )
    if streak > 0:
        text += f"🔥 *Серия без пропусков:* {streak} смен подряд\n"
    text += (
        f"\n{rating_emoji} *Рейтинг: {rating}/100*\n"
        f"Эффективность задач: {pct}%\n{bar}"
    )

    keyboard = [[InlineKeyboardButton("⬅️ Назад", callback_data=back)]]
    await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


# ─── WORKER: Schedule ────────────────────────────────────────────────────────

async def my_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    emp = get_employee(query.from_user.id)
    if not emp:
        return

    today = date.today()
    monday = today - timedelta(days=today.weekday())
    week_dates = [monday + timedelta(days=i) for i in range(7)]

    conn = get_db()
    shifts = conn.execute(
        "SELECT * FROM schedules WHERE employee_id=? AND date>=? AND date<=?",
        (emp["id"], week_dates[0].isoformat(), week_dates[-1].isoformat())
    ).fetchall()
    conn.close()

    shift_map = {s["date"]: s for s in shifts}
    tomorrow = today + timedelta(days=1)

    text = f"📅 *Мой график*\n_{week_dates[0].strftime('%d.%m')} – {week_dates[-1].strftime('%d.%m.%Y')}_\n\n"
    for i, d in enumerate(week_dates):
        prefix = "➡️ " if d == today else ""
        shift = shift_map.get(d.isoformat())
        if shift:
            note = f" — {shift['note']}" if shift["note"] else ""
            text += f"{prefix}*{DAYS_RU[i]}* ({d.strftime('%d.%m')}): 🕐 {shift['shift_start']}–{shift['shift_end']}{note}\n"
        else:
            text += f"{prefix}*{DAYS_RU[i]}* ({d.strftime('%d.%m')}): 🏖️ Выходной\n"

    # Show weather for tomorrow if it falls within this week
    if week_dates[0] <= tomorrow <= week_dates[-1]:
        try:
            from weather import get_weather, format_weather_short
            w = await asyncio.to_thread(get_weather)
            if w:
                text += f"\n{format_weather_short(w['tomorrow'])} _(завтра)_\n"
        except Exception:
            pass

    back = "adm_menu" if is_admin(emp) else "worker_menu"
    keyboard = [[InlineKeyboardButton("⬅️ Назад", callback_data=back)]]
    await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def today_shift(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    emp = get_employee(query.from_user.id)
    if not emp:
        return

    today = date.today()
    conn = get_db()
    shift = conn.execute("SELECT * FROM schedules WHERE employee_id=? AND date=?",
                         (emp["id"], today.isoformat())).fetchone()
    tasks = conn.execute(
        "SELECT * FROM tasks WHERE assigned_to=? AND status NOT IN ('completed') ORDER BY priority DESC",
        (emp["id"],)
    ).fetchall()
    conn.close()

    status_emoji = {"pending": "⏳", "in_progress": "🔄", "pending_review": "🔍"}
    text = f"🕐 *Смена сегодня*\n_{DAYS_RU[today.weekday()]}, {today.strftime('%d.%m.%Y')}_\n\n"
    if shift:
        note = f"\n   📝 {shift['note']}" if shift["note"] else ""
        text += f"⏰ *Время:* {shift['shift_start']} – {shift['shift_end']}{note}\n\n"
    else:
        text += "⏰ *Время:* 🏖️ Выходной\n\n"

    if tasks:
        text += f"📋 *Задания ({len(tasks)}):*\n"
        for t in tasks:
            p = PRIORITY_EMOJI.get(t["priority"], "⚪")
            s = status_emoji.get(t["status"], "⏳")
            text += f"{p}{s} *{t['title']}*\n"
            if t["description"]:
                text += f"   _{t['description']}_\n"
    else:
        text += "📋 *Задания:* нет"

    back = "adm_menu" if is_admin(emp) else "worker_menu"
    keyboard = [[InlineKeyboardButton("⬅️ Назад", callback_data=back)]]
    await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = [[InlineKeyboardButton("⬅️ Назад", callback_data="worker_menu")]]
    await safe_edit(query, 
        "💬 *Помощь*\n\n"
        "/start — Главное меню\n\n"
        "📝 *Как сдать отчёт:*\n"
        "1. Нажмите «📝 Сдать отчёт»\n"
        "2. Выберите задание\n"
        "3. Напишите комментарий\n"
        "4. Прикрепите фото (по одному)\n"
        "5. Нажмите «✅ Отправить»\n\n"
        "После проверки вы получите уведомление.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )


# ─── Message handler (text + photos during flows) ────────────────────────────

async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Routes text messages to the active flow (admin or report)."""
    adm = context.user_data.get("adm")
    report = context.user_data.get("report")

    # ── Admin flows ────────────────────────────────────────────────────────
    if adm:
        flow = adm.get("flow")
        step = adm.get("step")
        text = update.message.text.strip()

        if flow == "today_bcast" and step == "text":
            workers = _get_today_workers()
            if not workers:
                context.user_data.pop("adm", None)
                keyboard = [[InlineKeyboardButton("⬅️ Назад", callback_data="adm_today")]]
                await update.message.reply_text(
                    "⚠️ Нет сотрудников на смене сегодня.",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
                return
            delivered, failed, total, recipients = await _do_broadcast(
                update.get_bot(), workers, text, label="today_bcast"
            )
            context.user_data.pop("adm", None)
            keyboard = [
                [InlineKeyboardButton("🏖 Назад к сегодня", callback_data="adm_today")],
                [InlineKeyboardButton("⬅️ Меню", callback_data="adm_menu")],
            ]
            result = f"✅ Получили: {delivered}"
            if failed:
                result += f"\n❌ Ошибка: {failed}"
            if recipients:
                result += "\n\n👥 *Получатели:*\n" + "\n".join(f"• {n}" for n in recipients)
            await update.message.reply_text(
                f"📨 *Рассылка на смене отправлена!*\n\n_{text}_\n\n{result}",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )

        elif flow == "broadcast" and step == "text":
            adm["text"] = text
            adm["step"] = "confirm"
            context.user_data["adm"] = adm
            keyboard = [
                [InlineKeyboardButton("📨 Отправить работающим сегодня", callback_data="adm_bcast_confirm")],
                [InlineKeyboardButton("❌ Отмена", callback_data="adm_menu")],
            ]
            await update.message.reply_text(
                f"📨 *Предпросмотр рассылки:*\n\n{text}\n\n_Отправить сотрудникам, работающим сегодня?_",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )

        elif flow == "create_task" and step == "title":
            adm["title"] = text
            adm["step"] = "description"
            context.user_data["adm"] = adm
            keyboard = [
                [InlineKeyboardButton("➡️ Без описания", callback_data="adm_task_skip_desc")],
                [InlineKeyboardButton("❌ Отмена", callback_data="adm_tasks")],
            ]
            await update.message.reply_text(
                f"📋 *{adm['emp_name']}*\n"
                f"Название: _{text}_\n\n"
                f"Шаг 2/4 — Введите *описание* задания или пропустите:",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )

        elif flow == "create_task" and step == "description":
            adm["description"] = text
            adm["step"] = "photos"
            context.user_data["adm"] = adm
            keyboard = [
                [InlineKeyboardButton("➡️ Без фото", callback_data="adm_task_skip_photos")],
                [InlineKeyboardButton("❌ Отмена", callback_data="adm_tasks")],
            ]
            await update.message.reply_text(
                f"📋 *{adm['emp_name']}* — _{adm['title']}_\n\n"
                f"Шаг 3/4 — Прикрепите фотографии (по одному).\n"
                f"_Отправьте фото и нажмите «Продолжить» когда готово._",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )

        elif flow == "create_task" and step == "due_date":
            adm["due_date"] = text.strip()
            adm["step"] = "priority"
            context.user_data["adm"] = adm
            n_photos = len(adm.get("photo_file_ids", []))
            photo_info = f"\n📸 {n_photos} фото" if n_photos else ""
            keyboard = [
                [InlineKeyboardButton("🔴 Высокий", callback_data="adm_task_prio_high"),
                 InlineKeyboardButton("🟡 Средний", callback_data="adm_task_prio_normal"),
                 InlineKeyboardButton("🟢 Низкий", callback_data="adm_task_prio_low")],
                [InlineKeyboardButton("❌ Отмена", callback_data="adm_tasks")],
            ]
            await update.message.reply_text(
                f"📋 *{adm.get('emp_name','')}* — _{adm.get('title','')}_\n"
                f"📅 Срок: {text.strip()}{photo_info}\n\n"
                f"Шаг 4/4 — Выберите приоритет:",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )

        elif flow == "reject_report" and step == "reason":
            report_id = adm.get("report_id")
            reason = text
            context.user_data.pop("adm", None)
            await _do_reject_report(update.message, report_id, reason)

            keyboard = [[InlineKeyboardButton("📸 К отчётам", callback_data="adm_rpts"),
                         InlineKeyboardButton("🏠 Меню", callback_data="adm_menu")]]
            await update.message.reply_text(
                f"✅ Отчёт #{report_id} отклонён. Сотрудник уведомлён.",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        return

    # ── Handover comment flow ───────────────────────────────────────────────
    handover = context.user_data.get("handover")
    if handover and handover.get("state") == "comment" and update.message.text:
        comment = update.message.text.strip()
        handover["comment"] = "" if comment == "-" else comment
        handover["state"] = "photos"
        context.user_data["handover"] = handover

        keyboard = [
            [InlineKeyboardButton("✅ Отправить без фото", callback_data="sh_submit")],
            [InlineKeyboardButton("❌ Отмена", callback_data="sh_cancel")],
        ]
        await update.message.reply_text(
            "💬 Комментарий сохранён!\n\n"
            "Прикрепите фотографии (по одному).\n"
            "Когда готово — нажмите *«✅ Отправить»*.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return

    # ── Deposit amount flow ──────────────────────────────────────────────────
    deposit_flow = context.user_data.get("deposit")
    if deposit_flow and deposit_flow.get("state") == "amount" and update.message.text:
        raw = update.message.text.strip().replace(",", ".").replace(" ", "")
        try:
            amount = float(raw)
            if amount <= 0:
                raise ValueError("non-positive")
        except ValueError:
            keyboard = [[InlineKeyboardButton("❌ Отмена", callback_data="deposit_menu")]]
            await update.message.reply_text(
                "⚠️ Введите число больше нуля (например: *1000*):",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
            return

        emp = get_employee(update.message.from_user.id)
        context.user_data.pop("deposit", None)

        conn = get_db()
        conn.execute(
            "INSERT INTO deposit_requests (employee_id, amount, requested_at) VALUES (?, ?, ?)",
            (emp["id"], amount, now_kyiv().isoformat())
        )
        conn.commit()
        req_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        admins = conn.execute(
            "SELECT telegram_id FROM employees WHERE is_bot_admin=1 AND telegram_id IS NOT NULL AND is_active=1"
        ).fetchall()
        conn.close()

        for admin in admins:
            try:
                kb = InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Подтвердить", callback_data=f"adm_dep_ok_{req_id}"),
                    InlineKeyboardButton("❌ Отклонить",   callback_data=f"adm_dep_no_{req_id}"),
                ]])
                await send_with_retry(
                    context.bot, admin["telegram_id"],
                    f"💰 *Заявка на залог*\n\n"
                    f"👤 {emp['name']}\n"
                    f"💵 {amount:,.0f} грн\n"
                    f"📅 {now_kyiv().strftime('%d.%m.%Y %H:%M')}",
                    parse_mode="Markdown",
                    reply_markup=kb
                )
            except Exception as exc:
                logger.warning("Deposit notify admin failed: %s", exc)

        keyboard = [
            [InlineKeyboardButton("💰 К залогу", callback_data="deposit_menu"),
             InlineKeyboardButton("⬅️ Меню",     callback_data="worker_menu")],
        ]
        await update.message.reply_text(
            f"✅ *Заявка отправлена!*\n\n"
            f"Сумма: *{amount:,.0f} грн*\n"
            f"_Ожидайте подтверждения администратора._",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return

    # ── Report flow (comment step) ─────────────────────────────────────────
    if report and report.get("state") == "comment" and update.message.text:
        comment = update.message.text.strip()
        report["comment"] = comment
        report["state"] = "ready"
        context.user_data["report"] = report

        count = len(report.get("photo_file_ids", []))
        comment_str = f"_{comment}_"
        text = (
            f"📋 *Проверьте отчёт перед отправкой*\n\n"
            f"📸 Фотографий: *{count}*\n"
            f"📝 Комментарий: {comment_str}\n\n"
            f"Нажмите «Отправить», чтобы передать отчёт администратору."
        )
        keyboard = [
            [InlineKeyboardButton("📤 Отправить отчёт", callback_data="report_submit")],
            [InlineKeyboardButton("❌ Отмена", callback_data="report_cancel")],
        ]
        await update.message.reply_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )


async def handle_photo_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles photos during task creation, report, or handover flows."""
    os.makedirs(UPLOAD_DIR, exist_ok=True)

    # ── Admin task creation photo flow ──────────────────────────────────────
    adm = context.user_data.get("adm", {})
    if adm.get("flow") == "create_task" and adm.get("step") == "photos":
        photo = update.message.photo[-1]
        adm.setdefault("photo_file_ids", []).append(photo.file_id)
        context.user_data["adm"] = adm
        count = len(adm["photo_file_ids"])
        keyboard = [
            [InlineKeyboardButton(f"✅ Продолжить ({count} фото)", callback_data="adm_task_next")],
            [InlineKeyboardButton("❌ Отмена", callback_data="adm_tasks")],
        ]
        await update.message.reply_text(
            f"📸 Фото #{count} добавлено! Пришлите ещё или нажмите «Продолжить».",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    # ── Shift handover photo flow ───────────────────────────────────────────
    handover = context.user_data.get("handover")
    if handover and handover.get("state") == "photos":
        photo = update.message.photo[-1]
        tg_file = await context.bot.get_file(photo.file_id)
        filename = f"handover_{int(now_kyiv().timestamp())}_{len(handover['photos'])}.jpg"
        await tg_file.download_to_drive(os.path.join(UPLOAD_DIR, filename))
        handover["photos"].append(filename)
        context.user_data["handover"] = handover
        count = len(handover["photos"])

        keyboard = [
            [InlineKeyboardButton(f"✅ Отправить ({count} фото)", callback_data="sh_submit")],
            [InlineKeyboardButton("❌ Отмена", callback_data="sh_cancel")],
        ]
        await update.message.reply_text(
            f"📸 Фото #{count} загружено! Можно добавить ещё.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    # ── Task report photo flow ──────────────────────────────────────────────
    report = context.user_data.get("report")
    if not report or report.get("state") != "photos":
        return

    photo = update.message.photo[-1]
    # Store file_id for Telegram forwarding
    report.setdefault("photo_file_ids", []).append(photo.file_id)
    # Also download to disk so admin web panel can display it
    try:
        tg_file = await context.bot.get_file(photo.file_id)
        filename = f"{report['task_id']}_{int(now_kyiv().timestamp())}_{len(report['photos'])}.jpg"
        await tg_file.download_to_drive(os.path.join(UPLOAD_DIR, filename))
        report["photos"].append(filename)
    except Exception as exc:
        logger.warning("Photo download failed (file_id saved): %s", exc)

    context.user_data["report"] = report
    count = len(report["photo_file_ids"])

    keyboard = [
        [InlineKeyboardButton(f"✅ Продолжить ({count} фото)", callback_data="rpt_photos_done")],
        [InlineKeyboardButton("❌ Отмена", callback_data="report_cancel")],
    ]
    await update.message.reply_text(
        f"📸 Фото #{count} добавлено! Можно прикрепить ещё или нажмите «Продолжить».",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def _ask_priority(message_obj):
    keyboard = [
        [InlineKeyboardButton("🔴 Высокий", callback_data="adm_task_prio_high"),
         InlineKeyboardButton("🟡 Средний", callback_data="adm_task_prio_normal"),
         InlineKeyboardButton("🟢 Низкий", callback_data="adm_task_prio_low")],
        [InlineKeyboardButton("❌ Отмена", callback_data="adm_tasks")],
    ]
    await message_obj.reply_text(
        "🎯 Выберите приоритет задания:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def admin_task_skip_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    adm = context.user_data.get("adm", {})
    adm["description"] = ""
    adm["step"] = "photos"
    context.user_data["adm"] = adm

    keyboard = [
        [InlineKeyboardButton("➡️ Без фото", callback_data="adm_task_skip_photos")],
        [InlineKeyboardButton("❌ Отмена", callback_data="adm_tasks")],
    ]
    await safe_edit(query, 
        f"📋 *{adm.get('emp_name', '')}* — _{adm.get('title', '')}_\n\n"
        f"Шаг 3/4 — Прикрепите фотографии (по одному).\n"
        f"_Отправьте фото и нажмите «Продолжить» когда готово._",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )


async def admin_task_skip_photos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    adm = context.user_data.get("adm", {})
    adm["step"] = "due_date"
    context.user_data["adm"] = adm
    await _ask_task_due_date(query, adm)


async def admin_task_next_photos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin pressed 'Continue' after adding photos."""
    query = update.callback_query
    await query.answer()
    adm = context.user_data.get("adm", {})
    adm["step"] = "due_date"
    context.user_data["adm"] = adm
    await _ask_task_due_date(query, adm)


async def admin_task_skip_due(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    adm = context.user_data.get("adm", {})
    adm["due_date"] = ""
    adm["step"] = "priority"
    context.user_data["adm"] = adm
    n_photos = len(adm.get("photo_file_ids", []))
    photo_info = f"\n📸 {n_photos} фото" if n_photos else ""
    keyboard = [
        [InlineKeyboardButton("🔴 Высокий", callback_data="adm_task_prio_high"),
         InlineKeyboardButton("🟡 Средний", callback_data="adm_task_prio_normal"),
         InlineKeyboardButton("🟢 Низкий", callback_data="adm_task_prio_low")],
        [InlineKeyboardButton("❌ Отмена", callback_data="adm_tasks")],
    ]
    await safe_edit(query, 
        f"📋 *{adm.get('emp_name', '')}* — _{adm.get('title', '')}_{photo_info}\n\n"
        f"Шаг 4/4 — Выберите приоритет:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )


async def _ask_task_due_date(query, adm: dict):
    n_photos = len(adm.get("photo_file_ids", []))
    photo_info = f"\n📸 {n_photos} фото" if n_photos else ""
    keyboard = [
        [InlineKeyboardButton("➡️ Без срока", callback_data="adm_task_skip_due")],
        [InlineKeyboardButton("❌ Отмена", callback_data="adm_tasks")],
    ]
    await safe_edit(query, 
        f"📋 *{adm.get('emp_name', '')}* — _{adm.get('title', '')}_{photo_info}\n\n"
        f"Шаг 4/4 — Введите срок выполнения (ГГГГ-ММ-ДД)\n"
        f"_или нажмите «Без срока»_",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )


# ─── CHECK-IN ────────────────────────────────────────────────────────────────

async def check_in(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    emp = get_employee(query.from_user.id)
    if not emp:
        return

    today = today_kyiv()
    today_str = today.isoformat()
    now = now_kyiv()
    back = "adm_menu" if is_admin(emp) else "worker_menu"

    conn = get_db()
    existing = conn.execute("SELECT * FROM attendance WHERE employee_id=? AND date=?",
                            (emp["id"], today_str)).fetchone()
    if existing:
        conn.close()
        status_labels = {"on_time": "✅ Вовремя", "minor_late": "🟡 Небольшое опоздание", "major_late": "🔴 Серьёзное опоздание"}
        late_info = f" (+{existing['minutes_late']} мин)" if existing["minutes_late"] else ""
        keyboard = [[InlineKeyboardButton("⬅️ Назад", callback_data=back)]]
        await safe_edit(query, 
            f"✅ *Вы уже отметились сегодня*\n\n"
            f"⏰ Время прихода: *{existing['check_in_time']}*\n"
            f"Статус: {status_labels.get(existing['status'], '')}{late_info}",
            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown"
        )
        return

    shift = conn.execute("SELECT * FROM schedules WHERE employee_id=? AND date=?",
                         (emp["id"], today_str)).fetchone()
    check_in_time = now.strftime("%H:%M")

    if shift:
        h, m = map(int, shift["shift_start"].split(":"))
        shift_dt = datetime(today.year, today.month, today.day, h, m)
        minutes_late = max(0, int((now - shift_dt).total_seconds() / 60))

        if minutes_late == 0:
            status, status_text = "on_time", "✅ Вовремя!"
        elif minutes_late <= 10:
            status, status_text = "minor_late", f"🟡 Небольшое опоздание (+{minutes_late} мин)"
        else:
            status, status_text = "major_late", f"🔴 Серьёзное опоздание (+{minutes_late} мин)"

        try:
            conn.execute(
                "INSERT OR IGNORE INTO attendance (employee_id, date, check_in_time, shift_start, minutes_late, status) VALUES (?, ?, ?, ?, ?, ?)",
                (emp["id"], today_str, check_in_time, shift["shift_start"], minutes_late, status)
            )
            conn.commit()
        except Exception:
            pass

        msg = (
            f"✅ *Отметка принята!*\n\n"
            f"👤 {emp['name']}\n"
            f"📅 {today.strftime('%d.%m.%Y')}\n"
            f"⏰ Приход: *{check_in_time}*\n"
            f"🕐 Начало смены: {shift['shift_start']}\n\n"
            f"{status_text}"
        )
    else:
        try:
            conn.execute(
                "INSERT OR IGNORE INTO attendance (employee_id, date, check_in_time, status) VALUES (?, ?, ?, ?)",
                (emp["id"], today_str, check_in_time, "on_time")
            )
            conn.commit()
        except Exception:
            pass
        msg = (
            f"ℹ️ *Отметка принята*\n\n"
            f"⏰ Время: *{check_in_time}*\n"
            f"_(У вас нет смены сегодня по графику)_"
        )

    conn.close()
    logger.info("Check-in: %s at %s", emp["name"], check_in_time)
    keyboard = [[InlineKeyboardButton("⬅️ В меню", callback_data=back)]]
    await safe_edit(query, msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


# ─── MY SALARY ────────────────────────────────────────────────────────────────

async def my_salary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    emp = get_employee(query.from_user.id)
    if not emp:
        return

    today = today_kyiv()
    month_start = today.replace(day=1).isoformat()
    period = today.strftime("%Y-%m")

    conn = get_db()
    shifts_count = conn.execute(
        "SELECT COUNT(*) FROM schedules WHERE employee_id=? AND date>=? AND date<=?",
        (emp["id"], month_start, today.isoformat())
    ).fetchone()[0]

    components = conn.execute(
        "SELECT * FROM salary_components WHERE employee_id=? AND period=? ORDER BY created_at",
        (emp["id"], period)
    ).fetchall()
    conn.close()

    salary_per_shift = emp["salary_per_shift"] or 1000
    base_salary = shifts_count * salary_per_shift
    bonuses = sum(c["amount"] for c in components if c["type"] == "bonus")
    penalties = sum(c["amount"] for c in components if c["type"] == "penalty")
    deposit = sum(c["amount"] for c in components if c["type"] == "deposit")
    total = base_salary + bonuses - penalties - deposit

    months_ru = ["","Январь","Февраль","Март","Апрель","Май","Июнь",
                 "Июль","Август","Сентябрь","Октябрь","Ноябрь","Декабрь"]
    month_label = f"{months_ru[today.month]} {today.year}"

    text = (
        f"💵 *Моя зарплата*\n_{month_label}_\n\n"
        f"Смен: {shifts_count}\n"
        f"Начислено: {base_salary:,.0f} грн\n"
    )
    if penalties:
        text += f"Штрафы: {penalties:,.0f} грн\n"
    if bonuses:
        text += f"Премии: {bonuses:,.0f} грн\n"
    if deposit:
        text += f"Списания: {deposit:,.0f} грн\n"
    text += f"\n{'─'*22}\n💵 *К выплате: {total:,.0f} грн*"

    if components:
        text += "\n\n📋 *Начисления/штрафы:*\n"
        for c in components[-5:]:
            emoji = "🎁" if c["type"] == "bonus" else "📉"
            sign = "+" if c["type"] == "bonus" else "-"
            text += f"  {emoji} {sign}{abs(c['amount']):,.0f} грн — {c['description'] or c['type']}\n"

    back = "adm_menu" if is_admin(emp) else "worker_menu"
    keyboard = [[InlineKeyboardButton("⬅️ Назад", callback_data=back)]]
    await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


# ─── SHIFT EXCHANGE ───────────────────────────────────────────────────────────

async def exchange_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    emp = get_employee(query.from_user.id)
    if not emp:
        return

    today = today_kyiv()
    end_date = (today + timedelta(days=14)).isoformat()
    conn = get_db()
    my_shifts = conn.execute(
        "SELECT * FROM schedules WHERE employee_id=? AND date>=? AND date<=? ORDER BY date",
        (emp["id"], today.isoformat(), end_date)
    ).fetchall()
    conn.close()

    if not my_shifts:
        keyboard = [[InlineKeyboardButton("⬅️ Назад", callback_data="worker_menu")]]
        await safe_edit(query, 
            "🔄 *Обмен сменами*\n\nУ вас нет смен на ближайшие 2 недели для обмена.",
            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown"
        )
        return

    keyboard = []
    for s in my_shifts:
        d = datetime.strptime(s["date"], "%Y-%m-%d")
        date_enc = s["date"].replace("-", "")
        label = f"{DAYS_SHORT[d.weekday()]} {d.strftime('%d.%m')} {s['shift_start']}–{s['shift_end']}"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"exch_mydate_{date_enc}")])
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="worker_menu")])

    await safe_edit(query, 
        "🔄 *Обмен сменами*\n\nВыберите вашу смену для обмена:",
        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown"
    )


async def exchange_mydate_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    emp = get_employee(query.from_user.id)
    if not emp:
        return

    date_enc = query.data.split("_")[2]
    my_date = f"{date_enc[:4]}-{date_enc[4:6]}-{date_enc[6:]}"
    context.user_data["exchange"] = {"my_date": my_date}

    today = today_kyiv()
    end_date = (today + timedelta(days=14)).isoformat()
    conn = get_db()
    others = conn.execute("""
        SELECT s.*, e.name as emp_name, e.id as emp_id
        FROM schedules s JOIN employees e ON s.employee_id = e.id
        WHERE s.employee_id != ? AND s.date >= ? AND s.date <= ?
        AND e.telegram_id IS NOT NULL AND e.is_active = 1 AND s.date != ?
        ORDER BY s.date LIMIT 20
    """, (emp["id"], today.isoformat(), end_date, my_date)).fetchall()
    conn.close()

    if not others:
        keyboard = [[InlineKeyboardButton("⬅️ Назад", callback_data="exch_start")]]
        await safe_edit(query, 
            "🔄 Нет других сотрудников со сменами для обмена.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    my_d = datetime.strptime(my_date, "%Y-%m-%d")
    keyboard = []
    for s in others:
        d = datetime.strptime(s["date"], "%Y-%m-%d")
        date_enc2 = s["date"].replace("-", "")
        first = s["emp_name"].split()[0]
        label = f"{first}: {DAYS_SHORT[d.weekday()]} {d.strftime('%d.%m')} {s['shift_start']}–{s['shift_end']}"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"exch_tgt_{s['emp_id']}_{date_enc2}")])
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="exch_start")])

    await safe_edit(query, 
        f"🔄 *Обмен сменами*\n"
        f"Ваша смена: *{DAYS_SHORT[my_d.weekday()]} {my_d.strftime('%d.%m')}*\n\n"
        f"Выберите сотрудника для обмена:",
        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown"
    )


async def exchange_target_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    emp = get_employee(query.from_user.id)
    if not emp:
        return

    parts = query.data.split("_")  # exch_tgt_21_20260618
    target_emp_id = int(parts[2])
    date_enc = parts[3]
    target_date = f"{date_enc[:4]}-{date_enc[4:6]}-{date_enc[6:]}"

    my_date = context.user_data.get("exchange", {}).get("my_date")
    if not my_date:
        await show_worker_menu(update, context, emp)
        return

    conn = get_db()
    target_emp = conn.execute("SELECT name FROM employees WHERE id=?", (target_emp_id,)).fetchone()
    my_shift = conn.execute("SELECT * FROM schedules WHERE employee_id=? AND date=?", (emp["id"], my_date)).fetchone()
    tgt_shift = conn.execute("SELECT * FROM schedules WHERE employee_id=? AND date=?", (target_emp_id, target_date)).fetchone()
    conn.close()

    context.user_data["exchange"].update({
        "target_id": target_emp_id,
        "target_date": target_date,
        "target_name": target_emp["name"] if target_emp else "?",
    })

    my_d = datetime.strptime(my_date, "%Y-%m-%d")
    tgt_d = datetime.strptime(target_date, "%Y-%m-%d")
    my_label = f"{DAYS_SHORT[my_d.weekday()]} {my_d.strftime('%d.%m')}"
    if my_shift:
        my_label += f" {my_shift['shift_start']}–{my_shift['shift_end']}"
    tgt_label = f"{DAYS_SHORT[tgt_d.weekday()]} {tgt_d.strftime('%d.%m')}"
    if tgt_shift:
        tgt_label += f" {tgt_shift['shift_start']}–{tgt_shift['shift_end']}"

    keyboard = [
        [InlineKeyboardButton("✅ Отправить запрос", callback_data="exch_confirm")],
        [InlineKeyboardButton("❌ Отмена", callback_data="exch_start")],
    ]
    await safe_edit(query, 
        f"🔄 *Подтверждение обмена*\n\n"
        f"Ваша смена: *{my_label}*\n↕️\n"
        f"{target_emp['name']}: *{tgt_label}*\n\n"
        f"_После вашего запроса сотрудник должен согласиться,\nзатем администратор подтвердит обмен._",
        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown"
    )


async def exchange_confirm_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    emp = get_employee(query.from_user.id)
    if not emp:
        return

    ex = context.user_data.get("exchange", {})
    my_date = ex.get("my_date")
    target_id = ex.get("target_id")
    target_date = ex.get("target_date")

    if not all([my_date, target_id, target_date]):
        await show_worker_menu(update, context, emp)
        return

    conn = get_db()
    conn.execute(
        "INSERT INTO shift_exchanges (requester_id, target_id, requester_date, target_date) VALUES (?, ?, ?, ?)",
        (emp["id"], target_id, my_date, target_date)
    )
    exchange_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    target_emp = conn.execute("SELECT name, telegram_id FROM employees WHERE id=?", (target_id,)).fetchone()
    conn.commit()
    conn.close()
    context.user_data.pop("exchange", None)

    my_d = datetime.strptime(my_date, "%Y-%m-%d")
    tgt_d = datetime.strptime(target_date, "%Y-%m-%d")

    if target_emp and target_emp["telegram_id"]:
        try:
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Принять", callback_data=f"exch_ok_{exchange_id}"),
                InlineKeyboardButton("❌ Отклонить", callback_data=f"exch_no_{exchange_id}"),
            ]])
            await query.get_bot().send_message(
                chat_id=target_emp["telegram_id"],
                text=(
                    f"🔄 *Запрос на обмен сменой*\n\n"
                    f"От: *{emp['name']}*\n\n"
                    f"Его смена: {DAYS_SHORT[my_d.weekday()]} {my_d.strftime('%d.%m.%Y')}\n"
                    f"Ваша смена: {DAYS_SHORT[tgt_d.weekday()]} {tgt_d.strftime('%d.%m.%Y')}\n\n"
                    f"_До согласия администратора смены не меняются_"
                ),
                reply_markup=kb, parse_mode="Markdown"
            )
        except Exception as e:
            logger.warning("Exchange notify failed: %s", e)

    logger.info("Exchange requested: %s (%s) ↔ %s (%s)", emp["name"], my_date, target_emp["name"] if target_emp else "?", target_date)
    keyboard = [[InlineKeyboardButton("⬅️ В меню", callback_data="worker_menu")]]
    await safe_edit(query, 
        f"✅ *Запрос отправлен!*\n\n"
        f"Ожидайте ответа от *{target_emp['name'] if target_emp else '?'}*.\n"
        f"После его согласия запрос уйдёт администратору.",
        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown"
    )


async def exchange_accept(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    emp = get_employee(query.from_user.id)

    exchange_id = int(query.data.split("_")[2])
    conn = get_db()
    ex = conn.execute("""
        SELECT ex.*, e1.name as req_name, e1.telegram_id as req_tg
        FROM shift_exchanges ex JOIN employees e1 ON ex.requester_id=e1.id WHERE ex.id=?
    """, (exchange_id,)).fetchone()

    if not ex or ex["status"] != "pending_target":
        conn.close()
        await safe_edit(query, "⚠️ Этот запрос уже обработан.")
        return

    conn.execute("UPDATE shift_exchanges SET status='pending_admin' WHERE id=?", (exchange_id,))
    admins = conn.execute(
        "SELECT telegram_id FROM employees WHERE is_bot_admin=1 AND telegram_id IS NOT NULL AND is_active=1"
    ).fetchall()
    conn.commit()
    conn.close()

    req_d = datetime.strptime(ex["requester_date"], "%Y-%m-%d")
    tgt_d = datetime.strptime(ex["target_date"], "%Y-%m-%d")

    for admin in admins:
        try:
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Подтвердить", callback_data=f"adm_exch_ok_{exchange_id}"),
                InlineKeyboardButton("❌ Отклонить", callback_data=f"adm_exch_no_{exchange_id}"),
            ]])
            await query.get_bot().send_message(
                chat_id=admin["telegram_id"],
                text=(
                    f"🔄 *Обмен сменами — нужно подтверждение*\n\n"
                    f"*{ex['req_name']}* ↔ *{emp['name'] if emp else '?'}*\n\n"
                    f"{ex['req_name']}: {DAYS_SHORT[req_d.weekday()]} {req_d.strftime('%d.%m.%Y')}\n"
                    f"Его партнёр: {DAYS_SHORT[tgt_d.weekday()]} {tgt_d.strftime('%d.%m.%Y')}\n\n"
                    f"Оба сотрудника согласны. Подтвердите обмен:"
                ),
                reply_markup=kb, parse_mode="Markdown"
            )
        except Exception as e:
            logger.warning("Admin exchange notify failed: %s", e)

    await safe_edit(query, 
        "✅ *Вы приняли запрос!*\n\nОжидайте подтверждения администратора.\n_До решения смены не изменены._",
        parse_mode="Markdown"
    )


async def exchange_decline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    exchange_id = int(query.data.split("_")[2])
    conn = get_db()
    ex = conn.execute("""
        SELECT ex.*, e1.name as req_name, e1.telegram_id as req_tg
        FROM shift_exchanges ex JOIN employees e1 ON ex.requester_id=e1.id WHERE ex.id=?
    """, (exchange_id,)).fetchone()

    if not ex or ex["status"] != "pending_target":
        conn.close()
        await safe_edit(query, "⚠️ Этот запрос уже обработан.")
        return

    conn.execute("UPDATE shift_exchanges SET status='rejected', resolved_at=? WHERE id=?",
                 (datetime.now().isoformat(), exchange_id))
    conn.commit()
    conn.close()

    if ex["req_tg"]:
        try:
            await query.get_bot().send_message(
                chat_id=ex["req_tg"],
                text=f"❌ *Запрос на обмен сменами отклонён*\n\nСотрудник отказал в обмене.",
                parse_mode="Markdown"
            )
        except Exception:
            pass

    await safe_edit(query, "✅ Запрос на обмен отклонён.")


# ─── ADMIN: Shift Exchanges ───────────────────────────────────────────────────

async def admin_exchanges(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    conn = get_db()
    exchanges = conn.execute("""
        SELECT ex.*, e1.name as req_name, e2.name as tgt_name
        FROM shift_exchanges ex
        JOIN employees e1 ON ex.requester_id=e1.id JOIN employees e2 ON ex.target_id=e2.id
        WHERE ex.status IN ('pending_target','pending_admin')
        ORDER BY ex.created_at DESC LIMIT 10
    """).fetchall()
    conn.close()

    if not exchanges:
        keyboard = [[InlineKeyboardButton("⬅️ Назад", callback_data="adm_menu")]]
        await safe_edit(query, 
            "🔄 *Обмены сменами*\n\nНет активных запросов.",
            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown"
        )
        return

    text = f"🔄 *Активные запросы на обмен* ({len(exchanges)})\n\n"
    keyboard = []
    status_ru = {"pending_target": "⏳ Ждёт ответа", "pending_admin": "✅ Согласован (ждёт адм.)"}

    for ex in exchanges:
        req_d = datetime.strptime(ex["requester_date"], "%Y-%m-%d")
        tgt_d = datetime.strptime(ex["target_date"], "%Y-%m-%d")
        text += (
            f"*{ex['req_name']}* {req_d.strftime('%d.%m')} ↔ "
            f"*{ex['tgt_name']}* {tgt_d.strftime('%d.%m')}\n"
            f"  {status_ru.get(ex['status'], '')}\n\n"
        )
        if ex["status"] == "pending_admin":
            keyboard.append([
                InlineKeyboardButton(f"✅ #{ex['id']}", callback_data=f"adm_exch_ok_{ex['id']}"),
                InlineKeyboardButton(f"❌ #{ex['id']}", callback_data=f"adm_exch_no_{ex['id']}"),
            ])

    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="adm_menu")])
    await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def admin_exchange_approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    exchange_id = int(query.data.split("_")[3])
    conn = get_db()
    ex = conn.execute("""
        SELECT ex.*, e1.name as req_name, e1.telegram_id as req_tg,
               e2.name as tgt_name, e2.telegram_id as tgt_tg
        FROM shift_exchanges ex
        JOIN employees e1 ON ex.requester_id=e1.id JOIN employees e2 ON ex.target_id=e2.id
        WHERE ex.id=?
    """, (exchange_id,)).fetchone()

    if not ex or ex["status"] != "pending_admin":
        conn.close()
        await safe_edit(query, "⚠️ Этот запрос уже обработан.")
        return

    req_sched = conn.execute("SELECT * FROM schedules WHERE employee_id=? AND date=?",
                             (ex["requester_id"], ex["requester_date"])).fetchone()
    tgt_sched = conn.execute("SELECT * FROM schedules WHERE employee_id=? AND date=?",
                             (ex["target_id"], ex["target_date"])).fetchone()

    if req_sched and tgt_sched:
        conn.execute("DELETE FROM schedules WHERE employee_id=? AND date=?", (ex["requester_id"], ex["requester_date"]))
        conn.execute("DELETE FROM schedules WHERE employee_id=? AND date=?", (ex["target_id"], ex["target_date"]))
        conn.execute("INSERT INTO schedules (employee_id, date, shift_start, shift_end, note) VALUES (?, ?, ?, ?, ?)",
                     (ex["requester_id"], ex["target_date"], tgt_sched["shift_start"], tgt_sched["shift_end"], tgt_sched["note"]))
        conn.execute("INSERT INTO schedules (employee_id, date, shift_start, shift_end, note) VALUES (?, ?, ?, ?, ?)",
                     (ex["target_id"], ex["requester_date"], req_sched["shift_start"], req_sched["shift_end"], req_sched["note"]))

    conn.execute("UPDATE shift_exchanges SET status='approved', resolved_at=? WHERE id=?",
                 (datetime.now().isoformat(), exchange_id))
    conn.commit()

    req_d = datetime.strptime(ex["requester_date"], "%Y-%m-%d")
    tgt_d = datetime.strptime(ex["target_date"], "%Y-%m-%d")

    for tg_id, new_date in [(ex["req_tg"], tgt_d), (ex["tgt_tg"], req_d)]:
        if tg_id:
            try:
                await query.get_bot().send_message(
                    chat_id=tg_id,
                    text=(
                        f"✅ *Обмен сменами подтверждён!*\n\n"
                        f"Ваша новая смена: *{new_date.strftime('%d.%m.%Y')} ({DAYS_SHORT[new_date.weekday()]})*\n"
                        f"График обновлён."
                    ), parse_mode="Markdown"
                )
            except Exception:
                pass

    conn.close()
    logger.info("Exchange #%d approved: %s ↔ %s", exchange_id, ex["req_name"], ex["tgt_name"])
    await admin_exchanges(update, context)


async def admin_exchange_reject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    exchange_id = int(query.data.split("_")[3])
    conn = get_db()
    ex = conn.execute("""
        SELECT ex.*, e1.telegram_id as req_tg, e2.telegram_id as tgt_tg
        FROM shift_exchanges ex
        JOIN employees e1 ON ex.requester_id=e1.id JOIN employees e2 ON ex.target_id=e2.id
        WHERE ex.id=?
    """, (exchange_id,)).fetchone()

    if ex:
        conn.execute("UPDATE shift_exchanges SET status='rejected', resolved_at=? WHERE id=?",
                     (datetime.now().isoformat(), exchange_id))
        conn.commit()
        for tg_id in [ex["req_tg"], ex["tgt_tg"]]:
            if tg_id:
                try:
                    await query.get_bot().send_message(
                        chat_id=tg_id,
                        text="❌ *Запрос на обмен сменами отклонён администратором.*",
                        parse_mode="Markdown"
                    )
                except Exception:
                    pass

    conn.close()
    logger.info("Exchange #%d rejected by admin", exchange_id)
    await admin_exchanges(update, context)


# ─── WORKER: Deposit (Залог) ─────────────────────────────────────────────────

async def deposit_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show deposit balance, history and progress bar."""
    query = update.callback_query
    await query.answer()
    emp = get_employee(query.from_user.id)
    if not emp:
        return
    if is_admin(emp):
        keyboard = [[InlineKeyboardButton("⬅️ Меню", callback_data="adm_menu")]]
        await safe_edit(query, 
            "⛔ Раздел залогов предназначен только для сотрудников.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    conn = get_db()
    approved = conn.execute(
        "SELECT amount, requested_at FROM deposit_requests "
        "WHERE employee_id=? AND status='approved' ORDER BY requested_at",
        (emp["id"],)
    ).fetchall()
    pending_count = conn.execute(
        "SELECT COUNT(*) FROM deposit_requests WHERE employee_id=? AND status='pending'",
        (emp["id"],)
    ).fetchone()[0]
    conn.close()

    GOAL = 5000.0
    total_dep = sum(r["amount"] for r in approved)
    bar = _deposit_bar(total_dep, GOAL)

    text = (
        f"💰 *Залог*\n\n"
        f"Баланс: *{total_dep:,.0f} / {GOAL:,.0f} грн*\n"
        f"{bar}\n"
    )
    if pending_count:
        text += f"\n⏳ Ожидает подтверждения: {pending_count} заявка\n"

    if approved:
        text += "\n📋 *История пополнений:*\n"
        for r in approved[-10:]:
            dt = (r["requested_at"] or "")[:10]
            try:
                y, m, d = dt.split("-")
                dt = f"{d}.{m}.{y}"
            except Exception:
                pass
            text += f"  {dt} — +{r['amount']:,.0f} грн\n"
    else:
        text += "\n_Пополнений ещё нет._\n"

    keyboard = [
        [InlineKeyboardButton("➕ Внести залог", callback_data="deposit_add")],
        [InlineKeyboardButton("⬅️ В меню", callback_data="worker_menu")],
    ]
    await safe_edit(query, 
        text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown"
    )


async def deposit_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ask employee to enter deposit amount."""
    query = update.callback_query
    await query.answer()
    context.user_data["deposit"] = {"state": "amount"}
    keyboard = [[InlineKeyboardButton("❌ Отмена", callback_data="deposit_menu")]]
    await safe_edit(query, 
        "💰 *Внести залог*\n\n"
        "Введите сумму в гривнах (например: *1000*):",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )


async def adm_deposit_approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin confirms deposit request."""
    query = update.callback_query
    await query.answer()

    req_id = int(query.data.split("_")[-1])
    conn = get_db()
    req = conn.execute(
        "SELECT dr.*, e.name, e.telegram_id AS emp_tg "
        "FROM deposit_requests dr JOIN employees e ON dr.employee_id=e.id WHERE dr.id=?",
        (req_id,)
    ).fetchone()

    if not req or req["status"] != "pending":
        conn.close()
        await safe_edit(query, "⚠️ Заявка уже обработана.")
        return

    conn.execute(
        "UPDATE deposit_requests SET status='approved', reviewed_at=? WHERE id=?",
        (now_kyiv().isoformat(), req_id)
    )
    conn.commit()
    conn.close()

    if req["emp_tg"]:
        await send_with_retry(
            query.get_bot(), req["emp_tg"],
            f"✅ *Залог подтверждён!*\n\n💰 +{req['amount']:,.0f} грн добавлено к вашему залогу.",
            parse_mode="Markdown"
        )

    logger.info("Deposit #%d approved: %s %.0f грн", req_id, req["name"], req["amount"])
    await safe_edit(query, 
        f"✅ Залог *{req['name']}* на *{req['amount']:,.0f} грн* подтверждён.",
        parse_mode="Markdown"
    )


async def adm_deposit_reject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin rejects deposit request."""
    query = update.callback_query
    await query.answer()

    req_id = int(query.data.split("_")[-1])
    conn = get_db()
    req = conn.execute(
        "SELECT dr.*, e.name, e.telegram_id AS emp_tg "
        "FROM deposit_requests dr JOIN employees e ON dr.employee_id=e.id WHERE dr.id=?",
        (req_id,)
    ).fetchone()

    if not req or req["status"] != "pending":
        conn.close()
        await safe_edit(query, "⚠️ Заявка уже обработана.")
        return

    conn.execute(
        "UPDATE deposit_requests SET status='rejected', reviewed_at=? WHERE id=?",
        (now_kyiv().isoformat(), req_id)
    )
    conn.commit()
    conn.close()

    if req["emp_tg"]:
        await send_with_retry(
            query.get_bot(), req["emp_tg"],
            f"❌ *Заявка на залог отклонена*\n\nСумма: {req['amount']:,.0f} грн\n"
            f"Обратитесь к администратору.",
            parse_mode="Markdown"
        )

    logger.info("Deposit #%d rejected: %s %.0f грн", req_id, req["name"], req["amount"])
    await safe_edit(query, 
        f"❌ Заявка *{req['name']}* на *{req['amount']:,.0f} грн* отклонена.",
        parse_mode="Markdown"
    )


# ─── WORKER: SOS ─────────────────────────────────────────────────────────────

async def sos_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show SOS reason selector."""
    query = update.callback_query
    await query.answer()
    emp = get_employee(query.from_user.id)
    if not emp:
        return

    keyboard = [
        [InlineKeyboardButton(reason, callback_data=f"sos_r{i}")]
        for i, reason in enumerate(SOS_REASONS)
    ]
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="worker_menu")])
    await safe_edit(query, 
        "🚨 *SOS — Выберите причину:*",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )


async def sos_reason_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send SOS alert to all admins."""
    query = update.callback_query
    await query.answer()
    emp = get_employee(query.from_user.id)
    if not emp:
        return

    idx = int(query.data.split("_r")[-1])
    reason = SOS_REASONS[idx]
    time_str = now_kyiv().strftime("%H:%M")

    conn = get_db()
    admins = conn.execute(
        "SELECT telegram_id FROM employees WHERE is_bot_admin=1 AND telegram_id IS NOT NULL AND is_active=1"
    ).fetchall()
    conn.close()

    msg = (
        f"🚨 *SOS*\n\n"
        f"👤 Сотрудник: *{emp['name']}*\n"
        f"🕐 Время: *{time_str}*\n"
        f"❗ Причина: *{reason}*"
    )

    sent = 0
    for admin in admins:
        ok = await send_with_retry(query.get_bot(), admin["telegram_id"], msg, parse_mode="Markdown")
        if ok:
            sent += 1

    logger.info("SOS sent by %s [%s] → %d admins", emp["name"], reason, sent)

    keyboard = [[InlineKeyboardButton("⬅️ В меню", callback_data="worker_menu")]]
    await safe_edit(query, 
        f"🚨 *SOS отправлен!*\n\n"
        f"Причина: {reason}\n"
        f"Время: {time_str}\n\n"
        f"_Администраторы уведомлены ({sent} чел.). Ожидайте._",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )


# ─── ADMIN: Urgent broadcast ──────────────────────────────────────────────────

async def admin_urgent_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show urgent broadcast template menu."""
    query = update.callback_query
    await query.answer()

    conn = get_db()
    count = conn.execute("SELECT COUNT(*) FROM employees WHERE is_active=1 AND telegram_id IS NOT NULL").fetchone()[0]
    conn.close()

    keyboard = [
        [InlineKeyboardButton(label, callback_data=f"adm_urg_{i}")]
        for i, (label, _) in enumerate(URGENT_MESSAGES)
    ]
    keyboard.append([InlineKeyboardButton("⬅️ Меню", callback_data="adm_menu")])

    await safe_edit(query, 
        f"🚨 *Срочная рассылка*\n\n"
        f"Подключено: *{count}* сотрудников\n\n"
        f"Выберите шаблон — сообщение уйдёт немедленно:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )


async def admin_urgent_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send urgent broadcast immediately to all employees."""
    query = update.callback_query
    await query.answer("🚨 Отправляем срочное сообщение…")

    idx = int(query.data.split("_")[-1])
    label, msg_text = URGENT_MESSAGES[idx]

    conn = get_db()
    employees = conn.execute(
        "SELECT telegram_id, name FROM employees WHERE is_active=1 AND telegram_id IS NOT NULL"
    ).fetchall()
    conn.close()

    delivered, failed, total, _recipients = await _do_broadcast(
        query.get_bot(), employees, msg_text, is_urgent=True, label=f"URGENT:{label}"
    )

    keyboard = [
        [InlineKeyboardButton("🚨 Ещё срочная", callback_data="adm_urgent")],
        [InlineKeyboardButton("⬅️ Меню", callback_data="adm_menu")],
    ]
    result = f"✅ Отправлено: {delivered} (все сотрудники)"
    if failed:
        result += f"\n❌ Ошибка: {failed}"

    await safe_edit(query, 
        f"🚨 *Срочная рассылка отправлена!*\n\n_{msg_text}_\n\n{result}",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )


# ─── WORKER: Shift handover ──────────────────────────────────────────────────

async def shift_handover_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Employee starts shift handover report."""
    query = update.callback_query
    await query.answer()
    emp = get_employee(query.from_user.id)
    if not emp:
        return

    today = today_kyiv()
    today_str = today.isoformat()

    conn = get_db()
    existing = conn.execute(
        "SELECT * FROM shift_handovers WHERE employee_id=? AND date=?",
        (emp["id"], today_str)
    ).fetchone()
    shift = conn.execute(
        "SELECT * FROM schedules WHERE employee_id=? AND date=?",
        (emp["id"], today_str)
    ).fetchone()
    conn.close()

    if existing:
        status_labels = {
            "pending": "⏳ Ожидает подтверждения",
            "approved": "✅ Подтверждена",
            "rejected": "❌ Отклонена"
        }
        keyboard = [[InlineKeyboardButton("⬅️ Назад", callback_data="worker_menu")]]
        await safe_edit(query, 
            f"🏁 *Сдача смены*\n\n"
            f"Вы уже отправили отчёт о смене сегодня.\n"
            f"Статус: {status_labels.get(existing['status'], existing['status'])}",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return

    context.user_data["handover"] = {
        "state": "comment",
        "date": today_str,
        "shift_info": f"{shift['shift_start']}–{shift['shift_end']}" if shift else "—",
        "comment": "",
        "photos": [],
    }

    shift_text = f"\nСмена: {shift['shift_start']}–{shift['shift_end']}" if shift else ""
    keyboard = [
        [InlineKeyboardButton("➡️ Пропустить", callback_data="sh_skip_comment")],
        [InlineKeyboardButton("❌ Отмена", callback_data="sh_cancel")],
    ]
    await safe_edit(query, 
        f"🏁 *Сдача смены*\n"
        f"📅 {today.strftime('%d.%m.%Y')}{shift_text}\n\n"
        f"Напишите комментарий о прошедшей смене\n"
        f"_(или нажмите «Пропустить»)_",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )


async def shift_handover_skip_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Skip comment step → go to photo upload."""
    query = update.callback_query
    await query.answer()

    handover = context.user_data.get("handover", {})
    handover["state"] = "photos"
    context.user_data["handover"] = handover

    keyboard = [
        [InlineKeyboardButton("✅ Отправить без фото", callback_data="sh_submit")],
        [InlineKeyboardButton("❌ Отмена", callback_data="sh_cancel")],
    ]
    await safe_edit(query, 
        "📸 *Сдача смены — фото*\n\n"
        "Прикрепите фотографии (по одному).\n"
        "Когда готово — нажмите *«✅ Отправить»*.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )


async def shift_handover_submit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Submit handover report and notify admins."""
    query = update.callback_query
    await query.answer()
    emp = get_employee(query.from_user.id)
    if not emp:
        return

    handover = context.user_data.pop("handover", {})
    date_str = handover.get("date")
    comment = handover.get("comment", "")
    photos = handover.get("photos", [])
    shift_info = handover.get("shift_info", "—")

    if not date_str:
        await show_worker_menu(update, context, emp)
        return

    conn = get_db()
    conn.execute(
        "INSERT INTO shift_handovers (employee_id, date, comment, photos, submitted_at) VALUES (?, ?, ?, ?, ?)",
        (emp["id"], date_str, comment, json.dumps(photos), now_kyiv().isoformat())
    )
    handover_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    admins = conn.execute(
        "SELECT telegram_id FROM employees WHERE is_bot_admin=1 AND telegram_id IS NOT NULL AND is_active=1"
    ).fetchall()
    conn.commit()
    conn.close()

    comment_text = f"\n💬 _{comment}_" if comment else ""
    photos_text = f"\n📸 Фото: {len(photos)} шт." if photos else ""

    for admin in admins:
        try:
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Подтвердить", callback_data=f"adm_sh_ok_{handover_id}"),
                InlineKeyboardButton("❌ Отклонить", callback_data=f"adm_sh_no_{handover_id}"),
            ]])
            await query.get_bot().send_message(
                chat_id=admin["telegram_id"],
                text=(
                    f"🏁 *Сдача смены*\n\n"
                    f"👤 {emp['name']}\n"
                    f"📅 {date_str}  |  {shift_info}"
                    f"{comment_text}{photos_text}\n\n"
                    f"Подтвердите приём смены:"
                ),
                reply_markup=kb,
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.warning("Handover notify admin failed: %s", e)

    logger.info("Shift handover submitted: %s on %s (id=%d)", emp["name"], date_str, handover_id)
    keyboard = [[InlineKeyboardButton("⬅️ В меню", callback_data="worker_menu")]]
    await safe_edit(query, 
        "✅ *Смена сдана!*\n\n"
        "Отчёт отправлен администратору.\n"
        "_Ожидайте подтверждения._",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )


async def shift_handover_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel handover flow."""
    context.user_data.pop("handover", None)
    emp = get_employee(update.callback_query.from_user.id)
    await show_worker_menu(update, context, emp)


async def admin_sh_approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin approves a shift handover."""
    query = update.callback_query
    await query.answer()

    handover_id = int(query.data.split("_")[-1])
    conn = get_db()
    h = conn.execute("""
        SELECT sh.*, e.name, e.telegram_id as emp_tg
        FROM shift_handovers sh JOIN employees e ON sh.employee_id=e.id
        WHERE sh.id=?
    """, (handover_id,)).fetchone()

    if not h or h["status"] != "pending":
        conn.close()
        await safe_edit(query, "⚠️ Этот отчёт уже обработан.")
        return

    conn.execute("UPDATE shift_handovers SET status='approved', reviewed_at=? WHERE id=?",
                 (now_kyiv().isoformat(), handover_id))
    conn.commit()
    conn.close()

    if h["emp_tg"]:
        try:
            await query.get_bot().send_message(
                chat_id=h["emp_tg"],
                text=(
                    f"✅ *Смена подтверждена!*\n\n"
                    f"📅 {h['date']}\n"
                    f"Администратор принял ваш отчёт о смене. Отличная работа! 👍"
                ),
                parse_mode="Markdown"
            )
        except Exception:
            pass

    logger.info("Shift handover #%d approved", handover_id)
    await safe_edit(query, 
        f"✅ Смена *{h['name']}* ({h['date']}) подтверждена.",
        parse_mode="Markdown"
    )


async def admin_sh_reject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin rejects a shift handover."""
    query = update.callback_query
    await query.answer()

    handover_id = int(query.data.split("_")[-1])
    conn = get_db()
    h = conn.execute("""
        SELECT sh.*, e.name, e.telegram_id as emp_tg
        FROM shift_handovers sh JOIN employees e ON sh.employee_id=e.id
        WHERE sh.id=?
    """, (handover_id,)).fetchone()

    if not h or h["status"] != "pending":
        conn.close()
        await safe_edit(query, "⚠️ Этот отчёт уже обработан.")
        return

    conn.execute("UPDATE shift_handovers SET status='rejected', reviewed_at=? WHERE id=?",
                 (now_kyiv().isoformat(), handover_id))
    conn.commit()
    conn.close()

    if h["emp_tg"]:
        try:
            await query.get_bot().send_message(
                chat_id=h["emp_tg"],
                text=(
                    f"❌ *Сдача смены отклонена*\n\n"
                    f"📅 {h['date']}\n"
                    f"Обратитесь к администратору."
                ),
                parse_mode="Markdown"
            )
        except Exception:
            pass

    logger.info("Shift handover #%d rejected", handover_id)
    await safe_edit(query, 
        f"❌ Смена *{h['name']}* ({h['date']}) отклонена.",
        parse_mode="Markdown"
    )


# ─── Main callback router ─────────────────────────────────────────────────────

async def button_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    emp = get_employee(query.from_user.id)

    # ── Worker routes (any registered user) ───────────────────────────────────
    simple = {
        "worker_menu": show_worker_menu,
        "bot_weather": bot_weather,
        "my_tasks": my_tasks,
        "submit_report": submit_report_menu,
        "report_submit": report_submit,
        "report_cancel": report_cancel,
        "rpt_photos_done": report_photos_done,
        "rpt_skip_comment": report_skip_comment,
        "my_stats": my_stats,
        "my_schedule": my_schedule,
        "today_shift": today_shift,
        "help": help_cmd,
        "check_in": check_in,
        "my_salary": my_salary,
        "exch_start": exchange_start,
        "exch_confirm": exchange_confirm_handler,
        "shift_handover": shift_handover_start,
        "sh_skip_comment": shift_handover_skip_comment,
        "sh_submit": shift_handover_submit,
        "sh_cancel": shift_handover_cancel,
        "sos_start": sos_start,
        "deposit_menu": deposit_menu,
        "deposit_add": deposit_add_start,
    }
    if data in simple:
        await simple[data](update, context)
        return

    if data.startswith("adm_dep_ok_"):
        await adm_deposit_approve(update, context)
        return
    if data.startswith("adm_dep_no_"):
        await adm_deposit_reject(update, context)
        return
    if data.startswith("sos_r"):
        await sos_reason_selected(update, context)
        return
    if data.startswith("rpt_"):
        await report_task_selected(update, context)
        return
    if data.startswith("task_detail_"):
        await worker_task_detail(update, context)
        return
    if data.startswith("exch_mydate_"):
        await exchange_mydate_selected(update, context)
        return
    if data.startswith("exch_tgt_"):
        await exchange_target_selected(update, context)
        return
    if data.startswith("exch_ok_"):
        await exchange_accept(update, context)
        return
    if data.startswith("exch_no_"):
        await exchange_decline(update, context)
        return

    # ── Admin routes (guard: must be admin) ────────────────────────────────────
    if not is_admin(emp):
        await query.answer("⛔ Доступ запрещён", show_alert=True)
        return

    admin_simple = {
        "adm_menu": show_admin_menu,
        "adm_today": adm_today,
        "adm_today_bcast": adm_today_bcast,
        "adm_sched": admin_schedule,
        "adm_sched_prev": admin_schedule_nav,
        "adm_sched_next": admin_schedule_nav,
        "adm_tasks": admin_tasks,
        "adm_new_task": admin_new_task_start,
        "adm_bcast": admin_broadcast_start,
        "adm_bcast_confirm": admin_broadcast_confirm,
        "adm_bcast_custom": admin_broadcast_custom,
        "adm_urgent": admin_urgent_start,
        "adm_rpts": admin_reports,
        "adm_rpt_no_noreason": admin_report_reject_noreason,
        "adm_staff": admin_staff,
        "adm_stats": admin_stats,
        "adm_settings": admin_settings,
        "adm_sys_status": admin_system_status,
        "adm_task_skip_desc": admin_task_skip_desc,
        "adm_task_skip_photos": admin_task_skip_photos,
        "adm_task_next": admin_task_next_photos,
        "adm_task_skip_due": admin_task_skip_due,
        "adm_exchanges": admin_exchanges,
    }
    if data in admin_simple:
        await admin_simple[data](update, context)
        return

    if data.startswith("adm_urg_"):
        await admin_urgent_send(update, context)
    elif data.startswith("adm_rpt_view_"):
        await admin_report_view_photos(update, context)
    elif data.startswith("adm_bcast_q_"):
        await admin_broadcast_quick(update, context)
    elif data.startswith("adm_sh_ok_"):
        await admin_sh_approve(update, context)
    elif data.startswith("adm_sh_no_"):
        await admin_sh_reject(update, context)
    elif data.startswith("adm_task_emp_"):
        await admin_task_emp_selected(update, context)
    elif data.startswith("adm_task_prio_"):
        await admin_task_priority(update, context)
    elif data.startswith("adm_rpt_ok_"):
        await admin_report_approve(update, context)
    elif data.startswith("adm_rpt_no_"):
        await admin_report_reject_start(update, context)
    elif data.startswith("adm_toggle_"):
        await admin_toggle_role(update, context)
    elif data.startswith("adm_exch_ok_"):
        await admin_exchange_approve(update, context)
    elif data.startswith("adm_exch_no_"):
        await admin_exchange_reject(update, context)


# ─── Commands: /whoami and /setadmin ─────────────────────────────────────────

async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/whoami — show own Telegram ID, name and role."""
    user = update.effective_user
    emp = get_employee(user.id)

    if emp:
        role = "👑 Администратор" if is_admin(emp) else "👤 Сотрудник"
        text = (
            f"ℹ️ *Информация о вас*\n\n"
            f"🆔 Telegram ID: `{user.id}`\n"
            f"👤 Имя: *{emp['name']}*\n"
            f"🎭 Роль: {role}\n"
            f"📋 ID в системе: `{emp['id']}`"
        )
        if OWNER_TELEGRAM_ID and user.id == OWNER_TELEGRAM_ID:
            text += "\n\n🔑 _Вы являетесь владельцем бота_"
    else:
        text = (
            f"ℹ️ *Информация о вас*\n\n"
            f"🆔 Telegram ID: `{user.id}`\n"
            f"❌ Не зарегистрированы в системе\n\n"
            f"Используйте /start для регистрации."
        )

    await update.message.reply_text(text, parse_mode="Markdown")


async def setadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/setadmin <telegram_id> — grant admin role (admin only)."""
    caller = get_employee(update.effective_user.id)
    if not caller or not is_admin(caller):
        await update.message.reply_text("⛔ Только администраторы могут использовать эту команду.")
        return

    args = context.args
    if not args or not args[0].lstrip("-").isdigit():
        await update.message.reply_text(
            "⚠️ Использование: `/setadmin <telegram_id>`\n\n"
            "Пример: `/setadmin 851933268`\n"
            "Узнать ID пользователя можно командой /whoami",
            parse_mode="Markdown"
        )
        return

    target_tg_id = int(args[0])
    conn = get_db()
    target = conn.execute(
        "SELECT id, name, is_bot_admin FROM employees WHERE telegram_id=? AND is_active=1",
        (target_tg_id,)
    ).fetchone()

    if not target:
        conn.close()
        await update.message.reply_text(
            f"❌ Пользователь с Telegram ID `{target_tg_id}` не найден в системе.",
            parse_mode="Markdown"
        )
        return

    if target["is_bot_admin"]:
        conn.close()
        await update.message.reply_text(
            f"ℹ️ *{target['name']}* уже является администратором.",
            parse_mode="Markdown"
        )
        return

    conn.execute("UPDATE employees SET is_bot_admin=1 WHERE id=?", (target["id"],))
    conn.commit()
    conn.close()

    logger.info("/setadmin: %s granted admin to telegram_id=%d (%s)",
                caller["name"], target_tg_id, target["name"])

    # Notify the promoted user
    try:
        await context.bot.send_message(
            chat_id=target_tg_id,
            text=(
                "👑 *Вы назначены администратором!*\n\n"
                "Нажмите /start чтобы открыть меню администратора."
            ),
            parse_mode="Markdown"
        )
    except Exception:
        pass

    await update.message.reply_text(
        f"✅ *{target['name']}* теперь администратор.\nОн получил уведомление.",
        parse_mode="Markdown"
    )


# ─── App builder ──────────────────────────────────────────────────────────────

async def health_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/health — check system status."""
    import glob as _glob

    try:
        conn = get_db()
        conn.execute("SELECT COUNT(*) FROM employees")
        conn.close()
        db_status = "✅ База данных подключена"
    except Exception as exc:
        db_status = f"❌ База данных: {exc}"

    backup_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "backups")
    backups = sorted(_glob.glob(os.path.join(backup_dir, "backup_*.db"))) if os.path.exists(backup_dir) else []
    if backups:
        last_name = os.path.basename(backups[-1]).replace("backup_", "").replace(".db", "")
        # Format: 20260617_030000 → 17.06.2026 03:00
        try:
            d, t = last_name.split("_")
            backup_date = f"{d[6:8]}.{d[4:6]}.{d[:4]} {t[:2]}:{t[2:4]}"
        except Exception:
            backup_date = last_name
        backup_status = f"✅ Резервная копия: {backup_date} ({len(backups)}/14)"
    else:
        backup_status = "⚠️ Резервных копий нет (создаются в 03:00)"

    text = (
        f"🔍 *Статус системы*\n\n"
        f"✅ Бот работает\n"
        f"{db_status}\n"
        f"✅ Telegram API доступен\n"
        f"{backup_status}"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Catch ALL unhandled exceptions from any handler — log full traceback, never crash."""
    import traceback as tb
    from telegram.error import Conflict as TgConflict

    err = context.error
    update_id = getattr(update, "update_id", "?") if update else "?"

    # ── 409 Conflict: another bot instance is polling the same token ──────────
    # This is NOT a code bug — it happens when the production deployment also
    # runs the bot simultaneously with the dev workflow.
    # Root fix: start_prod.sh no longer starts the bot — redeploy to apply.
    # Until the old deployment restarts, log as WARNING only (no crash file).
    if isinstance(err, TgConflict):
        logger.warning(
            "409 Conflict from Telegram — duplicate polling instance detected. "
            "Cause: production deployment running bot simultaneously with dev workflow. "
            "Fix: redeploy with updated start_prod.sh (bot removed from prod). "
            "This instance continues polling; Telegram will recover when prod restarts."
        )
        return   # do not write crash file, do not propagate

    tb_str = "".join(tb.format_exception(type(err), err, err.__traceback__))

    logger.error(
        "UNHANDLED EXCEPTION [update_id=%s] %s: %s\n%s",
        update_id, type(err).__name__, err, tb_str,
    )

    # Persist last crash reason to file so startup message can show it
    _crash_file = os.path.join(os.path.dirname(os.path.dirname(__file__)), "bot_crash_reason.txt")
    try:
        first_line = tb_str.strip().split("\n")[-1][:200]
        with open(_crash_file, "w", encoding="utf-8") as _f:
            _f.write(f"{type(err).__name__}: {first_line}")
    except Exception:
        pass

    # Silently ack the callback so Telegram stops showing the spinner
    if hasattr(update, "callback_query") and update.callback_query:
        try:
            await update.callback_query.answer("⚠️ Произошла ошибка. Попробуйте ещё раз.", show_alert=True)
        except Exception:
            pass


def build_app():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN not set")

    from telegram.ext import JobQueue
    from telegram.request import HTTPXRequest

    # Robust timeouts — Replit's network can be flaky on long-running connections
    _request = HTTPXRequest(
        connection_pool_size=8,
        connect_timeout=15.0,
        read_timeout=30.0,
        write_timeout=15.0,
        pool_timeout=15.0,
    )
    _updates_request = HTTPXRequest(
        connection_pool_size=1,
        connect_timeout=15.0,
        read_timeout=40.0,   # long-polling needs a longer read window
        write_timeout=15.0,
        pool_timeout=15.0,
    )

    app = (
        Application.builder()
        .token(token)
        .request(_request)
        .get_updates_request(_updates_request)
        .job_queue(JobQueue())
        .build()
    )

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={REGISTER_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_name)]},
        fallbacks=[CommandHandler("start", start)],
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("whoami", whoami))
    app.add_handler(CommandHandler("setadmin", setadmin))
    app.add_handler(CommandHandler("tasks", my_tasks))
    app.add_handler(CommandHandler("stats", my_stats))
    app.add_handler(CommandHandler("health", health_command))
    app.add_handler(CallbackQueryHandler(button_router))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo_message))
    app.add_error_handler(global_error_handler)

    return app

import os
import sys
import glob
import shutil
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import asyncio
import logging
import sqlite3
import threading
import time as _tmod
from zoneinfo import ZoneInfo
from datetime import time, datetime, timedelta, date
from database import init_db, seed_demo_data, get_db
from handlers import build_app
from telegram.ext import ContextTypes
from weather import get_weather, format_weather_block, format_weather_short

KYIV_TZ = ZoneInfo("Europe/Kyiv")

BASE_DIR       = os.path.dirname(os.path.dirname(__file__))

# ── Watchdog state (updated by heartbeat_check; checked by _watchdog_thread) ──
_wd_last_alive: float = 0.0
_wd_lock = threading.Lock()


def _touch_watchdog():
    """Signal that the asyncio event loop is still alive."""
    global _wd_last_alive
    with _wd_lock:
        _wd_last_alive = _tmod.monotonic()


def _watchdog_thread():
    """
    Daemon thread — entirely outside the asyncio event loop.
    If the event loop produces no heartbeat for FREEZE_TIMEOUT seconds,
    writes a crash reason and sends SIGTERM → SIGKILL to force a restart.
    The supervisor script will bring Python back up automatically.
    """
    import signal as _sig

    FREEZE_TIMEOUT = 120   # seconds of silence → assume frozen
    CHECK_INTERVAL = 30    # how often to check
    GRACE_PERIOD   = 90    # allow time for first heartbeat after start

    logger.info(
        "Watchdog thread started — freeze_timeout=%ds, check_interval=%ds",
        FREEZE_TIMEOUT, CHECK_INTERVAL,
    )
    _tmod.sleep(GRACE_PERIOD)   # let the bot fully start before first check
    _touch_watchdog()            # seed the timestamp after grace period

    while True:
        _tmod.sleep(CHECK_INTERVAL)
        with _wd_lock:
            elapsed = _tmod.monotonic() - _wd_last_alive

        if elapsed > FREEZE_TIMEOUT:
            msg = (
                f"WATCHDOG: asyncio event loop frozen for {elapsed:.0f}s "
                f"(no heartbeat for >{FREEZE_TIMEOUT}s) — forcing restart"
            )
            logger.critical(msg)
            _write_crash_reason(
                f"WATCHDOG KILL: event loop frozen {elapsed:.0f}s "
                f"(last heartbeat >{FREEZE_TIMEOUT}s ago)"
            )
            try:
                os.kill(os.getpid(), _sig.SIGTERM)
            except Exception:
                pass
            _tmod.sleep(10)
            try:
                os.kill(os.getpid(), _sig.SIGKILL)
            except Exception:
                pass


DB_PATH        = os.path.join(BASE_DIR, "beach_manager.db")
BACKUP_DIR     = os.path.join(BASE_DIR, "backups")
RUN_COUNT_FILE = os.path.join(BASE_DIR, "bot_run_count.txt")
CRASH_FILE     = os.path.join(BASE_DIR, "bot_crash_reason.txt")
PID_FILE       = os.path.join(BASE_DIR, "bot.pid")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(
            os.path.join(BASE_DIR, "bot.log"),
            encoding="utf-8"
        ),
    ]
)
logger = logging.getLogger(__name__)

# ── Heartbeat state (in-process counters) ─────────────────────────────────────
_hb_db_failures   = 0   # consecutive DB-check failures
_hb_tg_failures   = 0   # consecutive Telegram-check failures
_hb_alert_sent_at: datetime | None = None   # when last admin alert was sent


def now_kyiv() -> datetime:
    return datetime.now(KYIV_TZ).replace(tzinfo=None)

def today_kyiv() -> date:
    return datetime.now(KYIV_TZ).date()


# ── Run-count helpers ─────────────────────────────────────────────────────────

def _read_run_count() -> int:
    try:
        return int(open(RUN_COUNT_FILE).read().strip())
    except Exception:
        return 1


def _read_crash_reason() -> str | None:
    """Return the last saved crash reason, or None."""
    try:
        with open(CRASH_FILE, encoding="utf-8") as f:
            return f.read().strip() or None
    except FileNotFoundError:
        return None
    except Exception:
        return None


def _write_crash_reason(reason: str):
    try:
        with open(CRASH_FILE, "w", encoding="utf-8") as f:
            f.write(reason)
    except Exception:
        pass


def _clear_crash_reason():
    try:
        os.remove(CRASH_FILE)
    except Exception:
        pass


# ── PID-file instance guard ───────────────────────────────────────────────────

def _count_live_bot_instances() -> int:
    """Return the number of python3 main.py processes currently alive."""
    import subprocess
    try:
        out = subprocess.check_output(
            ["pgrep", "-fc", "python3.*main\\.py"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        return max(0, int(out) - 1)   # -1 because pgrep itself may match its parent grep
    except Exception:
        return 1  # assume safe


def _write_pid_file():
    """Write current PID to the pid file so run_bot.sh can clean it up on restart."""
    try:
        with open(PID_FILE, "w") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass


def _release_pid_file():
    """Remove PID file if it still contains our PID (called on clean exit)."""
    try:
        stored = int(open(PID_FILE).read().strip())
        if stored == os.getpid():
            os.remove(PID_FILE)
    except Exception:
        pass


def _check_for_duplicate_instance() -> bool:
    """
    Return True if a duplicate bot instance appears to be running.
    Uses the PID file written by the previous incarnation.
    """
    if not os.path.exists(PID_FILE):
        return False
    try:
        old_pid = int(open(PID_FILE).read().strip())
        if old_pid == os.getpid():
            return False  # we already claimed the file
        os.kill(old_pid, 0)  # signal 0 — raises if process is dead
        return True           # process is alive → duplicate!
    except (ValueError, ProcessLookupError, PermissionError):
        return False          # stale PID file — process is dead, we're safe


def _uncaught_exception_hook(exc_type, exc_value, exc_tb):
    """sys.excepthook — log and persist any uncaught top-level exception."""
    import traceback as tb
    tb_str = "".join(tb.format_exception(exc_type, exc_value, exc_tb))
    logger.critical("UNCAUGHT EXCEPTION — process will exit:\n%s", tb_str)
    _write_crash_reason(
        f"{exc_type.__name__}: {exc_value}\n"
        f"Last crash reason: {tb_str.strip().split(chr(10))[-1][:200]}"
    )
    sys.__excepthook__(exc_type, exc_value, exc_tb)

def _get_admin_ids() -> list[int]:
    """Return telegram_ids of all active bot admins."""
    try:
        conn = get_db()
        rows = conn.execute(
            "SELECT telegram_id FROM employees "
            "WHERE is_bot_admin=1 AND telegram_id IS NOT NULL AND is_active=1"
        ).fetchall()
        conn.close()
        return [r["telegram_id"] for r in rows]
    except Exception:
        return []


# ─── Scheduled jobs ───────────────────────────────────────────────────────────

async def heartbeat_check(context: ContextTypes.DEFAULT_TYPE):
    """Runs every 60 seconds. Checks DB + Telegram API health."""
    global _hb_db_failures, _hb_tg_failures, _hb_alert_sent_at
    _touch_watchdog()  # tell the watchdog daemon thread the event loop is alive

    # ── 1. Database check ────────────────────────────────────────────────────
    db_ok = False
    try:
        conn = get_db()
        conn.execute("SELECT 1").fetchone()
        conn.close()
        db_ok = True
        if _hb_db_failures > 0:
            logger.info("Heartbeat: DB connection restored (was down %d checks)", _hb_db_failures)
        _hb_db_failures = 0
    except Exception as exc:
        _hb_db_failures += 1
        logger.error("Heartbeat: DB check FAILED (consecutive=%d): %s", _hb_db_failures, exc)

    # ── 2. Telegram API check ────────────────────────────────────────────────
    tg_ok = False
    try:
        await context.bot.get_me()
        tg_ok = True
        if _hb_tg_failures > 0:
            logger.info("Heartbeat: Telegram API restored (was down %d checks)", _hb_tg_failures)
        _hb_tg_failures = 0
    except Exception as exc:
        _hb_tg_failures += 1
        logger.error("Heartbeat: Telegram API FAILED (consecutive=%d): %s", _hb_tg_failures, exc)

    if db_ok and tg_ok:
        logger.debug("Heartbeat: OK (db=✅ telegram=✅)")
        return

    # ── 3. Alert admins after 3 consecutive failures (≈3 min), then each 10 min
    MIN_FAILURES_TO_ALERT = 3
    ALERT_COOLDOWN_SEC    = 600  # 10 minutes

    consecutive = max(_hb_db_failures, _hb_tg_failures)
    now = now_kyiv()
    cooldown_expired = (
        _hb_alert_sent_at is None or
        (now - _hb_alert_sent_at).total_seconds() >= ALERT_COOLDOWN_SEC
    )

    if consecutive >= MIN_FAILURES_TO_ALERT and cooldown_expired:
        problems = []
        if not db_ok:
            problems.append(f"🗄 База данных недоступна ({_hb_db_failures} проверок подряд)")
        if not tg_ok:
            problems.append(f"📡 Telegram API недоступен ({_hb_tg_failures} проверок подряд)")

        text = (
            "🚨 *Проблема с ботом*\n\n"
            + "\n".join(problems)
            + f"\n\n🕐 {now.strftime('%d.%m.%Y %H:%M')}"
        )
        _hb_alert_sent_at = now

        for tid in _get_admin_ids():
            try:
                await context.bot.send_message(chat_id=tid, text=text, parse_mode="Markdown")
            except Exception as exc:
                logger.warning("Heartbeat alert send failed → %d: %s", tid, exc)


async def create_daily_backup(context: ContextTypes.DEFAULT_TYPE):
    """Daily backup at 03:00 Kyiv time — keep last 14 copies."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    now_str = now_kyiv().strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join(BACKUP_DIR, f"backup_{now_str}.db")

    try:
        shutil.copy2(DB_PATH, backup_path)
        logger.info("Daily backup created: backup_%s.db", now_str)
    except Exception as exc:
        logger.error("Backup FAILED: %s", exc)
        return

    # Rotate: keep only 14 newest backups
    all_backups = sorted(glob.glob(os.path.join(BACKUP_DIR, "backup_*.db")))
    while len(all_backups) > 14:
        old = all_backups.pop(0)
        try:
            os.remove(old)
            logger.info("Old backup deleted: %s", os.path.basename(old))
        except Exception:
            pass


async def send_morning_reminder(context: ContextTypes.DEFAULT_TYPE):
    """Sends '1 hour until shift' message to a single employee."""
    data = context.job.data
    tg_id = data["telegram_id"]
    name = data["name"]
    shift_start = data["shift_start"]
    first_name = name.split()[0]

    try:
        text = (
            f"☀️ *Доброе утро, {first_name}!*\n\n"
            f"Через час начинается твоя смена.\n"
            f"⏰ Начало: *{shift_start}*\n\n"
            f"Пора собираться, удачной смены! 💪"
        )
        await context.bot.send_message(chat_id=tg_id, text=text, parse_mode="Markdown")
        logger.info("Morning reminder sent: %s (shift %s)", name, shift_start)
    except Exception as e:
        logger.warning("Morning reminder FAILED for %s: %s", name, e)


async def send_evening_reminders(context: ContextTypes.DEFAULT_TYPE):
    """Runs every day at 20:30 Kyiv time.
    Sends reminder only to employees who have a shift tomorrow and are off today.
    Also schedules morning reminder 1 hour before their shift start.
    """
    today = today_kyiv()
    today_str = today.isoformat()
    tomorrow_date = today + timedelta(days=1)
    tomorrow_str = tomorrow_date.isoformat()

    conn = get_db()

    tomorrow_shifts = conn.execute("""
        SELECT s.shift_start, s.shift_end, s.note,
               e.telegram_id, e.name, e.id AS emp_id
        FROM schedules s
        JOIN employees e ON s.employee_id = e.id
        WHERE s.date = ? AND e.telegram_id IS NOT NULL AND e.is_active = 1
          AND e.is_bot_admin = 0
    """, (tomorrow_str,)).fetchall()

    today_workers = set(
        row[0] for row in conn.execute("""
            SELECT e.telegram_id FROM schedules s
            JOIN employees e ON s.employee_id = e.id
            WHERE s.date = ? AND e.is_active = 1
        """, (today_str,)).fetchall()
    )
    conn.close()

    day_short = tomorrow_date.strftime("%d.%m.%Y")
    sent = 0
    skipped_working = 0
    skipped_error = 0

    for shift in tomorrow_shifts:
        tg_id = shift["telegram_id"]
        name = shift["name"]
        first_name = name.split()[0]
        shift_start = shift["shift_start"]
        shift_end = shift["shift_end"]

        if tg_id in today_workers:
            logger.info("Evening reminder SKIPPED (works today): %s", name)
            skipped_working += 1
            continue

        try:
            note_line = f"\n📝 {shift['note']}" if shift["note"] else ""

            weather_line = ""
            try:
                w = await asyncio.to_thread(get_weather)
                if w:
                    weather_line = "\n\n" + format_weather_block(w["tomorrow"], "Погода на завтра")
            except Exception:
                pass

            text = (
                f"🌅 *Напоминание о смене*\n\n"
                f"Привет, {first_name}!\n\n"
                f"Завтра у тебя рабочая смена.\n"
                f"⏰ Начало: *{shift_start}*\n"
                f"🔚 Конец: {shift_end}"
                f"{note_line}"
                f"{weather_line}\n\n"
                f"Постарайся лечь спать пораньше, чтобы быть бодрым и выйти вовремя.\n\n"
                f"Увидимся завтра 💪"
            )
            await context.bot.send_message(chat_id=tg_id, text=text, parse_mode="Markdown")
            logger.info("Evening reminder SENT: %s (shift %s–%s on %s)", name, shift_start, shift_end, day_short)
            sent += 1
        except Exception as e:
            logger.warning("Evening reminder FAILED for %s: %s", name, e)
            skipped_error += 1
            continue

        # Schedule morning reminder 1 hour before shift
        try:
            h, m = map(int, shift_start.split(":"))
            shift_start_dt = datetime(
                tomorrow_date.year, tomorrow_date.month, tomorrow_date.day, h, m
            )
            remind_at = shift_start_dt - timedelta(hours=1)

            if remind_at > now_kyiv():
                job_name = f"morning_{tg_id}_{tomorrow_str}"
                for j in context.job_queue.get_jobs_by_name(job_name):
                    j.schedule_removal()
                context.job_queue.run_once(
                    send_morning_reminder,
                    when=remind_at,
                    data={"telegram_id": tg_id, "name": name, "shift_start": shift_start},
                    name=job_name
                )
                logger.info("Morning reminder SCHEDULED: %s at %s",
                            name, remind_at.strftime("%Y-%m-%d %H:%M"))
            else:
                logger.info("Morning reminder SKIPPED (time passed): %s", name)
        except Exception as e:
            logger.warning("Could not schedule morning reminder for %s: %s", name, e)

    logger.info("Evening reminders done: sent=%d | skipped_working=%d | errors=%d | tomorrow=%d",
                sent, skipped_working, skipped_error, len(tomorrow_shifts))


async def check_weather_alerts(context: ContextTypes.DEFAULT_TYPE):
    """Runs at 08:00 daily. Sends weather alerts to admins + on-duty staff if danger level >= yellow."""
    from weather import danger_level

    try:
        w = await asyncio.to_thread(get_weather)
    except Exception as exc:
        logger.warning("Weather fetch failed in alert job: %s", exc)
        return

    if not w:
        logger.info("Weather alert check: no data")
        return

    alerts = w.get("alerts", [])
    tod = w.get("today", {})
    wind_max  = tod.get("wind_max") or 0
    gusts_max = tod.get("wind_gusts_max") or 0
    rain_pct  = tod.get("rain_pct") or 0
    temp_max  = tod.get("temp_max")

    level, level_label, rec = danger_level(wind_max, gusts_max)

    if not alerts:
        logger.info("Weather alert check: no alerts (level=%s)", level)
        return

    # Message for admins — full detail
    admin_lines = ["⚠️ *Предупреждение о погоде на сегодня*\n"]
    for a in alerts:
        admin_lines.append(f"{a['emoji']} {a['text']}")
    admin_lines.append("")
    admin_lines.append(f"💨 Ветер: *{wind_max:.0f} км/ч* | 🌪 Порывы: *{gusts_max:.0f} км/ч*")
    admin_lines.append(f"🌧 Дождь: *{rain_pct:.0f}%*")
    if temp_max is not None:
        admin_lines.append(f"🌡 Макс. температура: *+{temp_max:.0f}°C*")
    admin_lines.append(f"\n{level_label}")
    if rec:
        admin_lines.append(rec)
    admin_text = "\n".join(admin_lines)

    # Message for on-duty staff — shorter
    staff_lines = ["⚠️ *Сегодня ожидаются сложные погодные условия*\n"]
    if wind_max >= 20 or gusts_max >= 30:
        staff_lines.append(f"💨 Ветер: *{wind_max:.0f} км/ч* | 🌪 Порывы: *{gusts_max:.0f} км/ч*")
    if rain_pct >= 50:
        staff_lines.append(f"🌧 Вероятность дождя: *{rain_pct:.0f}%*")
    staff_lines.append("")
    staff_lines.append(rec if rec else "Будьте внимательны.")
    staff_text = "\n".join(staff_lines)

    today_str = today_kyiv().isoformat()
    conn = get_db()
    admins = conn.execute(
        "SELECT telegram_id, name FROM employees "
        "WHERE is_bot_admin=1 AND telegram_id IS NOT NULL AND is_active=1"
    ).fetchall()
    on_duty = conn.execute(
        """SELECT DISTINCT e.telegram_id, e.name FROM schedules s
           JOIN employees e ON s.employee_id=e.id
           WHERE s.date=? AND e.telegram_id IS NOT NULL AND e.is_active=1
             AND e.is_bot_admin=0""",
        (today_str,)
    ).fetchall()
    conn.close()

    admin_ids = {a["telegram_id"] for a in admins}

    sent_admins = 0
    for admin in admins:
        try:
            await context.bot.send_message(
                chat_id=admin["telegram_id"], text=admin_text, parse_mode="Markdown"
            )
            sent_admins += 1
        except Exception as exc:
            logger.warning("Weather alert (admin) failed for %s: %s", admin["name"], exc)

    sent_staff = 0
    if level in ("yellow", "red"):
        for worker in on_duty:
            if worker["telegram_id"] in admin_ids:
                continue
            try:
                await context.bot.send_message(
                    chat_id=worker["telegram_id"], text=staff_text, parse_mode="Markdown"
                )
                sent_staff += 1
            except Exception as exc:
                logger.warning("Weather alert (staff) failed for %s: %s", worker["name"], exc)

    logger.info("Weather alerts: level=%s admins=%d staff=%d alerts=%d",
                level, sent_admins, sent_staff, len(alerts))


# ─── Startup helpers ──────────────────────────────────────────────────────────

def promote_owner_on_startup():
    """If OWNER_TELEGRAM_ID is set, ensure that user has is_bot_admin=1."""
    try:
        owner_id = int(os.environ.get("OWNER_TELEGRAM_ID", "0"))
    except ValueError:
        return
    if not owner_id:
        return
    conn = get_db()
    result = conn.execute(
        "UPDATE employees SET is_bot_admin=1 WHERE telegram_id=? AND is_bot_admin=0",
        (owner_id,)
    )
    if result.rowcount:
        logger.info("Owner auto-promoted to admin: telegram_id=%d", owner_id)
    else:
        emp = conn.execute(
            "SELECT name, is_bot_admin FROM employees WHERE telegram_id=?", (owner_id,)
        ).fetchone()
        if emp:
            logger.info("Owner already admin: %s (telegram_id=%d)", emp["name"], owner_id)
        else:
            logger.warning("OWNER_TELEGRAM_ID=%d not found in employees table yet", owner_id)
    conn.commit()
    conn.close()


async def notify_admins_on_startup(app):
    """Send startup/restart notification to all bot admins."""
    run_count    = _read_run_count()
    time_str     = now_kyiv().strftime("%d.%m.%Y %H:%M")
    crash_reason = _read_crash_reason()
    _clear_crash_reason()          # consume it so it doesn't repeat next run

    is_restart = run_count > 1
    if is_restart:
        header = "🔄 *BeachManager перезапущен*"
        if crash_reason:
            # Show a compact one-liner, full details are in bot.log
            short = crash_reason.split("\n")[0][:180]
            detail = (
                f"Попытка запуска: #{run_count}\n"
                f"⚠️ *Last crash reason:*\n`{short}`"
            )
        else:
            detail = (
                f"Попытка запуска: #{run_count}\n"
                f"_(причина неизвестна — вероятно, плановый перезапуск Replit)_"
            )
        logger.info("Startup notify: RESTART (run #%d) crash=%s",
                    run_count, crash_reason or "unknown")
    else:
        header = "✅ *BeachManager запущен*"
        detail = "Первый запуск 🚀"
        logger.info("Startup notify: FIRST START")

    conn = get_db()
    admins = conn.execute(
        "SELECT telegram_id, name FROM employees "
        "WHERE is_bot_admin=1 AND telegram_id IS NOT NULL AND is_active=1"
    ).fetchall()
    conn.close()

    for admin in admins:
        try:
            await app.bot.send_message(
                chat_id=admin["telegram_id"],
                text=(
                    f"{header}\n\n"
                    f"🕐 {time_str}\n"
                    f"{detail}\n\n"
                    f"Бот готов к работе."
                ),
                parse_mode="Markdown"
            )
            logger.info("Startup notify sent → %s", admin["name"])
        except Exception as exc:
            logger.warning("Startup notify FAILED for %s: %s", admin["name"], exc)


def _notify_critical_error_sync(exc: Exception, tb_str: str):
    """Synchronously notify all admins of a fatal crash before the process exits."""
    import json as _json
    import urllib.request as _ureq

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        return

    targets: set = set()
    owner_str = os.environ.get("OWNER_TELEGRAM_ID", "")
    if owner_str:
        try:
            targets.add(int(owner_str))
        except ValueError:
            pass
    try:
        conn = get_db()
        rows = conn.execute(
            "SELECT telegram_id FROM employees "
            "WHERE is_bot_admin=1 AND telegram_id IS NOT NULL AND is_active=1"
        ).fetchall()
        conn.close()
        for r in rows:
            if r["telegram_id"]:
                targets.add(r["telegram_id"])
    except Exception:
        pass

    last_line = (tb_str.strip().split("\n")[-1][:200]) if tb_str else str(exc)[:200]
    text = (
        f"🚨 *BeachManager: КРИТИЧЕСКАЯ ОШИБКА*\n\n"
        f"`{type(exc).__name__}: {str(exc)[:150]}`\n\n"
        f"*Трейсбэк:* `{last_line}`\n\n"
        f"_Бот перезапускается..._"
    )

    for chat_id in targets:
        try:
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            data = _json.dumps({
                "chat_id": chat_id,
                "text":    text,
                "parse_mode": "Markdown"
            }).encode()
            req = _ureq.Request(url, data=data, headers={"Content-Type": "application/json"})
            _ureq.urlopen(req, timeout=5)
        except Exception:
            pass


# ─── Entry point ──────────────────────────────────────────────────────────────

def main():
    import atexit
    sys.excepthook = _uncaught_exception_hook   # catch any bare uncaught exceptions

    init_db()
    seed_demo_data()
    promote_owner_on_startup()

    run_count = _read_run_count()

    # ── Instance guard ────────────────────────────────────────────────────────
    # Check PID file for a live duplicate before claiming the slot.
    if _check_for_duplicate_instance():
        logger.critical(
            "DUPLICATE INSTANCE DETECTED — another bot process is already running. "
            "This instance will NOT start polling to avoid 409 Conflict. "
            "Exiting with code 0 (no restart needed)."
        )
        sys.exit(0)   # exit 0 → supervisor does NOT restart

    # Claim the PID file; release it on any exit (clean or crash).
    _write_pid_file()
    atexit.register(_release_pid_file)

    # Count and log live instances for transparency.
    instance_count = _count_live_bot_instances()
    logger.info("Telegram instance count: %d", instance_count)
    if instance_count > 1:
        logger.warning(
            "More than one bot process detected (%d). "
            "run_bot.sh should have killed stale instances. "
            "Proceeding with drop_pending_updates=True.",
            instance_count,
        )

    logger.info("=" * 55)
    logger.info("🤖 Bot starting (run #%d)", run_count)
    logger.info("Process: Telegram bot (independent from admin panel)")
    logger.info("Method:  long-polling (run_polling)")
    logger.info("Evening reminders: daily at 20:30 Kyiv")
    logger.info("Daily backup:      daily at 03:00 Kyiv")
    logger.info("Heartbeat:         every 60 seconds")
    logger.info("=" * 55)

    # ── Watchdog daemon thread (lives outside asyncio event loop) ────────────
    _touch_watchdog()  # seed timestamp so watchdog doesn't fire during grace period
    wd = threading.Thread(target=_watchdog_thread, daemon=True, name="watchdog")
    wd.start()

    app = build_app()

    job_queue = app.job_queue
    if job_queue is not None:
        job_queue.run_daily(
            send_evening_reminders,
            time=time(hour=20, minute=30, second=0),
            name="evening_reminders_2030"
        )
        logger.info("Job scheduled: evening_reminders_2030 at 20:30 daily")

        job_queue.run_daily(
            create_daily_backup,
            time=time(hour=3, minute=0, second=0),
            name="daily_backup_0300"
        )
        logger.info("Job scheduled: daily_backup_0300 at 03:00 daily")

        job_queue.run_daily(
            check_weather_alerts,
            time=time(hour=8, minute=0, second=0),
            name="weather_alerts_0800"
        )
        logger.info("Job scheduled: weather_alerts_0800 at 08:00 daily")

        job_queue.run_repeating(
            heartbeat_check,
            interval=60,
            first=30,
            name="heartbeat_60s"
        )
        logger.info("Job scheduled: heartbeat_60s every 60s (first in 30s)")
    else:
        logger.warning("JobQueue not available — reminders, backups and heartbeat disabled")

    app.post_init = notify_admins_on_startup

    logger.info("Polling started successfully")
    logger.info("BeachManager ready")

    try:
        app.run_polling(drop_pending_updates=True)
    except Exception as exc:
        import traceback as tb
        tb_str = tb.format_exc()
        logger.critical("FATAL ERROR in run_polling — bot will exit:\n%s", tb_str)
        _write_crash_reason(
            f"{type(exc).__name__}: {exc}\n"
            f"{tb_str.strip().split(chr(10))[-1][:200]}"
        )
        _notify_critical_error_sync(exc, tb_str)
        raise


if __name__ == "__main__":
    main()

import os
import re
import sys
import json
import logging
import urllib.request
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash, send_file
from functools import wraps
from database import get_db, init_db, seed_demo_data
from weather import get_weather, weather_emoji, weather_label, WIND_YELLOW_KMH, RAIN_WARN_PCT
from datetime import datetime, date, timedelta

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def tg_send(chat_id, text):
    """Send a Telegram message instantly using the bot token."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token or not chat_id:
        return False
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5)
        return True
    except Exception as e:
        logger.warning("Telegram send failed: %s", e)
        return False

app = Flask(
    __name__,
    template_folder=os.path.join(os.path.dirname(os.path.dirname(__file__)), "templates"),
    static_folder=os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")
)
app.secret_key = os.environ.get("SESSION_SECRET", "beach-manager-secret-2024")

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "beach2024")


@app.context_processor
def inject_sidebar_badges():
    """Injects badge counts into all templates for sidebar badges."""
    if session.get("admin_logged_in"):
        try:
            conn = get_db()
            pending_reports = conn.execute("SELECT COUNT(*) FROM task_reports WHERE status='pending'").fetchone()[0]
            pending_exchanges = conn.execute("SELECT COUNT(*) FROM shift_exchanges WHERE status='pending_admin'").fetchone()[0]
            conn.close()
            return {"pending_reports_count": pending_reports, "pending_exchanges_count": pending_exchanges}
        except Exception:
            pass
    return {"pending_reports_count": 0, "pending_exchanges_count": 0}


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("admin_logged_in"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        pwd = request.form.get("password", "")
        if pwd == ADMIN_PASSWORD:
            session["admin_logged_in"] = True
            return redirect(url_for("dashboard"))
        flash("Неверный пароль", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def dashboard():
    conn = get_db()
    today = date.today().isoformat()

    employees_total = conn.execute("SELECT COUNT(*) FROM employees WHERE is_active=1").fetchone()[0]
    tasks_pending = conn.execute("SELECT COUNT(*) FROM tasks WHERE status='pending'").fetchone()[0]
    tasks_done_today = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE status='completed' AND date(completed_at)=?", (today,)
    ).fetchone()[0]
    tasks_in_progress = conn.execute("SELECT COUNT(*) FROM tasks WHERE status='in_progress'").fetchone()[0]

    income_today = conn.execute(
        "SELECT COALESCE(SUM(amount),0) FROM income WHERE date=?", (today,)
    ).fetchone()[0]
    expenses_today = conn.execute(
        "SELECT COALESCE(SUM(amount),0) FROM expenses WHERE date=?", (today,)
    ).fetchone()[0]
    profit_today = income_today - expenses_today

    recent_tasks = conn.execute("""
        SELECT t.*, e.name as employee_name
        FROM tasks t LEFT JOIN employees e ON t.assigned_to = e.id
        ORDER BY t.created_at DESC LIMIT 8
    """).fetchall()

    employees = conn.execute(
        "SELECT e.*, COUNT(t.id) as task_count FROM employees e "
        "LEFT JOIN tasks t ON e.id = t.assigned_to AND t.status != 'completed' "
        "WHERE e.is_active=1 GROUP BY e.id"
    ).fetchall()

    last_7 = []
    for i in range(6, -1, -1):
        d = (date.today() - timedelta(days=i)).isoformat()
        inc = conn.execute("SELECT COALESCE(SUM(amount),0) FROM income WHERE date=?", (d,)).fetchone()[0]
        exp = conn.execute("SELECT COALESCE(SUM(amount),0) FROM expenses WHERE date=?", (d,)).fetchone()[0]
        last_7.append({"date": d, "income": inc, "expenses": exp, "profit": inc - exp})

    conn.close()

    return render_template("dashboard.html",
        employees_total=employees_total,
        tasks_pending=tasks_pending,
        tasks_done_today=tasks_done_today,
        tasks_in_progress=tasks_in_progress,
        income_today=income_today,
        expenses_today=expenses_today,
        profit_today=profit_today,
        recent_tasks=recent_tasks,
        employees=employees,
        chart_data=last_7,
        today=today
    )


@app.route("/employees")
@login_required
def employees():
    conn = get_db()
    emps = conn.execute("""
        SELECT e.*,
               COUNT(CASE WHEN t.status='completed' THEN 1 END) as completed_tasks,
               COUNT(CASE WHEN t.status!='completed' THEN 1 END) as active_tasks
        FROM employees e
        LEFT JOIN tasks t ON e.id = t.assigned_to
        WHERE e.is_active=1
        GROUP BY e.id
        ORDER BY e.name
    """).fetchall()
    conn.close()
    return render_template("employees.html", employees=emps)


@app.route("/employees/add", methods=["POST"])
@login_required
def add_employee():
    name = request.form.get("name", "").strip()
    role = request.form.get("role", "staff")
    if name:
        conn = get_db()
        conn.execute("INSERT INTO employees (name, role) VALUES (?, ?)", (name, role))
        conn.commit()
        conn.close()
        flash(f"Сотрудник «{name}» успешно добавлен", "success")
    return redirect(url_for("employees"))


@app.route("/employees/<int:emp_id>/delete", methods=["POST"])
@login_required
def delete_employee(emp_id):
    conn = get_db()
    conn.execute("UPDATE employees SET is_active=0 WHERE id=?", (emp_id,))
    conn.commit()
    conn.close()
    flash("Сотрудник удалён", "success")
    return redirect(url_for("employees"))


@app.route("/tasks")
@login_required
def tasks():
    conn = get_db()
    all_tasks = conn.execute("""
        SELECT t.*, e.name as employee_name
        FROM tasks t LEFT JOIN employees e ON t.assigned_to = e.id
        ORDER BY
          CASE t.status WHEN 'in_progress' THEN 1 WHEN 'pending_review' THEN 2 WHEN 'pending' THEN 3 ELSE 4 END,
          CASE t.priority WHEN 'high' THEN 1 WHEN 'normal' THEN 2 ELSE 3 END,
          t.created_at DESC
    """).fetchall()
    emps = conn.execute("SELECT id, name FROM employees WHERE is_active=1 ORDER BY name").fetchall()
    conn.close()
    return render_template("tasks.html", tasks=all_tasks, employees=emps, today=date.today().isoformat())


@app.route("/tasks/add", methods=["POST"])
@login_required
def add_task():
    title = request.form.get("title", "").strip()
    description = request.form.get("description", "").strip()
    assigned_to = request.form.get("assigned_to") or None
    priority = request.form.get("priority", "normal")
    due_date = request.form.get("due_date") or None

    if title:
        conn = get_db()
        conn.execute(
            "INSERT INTO tasks (title, description, assigned_to, priority, due_date) VALUES (?, ?, ?, ?, ?)",
            (title, description, assigned_to, priority, due_date)
        )
        if assigned_to:
            conn.execute(
                "INSERT INTO notifications (employee_id, message) VALUES (?, ?)",
                (assigned_to, f"📋 Назначена задача: {title}")
            )
            emp_row = conn.execute(
                "SELECT telegram_id, name FROM employees WHERE id=?", (assigned_to,)
            ).fetchone()
            if emp_row and emp_row["telegram_id"]:
                priority_ru = {"high": "🔴 Высокий", "normal": "🟡 Средний", "low": "🟢 Низкий"}
                msg = (
                    f"📋 *Вам назначена новая задача!*\n\n"
                    f"*{title}*\n"
                )
                if description:
                    msg += f"_{description}_\n"
                msg += f"\nПриоритет: {priority_ru.get(priority, priority)}"
                if due_date:
                    msg += f"\nСрок: 📅 {due_date}"
                tg_send(emp_row["telegram_id"], msg)
        conn.commit()
        conn.close()
        flash(f"Задача «{title}» создана", "success")
    return redirect(url_for("tasks"))


@app.route("/tasks/<int:task_id>/status", methods=["POST"])
@login_required
def update_task_status(task_id):
    status = request.form.get("status")
    conn = get_db()
    if status == "completed":
        conn.execute(
            "UPDATE tasks SET status=?, completed_at=? WHERE id=?",
            (status, datetime.now().isoformat(), task_id)
        )
    else:
        conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, task_id))
    conn.commit()
    conn.close()
    flash("Статус задачи обновлён", "success")
    return redirect(url_for("tasks"))


@app.route("/tasks/<int:task_id>/delete", methods=["POST"])
@login_required
def delete_task(task_id):
    conn = get_db()
    conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
    conn.commit()
    conn.close()
    flash("Задача удалена", "success")
    return redirect(url_for("tasks"))


@app.route("/finances")
@login_required
def finances():
    conn = get_db()
    today = date.today().isoformat()
    filter_date = request.args.get("date", today)

    income_rows = conn.execute(
        "SELECT * FROM income WHERE date=? ORDER BY created_at DESC", (filter_date,)
    ).fetchall()
    expense_rows = conn.execute(
        "SELECT * FROM expenses WHERE date=? ORDER BY created_at DESC", (filter_date,)
    ).fetchall()

    total_income = sum(r["amount"] for r in income_rows)
    total_expenses = sum(r["amount"] for r in expense_rows)
    profit = total_income - total_expenses

    monthly_income = conn.execute(
        "SELECT COALESCE(SUM(amount),0) FROM income WHERE strftime('%Y-%m', date)=strftime('%Y-%m', ?)",
        (filter_date,)
    ).fetchone()[0]
    monthly_expenses = conn.execute(
        "SELECT COALESCE(SUM(amount),0) FROM expenses WHERE strftime('%Y-%m', date)=strftime('%Y-%m', ?)",
        (filter_date,)
    ).fetchone()[0]

    income_by_cat = conn.execute(
        "SELECT category, SUM(amount) as total FROM income WHERE date=? GROUP BY category",
        (filter_date,)
    ).fetchall()
    expense_by_cat = conn.execute(
        "SELECT category, SUM(amount) as total FROM expenses WHERE date=? GROUP BY category",
        (filter_date,)
    ).fetchall()

    conn.close()
    return render_template("finances.html",
        income_rows=income_rows, expense_rows=expense_rows,
        total_income=total_income, total_expenses=total_expenses, profit=profit,
        monthly_income=monthly_income, monthly_expenses=monthly_expenses,
        income_by_cat=income_by_cat, expense_by_cat=expense_by_cat,
        filter_date=filter_date, today=today
    )


@app.route("/finances/income/add", methods=["POST"])
@login_required
def add_income():
    amount = float(request.form.get("amount", 0))
    category = request.form.get("category", "").strip()
    description = request.form.get("description", "").strip()
    entry_date = request.form.get("date", date.today().isoformat())
    if amount > 0 and category:
        conn = get_db()
        conn.execute(
            "INSERT INTO income (amount, category, description, date) VALUES (?, ?, ?, ?)",
            (amount, category, description, entry_date)
        )
        conn.commit()
        conn.close()
        flash(f"Доход {amount:.2f} ₽ добавлен", "success")
    return redirect(url_for("finances", date=entry_date))


@app.route("/finances/expense/add", methods=["POST"])
@login_required
def add_expense():
    amount = float(request.form.get("amount", 0))
    category = request.form.get("category", "").strip()
    description = request.form.get("description", "").strip()
    entry_date = request.form.get("date", date.today().isoformat())
    if amount > 0 and category:
        conn = get_db()
        conn.execute(
            "INSERT INTO expenses (amount, category, description, date) VALUES (?, ?, ?, ?)",
            (amount, category, description, entry_date)
        )
        conn.commit()
        conn.close()
        flash(f"Расход {amount:.2f} ₽ добавлен", "success")
    return redirect(url_for("finances", date=entry_date))


@app.route("/finances/income/<int:row_id>/delete", methods=["POST"])
@login_required
def delete_income(row_id):
    conn = get_db()
    conn.execute("DELETE FROM income WHERE id=?", (row_id,))
    conn.commit()
    conn.close()
    flash("Запись о доходе удалена", "success")
    return redirect(url_for("finances"))


@app.route("/finances/expense/<int:row_id>/delete", methods=["POST"])
@login_required
def delete_expense(row_id):
    conn = get_db()
    conn.execute("DELETE FROM expenses WHERE id=?", (row_id,))
    conn.commit()
    conn.close()
    flash("Запись о расходе удалена", "success")
    return redirect(url_for("finances"))


@app.route("/reports")
@login_required
def reports():
    conn = get_db()
    today = date.today().isoformat()

    weekly = []
    for i in range(6, -1, -1):
        d = (date.today() - timedelta(days=i)).isoformat()
        inc = conn.execute("SELECT COALESCE(SUM(amount),0) FROM income WHERE date=?", (d,)).fetchone()[0]
        exp = conn.execute("SELECT COALESCE(SUM(amount),0) FROM expenses WHERE date=?", (d,)).fetchone()[0]
        t_done = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE status='completed' AND date(completed_at)=?", (d,)
        ).fetchone()[0]
        weekly.append({"date": d, "income": inc, "expenses": exp, "profit": inc - exp, "tasks_done": t_done})

    top_earners = conn.execute("""
        SELECT category, SUM(amount) as total, COUNT(*) as count
        FROM income
        WHERE date >= date('now', '-30 days')
        GROUP BY category ORDER BY total DESC LIMIT 5
    """).fetchall()

    top_expenses = conn.execute("""
        SELECT category, SUM(amount) as total, COUNT(*) as count
        FROM expenses
        WHERE date >= date('now', '-30 days')
        GROUP BY category ORDER BY total DESC LIMIT 5
    """).fetchall()

    staff_performance = conn.execute("""
        SELECT e.name,
               COUNT(CASE WHEN t.status='completed' THEN 1 END) as completed,
               COUNT(CASE WHEN t.status!='completed' THEN 1 END) as pending
        FROM employees e
        LEFT JOIN tasks t ON e.id = t.assigned_to
        WHERE e.is_active=1
        GROUP BY e.id ORDER BY completed DESC
    """).fetchall()

    month_income = conn.execute(
        "SELECT COALESCE(SUM(amount),0) FROM income WHERE strftime('%Y-%m', date)=strftime('%Y-%m', 'now')"
    ).fetchone()[0]
    month_expenses = conn.execute(
        "SELECT COALESCE(SUM(amount),0) FROM expenses WHERE strftime('%Y-%m', date)=strftime('%Y-%m', 'now')"
    ).fetchone()[0]

    conn.close()
    return render_template("reports.html",
        weekly=weekly, top_earners=top_earners, top_expenses=top_expenses,
        staff_performance=staff_performance,
        month_income=month_income, month_expenses=month_expenses,
        month_profit=month_income - month_expenses,
        today=today
    )


@app.route("/schedule")
@login_required
def schedule():
    from datetime import date, timedelta
    week_offset = int(request.args.get("week_offset", 0))
    today = date.today()
    monday = today - timedelta(days=today.weekday()) + timedelta(weeks=week_offset)
    week_dates = [monday + timedelta(days=i) for i in range(7)]

    day_names = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    full_names = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
    week_days = [
        {"iso": d.isoformat(), "name": full_names[i], "short": d.strftime("%d.%m"), "abbr": day_names[i]}
        for i, d in enumerate(week_dates)
    ]

    conn = get_db()
    employees = conn.execute(
        "SELECT id, name, role FROM employees WHERE is_active=1 ORDER BY name"
    ).fetchall()

    shifts = conn.execute(
        "SELECT * FROM schedules WHERE date >= ? AND date <= ?",
        (week_dates[0].isoformat(), week_dates[-1].isoformat())
    ).fetchall()
    conn.close()

    schedule_map = {(s["employee_id"], s["date"]): s for s in shifts}

    week_label = f"{week_dates[0].strftime('%d.%m')} – {week_dates[-1].strftime('%d.%m.%Y')}"

    return render_template("schedule.html",
        employees=employees,
        week_days=week_days,
        schedule_map=schedule_map,
        week_offset=week_offset,
        week_label=week_label,
        today=today.isoformat()
    )


@app.route("/schedule/add", methods=["POST"])
@login_required
def add_shift():
    employee_id = request.form.get("employee_id")
    shift_date = request.form.get("date")
    shift_start = request.form.get("shift_start")
    shift_end = request.form.get("shift_end")
    note = request.form.get("note", "").strip()
    week_offset = request.form.get("week_offset", 0)

    if employee_id and shift_date and shift_start and shift_end:
        conn = get_db()
        conn.execute(
            """INSERT INTO schedules (employee_id, date, shift_start, shift_end, note)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(employee_id, date) DO UPDATE SET
                 shift_start=excluded.shift_start,
                 shift_end=excluded.shift_end,
                 note=excluded.note""",
            (employee_id, shift_date, shift_start, shift_end, note or None)
        )
        conn.commit()
        conn.close()
        flash("Смена добавлена", "success")

    return redirect(url_for("schedule", week_offset=week_offset))


@app.route("/schedule/<int:shift_id>/delete", methods=["POST"])
@login_required
def delete_shift(shift_id):
    week_offset = request.form.get("week_offset", 0)
    conn = get_db()
    conn.execute("DELETE FROM schedules WHERE id=?", (shift_id,))
    conn.commit()
    conn.close()
    flash("Смена удалена", "success")
    return redirect(url_for("schedule", week_offset=week_offset))


@app.route("/notify", methods=["POST"])
@login_required
def send_notification():
    emp_id = request.form.get("employee_id")
    message = request.form.get("message", "").strip()
    if message:
        conn = get_db()
        if emp_id == "all":
            emps = conn.execute("SELECT id FROM employees WHERE is_active=1").fetchall()
            for e in emps:
                conn.execute(
                    "INSERT INTO notifications (employee_id, message) VALUES (?, ?)",
                    (e["id"], message)
                )
        else:
            conn.execute(
                "INSERT INTO notifications (employee_id, message) VALUES (?, ?)",
                (emp_id, message)
            )
        conn.commit()
        conn.close()
        flash("Уведомление отправлено в очередь", "success")
    return redirect(url_for("dashboard"))


@app.route("/notifications")
@login_required
def notifications():
    conn = get_db()
    employees = conn.execute(
        "SELECT id, name, telegram_id FROM employees WHERE is_active=1 ORDER BY name"
    ).fetchall()
    linked_count = sum(1 for e in employees if e["telegram_id"])
    history = conn.execute(
        "SELECT * FROM broadcasts ORDER BY sent_at DESC LIMIT 20"
    ).fetchall()
    conn.close()
    return render_template("notifications.html",
        employees=employees, linked_count=linked_count, history=history)


@app.route("/notifications/send", methods=["POST"])
@login_required
def send_broadcast():
    message = request.form.get("message", "").strip()
    recipient = request.form.get("recipient", "all")

    if not message:
        flash("Сообщение не может быть пустым", "error")
        return redirect(url_for("notifications"))

    conn = get_db()
    if recipient == "all":
        employees = conn.execute(
            "SELECT telegram_id, name FROM employees WHERE telegram_id IS NOT NULL AND is_active=1"
        ).fetchall()
    else:
        employees = conn.execute(
            "SELECT telegram_id, name FROM employees WHERE id=? AND telegram_id IS NOT NULL AND is_active=1",
            (recipient,)
        ).fetchall()

    total = len(employees)
    delivered = sum(1 for emp in employees if tg_send(emp["telegram_id"], message))

    conn.execute(
        "INSERT INTO broadcasts (message, recipient, total, delivered) VALUES (?, ?, ?, ?)",
        (message, recipient, total, delivered)
    )
    conn.commit()
    conn.close()

    if total == 0:
        flash("Нет сотрудников с подключённым Telegram", "error")
    else:
        flash(f"✅ Доставлено {delivered} из {total} сотрудников", "success")

    return redirect(url_for("notifications"))


@app.route("/task-reports")
@login_required
def task_reports_list():
    conn = get_db()
    pending = conn.execute("""
        SELECT tr.*, t.title as task_title, e.name as employee_name
        FROM task_reports tr
        JOIN tasks t ON tr.task_id = t.id
        JOIN employees e ON tr.employee_id = e.id
        WHERE tr.status = 'pending'
        ORDER BY tr.submitted_at DESC
    """).fetchall()
    history = conn.execute("""
        SELECT tr.*, t.title as task_title, e.name as employee_name,
               rev.name as reviewer_name
        FROM task_reports tr
        JOIN tasks t ON tr.task_id = t.id
        JOIN employees e ON tr.employee_id = e.id
        LEFT JOIN employees rev ON tr.reviewed_by = rev.id
        WHERE tr.status != 'pending'
        ORDER BY tr.submitted_at DESC
        LIMIT 50
    """).fetchall()
    conn.close()

    # Deserialize photos JSON for each report
    def parse_photos(row):
        d = dict(row)
        try:
            d["photos_list"] = json.loads(d.get("photos") or "[]")
        except Exception:
            d["photos_list"] = []
        return d

    pending = [parse_photos(r) for r in pending]
    history = [parse_photos(r) for r in history]
    return render_template("task_reports.html", pending=pending, history=history)


@app.route("/task-reports/<int:report_id>/approve", methods=["POST"])
@login_required
def approve_report(report_id):
    conn = get_db()
    report = conn.execute(
        "SELECT tr.*, t.title, e.telegram_id, e.name as emp_name FROM task_reports tr "
        "JOIN tasks t ON tr.task_id=t.id JOIN employees e ON tr.employee_id=e.id WHERE tr.id=?",
        (report_id,)
    ).fetchone()

    if report:
        conn.execute(
            "UPDATE task_reports SET status='approved', reviewed_at=?, reviewed_by=? WHERE id=?",
            (datetime.now().isoformat(), session.get("admin_id", 1), report_id)
        )
        conn.execute(
            "UPDATE tasks SET status='completed', completed_at=? WHERE id=?",
            (datetime.now().isoformat(), report["task_id"])
        )
        conn.commit()

        if report["telegram_id"]:
            tg_send(report["telegram_id"],
                    f"✅ *Ваш отчёт принят!*\n\n"
                    f"Задача «{report['title']}» подтверждена.\n"
                    f"Отличная работа, {report['emp_name'].split()[0]}! 🎉")
        flash(f"Отчёт подтверждён. Задача переведена в «Выполнено».", "success")
        logger.info("Report approved: report_id=%d task=%s employee=%s", report_id, report["title"], report["emp_name"])
    conn.close()
    return redirect(url_for("task_reports_list"))


@app.route("/task-reports/<int:report_id>/reject", methods=["POST"])
@login_required
def reject_report(report_id):
    admin_comment = request.form.get("admin_comment", "").strip()
    conn = get_db()
    report = conn.execute(
        "SELECT tr.*, t.title, e.telegram_id, e.name as emp_name FROM task_reports tr "
        "JOIN tasks t ON tr.task_id=t.id JOIN employees e ON tr.employee_id=e.id WHERE tr.id=?",
        (report_id,)
    ).fetchone()

    if report:
        conn.execute(
            "UPDATE task_reports SET status='rejected', admin_comment=?, reviewed_at=?, reviewed_by=? WHERE id=?",
            (admin_comment, datetime.now().isoformat(), session.get("admin_id", 1), report_id)
        )
        conn.execute("UPDATE tasks SET status='in_progress' WHERE id=?", (report["task_id"],))
        conn.commit()

        if report["telegram_id"]:
            reason = f"\n\n💬 *Причина:* {admin_comment}" if admin_comment else ""
            tg_send(report["telegram_id"],
                    f"🔄 *Отчёт отклонён*\n\n"
                    f"Задача «{report['title']}» требует доработки.{reason}\n\n"
                    f"Вы можете отправить новый отчёт после исправления.")
        flash(f"Отчёт отклонён. Задача возвращена в работу.", "warning")
        logger.info("Report rejected: report_id=%d reason=%s", report_id, admin_comment)
    conn.close()
    return redirect(url_for("task_reports_list"))


@app.route("/employees/<int:emp_id>/toggle-admin", methods=["POST"])
@login_required
def toggle_bot_admin(emp_id):
    conn = get_db()
    current = conn.execute("SELECT is_bot_admin, name FROM employees WHERE id=?", (emp_id,)).fetchone()
    if current:
        new_val = 0 if current["is_bot_admin"] else 1
        conn.execute("UPDATE employees SET is_bot_admin=? WHERE id=?", (new_val, emp_id))
        conn.commit()
        action = "назначен бот-администратором" if new_val else "снят с роли бот-администратора"
        flash(f"«{current['name']}» {action}", "success")
    conn.close()
    return redirect(url_for("employees"))


# ─── Attendance ───────────────────────────────────────────────────────────────

@app.route("/attendance")
@login_required
def attendance():
    target_date = request.args.get("date", date.today().isoformat())
    try:
        d = date.fromisoformat(target_date)
    except ValueError:
        d = date.today()

    prev_date = (d - timedelta(days=1)).isoformat()
    next_date = (d + timedelta(days=1)).isoformat()
    month_start = d.replace(day=1).isoformat()

    conn = get_db()
    scheduled = conn.execute("""
        SELECT e.id, e.name, s.shift_start, s.shift_end,
               a.check_in_time, a.minutes_late, a.status
        FROM schedules s JOIN employees e ON s.employee_id=e.id
        LEFT JOIN attendance a ON a.employee_id=e.id AND a.date=s.date
        WHERE s.date=? ORDER BY s.shift_start, e.name
    """, (d.isoformat(),)).fetchall()

    total_scheduled = len(scheduled)
    on_time = sum(1 for r in scheduled if r["status"] == "on_time")
    minor_late = sum(1 for r in scheduled if r["status"] == "minor_late")
    major_late = sum(1 for r in scheduled if r["status"] == "major_late")
    no_show = sum(1 for r in scheduled if not r["check_in_time"])

    # Monthly stats per employee
    monthly_raw = conn.execute("""
        SELECT e.name,
               COUNT(DISTINCT s.date) as scheduled,
               SUM(CASE WHEN a.status='on_time' THEN 1 ELSE 0 END) as on_time,
               SUM(CASE WHEN a.status='minor_late' THEN 1 ELSE 0 END) as minor_late,
               SUM(CASE WHEN a.status='major_late' THEN 1 ELSE 0 END) as major_late,
               COUNT(a.id) as total_att
        FROM employees e
        LEFT JOIN schedules s ON s.employee_id=e.id AND s.date>=? AND s.date<=?
        LEFT JOIN attendance a ON a.employee_id=e.id AND a.date>=? AND a.date<=?
        WHERE e.is_active=1
        GROUP BY e.id ORDER BY e.name
    """, (month_start, d.isoformat(), month_start, d.isoformat())).fetchall()
    conn.close()

    stats = {"total_scheduled": total_scheduled, "on_time": on_time,
             "minor_late": minor_late, "major_late": major_late, "no_show": no_show}

    return render_template("attendance.html",
        rows=scheduled, stats=stats, monthly=monthly_raw,
        today=date.today().isoformat(), date_label=d.strftime("%d.%m.%Y (%A)"),
        prev_date=prev_date, next_date=next_date
    )


# ─── Salary ───────────────────────────────────────────────────────────────────

@app.route("/salary")
@login_required
def salary():
    period = request.args.get("period", date.today().strftime("%Y-%m"))
    try:
        year, month = map(int, period.split("-"))
        period_date = date(year, month, 1)
    except Exception:
        period_date = date.today().replace(day=1)
        period = period_date.strftime("%Y-%m")

    month_end = (period_date.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    prev_period = (period_date - timedelta(days=1)).strftime("%Y-%m")
    next_period = (month_end + timedelta(days=1)).strftime("%Y-%m")
    current_period = period

    months_ru = ["","Январь","Февраль","Март","Апрель","Май","Июнь",
                 "Июль","Август","Сентябрь","Октябрь","Ноябрь","Декабрь"]
    month_label = f"{months_ru[period_date.month]} {period_date.year}"

    conn = get_db()
    emps = conn.execute("SELECT * FROM employees WHERE is_active=1 ORDER BY name").fetchall()
    components_raw = conn.execute("""
        SELECT sc.*, e.name as emp_name FROM salary_components sc
        JOIN employees e ON sc.employee_id=e.id WHERE sc.period=? ORDER BY sc.created_at DESC
    """, (period,)).fetchall()

    employees_data = []
    for emp in emps:
        shifts = conn.execute(
            "SELECT COUNT(*) FROM schedules WHERE employee_id=? AND date>=? AND date<=?",
            (emp["id"], period_date.isoformat(), month_end.isoformat())
        ).fetchone()[0]
        comps = [c for c in components_raw if c["employee_id"] == emp["id"]]
        bonuses = sum(c["amount"] for c in comps if c["type"] == "bonus")
        penalties = sum(c["amount"] for c in comps if c["type"] == "penalty")
        deposit_deductions = sum(c["amount"] for c in comps if c["type"] == "deposit")
        rate = emp["salary_per_shift"] or 1000
        total = shifts * rate + bonuses - penalties - deposit_deductions
        employees_data.append({
            "id": emp["id"], "name": emp["name"],
            "salary_per_shift": rate, "shifts": shifts,
            "bonuses": bonuses, "penalties": penalties,
            "deposit_deductions": deposit_deductions, "total": total,
        })
    conn.close()

    return render_template("salary.html",
        employees=employees_data, components=components_raw,
        month_label=month_label, current_period=current_period,
        prev_period=prev_period, next_period=next_period,
    )


@app.route("/salary/set-rate/<int:emp_id>", methods=["POST"])
@login_required
def set_salary_rate(emp_id):
    rate = request.form.get("rate", 1500)
    try:
        rate = float(rate)
    except ValueError:
        rate = 1500
    conn = get_db()
    conn.execute("UPDATE employees SET salary_per_shift=? WHERE id=?", (rate, emp_id))
    conn.commit()
    emp = conn.execute("SELECT name FROM employees WHERE id=?", (emp_id,)).fetchone()
    conn.close()
    flash(f"Ставка «{emp['name'] if emp else '?'}» обновлена: {rate:,.0f} ₽/смена", "success")
    return redirect(request.referrer or url_for("salary"))


@app.route("/salary/add-component", methods=["POST"])
@login_required
def add_salary_component():
    emp_id = int(request.form.get("emp_id", 0))
    comp_type = request.form.get("type", "bonus")
    amount = float(request.form.get("amount", 0))
    description = request.form.get("description", "")
    period = request.form.get("period", date.today().strftime("%Y-%m"))
    if emp_id and amount > 0 and comp_type in ("bonus", "penalty", "deposit"):
        conn = get_db()
        conn.execute(
            "INSERT INTO salary_components (employee_id, type, amount, description, period) VALUES (?, ?, ?, ?, ?)",
            (emp_id, comp_type, amount, description, period)
        )
        conn.commit()
        conn.close()
        flash("Начисление добавлено", "success")
    return redirect(request.referrer or url_for("salary"))


@app.route("/salary/delete-component/<int:comp_id>", methods=["POST"])
@login_required
def delete_salary_component(comp_id):
    conn = get_db()
    conn.execute("DELETE FROM salary_components WHERE id=?", (comp_id,))
    conn.commit()
    conn.close()
    flash("Запись удалена", "success")
    return redirect(request.referrer or url_for("salary"))


# ─── Shift Exchanges ──────────────────────────────────────────────────────────

@app.route("/shift-exchanges")
@login_required
def shift_exchanges():
    filter_status = request.args.get("status", "all")
    conn = get_db()
    pending_admin = conn.execute("""
        SELECT ex.*, e1.name as req_name, e2.name as tgt_name
        FROM shift_exchanges ex JOIN employees e1 ON ex.requester_id=e1.id
        JOIN employees e2 ON ex.target_id=e2.id WHERE ex.status='pending_admin'
        ORDER BY ex.created_at DESC
    """).fetchall()

    q = "SELECT ex.*, e1.name as req_name, e2.name as tgt_name FROM shift_exchanges ex JOIN employees e1 ON ex.requester_id=e1.id JOIN employees e2 ON ex.target_id=e2.id"
    if filter_status != "all":
        exchanges = conn.execute(q + " WHERE ex.status=? ORDER BY ex.created_at DESC LIMIT 50", (filter_status,)).fetchall()
    else:
        exchanges = conn.execute(q + " ORDER BY ex.created_at DESC LIMIT 50").fetchall()
    conn.close()

    return render_template("shift_exchanges.html",
        pending_admin=pending_admin, exchanges=exchanges, filter_status=filter_status
    )


@app.route("/shift-exchanges/<int:ex_id>/approve", methods=["POST"])
@login_required
def approve_exchange(ex_id):
    conn = get_db()
    ex = conn.execute("""
        SELECT ex.*, e1.telegram_id as req_tg, e2.telegram_id as tgt_tg,
               e1.name as req_name, e2.name as tgt_name
        FROM shift_exchanges ex JOIN employees e1 ON ex.requester_id=e1.id
        JOIN employees e2 ON ex.target_id=e2.id WHERE ex.id=?
    """, (ex_id,)).fetchone()

    if ex and ex["status"] == "pending_admin":
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
                     (datetime.now().isoformat(), ex_id))
        conn.commit()
        # Telegram notifications
        for tg_id, new_date in [(ex["req_tg"], ex["target_date"]), (ex["tgt_tg"], ex["requester_date"])]:
            if tg_id:
                tg_send(tg_id, f"✅ *Обмен сменами подтверждён!*\n\nВаша новая смена: *{new_date}*. График обновлён.")
        flash(f"Обмен #{ex_id} подтверждён. Графики обновлены.", "success")
        logger.info("Exchange #%d approved via web admin", ex_id)
    conn.close()
    return redirect(url_for("shift_exchanges"))


@app.route("/shift-exchanges/<int:ex_id>/reject", methods=["POST"])
@login_required
def reject_exchange(ex_id):
    conn = get_db()
    ex = conn.execute("""
        SELECT ex.*, e1.telegram_id as req_tg, e2.telegram_id as tgt_tg
        FROM shift_exchanges ex JOIN employees e1 ON ex.requester_id=e1.id
        JOIN employees e2 ON ex.target_id=e2.id WHERE ex.id=?
    """, (ex_id,)).fetchone()
    if ex:
        conn.execute("UPDATE shift_exchanges SET status='rejected', resolved_at=? WHERE id=?",
                     (datetime.now().isoformat(), ex_id))
        conn.commit()
        for tg_id in [ex["req_tg"], ex["tgt_tg"]]:
            if tg_id:
                tg_send(tg_id, "❌ *Запрос на обмен сменами отклонён администратором.*")
        flash(f"Обмен #{ex_id} отклонён.", "warning")
    conn.close()
    return redirect(url_for("shift_exchanges"))


@app.route("/weather")
@login_required
def weather_page():
    from weather import LATITUDE, LONGITUDE
    w = get_weather()
    return render_template(
        "weather.html",
        weather=w,
        cur_emoji=weather_emoji(w["current"]["code"] if w else None),
        cur_label=weather_label(w["current"]["code"] if w else None),
        today_emoji=weather_emoji(w["today"]["code"] if w else None),
        today_label=weather_label(w["today"]["code"] if w else None),
        tmr_emoji=weather_emoji(w["tomorrow"]["code"] if w else None),
        tmr_label=weather_label(w["tomorrow"]["code"] if w else None),
        warn_wind=WIND_YELLOW_KMH,
        warn_rain=RAIN_WARN_PCT,
        lat=LATITUDE,
        lon=LONGITUDE,
    )


# ── Bot-status helpers ────────────────────────────────────────────────────────

_BOT_BASE       = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BOT_LOG_FILE   = os.path.join(_BOT_BASE, "bot.log")
_RUN_COUNT_FILE = os.path.join(_BOT_BASE, "bot_run_count.txt")
_CRASH_FILE     = os.path.join(_BOT_BASE, "bot_crash_reason.txt")

_LOG_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


def _tail_log(n=1000):
    """Return last n lines of bot.log."""
    try:
        with open(_BOT_LOG_FILE, encoding="utf-8", errors="replace") as f:
            return f.readlines()[-n:]
    except Exception:
        return []


def _parse_log_ts(line):
    m = _LOG_TS_RE.match(line)
    if m:
        try:
            return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
        except Exception:
            pass
    return None


_alert_cooldowns: dict = {}  # alert_key → last-sent epoch float


def _can_alert(key: str, cooldown: int = 1800) -> bool:
    """Return True and arm the cooldown if enough time has passed since the last alert."""
    now = datetime.now().timestamp()
    if now - _alert_cooldowns.get(key, 0) > cooldown:
        _alert_cooldowns[key] = now
        return True
    return False


def _notify_admins_telegram(text: str):
    """Send a Telegram message to all bot admins + OWNER_TELEGRAM_ID from the admin panel."""
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
    for chat_id in targets:
        tg_send(chat_id, text)


def _bot_status_data():
    """Collect all data needed for the bot status page."""
    # ── run count & crash reason ───────────────────────────────────────────
    run_count = 1
    try:
        run_count = int(open(_RUN_COUNT_FILE).read().strip())
    except Exception:
        pass

    crash_reason = None
    try:
        with open(_CRASH_FILE, encoding="utf-8") as f:
            crash_reason = f.read().strip() or None
    except Exception:
        pass

    # ── parse log ─────────────────────────────────────────────────────────
    lines = _tail_log(2000)
    today_str = datetime.now().strftime("%Y-%m-%d")

    # last startup time
    last_start_dt = None
    for line in reversed(lines):
        if "Bot starting (run #" in line:
            last_start_dt = _parse_log_ts(line)
            break

    # uptime
    uptime_str = "—"
    if last_start_dt:
        sec = int((datetime.now() - last_start_dt).total_seconds())
        h, rem = divmod(sec, 3600)
        m, s   = divmod(rem, 60)
        if h:
            uptime_str = f"{h}ч {m}м {s}с"
        elif m:
            uptime_str = f"{m}м {s}с"
        else:
            uptime_str = f"{s}с"

    # bot alive: last log line < 3 min ago
    last_log_dt = None
    for line in reversed(lines):
        dt = _parse_log_ts(line)
        if dt:
            last_log_dt = dt
            break

    bot_alive   = False
    bot_seen    = "никогда"
    if last_log_dt:
        age = (datetime.now() - last_log_dt).total_seconds()
        bot_alive = age < 180
        if age < 60:
            bot_seen = f"{int(age)}с назад"
        elif age < 3600:
            bot_seen = f"{int(age // 60)}м назад"
        else:
            bot_seen = last_log_dt.strftime("%H:%M:%S")

    # scheduler alive: heartbeat within 5 min
    scheduler_ok = False
    for line in reversed(lines):
        if "heartbeat_60s" in line and "executed successfully" in line:
            dt = _parse_log_ts(line)
            if dt and (datetime.now() - dt).total_seconds() < 300:
                scheduler_ok = True
            break

    # messages sent today (sendMessage 200 OK)
    msgs_today = sum(
        1 for l in lines
        if today_str in l and "sendMessage" in l and "200 OK" in l
    )

    # reports today (from DB)
    reports_today = 0
    try:
        conn = get_db()
        reports_today = conn.execute(
            "SELECT COUNT(*) FROM task_reports WHERE date(submitted_at)=?",
            (today_str,)
        ).fetchone()[0]
        conn.close()
    except Exception:
        pass

    # DB status
    db_ok = False
    try:
        conn = get_db()
        conn.execute("SELECT 1").fetchone()
        conn.close()
        db_ok = True
    except Exception:
        pass

    # last error
    last_err_text = None
    last_err_time = None
    last_err_type = None
    for line in reversed(lines):
        if " - ERROR - " in line or " - CRITICAL - " in line:
            m = re.match(
                r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ - .+ - (ERROR|CRITICAL) - (.+)",
                line
            )
            if m:
                last_err_time = m.group(1)
                last_err_type = m.group(2)
                last_err_text = m.group(3)[:400]
            break

    # critical problems (from recent 300 lines)
    recent = lines[-300:]
    problems = []
    recent_crashes = sum(1 for l in recent if "💥 Bot crashed" in l or "FATAL ERROR in run_polling" in l)
    if recent_crashes >= 2:
        problems.append(f"⚡ Частые перезапуски: {recent_crashes} за последние 300 строк логов")
    recent_errors = sum(1 for l in recent if " - ERROR - " in l or " - CRITICAL - " in l)
    if recent_errors >= 5:
        problems.append(f"🔥 Высокая частота ошибок: {recent_errors} в последних логах")
    if not db_ok:
        problems.append("🗄 Нет соединения с базой данных")
    if not scheduler_ok and bot_alive:
        problems.append("⏰ Планировщик задач не отвечает")

    # Tasks created today
    tasks_today = 0
    try:
        conn = get_db()
        tasks_today = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE date(created_at)=?", (today_str,)
        ).fetchone()[0]
        conn.close()
    except Exception:
        pass

    # Deployment availability (REPLIT_DOMAINS is only set in production)
    deployment_ok = None
    replit_domains = os.environ.get("REPLIT_DOMAINS", "")
    if replit_domains:
        primary_domain = replit_domains.split(",")[0].strip()
        try:
            dreq = urllib.request.Request(
                f"https://{primary_domain}/",
                headers={"User-Agent": "BeachManager-Health/1.0"}
            )
            dresp = urllib.request.urlopen(dreq, timeout=5)
            deployment_ok = dresp.status < 500
        except Exception:
            deployment_ok = False

    # Overall status indicator
    if not db_ok or recent_crashes >= 3:
        overall_status = "critical"
    elif problems:
        overall_status = "warning"
    else:
        overall_status = "stable"

    return {
        "run_count":       run_count,
        "crash_reason":    crash_reason,
        "last_start":      last_start_dt.strftime("%d.%m.%Y %H:%M:%S") if last_start_dt else "—",
        "uptime":          uptime_str,
        "bot_alive":       bot_alive,
        "bot_seen":        bot_seen,
        "scheduler_ok":    scheduler_ok,
        "db_ok":           db_ok,
        "msgs_today":      msgs_today,
        "reports_today":   reports_today,
        "tasks_today":     tasks_today,
        "last_err_text":   last_err_text,
        "last_err_time":   last_err_time,
        "last_err_type":   last_err_type,
        "deployment_ok":   deployment_ok,
        "overall_status":  overall_status,
        "problems":        problems,
    }


# ── Bot-status routes ─────────────────────────────────────────────────────────

@app.route("/bot-status")
@login_required
def bot_status():
    data = _bot_status_data()
    return render_template("bot_status.html", **data)


@app.route("/bot-status/check")
@login_required
def bot_status_check():
    """Live JSON health check — called by the page via AJAX."""
    result = {"tg": False, "db": False, "scheduler": False, "bot": False, "deployment": None, "details": {}}

    # Telegram API ping
    try:
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        url   = f"https://api.telegram.org/bot{token}/getMe"
        req   = urllib.request.Request(url)
        resp  = urllib.request.urlopen(req, timeout=8)
        body  = json.loads(resp.read())
        result["tg"] = body.get("ok", False)
        if result["tg"]:
            result["details"]["bot_name"] = body["result"].get("username", "")
    except Exception as e:
        result["details"]["tg_error"] = str(e)[:120]

    # DB check
    try:
        conn = get_db()
        conn.execute("SELECT 1").fetchone()
        conn.close()
        result["db"] = True
    except Exception as e:
        result["details"]["db_error"] = str(e)[:120]

    # Bot log freshness (< 3 min)
    lines = _tail_log(50)
    last_log_dt = None
    for line in reversed(lines):
        dt = _parse_log_ts(line)
        if dt:
            last_log_dt = dt
            break
    if last_log_dt:
        age = (datetime.now() - last_log_dt).total_seconds()
        result["bot"] = age < 180
        result["details"]["log_age_sec"] = int(age)

    # Scheduler (heartbeat < 5 min)
    lines2k = _tail_log(500)
    for line in reversed(lines2k):
        if "heartbeat_60s" in line and "executed successfully" in line:
            dt = _parse_log_ts(line)
            if dt and (datetime.now() - dt).total_seconds() < 300:
                result["scheduler"] = True
            break

    # Deployment check (production only — REPLIT_DOMAINS set by Replit Reserved VM)
    replit_domains = os.environ.get("REPLIT_DOMAINS", "")
    if replit_domains:
        primary_domain = replit_domains.split(",")[0].strip()
        try:
            dreq = urllib.request.Request(
                f"https://{primary_domain}/",
                headers={"User-Agent": "BeachManager-Health/1.0"}
            )
            dresp = urllib.request.urlopen(dreq, timeout=5)
            result["deployment"] = dresp.status < 500
        except Exception as de:
            result["deployment"] = False
            result["details"]["deployment_error"] = str(de)[:120]

    return jsonify(result)


@app.route("/bot-status/logs/<log_type>")
@login_required
def bot_status_logs(log_type):
    """Return last N log lines as JSON for AJAX display."""
    lines = _tail_log(2000)

    if log_type == "errors":
        entries = []
        for line in lines:
            if " - ERROR - " in line or " - CRITICAL - " in line or "UNCAUGHT EXCEPTION" in line:
                entries.append(line.rstrip())
        result = entries[-10:]

    elif log_type == "events":
        # Last 50 non-empty lines that aren't raw httpx noise
        entries = [
            l.rstrip() for l in lines
            if l.strip() and "httpx - INFO" not in l
        ]
        result = entries[-50:]

    else:
        return jsonify({"error": "unknown log type"}), 400

    return jsonify({"lines": result})


@app.route("/bot-status/download")
@login_required
def bot_status_download():
    """Download the full bot.log file."""
    if not os.path.exists(_BOT_LOG_FILE):
        flash("Файл логов не найден.", "warning")
        return redirect(url_for("bot_status"))
    return send_file(
        _BOT_LOG_FILE,
        as_attachment=True,
        download_name=f"bot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log",
        mimetype="text/plain"
    )


@app.route("/bot-status/send-alert", methods=["POST"])
@login_required
def bot_status_send_alert():
    """Dispatch a Telegram alert to all admins. Rate-limited to once per 30 min per type."""
    alert_type = "unknown"
    if request.is_json and request.json:
        alert_type = request.json.get("type", "unknown")
    if not _can_alert(alert_type, cooldown=1800):
        return jsonify({"sent": False, "reason": "rate_limited"})
    messages = {
        "db_down": (
            "🚨 *BeachManager: База данных недоступна!*\n\n"
            "Admin Panel зафиксировала потерю соединения с SQLite.\n"
            "Требуется срочное вмешательство."
        ),
        "bot_dead": (
            "🚨 *BeachManager: Telegram бот не отвечает!*\n\n"
            "Последняя активность бота зафиксирована более 3 минут назад.\n"
            "Возможен сбой процесса."
        ),
        "deploy_down": (
            "🚨 *BeachManager: Deployment недоступен!*\n\n"
            "Проверка доступности production URL завершилась ошибкой."
        ),
    }
    text = messages.get(
        alert_type,
        f"⚠️ *BeachManager: Системное предупреждение*\n\nТип проблемы: `{alert_type}`"
    )
    _notify_admins_telegram(text)
    logger.warning("Admin alert dispatched: type=%s", alert_type)
    return jsonify({"sent": True})


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    seed_demo_data()
    port = int(os.environ.get("PORT", 5000))
    logger.info("=" * 50)
    logger.info("🌐 Admin panel started")
    logger.info("Process: Flask admin panel (independent from bot)")
    logger.info("Running on port %d", port)
    logger.info("=" * 50)
    app.run(host="0.0.0.0", port=port, debug=False)

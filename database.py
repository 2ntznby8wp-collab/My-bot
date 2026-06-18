import sqlite3
import os
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), "beach_manager.db")


def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db():
    conn = get_db()
    cur = conn.cursor()

    cur.executescript("""
        CREATE TABLE IF NOT EXISTS employees (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            telegram_id INTEGER UNIQUE,
            role TEXT DEFAULT 'staff',
            is_active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT,
            assigned_to INTEGER REFERENCES employees(id),
            status TEXT DEFAULT 'pending',
            priority TEXT DEFAULT 'normal',
            due_date TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            completed_at TEXT
        );

        CREATE TABLE IF NOT EXISTS income (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            amount REAL NOT NULL,
            category TEXT NOT NULL,
            description TEXT,
            date TEXT DEFAULT (date('now')),
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            amount REAL NOT NULL,
            category TEXT NOT NULL,
            description TEXT,
            date TEXT DEFAULT (date('now')),
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id INTEGER REFERENCES employees(id),
            message TEXT NOT NULL,
            sent INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS broadcasts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message TEXT NOT NULL,
            recipient TEXT NOT NULL,
            total INTEGER DEFAULT 0,
            delivered INTEGER DEFAULT 0,
            sent_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS schedules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id INTEGER NOT NULL REFERENCES employees(id),
            date TEXT NOT NULL,
            shift_start TEXT NOT NULL,
            shift_end TEXT NOT NULL,
            note TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE(employee_id, date)
        );

        CREATE TABLE IF NOT EXISTS task_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL REFERENCES tasks(id),
            employee_id INTEGER NOT NULL REFERENCES employees(id),
            comment TEXT,
            photos TEXT DEFAULT '[]',
            status TEXT DEFAULT 'pending',
            admin_comment TEXT,
            submitted_at TEXT DEFAULT (datetime('now')),
            reviewed_at TEXT,
            reviewed_by INTEGER REFERENCES employees(id)
        );

        CREATE TABLE IF NOT EXISTS attendance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id INTEGER NOT NULL REFERENCES employees(id),
            date TEXT NOT NULL,
            check_in_time TEXT NOT NULL,
            shift_start TEXT,
            minutes_late INTEGER DEFAULT 0,
            status TEXT DEFAULT 'on_time',
            UNIQUE(employee_id, date)
        );

        CREATE TABLE IF NOT EXISTS shift_exchanges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            requester_id INTEGER NOT NULL REFERENCES employees(id),
            target_id INTEGER NOT NULL REFERENCES employees(id),
            requester_date TEXT NOT NULL,
            target_date TEXT NOT NULL,
            status TEXT DEFAULT 'pending_target',
            admin_comment TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            resolved_at TEXT
        );

        CREATE TABLE IF NOT EXISTS salary_components (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id INTEGER NOT NULL REFERENCES employees(id),
            type TEXT NOT NULL,
            amount REAL NOT NULL,
            description TEXT,
            period TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS deposit_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id INTEGER NOT NULL REFERENCES employees(id),
            amount REAL NOT NULL,
            status TEXT DEFAULT 'pending',
            note TEXT,
            requested_at TEXT DEFAULT (datetime('now')),
            reviewed_at TEXT,
            reviewed_by INTEGER REFERENCES employees(id)
        );

        CREATE TABLE IF NOT EXISTS shift_handovers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id INTEGER NOT NULL REFERENCES employees(id),
            date TEXT NOT NULL,
            comment TEXT,
            photos TEXT DEFAULT '[]',
            status TEXT DEFAULT 'pending',
            admin_comment TEXT,
            submitted_at TEXT DEFAULT (datetime('now')),
            reviewed_at TEXT,
            reviewed_by INTEGER REFERENCES employees(id)
        );
    """)

    conn.commit()

    # Migrations for existing databases (safe: try/except each)
    migrations = [
        "ALTER TABLE employees ADD COLUMN is_bot_admin INTEGER DEFAULT 0",
        "ALTER TABLE employees ADD COLUMN salary_per_shift REAL DEFAULT 1000",
        "ALTER TABLE employees ADD COLUMN salary_deposit REAL DEFAULT 0",
        "UPDATE employees SET salary_per_shift=1000 WHERE salary_per_shift=1500 OR salary_per_shift IS NULL",
        "ALTER TABLE task_reports ADD COLUMN photo_file_ids TEXT DEFAULT '[]'",
        "ALTER TABLE shift_handovers ADD COLUMN photo_file_ids TEXT DEFAULT '[]'",
        "ALTER TABLE tasks ADD COLUMN photo_file_ids TEXT DEFAULT '[]'",
    ]
    for sql in migrations:
        try:
            conn.execute(sql)
            conn.commit()
        except Exception:
            pass

    conn.close()


def seed_demo_data():
    conn = get_db()
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM employees")
    if cur.fetchone()[0] > 0:
        conn.close()
        return

    employees = [
        ("Alex Johnson", None, "staff"),
        ("Maria Garcia", None, "staff"),
        ("Tom Wilson", None, "staff"),
        ("Sofia Martinez", None, "staff"),
        ("James Brown", None, "senior_staff"),
        ("Emma Davis", None, "staff"),
        ("Liam Miller", None, "staff"),
        ("Olivia Taylor", None, "staff"),
        ("Noah Anderson", None, "senior_staff"),
        ("Ava Thomas", None, "staff"),
    ]
    cur.executemany(
        "INSERT INTO employees (name, telegram_id, role) VALUES (?, ?, ?)",
        employees
    )

    from datetime import date
    today = date.today().isoformat()

    tasks = [
        ("Set up beach chairs Zone A", "Arrange 50 chairs in Zone A", 1, "completed", "normal", today),
        ("Lifeguard duty morning shift", "Monitor the swimming area 8am-2pm", 5, "in_progress", "high", today),
        ("Clean restrooms", "Deep clean all restrooms", 2, "pending", "normal", today),
        ("Restock snack bar", "Check inventory and restock", 3, "pending", "normal", today),
        ("Umbrella maintenance", "Check and repair broken umbrellas", 4, "pending", "low", today),
        ("Evening cleanup Zone B", "Clear all chairs and umbrellas", 6, "pending", "normal", today),
        ("First aid kit check", "Verify supplies in all first aid kits", 9, "completed", "high", today),
        ("VIP area setup", "Prepare premium lounge area", 7, "in_progress", "high", today),
    ]
    cur.executemany(
        "INSERT INTO tasks (title, description, assigned_to, status, priority, due_date) VALUES (?, ?, ?, ?, ?, ?)",
        tasks
    )

    income_entries = [
        (1500.00, "Chair Rental", "50 chairs rented", today),
        (800.00, "Umbrella Rental", "40 umbrellas rented", today),
        (450.00, "Food & Beverage", "Snack bar sales", today),
        (200.00, "Parking", "Parking fees", today),
        (350.00, "VIP Lounge", "3 VIP packages", today),
    ]
    cur.executemany(
        "INSERT INTO income (amount, category, description, date) VALUES (?, ?, ?, ?)",
        income_entries
    )

    expense_entries = [
        (200.00, "Supplies", "Sunscreen and beach supplies", today),
        (150.00, "Maintenance", "Umbrella repairs", today),
        (80.00, "Cleaning", "Cleaning products", today),
        (120.00, "Food Supplies", "Snack bar restocking", today),
    ]
    cur.executemany(
        "INSERT INTO expenses (amount, category, description, date) VALUES (?, ?, ?, ?)",
        expense_entries
    )

    conn.commit()
    conn.close()

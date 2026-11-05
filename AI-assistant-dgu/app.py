import os
import re
import io
import json
import sqlite3
import secrets
from datetime import datetime
from functools import wraps
from pathlib import Path

import qrcode
import requests
from dotenv import load_dotenv

from flask import (
    Flask, render_template, request, redirect, url_for, session,
    flash, jsonify, abort, send_file
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

load_dotenv()

QR_BASE_URL = os.getenv(
   "QR_BASE_URL",
   "https://college-assistant-17ga.onrender.com"
).rstrip("/")

BASE_DIR = Path(__file__).resolve().parent
DATABASE = Path(os.getenv("DATABASE", str(BASE_DIR / "upk.db")))
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}
MAX_IMAGE_SIZE = 5 * 1024 * 1024

app = Flask(__name__)
# Не используем известный всем секретный ключ по умолчанию.
# Для постоянных сессий задайте SECRET_KEY в .env.
SECRET_KEY = os.getenv("SECRET_KEY", "").strip()
if not SECRET_KEY:
    SECRET_KEY = secrets.token_hex(32)
    print("ВНИМАНИЕ: SECRET_KEY не задан. Сессии будут сброшены после перезапуска приложения.")
app.secret_key = SECRET_KEY

DEBUG = os.getenv("FLASK_DEBUG", "0").strip().lower() in {"1", "true", "yes", "on"}
app.config.update(
    MAX_CONTENT_LENGTH=MAX_IMAGE_SIZE,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("SESSION_COOKIE_SECURE", "0").strip().lower() in {"1", "true", "yes", "on"},
)

AI_API_KEY = (
    os.getenv("AI_API_KEY")
    or os.getenv("GROQ_API_KEY", "")
)

AI_BASE_URL = os.getenv(
    "AI_BASE_URL",
    "https://api.groq.com/openai/v1"
)

AI_MODEL = os.getenv(
    "AI_MODEL",
    "openai/gpt-oss-120b"
)

STATUSES = [
    "Новая", "На рассмотрении", "Назначен исполнитель",
    "В работе", "Нужны уточнения", "Решена", "Отклонена"
]
CATEGORIES = [
    "Оборудование", "Мебель и имущество", "Электрика",
    "Интернет", "Чистота", "Другое"
]

DEFAULT_KNOWLEDGE = [
    {
        "keywords": ["проектор", "подключить ноутбук", "подключение ноутбука"],
        "question": "Как подключить ноутбук к проектору?",
        "answer": "Подключите кабель HDMI (если он предусмотрен), выберите на проекторе соответствующий источник сигнала и на ноутбуке откройте режим вывода изображения (обычно Win + P). Если изображение не появилось или оборудование не включается, не разбирайте его — оформите заявку сотруднику УПК."
    },
    {
        "keywords": ["принтер", "не печатает", "печать"],
        "question": "Что проверить, если принтер не печатает?",
        "answer": "Проверьте, выбран ли нужный принтер, есть ли бумага и отображается ли сообщение об ошибке. Не разбирайте принтер и не пытайтесь устранять внутреннюю неисправность. Если проблема сохраняется, создайте заявку."
    },
    {
        "keywords": ["статус заявки", "проверить заявку", "где посмотреть статус"],
        "question": "Как проверить статус заявки?",
        "answer": "После регистрации система выдаёт номер и персональную ссылку отслеживания. Откройте эту ссылку, чтобы увидеть текущий статус и публичные комментарии по своей заявке."
    },
    {
        "keywords": ["сообщить о неисправности", "оформить заявку", "создать заявку"],
        "question": "Как сообщить о неисправности?",
        "answer": "Опишите, что произошло, укажите корпус и кабинет, выберите категорию и отправьте форму. Если вы открыли страницу по QR-коду помещения, место подставится автоматически."
    },
]

def find_knowledge(message):
    """Ищет активную инструкцию по ключевым словам в базе SQLite."""
    text = (message or "").lower()
    with db() as con:
        items = con.execute(
            "SELECT id, question, answer, keywords FROM knowledge_base WHERE is_active=1"
        ).fetchall()

    best_item = None
    best_score = 0
    for item in items:
        try:
            keywords = json.loads(item["keywords"] or "[]")
        except (TypeError, ValueError):
            keywords = []
        score = sum(len(word) for word in keywords if word and word.lower() in text)
        if score > best_score:
            best_score = score
            best_item = dict(item)
    return best_item

DANGEROUS_PATTERNS = [
    r"искр", r"дым", r"запах гари", r"горит", r"огол[её]нн\w* провод",
    r"ударило током", r"коротк\w* замыкан", r"проводк", r"электрощит"
]
FAULT_PATTERNS = [
    r"не работает", r"сломал", r"сломана", r"сломался", r"не включается",
    r"не печатает", r"поврежд", r"разбил", r"теч[её]т", r"грязно",
    r"не горит", r"нет интернет", r"не подключается"
]

def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def db():
    connection = sqlite3.connect(DATABASE)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection

def init_db():
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            login TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'staff'
        );
        CREATE TABLE IF NOT EXISTS locations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            building TEXT NOT NULL,
            floor TEXT DEFAULT '',
            room TEXT DEFAULT '',
            zone_name TEXT DEFAULT '',
            qr_key TEXT NOT NULL UNIQUE
        );
        CREATE TABLE IF NOT EXISTS tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            public_number TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            applicant_name TEXT DEFAULT '',
            applicant_surname TEXT DEFAULT '',
            category TEXT NOT NULL,
            location_id INTEGER,
            status TEXT NOT NULL DEFAULT 'Новая',
            priority TEXT DEFAULT 'Обычный',
            assignee TEXT DEFAULT '',
            tracking_token_hash TEXT NOT NULL,
            image_name TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(location_id) REFERENCES locations(id)
        );
        CREATE TABLE IF NOT EXISTS comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id INTEGER NOT NULL,
            author TEXT NOT NULL,
            body TEXT NOT NULL,
            visibility TEXT NOT NULL DEFAULT 'public',
            created_at TEXT NOT NULL,
            FOREIGN KEY(ticket_id) REFERENCES tickets(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS ticket_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id INTEGER NOT NULL,
            old_status TEXT,
            new_status TEXT NOT NULL,
            actor TEXT NOT NULL,
            note TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY(ticket_id) REFERENCES tickets(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS knowledge_base (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            question TEXT NOT NULL,
            answer TEXT NOT NULL,
            keywords TEXT NOT NULL DEFAULT '[]',
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """)
        if con.execute("SELECT COUNT(*) FROM knowledge_base").fetchone()[0] == 0:
            seed_time = now()
            con.executemany(
                """INSERT INTO knowledge_base
                   (question, answer, keywords, is_active, created_at, updated_at)
                   VALUES (?, ?, ?, 1, ?, ?)""",
                [
                    (item["question"], item["answer"], json.dumps(item["keywords"], ensure_ascii=False), seed_time, seed_time)
                    for item in DEFAULT_KNOWLEDGE
                ]
            )
        # Миграция для баз, созданных предыдущей версией приложения.
        ticket_columns = {row[1] for row in con.execute("PRAGMA table_info(tickets)").fetchall()}
        if "applicant_name" not in ticket_columns:
            con.execute("ALTER TABLE tickets ADD COLUMN applicant_name TEXT DEFAULT ''")
        if "applicant_surname" not in ticket_columns:
            con.execute("ALTER TABLE tickets ADD COLUMN applicant_surname TEXT DEFAULT ''")
        if "submission_key" not in ticket_columns:
            con.execute("ALTER TABLE tickets ADD COLUMN submission_key TEXT")
            con.execute("""
   CREATE UNIQUE INDEX IF NOT EXISTS idx_tickets_submission_key
   ON tickets(submission_key)
   WHERE submission_key IS NOT NULL AND submission_key != ''
""")
        if not con.execute("SELECT id FROM users WHERE login='admin'").fetchone():
            con.execute(
                "INSERT INTO users(name, login, password_hash, role) VALUES (?, ?, ?, ?)",
                ("Администратор УПК", "admin",
                 generate_password_hash(os.getenv("ADMIN_PASSWORD", "ChangeMe123!")), "admin")
            )
        if con.execute("SELECT COUNT(*) FROM locations").fetchone()[0] == 0:
            demo_locations = [
                ("Главный корпус", "2", "214", "Кабинет 214", "cabinet-214"),
                ("Главный корпус", "3", "305", "Кабинет 305", "cabinet-305"),
                ("Главный корпус", "1", "", "Общая зона 1 этажа", "common-area-1"),
            ]
            con.executemany(
                "INSERT INTO locations(building, floor, room, zone_name, qr_key) VALUES (?, ?, ?, ?, ?)",
                demo_locations
            )
        

def csrf_value():
    return session.get("csrf_token")

@app.context_processor
def inject_globals():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(24)
    return {"csrf_token": session["csrf_token"], "statuses": STATUSES}

@app.before_request
def csrf_check():
   if request.method == "GET":
       if "csrf_token" not in session:
           session["csrf_token"] = secrets.token_urlsafe(24)
   if request.method == "POST":
       sent = (
           request.form.get("csrf_token")
           or request.headers.get("X-CSRFToken")
       )
       if not sent or sent != session.get("csrf_token"):
           abort(
               400,
               description="Проверка безопасности формы не пройдена. Обновите страницу."
           )
           
def staff_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("staff_id"):
            return redirect(url_for("login", next=request.path))
        return fn(*args, **kwargs)
    return wrapper

def safe_category(text):
    t = text.lower()
    if any(x in t for x in ["розет", "провод", "свет", "ламп", "искр", "элект"]):
        return "Электрика"
    if any(x in t for x in ["интернет", "wi-fi", "wifi", "сеть"]):
        return "Интернет"
    if any(x in t for x in ["стол", "стул", "двер", "окн", "ручк", "мебел"]):
        return "Мебель и имущество"
    if any(x in t for x in ["гряз", "мусор", "уборк"]):
        return "Чистота"
    if any(x in t for x in ["проектор", "компьютер", "принтер", "монитор", "оборуд", "ноутбук"]):
        return "Оборудование"
    return "Другое"

def classify_message(message):
    text = (message or "").strip()
    low = text.lower()

    # 1. Проверка опасных ситуаций
    if any(re.search(pattern, low) for pattern in DANGEROUS_PATTERNS):
        return {
            "type": "danger",
            "category": safe_category(text),
            "title": "Потенциально опасная ситуация",
            "answer": (
                "Это может быть опасно. Не приближайтесь к источнику "
                "опасности, не трогайте оборудование и не пытайтесь "
                "ремонтировать его самостоятельно. Немедленно сообщите "
                "ответственному сотруднику колледжа."
            )
        }

    # 2. Проверка недостаточной информации
    if len(text.split()) < 3 or low in [
        "помогите", "проблема", "у меня проблема"
    ]:
        return {
            "type": "unclear",
            "category": "Другое",
            "title": "Требуется уточнение",
            "answer": (
                "Уточните, пожалуйста: что именно произошло "
                "и в каком корпусе или кабинете?"
            )
        }

    # 3. Проверка неисправностей
    if any(re.search(pattern, low) for pattern in FAULT_PATTERNS):
        return {
            "type": "fault",
            "category": safe_category(text),
            "title": text[:80],
            "answer": (
                "Похоже, вы сообщаете о неисправности. "
                "Рекомендую оформить заявку, чтобы сотрудник УПК "
                "мог проверить ситуацию. Укажите помещение и, "
                "если возможно, добавьте фотографию. "
                "Не пытайтесь самостоятельно ремонтировать оборудование."
            )
        }

    # 4. Поиск подходящей инструкции
    instruction = find_knowledge(text)
    if instruction:
        ai_answer = ask_ai(text, instruction)
        return {
            "type": "question",
            "category": "Другое",
            "title": instruction["question"],
            "answer": ai_answer or instruction["answer"]
        }

    # 5. Если проверенной информации нет
    return {
        "type": "unknown",
        "category": safe_category(text),
        "title": "Нужна проверка сотрудника",
        "answer": (
            "В подготовленной базе знаний нет проверенной информации "
            "для точного ответа. Я не буду придумывать решение. "
            "Обратитесь к сотруднику УПК либо оформите заявку."
        )
    }

def ask_ai(question, instruction):
   """Формирует ответ Groq на основе инструкции колледжа."""
   if not AI_API_KEY:
       return None
   url = AI_BASE_URL.rstrip("/") + "/chat/completions"
   headers = {
       "Authorization": f"Bearer {AI_API_KEY}",
       "Content-Type": "application/json"
   }
   system_prompt = f"""
   Ты — ИИ-помощник колледжа УПК.
   Твоя задача — помогать студентам и сотрудникам.
   Используй предоставленную инструкцию как основной источник
   информации.
   Правила:
   1. Отвечай только на русском языке.
   2. Не выдумывай правила и факты о колледже.
   3. Не добавляй сведения, которых нет в инструкции.
   4. Если информации недостаточно, честно сообщи об этом.
   5. Не советуй самостоятельно ремонтировать оборудование.
   6. При неисправности рекомендуй оформить заявку.
   7. Отвечай понятно и по существу.
   ИНСТРУКЦИЯ КОЛЛЕДЖА:
   {instruction["answer"]}
   """
   payload = {
       "model": AI_MODEL,
       "messages": [
           {
               "role": "system",
               "content": system_prompt
           },
           {
               "role": "user",
               "content": question
           }
       ],
       "temperature": 0.2,
       "max_tokens": 500
   }
   try:
       response = requests.post(
           url,
           headers=headers,
           json=payload,
           timeout=30
       )
       response.raise_for_status()
       data = response.json()
       return data["choices"][0]["message"]["content"].strip()
   except (requests.RequestException, KeyError, IndexError, ValueError):
       return None

def get_location(location_id):
    if not location_id:
        return None
    with db() as con:
        return con.execute("SELECT * FROM locations WHERE id=?", (location_id,)).fetchone()

def create_ticket(
   title, description, category, location_id,
   image_name="", applicant_name="", applicant_surname="",
   submission_key=""
):
   token = submission_key
   if not token:
       raise ValueError("Отсутствует ключ отправки")
   try:
       with db() as con:
           # Проверяем, не создавалась ли уже заявка с таким ключом
           existing = con.execute(
               "SELECT public_number FROM tickets WHERE submission_key=?",
               (submission_key,)
           ).fetchone()
           if existing:
               return existing[0], token, False
           public_number = "UPK-" + secrets.token_hex(3).upper()
           while con.execute(
               "SELECT id FROM tickets WHERE public_number=?",
               (public_number,)
           ).fetchone():
               public_number = "UPK-" + secrets.token_hex(3).upper()
           cur = con.execute("""
               INSERT INTO tickets(
                   public_number, title, description,
                   applicant_name, applicant_surname,
                   category, location_id, tracking_token_hash,
                   image_name, created_at, updated_at, submission_key
               )
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
           """, (
               public_number, title, description,
               applicant_name, applicant_surname,
               category, location_id,
               generate_password_hash(token),
               image_name, now(), now(), submission_key
           ))
           ticket_id = cur.lastrowid
           con.execute("""
               INSERT INTO ticket_history(
                   ticket_id, old_status, new_status,
                   actor, note, created_at
               )
               VALUES(?,?,?,?,?,?)
           """, (
               ticket_id, None, "Новая",
               "Система", "Заявка зарегистрирована", now()
           ))
       return public_number, token, True
   except sqlite3.IntegrityError:
       # Если два одинаковых запроса пришли почти одновременно
       with db() as con:
           existing = con.execute(
               "SELECT public_number FROM tickets WHERE submission_key=?",
               (submission_key,)
           ).fetchone()
       if existing:
           return existing[0], token, False
       raise

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

@app.route("/")
def index():
   location_key = request.args.get("location", "")
   location = None
   if location_key:
       with db() as con:
           location = con.execute(
               "SELECT * FROM locations WHERE qr_key=?",
               (location_key,)
           ).fetchone()
   with db() as con:
       locations = con.execute(
           """
           SELECT id, building, floor, room, zone_name
           FROM locations
           ORDER BY building, floor, room
           """
       ).fetchall()
   submission_key = secrets.token_urlsafe(32)
   return render_template(
       "index.html",
       location=location,
       categories=CATEGORIES,
       locations=locations,
       submission_key=submission_key
   )


@app.route("/ask", methods=["POST"])
def ask():
    data = request.get_json(silent=True) or {}
    message = str(data.get("message", "")).strip()
    if not message:
        return jsonify({"error": "Введите вопрос или описание проблемы."}), 400
    result = classify_message(message)
    return jsonify(result)
@app.route("/ticket/new", methods=["POST"])
def ticket_new():
   submission_key = request.form.get("submission_key", "").strip()
   if not submission_key:
       flash("Ошибка отправки формы. Обновите страницу.", "error")
       return redirect(url_for("index"))
   # Проверяем повторную отправку до обработки фотографии
   with db() as con:
       existing = con.execute(
           "SELECT public_number FROM tickets WHERE submission_key=?",
           (submission_key,)
       ).fetchone()
   if existing:
       return render_template(
           "created.html",
           public_number=existing[0],
           token=submission_key
       )
   description = request.form.get("description", "").strip()
   title = request.form.get("title", "").strip()
   category = request.form.get("category", "Другое")
   location_id = (
       request.form.get("location_id")
       or request.form.get("location_id_manual")
       or None
   )
   if not description:
       flash("Опишите проблему перед отправкой.", "error")
       return redirect(url_for("index"))
   if category not in CATEGORIES:
       category = "Другое"
   if location_id:
       try:
           location_id = int(location_id)
           if not get_location(location_id):
               location_id = None
       except ValueError:
           location_id = None
   image_name = ""
   image = request.files.get("photo")
   if image and image.filename:
       if not allowed_file(image.filename):
           flash(
               "Можно прикрепить изображение PNG, JPG или WEBP.",
               "error"
           )
           return redirect(url_for("index"))
       image_name = (
           secrets.token_hex(12)
           + "_"
           + secure_filename(image.filename)
       )
       image.save(UPLOAD_DIR / image_name)
   if not title:
       title = description[:80]
   applicant_name = request.form.get(
       "applicant_name", ""
   ).strip()[:80]
   applicant_surname = request.form.get(
       "applicant_surname", ""
   ).strip()[:80]
   public_number, token, is_new = create_ticket(
       title,
       description,
       category,
       location_id,
       image_name,
       applicant_name,
       applicant_surname,
       submission_key
   )
   return render_template(
       "created.html",
       public_number=public_number,
       token=token
   )


@app.route("/track/<token>")
def track(token):
    # Поиск по хешам не позволяет получить заявку простым перебором ID.
    with db() as con:
        rows = con.execute("SELECT * FROM tickets ORDER BY id DESC").fetchall()
        ticket = next((row for row in rows if check_password_hash(row["tracking_token_hash"], token)), None)
        if not ticket:
            abort(404)
        location = get_location(ticket["location_id"])
        comments = con.execute("""
            SELECT body,created_at FROM comments
            WHERE ticket_id=? AND visibility='public' ORDER BY id
        """, (ticket["id"],)).fetchall()
        history = con.execute("""
            SELECT new_status,created_at,note FROM ticket_history
            WHERE ticket_id=? ORDER BY id
        """, (ticket["id"],)).fetchall()
    return render_template("track.html", ticket=ticket, location=location, token=token,
                           comments=comments, history=history)

@app.route("/admin/login", methods=["GET", "POST"])
def login():
    if "csrf_token" not in session:
       session["csrf_token"] = secrets.token_urlsafe(24)
       
    if request.method == "POST":
        login_name = request.form.get("login", "").strip()
        password = request.form.get("password", "")
        with db() as con:
            user = con.execute("SELECT * FROM users WHERE login=?", (login_name,)).fetchone()
        if user and check_password_hash(user["password_hash"], password):
            session.clear()
            session["staff_id"] = user["id"]
            session["staff_name"] = user["name"]
            session["staff_role"] = user["role"]
            session["csrf_token"] = secrets.token_urlsafe(24)
            return redirect(url_for("admin"))
        flash("Неверный логин или пароль.", "error")
    return render_template("login.html")

@app.route("/admin/logout", methods=["POST"])
@staff_required
def logout():
    session.clear()
    return redirect(url_for("index"))

@app.route("/admin")
@staff_required
def admin():
    """Главная панель сотрудника УПК."""
    with db() as con:
        counts = {
            "all": con.execute("SELECT COUNT(*) FROM tickets").fetchone()[0],
            "open": con.execute("SELECT COUNT(*) FROM tickets WHERE status NOT IN ('Решена','Отклонена')").fetchone()[0],
            "new": con.execute("SELECT COUNT(*) FROM tickets WHERE status='Новая'").fetchone()[0],
            "done": con.execute("SELECT COUNT(*) FROM tickets WHERE status='Решена'").fetchone()[0],
        }
        recent_tickets = con.execute("""
            SELECT t.*, l.building, l.room, l.zone_name
            FROM tickets t LEFT JOIN locations l ON l.id=t.location_id
            ORDER BY t.created_at DESC LIMIT 7
        """).fetchall()
        category_stats = con.execute("""
            SELECT category, COUNT(*) AS total
            FROM tickets GROUP BY category ORDER BY total DESC LIMIT 5
        """).fetchall()
        locations = con.execute("""
            SELECT l.*, COUNT(t.id) AS tickets_count
            FROM locations l LEFT JOIN tickets t ON t.location_id=l.id
            GROUP BY l.id ORDER BY l.building, l.floor, l.room, l.zone_name
        """).fetchall()
        locations_count = len(locations)
        qr_version = secrets.token_hex(8)
    return render_template(
        "admin_dashboard.html", counts=counts,
        recent_tickets=recent_tickets, category_stats=category_stats,
        locations=locations, locations_count=locations_count, qr_version=qr_version
    )

@app.route("/admin/tickets")
@staff_required
def admin_tickets():
    status_filter = request.args.get("status", "")
    category_filter = request.args.get("category", "")
    location_filter = request.args.get("location", "")
    date_from = request.args.get("date_from", "")
    date_to = request.args.get("date_to", "")
    search = request.args.get("q", "").strip()

    query = """
        SELECT t.*, l.building, l.room, l.zone_name
        FROM tickets t LEFT JOIN locations l ON l.id=t.location_id
        WHERE 1=1
    """
    params = []
    if status_filter in STATUSES:
        query += " AND t.status=?"
        params.append(status_filter)
    if category_filter in CATEGORIES:
        query += " AND t.category=?"
        params.append(category_filter)
    if location_filter.isdigit():
        query += " AND t.location_id=?"
        params.append(int(location_filter))
    if date_from:
        query += " AND date(t.created_at)>=date(?)"
        params.append(date_from)
    if date_to:
        query += " AND date(t.created_at)<=date(?)"
        params.append(date_to)
    if search:
        like = f"%{search}%"
        query += """
            AND (
                t.public_number LIKE ?
                OR t.title LIKE ?
                OR t.description LIKE ?
                OR t.assignee LIKE ?
                OR t.applicant_name LIKE ?
                OR t.applicant_surname LIKE ?
            )
        """
        params.extend([like, like, like, like, like, like])
    query += " ORDER BY t.created_at DESC"

    with db() as con:
        tickets = con.execute(query, params).fetchall()
        locations = con.execute("SELECT id, building, room, zone_name FROM locations ORDER BY building, room").fetchall()
        counts = {
            "all": con.execute("SELECT COUNT(*) FROM tickets").fetchone()[0],
            "open": con.execute("SELECT COUNT(*) FROM tickets WHERE status NOT IN ('Решена','Отклонена')").fetchone()[0],
            "new": con.execute("SELECT COUNT(*) FROM tickets WHERE status='Новая'").fetchone()[0],
            "done": con.execute("SELECT COUNT(*) FROM tickets WHERE status='Решена'").fetchone()[0],
        }
    return render_template(
        "admin_tickets.html", tickets=tickets, counts=counts,
        categories=CATEGORIES, locations=locations,
        selected_status=status_filter, selected_category=category_filter,
        selected_location=location_filter, date_from=date_from,
        date_to=date_to, q=search
    )

@app.route("/admin/ticket/<int:ticket_id>", methods=["GET", "POST"])
@staff_required
def admin_ticket(ticket_id):
    with db() as con:
        ticket = con.execute("""
            SELECT t.*, l.building,l.floor,l.room,l.zone_name
            FROM tickets t LEFT JOIN locations l ON l.id=t.location_id WHERE t.id=?
        """, (ticket_id,)).fetchone()
        if not ticket:
            abort(404)
        if request.method == "POST":
            new_status = request.form.get("status", ticket["status"])
            assignee = request.form.get("assignee", "").strip()[:100]
            comment = request.form.get("comment", "").strip()
            old_status = ticket["status"]
            old_assignee = ticket["assignee"] or ""
            visibility = request.form.get("visibility", "public")
            if visibility not in {"public", "internal"}:
                visibility = "public"
            if new_status not in STATUSES:
                new_status = ticket["status"]

            actor = session.get("staff_name", "Сотрудник")
            changed_at = now()

            if new_status != old_status:
                history_note = comment if visibility == "public" else ""
                con.execute("""
                    INSERT INTO ticket_history(ticket_id, old_status, new_status, actor, note, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    ticket_id, old_status, new_status, actor, history_note, changed_at
                ))

            if assignee != old_assignee:
                audit_text = (
                    f"Исполнитель назначен: {assignee}"
                    if assignee else "Исполнитель снят"
                )
                con.execute("""
                    INSERT INTO comments(ticket_id, author, body, visibility, created_at)
                    VALUES (?, ?, ?, 'internal', ?)
                """, (ticket_id, actor, audit_text, changed_at))

            if comment:
                con.execute("""
                    INSERT INTO comments(ticket_id,author,body,visibility,created_at)
                    VALUES(?,?,?,?,?)
                """, (ticket_id, actor, comment, visibility, changed_at))

            con.execute("""
                UPDATE tickets
                SET status=?, assignee=?, updated_at=?
                WHERE id=?
            """, (new_status, assignee, changed_at, ticket_id))

            flash("Изменения сохранены.", "success")
            return redirect(url_for("admin_ticket", ticket_id=ticket_id))
        comments = con.execute(
            "SELECT * FROM comments WHERE ticket_id=? ORDER BY id DESC", (ticket_id,)
        ).fetchall()
        history = con.execute(
            "SELECT * FROM ticket_history WHERE ticket_id=? ORDER BY id DESC", (ticket_id,)
        ).fetchall()
    return render_template("ticket_detail.html", ticket=ticket, comments=comments, history=history)

@app.route("/admin/knowledge", methods=["GET", "POST"])
@staff_required
def admin_knowledge():
    """Просмотр и добавление инструкций базы знаний."""
    if request.method == "POST":
        question = request.form.get("question", "").strip()[:250]
        answer = request.form.get("answer", "").strip()[:10000]
        keywords_text = request.form.get("keywords", "")
        keywords = list(dict.fromkeys(
            word.strip().lower()[:80]
            for word in re.split(r"[,;\n]", keywords_text)
            if word.strip()
        ))[:30]

        if not question or not answer or not keywords:
            flash("Заполните вопрос, ответ и хотя бы одно ключевое слово.", "error")
        else:
            stamp = now()
            with db() as con:
                con.execute(
                    """INSERT INTO knowledge_base
                       (question, answer, keywords, is_active, created_at, updated_at)
                       VALUES (?, ?, ?, 1, ?, ?)""",
                    (question, answer, json.dumps(keywords, ensure_ascii=False), stamp, stamp)
                )
            flash("Инструкция добавлена в базу знаний.", "success")
            return redirect(url_for("admin_knowledge"))

    with db() as con:
        items = con.execute(
            "SELECT * FROM knowledge_base ORDER BY updated_at DESC, id DESC"
        ).fetchall()
    knowledge_items = []
    for item in items:
        row = dict(item)
        try:
            row["keywords_list"] = json.loads(row["keywords"] or "[]")
        except (TypeError, ValueError):
            row["keywords_list"] = []
        knowledge_items.append(row)
    return render_template("knowledge.html", knowledge_items=knowledge_items)


@app.route("/admin/knowledge/<int:item_id>/edit", methods=["POST"])
@staff_required
def edit_knowledge(item_id):
    question = request.form.get("question", "").strip()[:250]
    answer = request.form.get("answer", "").strip()[:10000]
    keywords_text = request.form.get("keywords", "")
    keywords = list(dict.fromkeys(
        word.strip().lower()[:80]
        for word in re.split(r"[,;\n]", keywords_text)
        if word.strip()
    ))[:30]
    is_active = 1 if request.form.get("is_active") == "1" else 0

    if not question or not answer or not keywords:
        flash("Вопрос, ответ и ключевые слова обязательны.", "error")
        return redirect(url_for("admin_knowledge"))

    with db() as con:
        result = con.execute(
            """UPDATE knowledge_base
               SET question=?, answer=?, keywords=?, is_active=?, updated_at=?
               WHERE id=?""",
            (question, answer, json.dumps(keywords, ensure_ascii=False), is_active, now(), item_id)
        )
        if result.rowcount == 0:
            abort(404)
    flash("Инструкция обновлена.", "success")
    return redirect(url_for("admin_knowledge"))


@app.route("/admin/knowledge/<int:item_id>/delete", methods=["POST"])
@staff_required
def delete_knowledge(item_id):
    with db() as con:
        result = con.execute("DELETE FROM knowledge_base WHERE id=?", (item_id,))
        if result.rowcount == 0:
            abort(404)
    flash("Инструкция удалена.", "success")
    return redirect(url_for("admin_knowledge"))


@app.route("/admin/stats")
@staff_required
def stats():
    with db() as con:
        by_category = con.execute(
            "SELECT category,COUNT(*) total FROM tickets GROUP BY category ORDER BY total DESC"
        ).fetchall()
        by_status = con.execute(
            "SELECT status,COUNT(*) total FROM tickets GROUP BY status ORDER BY total DESC"
        ).fetchall()
        by_location = con.execute("""
            SELECT COALESCE(l.zone_name,'Не указано') location,COUNT(*) total
            FROM tickets t LEFT JOIN locations l ON l.id=t.location_id
            GROUP BY location ORDER BY total DESC
        """).fetchall()
    return render_template("stats.html", by_category=by_category,
                           by_status=by_status, by_location=by_location)

@app.route("/admin/locations", methods=["GET", "POST"])
@staff_required
def admin_locations():
    """Управление помещениями и привязанными к ним QR-кодами."""
    if request.method == "POST":
        building = request.form.get("building", "").strip()[:120]
        floor = request.form.get("floor", "").strip()[:30]
        room = request.form.get("room", "").strip()[:50]
        zone_name = request.form.get("zone_name", "").strip()[:160]

        if not building or not (room or zone_name):
            flash("Укажите корпус и кабинет или название зоны.", "error")
            return redirect(url_for("admin_locations"))

        qr_key = "loc-" + secrets.token_urlsafe(12).replace("-", "").replace("_", "")
        with db() as con:
            con.execute(
                """INSERT INTO locations(building, floor, room, zone_name, qr_key)
                   VALUES (?, ?, ?, ?, ?)""",
                (building, floor, room, zone_name or (f"Кабинет {room}" if room else "Помещение"), qr_key)
            )
        flash("Помещение добавлено. Для него автоматически создан QR-код.", "success")
        return redirect(url_for("admin_locations"))

    with db() as con:
        locations = con.execute("""
            SELECT l.*, COUNT(t.id) AS tickets_count
            FROM locations l LEFT JOIN tickets t ON t.location_id=l.id
            GROUP BY l.id ORDER BY l.building, l.floor, l.room, l.zone_name
        """).fetchall()
    return render_template("locations.html", locations=locations, qr_version=secrets.token_hex(8))


@app.route("/admin/location/<int:location_id>/edit", methods=["POST"])
@staff_required
def edit_location(location_id):
    building = request.form.get("building", "").strip()[:120]
    floor = request.form.get("floor", "").strip()[:30]
    room = request.form.get("room", "").strip()[:50]
    zone_name = request.form.get("zone_name", "").strip()[:160]
    if not building or not (room or zone_name):
        flash("Укажите корпус и кабинет или название зоны.", "error")
        return redirect(url_for("admin_locations"))

    with db() as con:
        location = con.execute("SELECT id FROM locations WHERE id=?", (location_id,)).fetchone()
        if not location:
            abort(404)
        con.execute(
            "UPDATE locations SET building=?, floor=?, room=?, zone_name=? WHERE id=?",
            (building, floor, room, zone_name or (f"Кабинет {room}" if room else "Помещение"), location_id)
        )
    flash("Данные помещения обновлены. QR-код продолжает работать.", "success")
    return redirect(url_for("admin_locations"))


@app.route("/admin/location/<int:location_id>/delete", methods=["POST"])
@staff_required
def delete_location(location_id):
    with db() as con:
        location = con.execute("SELECT id FROM locations WHERE id=?", (location_id,)).fetchone()
        if not location:
            abort(404)
        linked = con.execute("SELECT COUNT(*) FROM tickets WHERE location_id=?", (location_id,)).fetchone()[0]
        if linked:
            flash(f"Нельзя удалить помещение: к нему привязано обращений — {linked}. Сначала переназначьте их.", "error")
            return redirect(url_for("admin_locations"))
        con.execute("DELETE FROM locations WHERE id=?", (location_id,))
    flash("Помещение удалено.", "success")
    return redirect(url_for("admin_locations"))


@app.route("/qr-new/<qr_key>")
def qr_image(qr_key):
   with db() as con:
       location = con.execute(
           "SELECT id FROM locations WHERE qr_key = ?",
           (qr_key,)
       ).fetchone()
   if not location:
       abort(404)
   target = f"{QR_BASE_URL}/?location={qr_key}"
   qr = qrcode.QRCode(
       version=None,
       error_correction=qrcode.constants.ERROR_CORRECT_M,
       box_size=10,
       border=4
   )
   qr.add_data(target)
   qr.make(fit=True)
   img = qr.make_image(
       fill_color="black",
       back_color="white"
   )
   output = io.BytesIO()
   img.save(output, format="PNG")
   output.seek(0)
   response = send_file(
       output,
       mimetype="image/png"
   )
   response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
   response.headers["Pragma"] = "no-cache"
   response.headers["Expires"] = "0"
   return response

@app.route("/uploads/<filename>")
@staff_required
def uploaded_file(filename):
    # Административный просмотр файлов после авторизации.
    from flask import send_from_directory
    return send_from_directory(UPLOAD_DIR, filename)


@app.route("/track/<token>/photo")
def tracked_ticket_photo(token):
    # Фото доступно только по персональному токену отслеживания заявки.
    from flask import send_from_directory

    with db() as con:
        tickets = con.execute(
            "SELECT tracking_token_hash, image_name FROM tickets WHERE image_name != ''"
        ).fetchall()

    ticket = next(
        (row for row in tickets if check_password_hash(row["tracking_token_hash"], token)),
        None
    )
    if not ticket:
        abort(404)

    return send_from_directory(UPLOAD_DIR, ticket["image_name"])

@app.errorhandler(413)
def too_large(_error):
    return "Файл слишком большой. Максимальный размер — 5 МБ.", 413

@app.errorhandler(400)
def bad_request(error):
    return render_template("error.html", code=400, message=getattr(error, "description", "Некорректный запрос")), 400

@app.errorhandler(404)
def not_found(_error):
    return render_template("error.html", code=404, message="Страница или заявка не найдена."), 404

init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=DEBUG)

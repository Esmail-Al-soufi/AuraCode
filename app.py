import os
import json
import uuid
import sqlite3
import time
from datetime import datetime, timedelta
from functools import wraps
from google import genai
from flask import Flask, request, render_template, redirect, url_for, flash, jsonify, session
from flask_mail import Mail, Message
from itsdangerous import URLSafeTimedSerializer
from flask_apscheduler import APScheduler
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash

# إنشاء التطبيق وتحديد المجلدات
app = Flask(__name__, template_folder='.', static_folder='static')

# مفتاح الجلسات، إعدادات الأمان، وحماية الكوكيز والرفع
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'a_very_secret_key_123')
app.config['JSON_AS_ASCII'] = False
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
if os.environ.get('FLASK_ENV') == 'production':
    app.config['SESSION_COOKIE_SECURE'] = True
app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024

# الامتدادات المسموحة لرفع الملفات
ALLOWED_IMAGE_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp', 'avif'}
ALLOWED_MEDIA_EXTENSIONS = ALLOWED_IMAGE_EXTENSIONS | {
    'mp4', 'avi', 'mov', 'webm', 'mkv',
    'mp3', 'wav', 'ogg', 'm4a',
    'pdf', 'txt', 'zip'
}

# كلمة سر صفحة المدير (عدّلها هنا أو عبر متغير البيئة ADMIN_PASSWORD)
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', '66EAEA@77')

# سعر كل 10 دقائق من الذكاء الاصطناعي بالنقاط (أقل مدة = 10 دقائق)
GEMINI_MIN_MINUTES = 10
GEMINI_COST_PER_10_MIN = int(os.environ.get('GEMINI_COST_PER_10_MIN', '10'))

# إعداد العميل الجديد لجوجل جيميناي من متغير البيئة
GEMINI_API_KEY = (os.environ.get('GEMINI_API_KEY') or '').strip()
client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

# إعداد المجدول (Scheduler)
scheduler = APScheduler()
if not app.debug or os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
    scheduler.init_app(app)
    scheduler.start()

# تحديد مسار قاعدة البيانات بدقة
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_NAME = os.path.join(BASE_DIR, 'alchemist_world.db')

# إعدادات البريد الإلكتروني
app.config['MAIL_SERVER'] = 'smtp.gmail.com'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USERNAME'] = os.environ.get('MAIL_USERNAME', 'alswfyasmayl30@gmail.com')
app.config['MAIL_PASSWORD'] = os.environ.get('MAIL_PASSWORD', 'ijuglmtdcamgxiex')

mail = Mail(app)
s = URLSafeTimedSerializer(app.config['SECRET_KEY'])


def _save_uploaded_file(file_storage, allowed_extensions, default_name):
    if not file_storage or not file_storage.filename:
        return default_name, None
    safe_name = secure_filename(file_storage.filename)
    if not safe_name or '.' not in safe_name:
        return None, "امتداد الملف غير مسموح به."
    ext = safe_name.rsplit('.', 1)[1].lower()
    if ext not in allowed_extensions:
        return None, "نوع الملف المرفوع غير مسموح به."
    unique_filename = f"{uuid.uuid4().hex}.{ext}"
    upload_dir = os.path.join(BASE_DIR, 'static')
    os.makedirs(upload_dir, exist_ok=True)
    file_storage.save(os.path.join(upload_dir, unique_filename))
    return unique_filename, None


# دالة حماية مسارات المدير على السيرفر
def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('is_admin'):
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return decorated_function


def get_db():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn


def _now_db_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _parse_db_dt(dt_val):
    if not dt_val:
        return None
    raw = str(dt_val).strip()
    if not raw or raw in ("لم يسجل", "لم يسجل دخول بعد", "متصل الآن / لم يخرج بعد"):
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _format_session_duration(login_val, logout_val):
    start_dt = _parse_db_dt(login_val)
    if not start_dt:
        return "غير محسوب"

    end_dt = _parse_db_dt(logout_val) or datetime.now()
    diff_seconds = max(0, int((end_dt - start_dt).total_seconds()))
    total_minutes = diff_seconds // 60
    hours = total_minutes // 60
    minutes = total_minutes % 60

    if hours > 0:
        return f"{hours} ساعة و {minutes} دقيقة"
    return f"{minutes} دقيقة"


def _set_user_logout_timestamp(nickname):
    if not nickname:
        return
    clean_nick = str(nickname).strip()
    if not clean_nick:
        return
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE users
        SET last_logout = ?
        WHERE TRIM(nickname) = TRIM(?)
    """, (_now_db_str(), clean_nick))
    conn.commit()
    conn.close()


def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fullname TEXT,
            nickname TEXT UNIQUE,
            email TEXT UNIQUE,
            phone TEXT,
            password TEXT,
            profile_pic TEXT DEFAULT 'default.png',
            balance INTEGER DEFAULT 100,
            debt INTEGER DEFAULT 0,
            farm_hours INTEGER DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            last_ip TEXT,
            last_login DATETIME,
            last_logout DATETIME
        )
    ''')

    for col_def in [
        "ALTER TABLE users ADD COLUMN created_at DATETIME DEFAULT CURRENT_TIMESTAMP",
        "ALTER TABLE users ADD COLUMN last_ip TEXT",
        "ALTER TABLE users ADD COLUMN last_login DATETIME",
        "ALTER TABLE users ADD COLUMN last_logout DATETIME"
    ]:
        try:
            cursor.execute(col_def)
        except sqlite3.OperationalError:
            pass

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT NOT NULL,
            subject TEXT NOT NULL,
            message TEXT NOT NULL,
            admin_reply TEXT,
            status TEXT DEFAULT 'جديدة'
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT,
            icon TEXT DEFAULT 'default_group.png',
            ad_text TEXT DEFAULT 'إعلان ترويجي للمجموعة'
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS group_questions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER,
            question_text TEXT NOT NULL,
            FOREIGN KEY (group_id) REFERENCES groups (id) ON DELETE CASCADE
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS group_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER,
            username TEXT,
            answers TEXT,
            status TEXT DEFAULT 'pending',
            FOREIGN KEY (group_id) REFERENCES groups (id) ON DELETE CASCADE
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS group_members (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER,
            username TEXT,
            FOREIGN KEY (group_id) REFERENCES groups (id) ON DELETE CASCADE
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS group_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER,
            sender TEXT,
            message TEXT,
            media_type TEXT DEFAULT 'text',
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS shop_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_name TEXT,
            price INTEGER,
            icon TEXT
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_purchases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT,
            item_id INTEGER,
            purchase_date DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS tech_news (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            content TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            title TEXT NOT NULL,
            message TEXT NOT NULL,
            is_read INTEGER DEFAULT 0,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS references_list (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER,
            title TEXT NOT NULL,
            description TEXT,
            link TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (group_id) REFERENCES groups (id) ON DELETE SET NULL
        )
    ''')

    try:
        cursor.execute("ALTER TABLE references_list ADD COLUMN group_id INTEGER")
    except sqlite3.OperationalError:
        pass

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS platform_ratings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT,
            stars INTEGER,
            feedback TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS welcome_settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            title TEXT NOT NULL,
            subtitle TEXT NOT NULL
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS welcome_notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT NOT NULL
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS surprise_quizzes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            is_active INTEGER DEFAULT 0,
            group_id INTEGER,
            duration_seconds INTEGER DEFAULT 120,
            reward_coins INTEGER DEFAULT 25,
            scheduled_time TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (group_id) REFERENCES groups (id) ON DELETE SET NULL
        )
    ''')

    try:
        cursor.execute("ALTER TABLE surprise_quizzes ADD COLUMN scheduled_time TEXT")
    except sqlite3.OperationalError:
        pass

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS surprise_quiz_questions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            quiz_id INTEGER NOT NULL,
            question_text TEXT NOT NULL,
            option_1 TEXT NOT NULL,
            option_2 TEXT NOT NULL,
            option_3 TEXT NOT NULL,
            option_4 TEXT NOT NULL,
            correct_option INTEGER DEFAULT 0,
            FOREIGN KEY (quiz_id) REFERENCES surprise_quizzes (id) ON DELETE CASCADE
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS surprise_quiz_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            quiz_id INTEGER NOT NULL,
            username TEXT NOT NULL,
            score INTEGER DEFAULT 0,
            total_questions INTEGER DEFAULT 0,
            answers_json TEXT,
            submitted_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (quiz_id) REFERENCES surprise_quizzes (id) ON DELETE CASCADE
        )
    ''')

    cursor.execute("SELECT COUNT(*) FROM welcome_settings WHERE id = 1")
    if cursor.fetchone()[0] == 0:
        cursor.execute('''
            INSERT INTO welcome_settings (id, title, subtitle)
            VALUES (1, 'أهلاً بك مجدداً في AuraCode! 🚀', 'استمر في كتابة الأكواد وتطوير مهاراتك التقنية في بيئة AuraCode، كل خطوة تقربك أكثر لهدفك التقني!')
        ''')

    cursor.execute("SELECT COUNT(*) FROM welcome_notifications")
    if cursor.fetchone()[0] == 0:
        cursor.execute(
            "INSERT INTO welcome_notifications (text) VALUES (?)",
            ("مرحباً بك في منصة AuraCode، نتمنى لك رحلة تعلم وتطوير مثمرة.",)
        )

    cursor.execute("SELECT COUNT(*) FROM groups")
    if cursor.fetchone()[0] == 0:
        cursor.execute("INSERT INTO groups (name, description, icon) VALUES ('مجموعة الأمن السيبراني', 'مجموعة الحماية والاختراق الأخلاقي في AuraCode', 'shield.png')")
        cursor.execute("INSERT INTO groups (name, description, icon) VALUES ('مجموعة تصميم ويب', 'بيئة تصميم وتشغيل صفحات الويب HTML/CSS/JS في AuraCode', 'web.png')")
        cursor.execute("INSERT INTO groups (name, description, icon) VALUES ('مجموعة بيثون', 'بيئة تطوير وتشغيل أكواد بايثون في AuraCode', 'python.png')")
        cursor.execute("INSERT INTO groups (name, description, icon) VALUES ('مجموعة سي بلس بلس', 'بيئة تطوير لغة C++ المتقدمة في AuraCode', 'cpp.png')")
        cursor.execute("INSERT INTO groups (name, description, icon) VALUES ('مجموعة سي شارب', 'بيئة عمل لغة C# وتطبيقات دوت نت في AuraCode', 'csharp.png')")
        cursor.execute("INSERT INTO groups (name, description, icon) VALUES ('مجموعة SQL', 'بيئة تصميم قواعد البيانات وإدارة الجداول في AuraCode', 'sql.png')")

        cursor.execute("INSERT INTO group_questions (group_id, question_text) VALUES (1, 'ما هي خبرتك في أدوات Kali Linux وأمن الشبكات؟')")
        cursor.execute("INSERT INTO group_questions (group_id, question_text) VALUES (2, 'ما هي خلفيتك في تصميم وتطوير واجهات HTML5 و JavaScript؟')")
        cursor.execute("INSERT INTO group_questions (group_id, question_text) VALUES (3, 'ما هي المكتبات التي تتعامل معها عادةً في لغة Python؟')")
        cursor.execute("INSERT INTO group_questions (group_id, question_text) VALUES (4, 'هل تجيد التعامل مع المؤشرات (Pointers) والهياكل في C++؟')")
        cursor.execute("INSERT INTO group_questions (group_id, question_text) VALUES (5, 'ما هي خبرتك في بناء تطبيقات سطح المكتب باستخدام C#؟')")
        cursor.execute("INSERT INTO group_questions (group_id, question_text) VALUES (6, 'كيف تصمم علاقة صحيحة بين الجداول (ERD) في قواعد بيانات SQL؟')")

        cursor.execute("INSERT INTO shop_items (item_name, price, icon) VALUES ('درع الحماية السيبرانية', 150, '🛡️')")
        cursor.execute("INSERT INTO shop_items (item_name, price, icon) VALUES ('معالج كمومي افتراضي', 300, '⚡')")

    conn.commit()
    conn.close()


init_db()


def _get_latest_quiz_payload(cursor, include_correct=False):
    cursor.execute("""
        SELECT q.id, q.title, q.is_active, q.group_id, COALESCE(g.name, 'جميع المستخدمين'),
               q.duration_seconds, q.reward_coins, q.scheduled_time, q.created_at
        FROM surprise_quizzes q
        LEFT JOIN groups g ON q.group_id = g.id
        ORDER BY q.id DESC LIMIT 1
    """)
    quiz_row = cursor.fetchone()
    if not quiz_row:
        return None

    quiz_id = quiz_row[0]
    cursor.execute("""
        SELECT id, question_text, option_1, option_2, option_3, option_4, correct_option
        FROM surprise_quiz_questions
        WHERE quiz_id = ?
        ORDER BY id ASC
    """, (quiz_id,))
    q_rows = cursor.fetchall()

    questions_payload = []
    for qr in q_rows:
        item = {
            "id": qr[0],
            "question_text": qr[1],
            "options": [qr[2], qr[3], qr[4], qr[5]]
        }
        if include_correct:
            item["correct_option"] = qr[6]
            item["correct_index"] = qr[6]
        questions_payload.append(item)

    is_active_bool = bool(quiz_row[2])
    return {
        "id": quiz_row[0],
        "title": quiz_row[1],
        "active": is_active_bool,
        "is_active": is_active_bool,
        "group_id": quiz_row[3],
        "group_name": quiz_row[4],
        "duration_seconds": quiz_row[5] or 120,
        "reward_coins": quiz_row[6] or 0,
        "scheduled_time": quiz_row[7],
        "created_at": quiz_row[8],
        "questions": questions_payload
    }


def fetch_welcome_payload():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT title, subtitle FROM welcome_settings WHERE id = 1")
    row = cursor.fetchone()
    cursor.execute("SELECT id, text FROM welcome_notifications ORDER BY id ASC")
    notifs = [{"id": r[0], "text": r[1]} for r in cursor.fetchall()]
    conn.close()

    title = row[0] if (row and row[0]) else "أهلاً بك مجدداً في AuraCode! 🚀"
    subtitle = row[1] if (row and row[1]) else "استمر في كتابة الأكواد وتطوير مهاراتك التقنية في بيئة AuraCode، كل خطوة تقربك أكثر لهدفك التقني!"
    return {
        "title": title,
        "subtitle": subtitle,
        "welcomeNotifications": notifs
    }


def _resolve_back_target():
    source = (
        request.args.get("from")
        or request.form.get("from")
        or ""
    ).strip().lower()
    req_nickname = (
        request.args.get("nickname")
        or request.form.get("nickname")
        or ""
    ).strip()
    sess_user = (session.get("user") or "").strip()
    referer = (request.headers.get("Referer") or "").lower()

    if source == "login":
        return "login", None

    if source == "dashboard" or "/dashboard" in referer:
        target_nick = req_nickname or sess_user
        return "dashboard", target_nick if target_nick else None

    if sess_user and "/login" not in referer:
        return "dashboard", sess_user

    return "login", None


# --- المسارات والمتحكمات (Routes) ---

@app.route('/')
def home():
    return render_template('index.html')


@app.route('/api/welcome_settings', methods=['GET'])
def api_welcome_settings():
    return jsonify(fetch_welcome_payload()), 200


@app.route('/api/admin/welcome_settings', methods=['POST'])
@admin_required
def api_admin_welcome_settings():
    data = request.get_json(silent=True) or {}
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    cursor.execute("SELECT title, subtitle FROM welcome_settings WHERE id = 1")
    current = cursor.fetchone()
    current_title = current[0] if current else "أهلاً بك مجدداً في AuraCode! 🚀"
    current_subtitle = current[1] if current else "استمر في كتابة الأكواد وتطوير مهاراتك التقنية في بيئة AuraCode، كل خطوة تقربك أكثر لهدفك التقني!"

    new_title = str(data.get('title')).strip() if 'title' in data and str(data.get('title')).strip() else current_title
    new_subtitle = str(data.get('subtitle')).strip() if 'subtitle' in data and str(data.get('subtitle')).strip() else current_subtitle

    cursor.execute('''
        INSERT INTO welcome_settings (id, title, subtitle)
        VALUES (1, ?, ?)
        ON CONFLICT(id) DO UPDATE SET title = excluded.title, subtitle = excluded.subtitle
    ''', (new_title, new_subtitle))

    if 'welcomeNotifications' in data and isinstance(data.get('welcomeNotifications'), list):
        cursor.execute("DELETE FROM welcome_notifications")
        for item in data['welcomeNotifications']:
            text_val = ""
            if isinstance(item, dict):
                text_val = str(item.get('text', '')).strip()
            elif isinstance(item, str):
                text_val = item.strip()
            if text_val:
                cursor.execute("INSERT INTO welcome_notifications (text) VALUES (?)", (text_val,))

    conn.commit()
    conn.close()

    return jsonify({
        "status": "success",
        "message": "تم حفظ إعدادات الترحيب والإشعارات الترحيبية بنجاح في AuraCode",
        "data": fetch_welcome_payload()
    }), 200


# --- مسارات أسئلة المقابلة والقبول في المجموعات ---

@app.route('/api/group_questions', methods=['GET'])
def api_get_group_questions():
    raw_gid = (request.args.get('group_id') or '').strip()
    if not raw_gid.isdigit():
        return jsonify({"status": "error", "message": "معرف المجموعة غير صالح", "questions": []}), 400

    group_id = int(raw_gid)
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT id, question_text FROM group_questions WHERE group_id = ? ORDER BY id ASC", (group_id,))
    rows = cursor.fetchall()
    conn.close()

    questions = [{"id": r[0], "text": r[1], "question_text": r[1]} for r in rows]
    return jsonify({"status": "success", "group_id": group_id, "questions": questions}), 200


@app.route('/api/admin/group_questions', methods=['GET', 'POST'])
@admin_required
def api_admin_group_questions():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    if request.method == 'GET':
        raw_gid = (request.args.get('group_id') or '').strip()
        if not raw_gid.isdigit():
            conn.close()
            return jsonify({"status": "error", "message": "معرف المجموعة مطلوب", "questions": []}), 400

        group_id = int(raw_gid)
        cursor.execute("SELECT id, question_text FROM group_questions WHERE group_id = ? ORDER BY id ASC", (group_id,))
        rows = cursor.fetchall()
        conn.close()

        questions = [{"id": r[0], "text": r[1], "question_text": r[1]} for r in rows]
        return jsonify({"status": "success", "group_id": group_id, "questions": questions}), 200

    data = request.get_json(silent=True) or {}
    raw_gid = str(data.get('group_id') or '').strip()
    if not raw_gid.isdigit():
        conn.close()
        return jsonify({"status": "error", "message": "يرجى اختيار مجموعة صالحة"}), 400

    group_id = int(raw_gid)
    raw_questions = data.get('questions')
    if not isinstance(raw_questions, list):
        conn.close()
        return jsonify({"status": "error", "message": "قائمة الأسئلة غير صالحة"}), 400

    cleaned_texts = []
    for item in raw_questions:
        if isinstance(item, str):
            q_str = item.strip()
        elif isinstance(item, dict):
            q_str = str(item.get('text') or item.get('question_text') or '').strip()
        else:
            q_str = ""
        if q_str:
            cleaned_texts.append(q_str)

    cursor.execute("DELETE FROM group_questions WHERE group_id = ?", (group_id,))
    for q_str in cleaned_texts:
        cursor.execute(
            "INSERT INTO group_questions (group_id, question_text) VALUES (?, ?)",
            (group_id, q_str)
        )

    conn.commit()
    cursor.execute("SELECT id, question_text FROM group_questions WHERE group_id = ? ORDER BY id ASC", (group_id,))
    saved_rows = cursor.fetchall()
    conn.close()

    saved_questions = [{"id": r[0], "text": r[1], "question_text": r[1]} for r in saved_rows]
    return jsonify({
        "status": "success",
        "message": "تم حفظ وتحديث أسئلة المقابلة للمجموعة بنجاح.",
        "group_id": group_id,
        "questions": saved_questions
    }), 200


# --- مسارات الاختبار / الكويز المفاجئ (للمستخدم وللمدير) ---

@app.route('/api/surprise_quiz', methods=['GET'])
def api_get_surprise_quiz():
    user_nickname = (session.get('user') or request.args.get('nickname') or '').strip()
    if not user_nickname:
        return jsonify({"active": False, "is_active": False, "quiz": None}), 200

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    quiz = _get_latest_quiz_payload(cursor, include_correct=False)

    if not quiz or not quiz["is_active"] or not quiz["questions"]:
        conn.close()
        return jsonify({"active": False, "is_active": False, "quiz": None}), 200

    if quiz["group_id"] is not None:
        cursor.execute("""
            SELECT 1 FROM group_members
            WHERE group_id = ? AND TRIM(username) = TRIM(?)
        """, (quiz["group_id"], user_nickname))
        if not cursor.fetchone():
            conn.close()
            return jsonify({"active": False, "is_active": False, "quiz": None}), 200

    cursor.execute("""
        SELECT id FROM surprise_quiz_attempts
        WHERE quiz_id = ? AND TRIM(username) = TRIM(?)
    """, (quiz["id"], user_nickname))
    already_done = cursor.fetchone()
    conn.close()

    if already_done:
        return jsonify({"active": False, "is_active": False, "already_completed": True, "quiz": None}), 200

    return jsonify({"active": True, "is_active": True, "quiz": quiz}), 200


@app.route('/api/surprise_quiz/submit', methods=['POST'])
def api_submit_surprise_quiz():
    user_nickname = (session.get('user') or '').strip()
    if not user_nickname:
        return jsonify({"status": "error", "message": "يرجى تسجيل الدخول أولاً"}), 401

    data = request.get_json(silent=True) or {}
    answers_map = data.get("answers") or {}

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    quiz = _get_latest_quiz_payload(cursor, include_correct=True)

    if not quiz or not quiz["is_active"]:
        conn.close()
        return jsonify({"status": "error", "message": "لا يوجد اختبار مفاجئ نشط حالياً"}), 400

    if quiz["group_id"] is not None:
        cursor.execute("""
            SELECT 1 FROM group_members
            WHERE group_id = ? AND TRIM(username) = TRIM(?)
        """, (quiz["group_id"], user_nickname))
        if not cursor.fetchone():
            conn.close()
            return jsonify({"status": "error", "message": "هذا الاختبار مخصص لأعضاء مجموعة محددة فقط"}), 403

    cursor.execute("""
        SELECT id, score, total_questions FROM surprise_quiz_attempts
        WHERE quiz_id = ? AND TRIM(username) = TRIM(?)
    """, (quiz["id"], user_nickname))
    existing = cursor.fetchone()
    if existing:
        conn.close()
        return jsonify({
            "status": "success",
            "message": "تم تسجيل إجابتك لهذا الاختبار مسبقاً.",
            "score": existing[1],
            "total": existing[2],
            "earned_coins": 0
        }), 200

    score = 0
    total = len(quiz["questions"])
    for q in quiz["questions"]:
        q_key = str(q["id"])
        chosen = answers_map.get(q_key, answers_map.get(q["id"]))
        try:
            chosen_int = int(chosen)
        except (ValueError, TypeError):
            chosen_int = -1
        if chosen_int == int(q["correct_option"]):
            score += 1

    earned_coins = int(quiz["reward_coins"] or 0) if (total > 0 and score == total) else (
        int((quiz["reward_coins"] or 0) * (score / total)) if total > 0 else 0
    )

    cursor.execute("""
        INSERT INTO surprise_quiz_attempts (quiz_id, username, score, total_questions, answers_json, submitted_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (quiz["id"], user_nickname, score, total, json.dumps(answers_map, ensure_ascii=False), _now_db_str()))

    if earned_coins > 0:
        cursor.execute("""
            UPDATE users SET balance = COALESCE(balance, 0) + ?
            WHERE TRIM(nickname) = TRIM(?)
        """, (earned_coins, user_nickname))

    conn.commit()
    conn.close()

    return jsonify({
        "status": "success",
        "message": f"تم تسليم الاختبار المفاجئ! درجتك: {score} من {total}",
        "score": score,
        "total": total,
        "earned_coins": earned_coins
    }), 200


@app.route('/api/admin/surprise_quiz', methods=['GET', 'POST'])
@admin_required
def api_admin_surprise_quiz():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    if request.method == 'GET':
        quiz = _get_latest_quiz_payload(cursor, include_correct=True)
        conn.close()
        return jsonify({"status": "success", "quiz": quiz}), 200

    data = request.get_json(silent=True) or {}
    title = str(data.get("title") or "اختبار مفاجئ سريع").strip()
    raw_active = data.get("active") if "active" in data else data.get("is_active")
    is_active = 1 if bool(raw_active) else 0
    raw_group = data.get("group_id")
    group_id = int(raw_group) if str(raw_group).isdigit() else None

    scheduled_raw = data.get("scheduled_time")
    scheduled_time = str(scheduled_raw).strip() if scheduled_raw else None

    try:
        duration_seconds = max(15, min(3600, int(data.get("duration_seconds", 120))))
    except (ValueError, TypeError):
        duration_seconds = 120

    try:
        reward_coins = max(0, min(1000, int(data.get("reward_coins", 25))))
    except (ValueError, TypeError):
        reward_coins = 25

    raw_questions = data.get("questions") or []
    valid_questions = []
    for item in raw_questions:
        if not isinstance(item, dict):
            continue
        q_text = str(item.get("question_text") or "").strip()
        opts = item.get("options") or []
        if not isinstance(opts, list) or len(opts) < 2:
            opts = [
                str(item.get("option_1") or "").strip(),
                str(item.get("option_2") or "").strip(),
                str(item.get("option_3") or "").strip(),
                str(item.get("option_4") or "").strip()
            ]
        clean_opts = [str(o or "").strip() for o in opts[:4]]
        while len(clean_opts) < 4:
            clean_opts.append(f"خيار {len(clean_opts) + 1}")
        clean_opts = [o if o else f"خيار {idx + 1}" for idx, o in enumerate(clean_opts)]

        raw_correct = item.get("correct_index") if "correct_index" in item else item.get("correct_option", 0)
        try:
            correct_opt = int(raw_correct)
        except (ValueError, TypeError):
            correct_opt = 0
        correct_opt = max(0, min(3, correct_opt))

        if q_text:
            valid_questions.append((q_text, clean_opts[0], clean_opts[1], clean_opts[2], clean_opts[3], correct_opt))

    cursor.execute("""
        INSERT INTO surprise_quizzes (title, is_active, group_id, duration_seconds, reward_coins, scheduled_time, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (title, is_active, group_id, duration_seconds, reward_coins, scheduled_time, _now_db_str()))
    new_quiz_id = cursor.lastrowid

    for q_tuple in valid_questions:
        cursor.execute("""
            INSERT INTO surprise_quiz_questions
                (quiz_id, question_text, option_1, option_2, option_3, option_4, correct_option)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (new_quiz_id, *q_tuple))

    conn.commit()
    quiz_payload = _get_latest_quiz_payload(cursor, include_correct=True)
    conn.close()

    return jsonify({
        "status": "success",
        "message": "تم حفظ إعدادات الكويز المفاجئ بنجاح.",
        "quiz": quiz_payload
    }), 200


@app.route('/api/admin/surprise_quiz/toggle', methods=['POST'])
@admin_required
def api_admin_toggle_surprise_quiz():
    data = request.get_json(silent=True) or {}
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    cursor.execute("SELECT id, is_active FROM surprise_quizzes ORDER BY id DESC LIMIT 1")
    row = cursor.fetchone()
    if not row:
        conn.close()
        return jsonify({"status": "error", "message": "لا يوجد كويز مفاجئ محفوظ بعد."}), 404

    quiz_id, current_active = row
    if "active" in data:
        new_active = 1 if bool(data.get("active")) else 0
    elif "is_active" in data:
        new_active = 1 if bool(data.get("is_active")) else 0
    else:
        new_active = 0 if current_active else 1

    cursor.execute("UPDATE surprise_quizzes SET is_active = ? WHERE id = ?", (new_active, quiz_id))
    conn.commit()
    quiz_payload = _get_latest_quiz_payload(cursor, include_correct=True)
    conn.close()

    status_label = "تم تفعيل الكويز المفاجئ للمستخدمين ✅" if new_active else "تم إيقاف الكويز المفاجئ ⏸️"
    return jsonify({"status": "success", "message": status_label, "quiz": quiz_payload}), 200


@app.route('/api/admin/surprise_quiz/results', methods=['GET'])
@admin_required
def api_admin_surprise_quiz_results():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT a.id, a.username, sq.title, a.score, a.total_questions, a.submitted_at
        FROM surprise_quiz_attempts a
        LEFT JOIN surprise_quizzes sq ON a.quiz_id = sq.id
        ORDER BY a.id DESC
    """)
    rows = cursor.fetchall()
    conn.close()

    results = [
        {
            "id": r[0],
            "username": r[1],
            "quiz_title": r[2] or "اختبار مفاجئ",
            "score": r[3],
            "total": r[4],
            "submitted_at": r[5]
        }
        for r in rows
    ]
    return jsonify({"status": "success", "results": results}), 200


@app.route('/api/track_logout', methods=['POST'])
def api_track_logout():
    data = request.get_json(silent=True) or {}
    sess_user = (session.get('user') or '').strip()
    req_user = str(data.get('nickname') or '').strip()
    target_user = sess_user or req_user

    if target_user:
        _set_user_logout_timestamp(target_user)
        return jsonify({"status": "ok", "logged_out_at": _now_db_str()}), 200

    return jsonify({"status": "ignored"}), 200


@app.route('/api/admin/users_activity', methods=['GET'])
@admin_required
def api_admin_users_activity():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT id, fullname, nickname, created_at, COALESCE(farm_hours, 0),
                   COALESCE(last_ip, 'غير محدد'), last_login, last_logout
            FROM users
            ORDER BY id DESC
        """)
    except sqlite3.OperationalError:
        cursor.execute("""
            SELECT id, fullname, nickname, 'تسجيل سابق', COALESCE(farm_hours, 0),
                   'غير محدد', NULL, NULL
            FROM users
            ORDER BY id DESC
        """)
    rows = cursor.fetchall()
    conn.close()

    users_payload = []
    for r in rows:
        last_login = r[6]
        last_logout = r[7]
        users_payload.append({
            "id": r[0],
            "fullname": r[1] or r[2] or "",
            "nickname": r[2] or "",
            "created_at": r[3],
            "farm_hours": r[4] if r[4] is not None else 0,
            "ip_address": r[5] or "غير محدد",
            "last_login": last_login or "لم يسجل دخول بعد",
            "last_logout": last_logout or "متصل الآن / لم يخرج بعد",
            "session_duration": _format_session_duration(last_login, last_logout)
        })

    return jsonify({"status": "success", "users": users_payload}), 200


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('nickname') or request.form.get('username')
        password = request.form.get('password')

        if not username or not password:
            return jsonify({'status': 'error', 'message': 'يرجى إدخال اسم المستخدم أو اللقب وكلمة المرور'})

        username = username.strip()
        password = password.strip()

        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()

        cursor.execute("""
            SELECT nickname, password FROM users 
            WHERE TRIM(nickname) = TRIM(?) OR TRIM(fullname) = TRIM(?)
        """, (username, username))
        user = cursor.fetchone()

        if user:
            db_nickname = user[0]
            db_password = str(user[1]).strip()

            is_valid_password = False
            if check_password_hash(db_password, password) or db_password == password:
                is_valid_password = True

            if is_valid_password:
                session['user'] = db_nickname

                user_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
                now_str = _now_db_str()
                cursor.execute("""
                    UPDATE users 
                    SET last_ip = ?, last_login = ?, last_logout = NULL 
                    WHERE TRIM(nickname) = TRIM(?)
                """, (user_ip, now_str, db_nickname))
                conn.commit()
                conn.close()

                return jsonify({
                    'status': 'success',
                    'redirect': url_for('dashboard', nickname=db_nickname)
                })

        conn.close()
        return jsonify({'status': 'error', 'message': 'اسم المستخدم أو اللقب أو كلمة المرور غير صحيحة'})

    return render_template('index.html')


@app.route('/logout')
def logout():
    user_nickname = session.get('user')
    if user_nickname:
        _set_user_logout_timestamp(user_nickname)
        session.pop('user', None)

    return redirect(url_for('login'))


@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        fullname = (request.form.get('fullname') or '').strip()
        nickname = (request.form.get('nickname') or '').strip()
        email = (request.form.get('email') or '').strip()
        phone = (request.form.get('phone') or '').strip()
        password = (request.form.get('password') or '').strip()
        confirm_password = (request.form.get('confirm_password') or '').strip()
        file = request.files.get('profile_pic')

        if not nickname or not password or not email:
            return "يرجى ملء جميع الحقول المطلوبة.", 400

        if password != confirm_password:
            return "كلمة المرور وتأكيد كلمة المرور غير متطابقين.", 400

        hashed_password = generate_password_hash(password)

        filename, upload_err = _save_uploaded_file(file, ALLOWED_IMAGE_EXTENSIONS, 'default.png')
        if upload_err:
            return upload_err, 400

        conn = sqlite3.connect(DB_NAME)
        try:
            cursor = conn.cursor()
            user_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
            now_str = _now_db_str()
            cursor.execute("""
                INSERT INTO users (fullname, nickname, email, phone, password, profile_pic, last_ip, last_login) 
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (fullname, nickname, email, phone, hashed_password, filename, user_ip, now_str))
            conn.commit()
            return redirect(url_for('login'))
        except sqlite3.IntegrityError:
            return "اللقب أو البريد الإلكتروني مسجل بالفعل.", 400
        finally:
            conn.close()

    return render_template('register.html')


@app.route('/dashboard')
def dashboard():
    raw_nickname = request.args.get('nickname') or session.get('user') or "مستخدم"
    user_nickname = raw_nickname.strip()

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    cursor.execute("SELECT profile_pic, fullname, balance, farm_hours FROM users WHERE TRIM(nickname) = TRIM(?)", (user_nickname,))
    result = cursor.fetchone()

    cursor.execute("SELECT title, content FROM tech_news ORDER BY id DESC LIMIT 3")
    recent_news = cursor.fetchall()
    conn.close()

    user_image = result[0] if (result and result[0]) else 'default.png'
    user_fullname = result[1] if (result and result[1]) else user_nickname
    user_balance = result[2] if result else 100
    user_farm_hours = result[3] if result else 0

    return render_template(
        'dashboard.html',
        user_nickname=user_nickname,
        user_fullname=user_fullname,
        user_image=user_image,
        user_balance=user_balance,
        user_farm_hours=user_farm_hours,
        news=recent_news
    )


@app.route('/update_profile', methods=['POST'])
def update_profile():
    old_nickname = session.get('user')
    if not old_nickname:
        return redirect(url_for('login'))

    old_nickname = old_nickname.strip()

    fullname = request.form.get('fullname')
    new_nickname = request.form.get('nickname')
    email = request.form.get('email')
    phone = request.form.get('phone')
    new_password = request.form.get('new_password')
    file = request.files.get('profile_pic')

    conn = sqlite3.connect(DB_NAME)
    try:
        cursor = conn.cursor()

        cursor.execute("SELECT fullname, email, phone, profile_pic FROM users WHERE TRIM(nickname)=TRIM(?)", (old_nickname,))
        user_data = cursor.fetchone()

        if user_data:
            fullname = fullname.strip() if fullname and fullname.strip() else user_data[0]
            email = email.strip() if email and email.strip() else user_data[1]
            phone = phone.strip() if phone and phone.strip() else user_data[2]
            current_pic = user_data[3]
        else:
            current_pic = 'default.png'

        final_nickname = new_nickname.strip() if new_nickname and new_nickname.strip() else old_nickname

        saved_pic, upload_err = _save_uploaded_file(file, ALLOWED_IMAGE_EXTENSIONS, None)
        if upload_err:
            return upload_err, 400
        if saved_pic:
            current_pic = saved_pic

        if new_password and new_password.strip():
            hashed_password = generate_password_hash(new_password.strip())
            cursor.execute("""
                UPDATE users 
                SET fullname=?, nickname=?, email=?, phone=?, password=?, profile_pic=? 
                WHERE TRIM(nickname)=TRIM(?)
            """, (fullname, final_nickname, email, phone, hashed_password, current_pic, old_nickname))
        else:
            cursor.execute("""
                UPDATE users 
                SET fullname=?, nickname=?, email=?, phone=?, profile_pic=? 
                WHERE TRIM(nickname)=TRIM(?)
            """, (fullname, final_nickname, email, phone, current_pic, old_nickname))

        conn.commit()
        session['user'] = final_nickname
        return redirect(url_for('dashboard', nickname=final_nickname))
    except sqlite3.IntegrityError:
        return "اللقب أو البريد الإلكتروني مستخدم من قِبل حساب آخر.", 400
    finally:
        conn.close()


@app.route('/forgot_password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        email = request.form.get('email')
        conn = sqlite3.connect(DB_NAME)
        user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        conn.close()

        if user:
            token = s.dumps(email, salt='recover-password')
            msg = Message("استعادة كلمة المرور - AuraCode", sender=app.config['MAIL_USERNAME'], recipients=[email])
            link = url_for('reset_password', token=token, _external=True)
            msg.body = f"مرحباً، اضغط على الرابط التالي لإعادة تعيين كلمة المرور الخاصة بك في AuraCode: {link}"
            try:
                mail.send(msg)
                return render_template('success.html', token=token)
            except Exception as e:
                return f"حدث خطأ أثناء إرسال البريد: {e}"
        else:
            return "هذا البريد الإلكتروني غير مسجل."
    return render_template('forgot_password.html')


@app.route('/reset_password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    try:
        email = s.loads(token, salt='recover-password', max_age=900)
    except Exception:
        return "رابط غير صالح أو منتهي الصلاحية.", 400

    if request.method == 'POST':
        new_password = (request.form.get('password') or '').strip()
        confirm_password = (request.form.get('confirm_password') or '').strip()

        if not new_password:
            return "يرجى إدخال كلمة المرور الجديدة.", 400

        if new_password != confirm_password:
            return "كلمة المرور الجديدة وتأكيدها غير متطابقين.", 400

        hashed_password = generate_password_hash(new_password)
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET password = ? WHERE email = ?", (hashed_password, email))
        conn.commit()
        conn.close()
        return redirect(url_for('login'))
    return render_template('reset_password.html', token=token)


@app.route('/commun', methods=['GET', 'POST'])
def commun():
    back_target, back_nickname = _resolve_back_target()
    if request.method == 'POST':
        sender_name = (request.form.get('name') or '').strip()
        sender_email = (request.form.get('email') or '').strip()
        sender_message = (request.form.get('message') or '').strip()
        if sender_name and sender_email and sender_message:
            conn = sqlite3.connect(DB_NAME)
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO messages (name, email, subject, message) VALUES (?, ?, ?, ?)",
                (sender_name, sender_email, "تواصل معنا - AuraCode", sender_message)
            )
            conn.commit()
            conn.close()
            flash("تم إرسال رسالتك بنجاح، شكراً لتواصلك مع AuraCode.", "success")
        else:
            flash("يرجى تعبئة جميع الحقول المطلوبة قبل الإرسال.", "error")

        if back_target == 'dashboard' and back_nickname:
            return redirect(url_for('commun', **{'from': 'dashboard', 'nickname': back_nickname}))
        if back_target == 'dashboard':
            return redirect(url_for('commun', **{'from': 'dashboard'}))
        return redirect(url_for('commun', **{'from': 'login'}))

    return render_template('commun.html', back_target=back_target, back_nickname=back_nickname)


@app.route('/about', methods=['GET'])
def about():
    back_target, back_nickname = _resolve_back_target()
    return render_template('about.html', back_target=back_target, back_nickname=back_nickname)


# --- مسار API لجلب جميع المجموعات المحدثة للواجهة الرئيسية ---
@app.route('/api/groups')
def api_get_groups():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT id, name, description, icon FROM groups ORDER BY id ASC")
    rows = cursor.fetchall()
    conn.close()
    return jsonify([
        {"id": r[0], "name": r[1], "description": r[2], "icon": r[3]}
        for r in rows
    ])


# --- مسار API لجلب المراجع لتبويب المراجع ---
@app.route('/api/references', methods=['GET'])
def api_get_references():
    group_id = (request.args.get('group_id') or '').strip()
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    if group_id.isdigit():
        cursor.execute("""
            SELECT r.id, r.group_id, COALESCE(g.name, 'عام / جميع المجموعات') AS group_name,
                   r.title, r.description, r.link, r.timestamp
            FROM references_list r
            LEFT JOIN groups g ON r.group_id = g.id
            WHERE r.group_id = ? OR r.group_id IS NULL
            ORDER BY r.id DESC
        """, (int(group_id),))
    else:
        cursor.execute("""
            SELECT r.id, r.group_id, COALESCE(g.name, 'عام / جميع المجموعات') AS group_name,
                   r.title, r.description, r.link, r.timestamp
            FROM references_list r
            LEFT JOIN groups g ON r.group_id = g.id
            ORDER BY r.id DESC
        """)
    rows = cursor.fetchall()
    conn.close()
    return jsonify([
        {
            "id": row[0],
            "group_id": row[1],
            "group_name": row[2],
            "title": row[3],
            "description": row[4],
            "link": row[5],
            "timestamp": row[6]
        }
        for row in rows
    ])


# --- إدارة المجموعات والدردشة ---

@app.route('/groups')
def view_groups():
    user_nickname = session.get('user', 'مستخدم')
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    cursor.execute("SELECT profile_pic, fullname FROM users WHERE TRIM(nickname) = TRIM(?)", (user_nickname,))
    user_data = cursor.fetchone()
    user_image = user_data[0] if (user_data and user_data[0]) else 'default.png'
    user_fullname = user_data[1] if (user_data and user_data[1]) else user_nickname

    cursor.execute("SELECT * FROM groups")
    groups = cursor.fetchall()
    conn.close()

    return render_template(
        'groups.html',
        groups=groups,
        user_nickname=user_nickname,
        user_fullname=user_fullname,
        user_image=user_image
    )


@app.route('/group/<int:group_id>/join', methods=['GET', 'POST'])
def join_group(group_id):
    user_nickname = session.get('user')
    if not user_nickname:
        return redirect(url_for('login'))

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    cursor.execute("SELECT profile_pic, fullname FROM users WHERE TRIM(nickname) = TRIM(?)", (user_nickname,))
    user_data = cursor.fetchone()
    user_image = user_data[0] if (user_data and user_data[0]) else 'default.png'
    user_fullname = user_data[1] if (user_data and user_data[1]) else user_nickname

    cursor.execute("SELECT * FROM groups WHERE id = ?", (group_id,))
    group = cursor.fetchone()

    cursor.execute("SELECT * FROM group_questions WHERE group_id = ? ORDER BY id ASC", (group_id,))
    questions = cursor.fetchall()

    if request.method == 'POST':
        answers_list = []
        for q in questions:
            ans = request.form.get(f'question_{q[0]}')
            if ans:
                answers_list.append(f"{q[2]}: {ans}")

        answers_str = " | ".join(answers_list) if answers_list else "انضمام مباشر بدون أسئلة"

        cursor.execute("SELECT id FROM group_requests WHERE group_id = ? AND username = ? AND status = 'pending'", (group_id, user_nickname))
        existing_req = cursor.fetchone()

        if not existing_req:
            cursor.execute(
                "INSERT INTO group_requests (group_id, username, answers, status) VALUES (?, ?, ?, 'pending')",
                (group_id, user_nickname, answers_str)
            )
            conn.commit()
            flash("تم تقديم طلب انضمامك للمجموعة بنجاح، بانتظار موافقة المشرف.", "success")
        else:
            flash("لديك طلب انضمام قيد الانتظار لهذه المجموعة مسبقاً.", "warning")

        conn.close()
        return redirect(url_for('view_groups'))

    conn.close()
    return render_template(
        'join_group.html',
        group=group,
        questions=questions,
        user_nickname=user_nickname,
        user_fullname=user_fullname,
        user_image=user_image
    )


@app.route('/group/<int:group_id>/chat')
def group_chat(group_id):
    user_nickname = session.get('user')
    if not user_nickname:
        return redirect(url_for('login'))

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM groups WHERE id=?", (group_id,))
    group = cursor.fetchone()
    group_name = group[0] if group else f"المجموعة {group_id}"

    cursor.execute("""
        SELECT m.sender, m.message, COALESCE(u.profile_pic, 'default.png'), m.media_type 
        FROM group_messages m
        LEFT JOIN users u ON TRIM(m.sender) = TRIM(u.nickname)
        WHERE m.group_id=? 
        ORDER BY m.id ASC
    """, (group_id,))
    messages = cursor.fetchall()
    conn.close()

    return render_template('chat.html', group_id=group_id, group_name=group_name, user_nickname=user_nickname, messages=messages)


@app.route('/send_message', methods=['POST'])
def send_message():
    user_nickname = session.get('user')
    if not user_nickname:
        return jsonify({'status': 'error', 'message': 'يرجى تسجيل الدخول'}), 401

    group_id = request.form.get('group_id')
    message = request.form.get('message', '')
    media_file = request.files.get('media_file')

    media_type = 'text'
    has_media = False
    if media_file and media_file.filename:
        saved_name, upload_err = _save_uploaded_file(media_file, ALLOWED_MEDIA_EXTENSIONS, None)
        if upload_err:
            return jsonify({'status': 'error', 'message': upload_err}), 400
        if saved_name:
            has_media = True
            message = saved_name
            ext = saved_name.rsplit('.', 1)[1].lower()
            if ext in ALLOWED_IMAGE_EXTENSIONS:
                media_type = 'image'
            elif ext in ('mp4', 'avi', 'mov', 'webm', 'mkv'):
                media_type = 'video'
            elif ext in ('mp3', 'wav', 'ogg', 'm4a'):
                media_type = 'audio'
            else:
                media_type = 'file'

    if group_id and (message or has_media):
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO group_messages (group_id, sender, message, media_type) VALUES (?, ?, ?, ?)",
            (group_id, user_nickname, message, media_type)
        )
        conn.commit()
        conn.close()
        return jsonify({'status': 'success'})

    return jsonify({'status': 'error', 'message': 'بيانات غير كافية'}), 400


@app.route('/api/gemini_session', methods=['POST'])
def gemini_session():
    user_nickname = session.get('user')
    if not user_nickname:
        return jsonify({'status': 'error', 'message': 'يرجى تسجيل الدخول أولاً'}), 401

    data = request.get_json(silent=True) or {}
    try:
        minutes = int(data.get('minutes', GEMINI_MIN_MINUTES))
    except (ValueError, TypeError):
        minutes = GEMINI_MIN_MINUTES

    if minutes < GEMINI_MIN_MINUTES or minutes > 480:
        return jsonify({
            'status': 'error',
            'message': f'أقل مدة متاحة لجلسة الذكاء الاصطناعي هي {GEMINI_MIN_MINUTES} دقائق (حتى 480 دقيقة)'
        }), 400

    blocks_of_10 = max(1, (minutes + 9) // 10)
    cost = blocks_of_10 * GEMINI_COST_PER_10_MIN

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT COALESCE(balance, 0) FROM users WHERE TRIM(nickname) = TRIM(?)", (user_nickname,))
    user_row = cursor.fetchone()
    current_balance = int(user_row[0]) if user_row else 0

    if current_balance < cost:
        conn.close()
        return jsonify({
            'status': 'error',
            'message': f'رصيدك الحالي ({current_balance}) غير كافٍ. أقل باقة ({GEMINI_MIN_MINUTES} دقائق) تتطلب {GEMINI_COST_PER_10_MIN} نقاط.'
        }), 400

    new_balance = current_balance - cost
    cursor.execute("UPDATE users SET balance = ? WHERE TRIM(nickname) = TRIM(?)", (new_balance, user_nickname))
    conn.commit()
    conn.close()

    now = datetime.now()
    expires_at = now + timedelta(minutes=minutes)
    session['gemini_session_expires_at'] = expires_at.isoformat()
    session['gemini_session_minutes'] = minutes

    return jsonify({
        'status': 'success',
        'minutes': minutes,
        'cost': cost,
        'min_minutes': GEMINI_MIN_MINUTES,
        'min_cost': GEMINI_COST_PER_10_MIN,
        'new_balance': new_balance,
        'remaining_seconds': minutes * 60,
        'expires_at': expires_at.isoformat(),
        'message': f'تم بدء جلسة الذكاء الاصطناعي في AuraCode لمدة {minutes} دقائق (التكلفة: {cost} نقاط).'
    }), 200


@app.route('/api/gemini_chat', methods=['POST'])
def gemini_chat():
    user_nickname = session.get('user')
    if not user_nickname:
        return jsonify({'status': 'error', 'message': 'يرجى تسجيل الدخول أولاً'}), 401

    expires_raw = session.get('gemini_session_expires_at')
    if expires_raw:
        try:
            expires_dt = datetime.fromisoformat(expires_raw)
            if datetime.now() > expires_dt:
                return jsonify({'status': 'error', 'message': 'انتهى وقت جلسة الذكاء الاصطناعي، يرجى بدء جلسة جديدة (أقل مدة 10 دقائق).'}), 403
        except ValueError:
            pass

    data = request.get_json(silent=True) or {}
    prompt = (data.get('prompt') or '').strip()

    if not prompt:
        return jsonify({'status': 'error', 'message': 'السؤال فارغ'}), 400

    models_to_try = ['gemini-3.8-flash', 'gemini-2.5-flash']
    for model_name in models_to_try:
        for attempt in range(3):
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                )
                return jsonify({'status': 'success', 'reply': response.text})
            except Exception as e:
                err_str = str(e)
                if '503' in err_str or 'UNAVAILABLE' in err_str:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                return jsonify({'status': 'error', 'message': err_str}), 500

    return jsonify({
        'status': 'error',
        'message': 'النموذج يواجه ضغطاً مؤقتاً الآن، يرجى المحاولة بعد ثوانٍ.'
    }), 503


@app.route('/my_groups_data/<username>')
def my_groups_data(username):
    current_user = (session.get('user') or '').strip()
    if not current_user:
        return jsonify({'status': 'error', 'message': 'غير مصرح'}), 401

    if current_user.lower() != username.strip().lower() and not session.get('is_admin'):
        return jsonify({'status': 'error', 'message': 'غير مسموح بالوصول لبيانات مستخدم آخر'}), 403

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT DISTINCT g.id, g.name, g.description 
        FROM groups g 
        JOIN group_members m ON g.id = m.group_id 
        WHERE TRIM(m.username) = TRIM(?)
    """, (username.strip(),))
    groups = cursor.fetchall()
    conn.close()
    return jsonify(groups)


@app.route('/submit_rating', methods=['POST'])
def submit_rating():
    username = (session.get('user') or 'زائر').strip()
    data = request.get_json(silent=True) or {}

    try:
        stars = int(data.get('stars', 5))
    except (ValueError, TypeError):
        stars = 5
    stars = max(1, min(5, stars))
    feedback = str(data.get('feedback') or '').strip()

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO platform_ratings (username, stars, feedback) VALUES (?, ?, ?)",
        (username, stars, feedback)
    )
    conn.commit()
    conn.close()
    return jsonify({
        'status': 'success',
        'message': 'تم تسجيل تقييمك بنجاح وسيظهر في صفحة المدير!'
    })


# --- تسجيل دخول وخروج المشرف (Admin Auth) ---

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    error = None
    if request.method == 'POST':
        entered_pass = request.form.get('admin_password', '').strip()
        if entered_pass == ADMIN_PASSWORD:
            session['is_admin'] = True
            return redirect(url_for('admin_group_requests'))
        else:
            error = "❌ كلمة مرور المشرف غير صحيحة!"
    return render_template(
        'admin_requests.html',
        admin_authenticated=False,
        admin_error=error,
        requests_list=[],
        groups_list=[],
        members_list=[],
        users_activity_list=[],
        references_list=[],
        ratings_list=[]
    )


@app.route('/admin/logout')
def admin_logout():
    session.pop('is_admin', None)
    return redirect(url_for('admin_login'))


# --- لوحة تحكم المشرف (Admin) المحمية ---

@app.route('/send_request_reply', methods=['POST'])
@admin_required
def send_request_reply():
    data = request.get_json() or {}
    request_id = data.get('request_id')
    reply_text = data.get('reply')

    if request_id and reply_text:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()

        cursor.execute("""
            SELECT r.username, g.name 
            FROM group_requests r 
            JOIN groups g ON r.group_id = g.id 
            WHERE r.id = ?
        """, (request_id,))
        row = cursor.fetchone()

        if row:
            username, group_name = row
            cursor.execute("""
                INSERT INTO notifications (username, title, message) 
                VALUES (?, ?, ?)
            """, (username, f"رد مشرف مجموعة: {group_name}", reply_text))

            cursor.execute("UPDATE group_requests SET answers = answers || ? WHERE id = ?", (f" | رد المشرف: {reply_text}", request_id))
            conn.commit()
            conn.close()
            return jsonify({'status': 'success', 'message': 'تم إرسال الإشعار والرد للمستخدم بنجاح!'})

        conn.close()
    return jsonify({'status': 'error', 'message': 'تعذر عثور الطلب أو البيانات مفقودة'}), 400


@app.route('/get_notifications/<username>')
def get_notifications(username):
    current_user = (session.get('user') or '').strip()
    if not current_user:
        return jsonify({'status': 'error', 'message': 'غير مصرح'}), 401

    if current_user.lower() != username.strip().lower() and not session.get('is_admin'):
        return jsonify({'status': 'error', 'message': 'غير مسموح بالوصول لإشعارات مستخدم آخر'}), 403

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, title, message, timestamp 
        FROM notifications 
        WHERE TRIM(username) = TRIM(?) 
        ORDER BY id DESC LIMIT 10
    """, (username.strip(),))
    notifs = cursor.fetchall()
    conn.close()
    return jsonify([{'id': n[0], 'title': n[1], 'message': n[2], 'time': n[3]} for n in notifs])


@app.route('/dashboard/admin/group_requests')
@admin_required
def admin_group_requests():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT r.id, g.name, r.username, r.answers, r.status 
        FROM group_requests r
        JOIN groups g ON r.group_id = g.id
        WHERE r.status = 'pending'
    """)
    requests_list = cursor.fetchall()

    cursor.execute("SELECT * FROM groups")
    groups_list = cursor.fetchall()

    cursor.execute("""
        SELECT m.id, m.username, g.name, u.fullname, u.email 
        FROM group_members m 
        JOIN groups g ON m.group_id = g.id
        LEFT JOIN users u ON TRIM(m.username) = TRIM(u.nickname)
    """)
    members_list = cursor.fetchall()

    try:
        cursor.execute("""
            SELECT id, fullname, nickname, created_at, COALESCE(farm_hours, 0),
                   COALESCE(last_ip, 'غير محدد'), last_login, last_logout
            FROM users ORDER BY id DESC
        """)
        raw_activity = cursor.fetchall()
        users_activity_list = [
            (
                r[0],
                r[1],
                r[2],
                r[3],
                r[4],
                r[5],
                r[6] or 'لم يسجل دخول بعد',
                r[7] or 'متصل الآن / لم يخرج بعد',
                _format_session_duration(r[6], r[7])
            )
            for r in raw_activity
        ]
    except sqlite3.OperationalError:
        cursor.execute("""
            SELECT id, fullname, nickname, 'تسجيل سابق', COALESCE(farm_hours, 0),
                   'غير محدد', 'لم يسجل', 'لم يسجل', 'غير محسوب'
            FROM users ORDER BY id DESC
        """)
        users_activity_list = cursor.fetchall()

    cursor.execute("""
        SELECT r.id, r.title, r.description, r.link, r.group_id,
               COALESCE(g.name, 'عام / جميع المجموعات') AS group_name
        FROM references_list r
        LEFT JOIN groups g ON r.group_id = g.id
        ORDER BY r.id DESC
    """)
    references_list = cursor.fetchall()

    cursor.execute("SELECT id, username, stars, feedback, timestamp FROM platform_ratings ORDER BY id DESC")
    ratings_list = cursor.fetchall()

    conn.close()

    return render_template(
        'admin_requests.html',
        admin_authenticated=True,
        admin_error=None,
        requests_list=requests_list,
        groups_list=groups_list,
        members_list=members_list,
        users_activity_list=users_activity_list,
        references_list=references_list,
        ratings_list=ratings_list
    )


@app.route('/dashboard/admin/approve_request/<int:request_id>')
@admin_required
def approve_request(request_id):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT group_id, username FROM group_requests WHERE id = ?", (request_id,))
    req = cursor.fetchone()

    if req:
        group_id, username = req
        cursor.execute("INSERT OR IGNORE INTO group_members (group_id, username) VALUES (?, ?)", (group_id, username))
        cursor.execute("UPDATE group_requests SET status = 'approved' WHERE id = ?", (request_id,))

        cursor.execute(
            "INSERT INTO notifications (username, title, message) VALUES (?, ?, ?)",
            (username, "تمت الموافقة على طلبك!", "مبروك! تمت الموافقة على انضمامك للمجموعة في AuraCode.")
        )
        conn.commit()
    conn.close()
    return redirect(url_for('admin_group_requests'))


@app.route('/dashboard/admin/reject_request/<int:request_id>')
@admin_required
def reject_request(request_id):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT group_id, username FROM group_requests WHERE id = ?", (request_id,))
    req = cursor.fetchone()
    if req:
        group_id, username = req
        cursor.execute("UPDATE group_requests SET status = 'rejected' WHERE id = ?", (request_id,))
        cursor.execute(
            "INSERT INTO notifications (username, title, message) VALUES (?, ?, ?)",
            (username, "نتيجة طلب الانضمام", "للأسف تم رفض طلب انضمامك للمجموعة.")
        )
        conn.commit()
    conn.close()
    return redirect(url_for('admin_group_requests'))


@app.route('/dashboard/admin/add_group', methods=['POST'])
@admin_required
def add_group():
    group_name = request.form.get('group_name')
    group_desc = request.form.get('group_desc')
    icon_file = request.files.get('group_icon')

    icon_filename, upload_err = _save_uploaded_file(icon_file, ALLOWED_IMAGE_EXTENSIONS, 'default_group.png')
    if upload_err:
        flash(upload_err, "error")
        return redirect(url_for('admin_group_requests'))

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO groups (name, description, icon) VALUES (?, ?, ?)", (group_name, group_desc, icon_filename))
    conn.commit()
    conn.close()
    return redirect(url_for('admin_group_requests'))


@app.route('/dashboard/admin/edit_group', methods=['POST'])
@admin_required
def edit_group():
    group_id = request.form.get('group_id')
    group_name = request.form.get('group_name')
    group_desc = request.form.get('group_desc')
    icon_file = request.files.get('group_icon')

    saved_icon, upload_err = _save_uploaded_file(icon_file, ALLOWED_IMAGE_EXTENSIONS, None)
    if upload_err:
        flash(upload_err, "error")
        return redirect(url_for('admin_group_requests'))

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    if saved_icon:
        cursor.execute(
            "UPDATE groups SET name = ?, description = ?, icon = ? WHERE id = ?",
            (group_name, group_desc, saved_icon, group_id)
        )
    else:
        cursor.execute(
            "UPDATE groups SET name = ?, description = ? WHERE id = ?",
            (group_name, group_desc, group_id)
        )

    conn.commit()
    conn.close()
    return redirect(url_for('admin_group_requests'))


@app.route('/dashboard/admin/delete_group/<int:group_id>')
@admin_required
def delete_group(group_id):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM group_messages WHERE group_id = ?", (group_id,))
    cursor.execute("DELETE FROM group_requests WHERE group_id = ?", (group_id,))
    cursor.execute("DELETE FROM group_members WHERE group_id = ?", (group_id,))
    cursor.execute("DELETE FROM group_questions WHERE group_id = ?", (group_id,))
    cursor.execute("DELETE FROM groups WHERE id = ?", (group_id,))
    conn.commit()
    conn.close()
    return redirect(url_for('admin_group_requests'))


@app.route('/dashboard/admin/remove_member/<int:member_id>')
@admin_required
def remove_member(member_id):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM group_members WHERE id = ?", (member_id,))
    conn.commit()
    conn.close()
    return redirect(url_for('admin_group_requests'))


@app.route('/dashboard/admin/add_reference', methods=['POST'])
@admin_required
def add_reference():
    raw_group_id = (request.form.get('group_id') or '').strip()
    group_id = int(raw_group_id) if raw_group_id.isdigit() else None
    title = (request.form.get('ref_title') or '').strip()
    description = (request.form.get('ref_desc') or '').strip()
    link = (request.form.get('ref_link') or '#').strip() or '#'

    if title:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO references_list (group_id, title, description, link) VALUES (?, ?, ?, ?)",
            (group_id, title, description, link)
        )
        conn.commit()
        conn.close()
        flash("تمت إضافة المرجع وربطه بالمجموعة بنجاح، وسيظهر في تبويب المراجع.", "success")
    return redirect(url_for('admin_group_requests'))


@app.route('/dashboard/admin/edit_reference', methods=['POST'])
@admin_required
def edit_reference():
    ref_id = (request.form.get('ref_id') or '').strip()
    raw_group_id = (request.form.get('group_id') or '').strip()
    group_id = int(raw_group_id) if raw_group_id.isdigit() else None
    title = (request.form.get('ref_title') or '').strip()
    description = (request.form.get('ref_desc') or '').strip()
    link = (request.form.get('ref_link') or '#').strip() or '#'

    if ref_id.isdigit() and title:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE references_list SET group_id = ?, title = ?, description = ?, link = ? WHERE id = ?",
            (group_id, title, description, link, int(ref_id))
        )
        conn.commit()
        conn.close()
        flash("تم تعديل بيانات المرجع بنجاح.", "success")
    return redirect(url_for('admin_group_requests'))


@app.route('/dashboard/admin/delete_reference/<int:ref_id>')
@admin_required
def delete_reference(ref_id):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM references_list WHERE id = ?", (ref_id,))
    conn.commit()
    conn.close()
    flash("تم حذف المرجع بنجاح.", "success")
    return redirect(url_for('admin_group_requests'))


# --- المزرعة والمتجر والخدمات الإضافية ---

@app.route('/farm_setup', methods=['POST'])
def farm_setup():
    username = session.get('user')
    if not username:
        return jsonify({'status': 'error', 'message': 'غير مصرح'}), 401

    data = request.get_json(silent=True) or {}
    try:
        hours = int(data.get('hours', 1))
    except (ValueError, TypeError):
        return jsonify({'status': 'error', 'message': 'عدد الساعات غير صالح'}), 400

    if hours < 1 or hours > 24:
        return jsonify({'status': 'error', 'message': 'يجب أن يكون عدد الساعات بين 1 و 24 ساعة'}), 400

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE users SET farm_hours = COALESCE(farm_hours, 0) + ?, balance = COALESCE(balance, 0) + (? * 10) WHERE TRIM(nickname)=TRIM(?)",
        (hours, hours, username)
    )
    conn.commit()
    conn.close()
    return jsonify({'status': 'success', 'message': f'تمت إضافة {hours * 10} نقطة بنجاح!'})


@app.route('/api/store_items')
def get_store_items():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT id, item_name, price, icon FROM shop_items")
    items = cursor.fetchall()
    conn.close()
    return jsonify([{'id': i[0], 'name': i[1], 'price': i[2], 'desc': 'أداة مساعدة داخل المنصة'} for i in items])


@app.route('/buy_item', methods=['POST'])
def buy_item():
    username = session.get('user')
    if not username:
        return jsonify({'status': 'error', 'message': 'غير مصرح'}), 401

    data = request.get_json(silent=True) or {}
    item_id = data.get('item_id')

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT balance FROM users WHERE TRIM(nickname)=TRIM(?)", (username,))
    u = cursor.fetchone()

    cursor.execute("SELECT price FROM shop_items WHERE id=?", (item_id,))
    it = cursor.fetchone()

    if u and it and u[0] >= it[0]:
        cursor.execute("UPDATE users SET balance = balance - ? WHERE TRIM(nickname)=TRIM(?)", (it[0], username))
        cursor.execute("INSERT INTO user_purchases (username, item_id) VALUES (?, ?)", (username, item_id))
        conn.commit()
        conn.close()
        return jsonify({'status': 'success', 'message': 'تم الشراء بنجاح!'})

    conn.close()
    return jsonify({'status': 'error', 'message': 'رصيدك غير كافٍ!'})


@app.route('/update_pic', methods=['POST'])
def update_pic():
    user_nickname = session.get('user')
    if not user_nickname:
        return redirect(url_for('login'))

    file = request.files.get('profile_pic')
    saved_pic, upload_err = _save_uploaded_file(file, ALLOWED_IMAGE_EXTENSIONS, None)
    if upload_err:
        return upload_err, 400

    if saved_pic:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET profile_pic = ? WHERE TRIM(nickname) = TRIM(?)", (saved_pic, user_nickname))
        conn.commit()
        conn.close()

    return redirect(url_for('dashboard', nickname=user_nickname))


# المهمة المجدولة
if not app.debug or os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
    @scheduler.task('interval', id='debt_penalty_job', hours=24)
    def apply_daily_debt():
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET debt = debt + 10 WHERE balance < 50")
        conn.commit()
        conn.close()


if __name__ == '__main__':
    app.run(debug=True)
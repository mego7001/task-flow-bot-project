# -*- coding: utf-8 -*-

import logging
import sqlite3
import os
import asyncio
import re
from datetime import datetime, timedelta
from enum import Enum
from dataclasses import dataclass
from typing import Optional, List

# --- استيراد المكتبات الأساسية ---
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, ConversationHandler, CallbackQueryHandler
from flask import Flask, render_template_string, request, redirect, session, url_for, flash, Response
from fpdf import FPDF
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# --- 1. الإعدادات وقراءة الأسرار من بيئة ريندر ---
# سيتم تعبئة هذه المتغيرات من الـ Environment Group في ريندر
TOKEN = os.environ.get("TELEGRAM_TOKEN")
EMAIL_FROM = os.environ.get("EMAIL_FROM") # غير مستخدم حاليًا ولكن جاهز
EMAIL_PASS = os.environ.get("EMAIL_PASS") # غير مستخدم حاليًا ولكن جاهز
SECRET_KEY = os.environ.get("SECRET_KEY")

# إعداد نظام تسجيل المعلومات (Logging)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- 2. إعداد مسار قاعدة البيانات ---
# هذا الكود يضمن أن قاعدة البيانات تُحفظ على القرص الصلب الدائم في ريندر
# وفي نفس الوقت يعمل على جهازك المحلي للتجربة
db_path = "/data/taskflow.db"
if not os.path.exists("/data"):
    logger.info("'/data' path not found, running in local mode. DB path: taskflow.db")
    db_path = "taskflow.db"
else:
    logger.info(f"Running in Render mode. DB path: {db_path}")

# --- 3. تعريف هياكل البيانات (Dataclasses & Enums) ---
class TaskStatus(Enum):
    PENDING = "قيد التنفيذ"
    DONE = "مكتملة"

@dataclass
class User:
    id: int
    first_name: str
    last_name: Optional[str] = None
    username: Optional[str] = None

@dataclass
class Task:
    id: int
    user_id: int
    description: str
    status: TaskStatus
    due_date: Optional[datetime] = None
    created_at: datetime = datetime.now()

# --- 4. كلاس قاعدة البيانات (DB) ---
class DB:
    def __init__(self, db_name):
        self.conn = sqlite3.connect(db_name, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.create_tables()

    def create_tables(self):
        cursor = self.conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                first_name TEXT NOT NULL,
                last_name TEXT,
                username TEXT
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                description TEXT NOT NULL,
                status TEXT DEFAULT 'PENDING',
                due_date DATETIME,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
        ''')
        self.conn.commit()

    def add_user(self, user: User):
        cursor = self.conn.cursor()
        cursor.execute("INSERT OR IGNORE INTO users (id, first_name, last_name, username) VALUES (?, ?, ?, ?)",
                       (user.id, user.first_name, user.last_name, user.username))
        self.conn.commit()

    def get_user(self, user_id: int) -> Optional[User]:
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM users WHERE id = ?", (user_id,))
        row = cursor.fetchone()
        return User(**row) if row else None
        
    def get_all_users(self) -> List[User]:
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM users")
        rows = cursor.fetchall()
        return [User(**row) for row in rows]

    def add_task(self, task: Task) -> int:
        cursor = self.conn.cursor()
        cursor.execute("INSERT INTO tasks (user_id, description, status, due_date) VALUES (?, ?, ?, ?)",
                       (task.user_id, task.description, task.status.name, task.due_date))
        self.conn.commit()
        return cursor.lastrowid

    def get_tasks(self, user_id: int, status: Optional[TaskStatus] = None) -> List[Task]:
        cursor = self.conn.cursor()
        query = "SELECT * FROM tasks WHERE user_id = ?"
        params = [user_id]
        if status:
            query += " AND status = ?"
            params.append(status.name)
        cursor.execute(query, params)
        rows = cursor.fetchall()
        tasks = []
        for row in rows:
            task_data = dict(row)
            task_data['status'] = TaskStatus[task_data['status']]
            tasks.append(Task(**task_data))
        return tasks

    def update_task_status(self, task_id: int, status: TaskStatus):
        cursor = self.conn.cursor()
        cursor.execute("UPDATE tasks SET status = ? WHERE id = ?", (status.name, task_id))
        self.conn.commit()

    def delete_task(self, task_id: int):
        cursor = self.conn.cursor()
        cursor.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        self.conn.commit()
        
    def get_all_users_with_tasks_due(self) -> List[dict]:
        cursor = self.conn.cursor()
        # يجلب المستخدمين الذين لديهم مهام حان وقتها ولم تكتمل بعد
        query = """
            SELECT DISTINCT u.id, u.first_name FROM users u
            JOIN tasks t ON u.id = t.user_id
            WHERE t.status = 'PENDING' AND t.due_date IS NOT NULL AND datetime(t.due_date) < datetime('now', '+5 minutes')
        """
        cursor.execute(query)
        rows = cursor.fetchall()
        return [dict(row) for row in rows]


# --- 5. كود واجهة الويب (Flask) ---
db = DB(db_path)
web_app = Flask(__name__)
web_app.config['SECRET_KEY'] = SECRET_KEY

# قوالب HTML
login_template = """
<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>تسجيل الدخول</title>
    <style>
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background-color: #f4f4f9; color: #333; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; }
        .login-container { background-color: #fff; padding: 40px; border-radius: 10px; box-shadow: 0 4px 8px rgba(0,0,0,0.1); text-align: center; }
        h1 { color: #5a5a5a; }
        select, button { width: 100%; padding: 12px; margin-top: 20px; border-radius: 5px; border: 1px solid #ddd; font-size: 16px; }
        button { background-color: #007bff; color: white; border: none; cursor: pointer; transition: background-color 0.3s; }
        button:hover { background-color: #0056b3; }
    </style>
</head>
<body>
    <div class="login-container">
        <h1>اختر حسابك</h1>
        <form method="post">
            <select name="user_id">
                {% for user in users %}
                <option value="{{ user.id }}">{{ user.first_name }} {{ user.last_name or '' }} (@{{ user.username or 'N/A' }})</option>
                {% endfor %}
            </select>
            <button type="submit">دخول</button>
        </form>
    </div>
</body>
</html>
"""

tasks_template = """
<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>قائمة المهام</title>
    <style>
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background-color: #f4f4f9; margin: 0; padding: 20px; }
        .container { max-width: 800px; margin: auto; background: #fff; padding: 20px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
        h1 { color: #333; border-bottom: 2px solid #007bff; padding-bottom: 10px; }
        .task-list { list-style: none; padding: 0; }
        .task-item { background: #fff; border: 1px solid #ddd; padding: 15px; margin-bottom: 10px; border-radius: 5px; display: flex; justify-content: space-between; align-items: center; transition: box-shadow 0.2s; }
        .task-item:hover { box-shadow: 0 0 5px rgba(0,123,255,0.5); }
        .task-item.done { text-decoration: line-through; color: #888; background-color: #e9ecef; }
        .task-info { flex-grow: 1; }
        .task-date { font-size: 0.9em; color: #666; }
        .actions a { text-decoration: none; color: #fff; padding: 8px 12px; border-radius: 5px; margin-left: 5px; font-size: 14px; }
        .pdf-link { background-color: #28a745; }
        .logout-link { background-color: #dc3545; }
        .header { display: flex; justify-content: space-between; align-items: center; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>مهام {{ user.first_name }}</h1>
            <div>
                <a href="{{ url_for('generate_pdf', user_id=user.id) }}" class="pdf-link">تصدير PDF</a>
                <a href="{{ url_for('logout') }}" class="logout-link">خروج</a>
            </div>
        </div>
        <ul class="task-list">
            {% for task in tasks %}
            <li class="task-item {% if task.status == TaskStatus.DONE %}done{% endif %}">
                <div class="task-info">
                    {{ task.description }}
                    {% if task.due_date %}
                    <div class="task-date">تاريخ الاستحقاق: {{ task.due_date.strftime('%Y-%m-%d %H:%M') }}</div>
                    {% endif %}
                </div>
            </li>
            {% else %}
            <li>لا توجد مهام حاليًا.</li>
            {% endfor %}
        </ul>
    </div>
</body>
</html>
"""

@web_app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        user_id = request.form.get('user_id')
        if user_id:
            session['user_id'] = int(user_id)
            return redirect(url_for('index'))
    users = db.get_all_users()
    return render_template_string(login_template, users=users)

@web_app.route('/')
def index():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    user_id = session['user_id']
    user = db.get_user(user_id)
    if not user:
        session.pop('user_id', None)
        return redirect(url_for('login'))
    tasks = db.get_tasks(user_id)
    return render_template_string(tasks_template, user=user, tasks=tasks, TaskStatus=TaskStatus)

@web_app.route('/logout')
def logout():
    session.pop('user_id', None)
    return redirect(url_for('login'))
    
@web_app.route('/pdf/<int:user_id>')
def generate_pdf(user_id):
    if 'user_id' not in session or session['user_id'] != user_id:
        return "Unauthorized", 401
        
    user = db.get_user(user_id)
    tasks = db.get_tasks(user_id)
    
    pdf = FPDF()
    pdf.add_page()
    
    # إضافة الخط العربي
    # تأكد من أن ملف الخط موجود في نفس المجلد
    # يمكنك تحميله من arfonts.net مثلا (خط Amiri)
    # pdf.add_font('Amiri', '', 'Amiri-Regular.ttf', uni=True)
    # pdf.set_font('Amiri', '', 14)
    pdf.set_font('Arial', 'B', 16) # حل بديل لو الخط العربي غير موجود
    
    pdf.cell(0, 10, f'Task Report for {user.first_name}', 0, 1, 'C')
    pdf.ln(10)
    
    for task in tasks:
        status = task.status.name
        due_date = task.due_date.strftime('%Y-%m-%d') if task.due_date else 'N/A'
        line = f"Task: {task.description} | Status: {status} | Due: {due_date}"
        pdf.cell(0, 10, line, 0, 1)

    return Response(pdf.output(dest='S').encode('latin-1'), mimetype='application/pdf', headers={'Content-Disposition':'attachment;filename=tasks.pdf'})


# --- 6. دوال بوت التليجرام ---
(WAITING_FOR_TASK, WAITING_FOR_DATE, WAITING_FOR_DELETION, WAITING_FOR_DONE) = range(4)

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg_user = update.effective_user
    user = User(id=tg_user.id, first_name=tg_user.first_name, last_name=tg_user.last_name, username=tg_user.username)
    db.add_user(user)
    
    webapp_url = "https://YOUR_RENDER_APP_URL.onrender.com" # << غير هذا الرابط
    keyboard = [[InlineKeyboardButton("فتح لوحة التحكم 🌐", web_app=WebAppInfo(url=webapp_url))],
                [InlineKeyboardButton("إضافة مهمة جديدة ➕", callback_data="add_task")],
                [InlineKeyboardButton("عرض مهامي الحالية 📝", callback_data="list_tasks")]]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(f'أهلاً بك يا {tg_user.first_name} في بوت إدارة المهام!', reply_markup=reply_markup)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == 'add_task':
        await query.edit_message_text(text="الرجاء إدخال وصف المهمة:")
        return WAITING_FOR_TASK
    elif query.data == 'list_tasks':
        await list_tasks_as_buttons(update, context)
        return ConversationHandler.END
    elif query.data.startswith('delete_'):
        task_id = int(query.data.split('_')[1])
        db.delete_task(task_id)
        await query.edit_message_text(text="✅ تم حذف المهمة بنجاح.")
        await list_tasks_as_buttons(update, context) # عرض القائمة المحدثة
        return ConversationHandler.END
    elif query.data.startswith('done_'):
        task_id = int(query.data.split('_')[1])
        db.update_task_status(task_id, TaskStatus.DONE)
        await query.edit_message_text(text="✅ تم إنجاز المهمة. عمل رائع!")
        await list_tasks_as_buttons(update, context) # عرض القائمة المحدثة
        return ConversationHandler.END
    
    return ConversationHandler.END

async def receive_task_description(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['task_description'] = update.message.text
    await update.message.reply_text("رائع! هل تريد إضافة تاريخ استحقاق؟ (مثال: 'غدا 10م' أو '25/12 9ص' أو أرسل 'لا')")
    return WAITING_FOR_DATE

async def receive_due_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.lower()
    due_date = None
    if text not in ['لا', 'no']:
        # منطق بسيط لتحليل التاريخ - يمكن تطويره
        try:
            # يمكن استخدام مكتبة مثل dateparser هنا لمزيد من الدقة
            # هذا مجرد مثال بسيط
            if 'غدا' in text or 'tomorrow' in text:
                due_date = datetime.now() + timedelta(days=1)
            else:
                due_date = datetime.strptime(text, "%d/%m %I%p") # 25/12 9am
        except ValueError:
            await update.message.reply_text("لم أتمكن من فهم التاريخ. سيتم حفظ المهمة بدون تاريخ استحقاق.")
    
    new_task = Task(
        id=0,
        user_id=update.effective_user.id,
        description=context.user_data['task_description'],
        status=TaskStatus.PENDING,
        due_date=due_date
    )
    db.add_task(new_task)
    await update.message.reply_text("✅ تم إضافة المهمة بنجاح!")
    return ConversationHandler.END

async def list_tasks_as_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    tasks = db.get_tasks(user_id, status=TaskStatus.PENDING)
    
    if not tasks:
        await update.callback_query.edit_message_text("لا توجد مهام قيد التنفيذ حاليًا.")
        return

    keyboard = []
    for task in tasks:
        # زر لإنجاز المهمة وزر لحذفها
        buttons = [
            InlineKeyboardButton(f"✅ إنجاز", callback_data=f"done_{task.id}"),
            InlineKeyboardButton(f"❌ حذف", callback_data=f"delete_{task.id}")
        ]
        keyboard.append([InlineKeyboardButton(task.description, callback_data=f"task_{task.id}")])
        keyboard.append(buttons)
        
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.callback_query.edit_message_text('اختر مهمة لتعديلها:', reply_markup=reply_markup)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text('تم إلغاء العملية.')
    return ConversationHandler.END
    
async def check_due_tasks(app: Application):
    logger.info("Running scheduled check for due tasks...")
    users_to_notify = db.get_all_users_with_tasks_due()
    for user in users_to_notify:
        try:
            await app.bot.send_message(chat_id=user['id'], text=f"👋 مرحبًا {user['first_name']}، لديك مهام حان وقتها!")
            logger.info(f"Sent due task notification to user {user['id']}.")
        except Exception as e:
            logger.error(f"Failed to send notification to user {user['id']}: {e}")

# --- 7. الجزء الرئيسي للتشغيل ---
# هذا الجزء هو نقطة الدخول لخدمة الـ "Worker" في ريندر
# خدمة الويب (gunicorn) لا تقوم بتشغيل هذا البلوك
if __name__ == "__main__":
    if not TOKEN or not SECRET_KEY:
        raise ValueError("Critical environment variables TOKEN or SECRET_KEY are not set!")

    # إعداد تطبيق التليجرام
    telegram_app = Application.builder().token(TOKEN).build()
    
    # محادثة إضافة مهمة جديدة
    conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(button_handler, pattern='^add_task$')],
        states={
            WAITING_FOR_TASK: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_task_description)],
            WAITING_FOR_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_due_date)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
    )

    telegram_app.add_handler(CommandHandler("start", start_command))
    telegram_app.add_handler(conv_handler)
    telegram_app.add_handler(CallbackQueryHandler(button_handler))

    # إعداد وتشغيل المجدول (Scheduler)
    # سيقوم المجدول بالعمل في نفس processo الخاص بالبوت
    scheduler = AsyncIOScheduler(timezone="Africa/Cairo")
    scheduler.add_job(check_due_tasks, 'interval', minutes=1, args=[telegram_app])
    scheduler.start()
    logger.info("APScheduler started in the worker process.")

    # تشغيل البوت
    logger.info("Starting Telegram bot polling...")
    telegram_app.run_polling()

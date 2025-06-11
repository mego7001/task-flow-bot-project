# =======================================================================
#       TaskFlow Bot - نسخة معدلة للعمل على Render
# =======================================================================
import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Optional, List
from enum import Enum
from dataclasses import dataclass
import os
import smtplib
from email.message import EmailMessage
import threading
import re
import asyncio

# Telegram Bot Imports
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# Web Interface & PDF Imports
from flask import Flask, render_template_string, request, redirect, session, url_for
from fpdf import FPDF

# APScheduler for background tasks
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# --------------------------- General Settings --------------------------- #
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- قراءة بيانات الإعدادات من متغيرات البيئة الخاصة بـ Render ---
TOKEN = os.environ.get("TELEGRAM_TOKEN", "YOUR_DEFAULT_TOKEN_IF_NOT_SET")
EMAIL_FROM = os.environ.get("EMAIL_FROM", "default@email.com")
EMAIL_PASS = os.environ.get("EMAIL_PASS", "default_password")
SECRET_KEY = os.environ.get("SECRET_KEY", "a_very_secret_key_that_is_long_and_random")
# مسار قاعدة البيانات على القرص الصلب الدائم في Render
DB_PATH = os.path.join(os.environ.get("RENDER_DISK_PATH", "."), "taskflow_render.db")

UPLOAD_DIR = "attachments"
if not os.path.exists(UPLOAD_DIR):
    os.makedirs(UPLOAD_DIR)

# --------------------------- Enums & Dataclasses --------------------------- #
class Role(Enum): OWNER = "owner"; MANAGER = "manager"; EMPLOYEE = "employee"
class TaskStatus(Enum): PENDING = "pending"; IN_PROGRESS = "in_progress"; COMPLETED = "completed"; OVERDUE = "overdue"

@dataclass
class User: id: int; telegram_id: int; name: str; role: Role; manager_id: Optional[int]; email: Optional[str]; password: Optional[str]; language: str; start_date: datetime; activated: bool
@dataclass
class Task: id: int; description: str; assigned_to: int; due: datetime; status: TaskStatus; attachment_path: Optional[str] = None; notification_level: int = 0

# --------------------------- Database Class --------------------------- #
class DB:
    def __init__(self, db_name=DB_PATH):
        self.conn = sqlite3.connect(db_name, check_same_thread=False)
        self.create_tables()

    def create_tables(self):
        cur = self.conn.cursor()
        cur.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, telegram_id INTEGER UNIQUE NOT NULL, name TEXT, role TEXT, manager_id INTEGER, email TEXT, password TEXT, language TEXT DEFAULT 'ar', start_date TEXT NOT NULL, activated INTEGER DEFAULT 0)")
        cur.execute("CREATE TABLE IF NOT EXISTS tasks (id INTEGER PRIMARY KEY, description TEXT NOT NULL, assigned_to INTEGER NOT NULL, due TEXT NOT NULL, status TEXT NOT NULL, attachment_path TEXT, notification_level INTEGER DEFAULT 0, FOREIGN KEY (assigned_to) REFERENCES users (id))")
        self.conn.commit()

    def _row_to_user(self, r): return User(id=r[0], telegram_id=r[1], name=r[2], role=Role(r[3]), manager_id=r[4], email=r[5], password=r[6], language=r[7], start_date=datetime.fromisoformat(r[8]), activated=bool(r[9])) if r else None
    def _row_to_task(self, r): return Task(id=r[0], description=r[1], assigned_to=r[2], due=datetime.fromisoformat(r[3]), status=TaskStatus(r[4]), attachment_path=r[5], notification_level=r[6]) if r else None
    def get_user_by_telegram_id(self, telegram_id: int): cur = self.conn.cursor(); cur.execute("SELECT * FROM users WHERE telegram_id=?", (telegram_id,)); return self._row_to_user(cur.fetchone())
    def get_user_by_id(self, user_id: int): cur = self.conn.cursor(); cur.execute("SELECT * FROM users WHERE id=?", (user_id,)); return self._row_to_user(cur.fetchone())
    def get_user_by_email(self, email: str): cur = self.conn.cursor(); cur.execute("SELECT * FROM users WHERE email=?", (email,)); return self._row_to_user(cur.fetchone())
    def add_user_from_telegram(self, telegram_id: int, name: str, lang: str = "ar"):
        cur = self.conn.cursor(); today = datetime.now().isoformat()
        try:
            cur.execute("INSERT INTO users (telegram_id, name, role, language, start_date, activated) VALUES (?, ?, ?, ?, ?, 0)", (telegram_id, name, Role.EMPLOYEE.value, lang, today))
            self.conn.commit(); return self.get_user_by_telegram_id(telegram_id)
        except sqlite3.IntegrityError: return self.get_user_by_telegram_id(telegram_id)
    def activate_user(self, telegram_id: int): cur = self.conn.cursor(); cur.execute("UPDATE users SET activated=1 WHERE telegram_id=?", (telegram_id,)); self.conn.commit()
    def add_task(self, description: str, assigned_to_id: int, due_date_iso: str):
        cur = self.conn.cursor()
        cur.execute("INSERT INTO tasks (description, assigned_to, due, status, notification_level) VALUES (?, ?, ?, ?, 0)", (description, assigned_to_id, due_date_iso, TaskStatus.PENDING.value))
        self.conn.commit(); return self._row_to_task(cur.execute("SELECT * FROM tasks WHERE id=?", (cur.lastrowid,)).fetchone())
    def get_tasks_for_user(self, user_id: int): cur = self.conn.cursor(); cur.execute("SELECT * FROM tasks WHERE assigned_to=? AND status != ?", (user_id, TaskStatus.COMPLETED.value)); return [self._row_to_task(row) for row in cur.fetchall()]
    def get_task_by_id(self, task_id: int): cur = self.conn.cursor(); cur.execute("SELECT * FROM tasks WHERE id=?", (task_id,)); return self._row_to_task(cur.fetchone())
    def complete_task(self, task_id: int, user_id: int):
        task = self.get_task_by_id(task_id)
        if not task or task.assigned_to != user_id: return False
        cur = self.conn.cursor(); cur.execute("UPDATE tasks SET status=? WHERE id=?", (TaskStatus.COMPLETED.value, task_id)); self.conn.commit(); return True
    def get_all_tasks(self): cur = self.conn.cursor(); cur.execute("SELECT * FROM tasks ORDER BY due ASC"); return [self._row_to_task(row) for row in cur.fetchall()]
    def update_task_status(self, task_id: int, status: TaskStatus): cur = self.conn.cursor(); cur.execute("UPDATE tasks SET status=? WHERE id=?", (status.value, task_id)); self.conn.commit()
    def mark_notification_sent(self, task_id: int, level: int): cur = self.conn.cursor(); cur.execute("UPDATE tasks SET notification_level=? WHERE id=?", (level, task_id)); self.conn.commit()
    def get_tasks_for_notification_check(self): cur = self.conn.cursor(); cur.execute("SELECT * FROM tasks WHERE status != ? AND notification_level < 2", (TaskStatus.COMPLETED.value,)); return [self._row_to_task(row) for row in cur.fetchall()]

# --------------------------- Notification Scheduler Function --------------------------- #
async def check_due_tasks(app: Application):
    db = DB()
    now = datetime.now()
    tasks_to_check = db.get_tasks_for_notification_check()
    logger.info(f"Scheduler running: Checking {len(tasks_to_check)} tasks at {now.strftime('%H:%M:%S')}")
    for task in tasks_to_check:
        user = db.get_user_by_id(task.assigned_to)
        if not user: continue
        time_until_due = task.due - now
        if timedelta(minutes=0) < time_until_due <= timedelta(hours=1) and task.notification_level == 0:
            try:
                await app.bot.send_message(chat_id=user.telegram_id, text=f"🔔 تنبيه: تبقى أقل من ساعة على انتهاء المهمة:\n- {task.description}")
                db.mark_notification_sent(task.id, 1)
            except Exception as e: logger.error(f"Failed to send 'approaching' notification for task {task.id}: {e}")
        elif time_until_due <= timedelta(minutes=0) and task.notification_level < 2:
            try:
                await app.bot.send_message(chat_id=user.telegram_id, text=f"❗️انتهى الموعد المحدد لتسليم المهمة:\n- {task.description}")
                db.mark_notification_sent(task.id, 2)
                if task.status != TaskStatus.COMPLETED: db.update_task_status(task.id, TaskStatus.OVERDUE)
            except Exception as e: logger.error(f"Failed to send 'overdue' notification for task {task.id}: {e}")

# --------------------------- Telegram Bot Handlers --------------------------- #
async def check_access(update: Update):
    db = DB(); user = db.get_user_by_telegram_id(update.effective_user.id)
    if not user: await update.message.reply_text("الرجاء كتابة /start للبدء."); return None
    if user.activated or (datetime.now() - user.start_date).days < 14: return user
    await update.message.reply_text("انتهت فترتك التجريبية. الرجاء إدخال كود التفعيل للمتابعة."); return None
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = DB(); user = db.get_user_by_telegram_id(update.effective_user.id)
    if not user:
        db.add_user_from_telegram(update.effective_user.id, update.effective_user.first_name)
        await update.message.reply_text(f"أهلاً بك، {update.effective_user.first_name}! 🎉\nبدأت فترتك التجريبية لمدة 14 يومًا."); await help_command(update, context)
    else: await update.message.reply_text(f"أهلاً بعودتك، {user.name}!")
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("قائمة الأوامر المتاحة:\n/help - عرض هذه القائمة\n/mytasks - عرض مهامك الحالية\n/addtask <وصف المهمة> due: <مدة> - لإضافة مهمة\n/done <رقم المهمة> - لإكمال مهمة\n\nأمثلة على المدة:\n`30m` (30 دقيقة), `3h` (3 ساعات), `1.5d` (يوم ونصف)")
async def add_task_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await check_access(update);
    if not user: return
    db = DB(); full_text = ' '.join(context.args)
    if not full_text: await update.message.reply_text("للاستخدام: /addtask <الوصف> due: <المدة>"); return
    task_description = full_text; due_date = datetime.now() + timedelta(days=1)
    match = re.search(r'due:\s*(\d*\.?\d+)\s*([mhd])', full_text, re.IGNORECASE)
    if match:
        value = float(match.group(1)); unit = match.group(2).lower()
        if unit == 'm': due_date = datetime.now() + timedelta(minutes=value)
        elif unit == 'h': due_date = datetime.now() + timedelta(hours=value)
        elif unit == 'd': due_date = datetime.now() + timedelta(days=value)
        task_description = re.sub(r'due:\s*(\d*\.?\d+)\s*[mhd]', '', task_description, flags=re.IGNORECASE).strip()
    if not task_description: await update.message.reply_text("❌ وصف المهمة لا يمكن أن يكون فارغًا."); return
    new_task = db.add_task(task_description, user.id, due_date.isoformat())
    await update.message.reply_text(f"✅ تم إضافة المهمة بنجاح!\n[ID: {new_task.id}] {new_task.description}\nتاريخ الاستحقاق: {new_task.due.strftime('%Y-%m-%d %H:%M')}")
async def my_tasks_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await check_access(update);
    if not user: return
    db = DB(); tasks = db.get_tasks_for_user(user.id)
    if not tasks: await update.message.reply_text("لا توجد لديك مهام حالية. رائع!"); return
    message = "قائمة مهامك الحالية:\n" + "".join([f"- [ID: {t.id}] {t.description} (تستحق في: {t.due.strftime('%Y-%m-%d %H:%M')})\n" for t in tasks])
    await update.message.reply_text(message)
async def done_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await check_access(update);
    if not user: return
    db = DB()
    if not context.args or not context.args[0].isdigit(): await update.message.reply_text("للاستخدام الصحيح: /done <رقم المهمة>"); return
    task_id = int(context.args[0])
    if db.complete_task(task_id, user.id): await update.message.reply_text(f"✅ رائع! تم إكمال المهمة رقم {task_id}.")
    else: await update.message.reply_text("❌ خطأ: المهمة غير موجودة أو لا تخصك.")
async def handle_activation_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = DB(); user = db.get_user_by_telegram_id(update.effective_user.id)
    if not user: await start_command(update, context); return
    if user.activated: await update.message.reply_text("حسابك مفعل بالفعل. استخدم /help لمعرفة الأوامر."); return
    code = update.message.text.strip()
    if code == "TASKFLOW2025": db.activate_user(update.effective_user.id); await update.message.reply_text("✅ تم تفعيل حسابك بنجاح!")
    else: await update.message.reply_text("كود تفعيل غير صالح.")

# --------------------------- Web Interface (Flask) --------------------------- #
web_app = Flask(__name__)
web_app.secret_key = SECRET_KEY
@web_app.route("/")
def index():
    return redirect(url_for('login'))
@web_app.route("/login", methods=['GET', 'POST'])
def login():
    db = DB()
    if request.method == 'POST':
        email, password = request.form['email'], request.form['password']
        user = db.get_user_by_email(email)
        if user and user.password == password:
            if user.activated or (datetime.now() - user.start_date).days < 14:
                session['user_id'] = user.id; return redirect(url_for('dashboard'))
            else: return "⚠️ حسابك غير مفعل وقد انتهت الفترة التجريبية."
        return "⚠️ بيانات الدخول غير صحيحة."
    login_html = ("<!DOCTYPE html><html lang='ar' dir='rtl'><head><meta charset='UTF-8'><title>تسجيل الدخول</title>"
                  "<style>body{font-family:sans-serif;background-color:#f4f4f4;display:flex;justify-content:center;align-items:center;height:100vh}"
                  ".login-container{background:white;padding:2rem;border-radius:8px;box-shadow:0 4px 8px rgba(0,0,0,0.1)}"
                  "input{width:100%;padding:0.5rem;margin-bottom:1rem;border:1px solid #ccc;border-radius:4px}"
                  "button{width:100%;padding:0.7rem;background-color:#007bff;color:white;border:none;border-radius:4px;cursor:pointer}</style></head>"
                  "<body><div class='login-container'><h2>تسجيل الدخول إلى لوحة التحكم</h2><form method='post'>"
                  "<input type='email' name='email' placeholder='البريد الإلكتروني' required>"
                  "<input type='password' name='password' placeholder='كلمة المرور' required>"
                  "<button type='submit'>دخول</button></form></div></body></html>")
    return render_template_string(login_html)
@web_app.route("/dashboard")
def dashboard():
    db = DB()
    if 'user_id' not in session: return redirect(url_for('login'))
    user = db.get_user_by_id(session['user_id'])
    if not user: return redirect(url_for('login'))
    tasks = db.get_all_tasks()
    dashboard_html = ("<!DOCTYPE html><html lang='ar' dir='rtl'><head><meta charset='UTF-8'><title>لوحة التحكم</title>"
                      "<style>body{font-family:sans-serif;margin:2rem}.header{display:flex;justify-content:space-between;align-items:center}"
                      ".actions a{margin-right:1rem;text-decoration:none;padding:0.5rem 1rem;background:#007bff;color:white;border-radius:4px}"
                      "table{width:100%;border-collapse:collapse;margin-top:2rem}th,td{border:1px solid #ddd;padding:8px;text-align:right}tr:nth-child(even){background-color:#f2f2f2}</style></head>"
                      "<body><div class='header'><h1>لوحة تحكم المهام - مرحباً {{user.name}}</h1><div class='actions'><a href='{{ url_for('send_report') }}'>إرسال التقرير للبريد</a></div></div>"
                      "<table><thead><tr><th>#</th><th>الوصف</th><th>الحالة</th><th>تاريخ ووقت الاستحقاق</th></tr></thead>"
                      "<tbody>{% for t in tasks %}<tr><td>{{t.id}}</td><td>{{t.description}}</td><td>{{t.status.name}}</td><td>{{ t.due.strftime('%Y-%m-%d %H:%M') }}</td></tr>"
                      "{% else %}<tr><td colspan='4' style='text-align:center;'>لا توجد مهام حالياً.</td></tr>{% endfor %}</tbody></table></body></html>")
    return render_template_string(dashboard_html, user=user, tasks=tasks)
def generate_pdf_report(user, tasks):
    # This function will not work fully on Render's free tier as it needs a place to write the font file.
    # For a full solution, a paid plan or alternative PDF library might be needed.
    pdf = FPDF(); pdf.add_page(); pdf.set_font("Arial", size=12)
    pdf.cell(200, 10, txt=f"Task Report for {user.name}", ln=True, align="C")
    for t in tasks: pdf.cell(200, 10, txt=f"[{t.id}] {t.description} - Status: {t.status.name} - Due: {t.due.strftime('%Y-%m-%d %H:%M')}", ln=True)
    path = os.path.join(UPLOAD_DIR, f"report_{user.id}.pdf"); pdf.output(path); return path
def send_report_via_email(to_email, pdf_path):
    # This requires email credentials to be set correctly in Render's environment variables.
    if not EMAIL_FROM or not EMAIL_PASS:
        logger.warning("Email credentials are not set. Cannot send report.")
        return False
    msg = EmailMessage(); msg['Subject'] = 'Your TaskFlow Report'; msg['From'] = EMAIL_FROM; msg['To'] = to_email
    msg.set_content('Please find your task report attached.')
    with open(pdf_path, 'rb') as f: msg.add_attachment(f.read(), maintype='application', subtype='pdf', filename=os.path.basename(pdf_path))
    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as smtp: smtp.login(EMAIL_FROM, EMAIL_PASS); smtp.send_message(msg)
        return True
    except Exception as e: logger.error(f"Failed to send email: {e}"); return False
@web_app.route("/send_report")
def send_report():
    db = DB()
    if 'user_id' not in session: return redirect(url_for('login'))
    user = db.get_user_by_id(session['user_id'])
    if not user or not user.email: return "لا يوجد بريد إلكتروني مسجل."
    tasks = db.get_all_tasks(); pdf_path = generate_pdf_report(user, tasks)
    if send_report_via_email(user.email, pdf_path): return "📤 تم إرسال التقرير بنجاح."
    else: return "❌ حدث خطأ أثناء إرسال البريد."

# --------------------------- Main Application Runner --------------------------- #
async def run_bot():
    """This function will be started by the 'worker' service on Render."""
    if not TOKEN:
        logger.error("TELEGRAM_TOKEN is not set. Bot cannot start.")
        return

    telegram_app = Application.builder().token(TOKEN).build()
    telegram_app.add_handler(CommandHandler("start", start_command)); telegram_app.add_handler(CommandHandler("help", help_command))
    telegram_app.add_handler(CommandHandler("addtask", add_task_command)); telegram_app.add_handler(CommandHandler("mytasks", my_tasks_command))
    telegram_app.add_handler(CommandHandler("done", done_command)); telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_activation_code))
    
    scheduler = AsyncIOScheduler(timezone="Africa/Cairo")
    scheduler.add_job(check_due_tasks, 'interval', seconds=60, args=[telegram_app]); scheduler.start()
    print("Automated notification scheduler started.")
    
    print("🚀 Telegram Bot is running... This process will run indefinitely.")
    await telegram_app.run_polling()

if __name__ == "__main__":
    # This block is only executed when the script is run directly (for the worker).
    # The web service is started by Gunicorn and does not run this block.
    print("Starting bot worker process...")
    asyncio.run(run_bot())

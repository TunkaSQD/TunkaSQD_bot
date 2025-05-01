import nest_asyncio
import asyncio
import logging
import sqlite3
import datetime
import shutil
import os
from threading import Lock
from pathlib import Path
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, CallbackQueryHandler, CallbackContext
from enum import IntFlag, auto

nest_asyncio.apply()
db_lock = Lock()

# ==================== CONFIGURATION ====================
DB_NAME = 'archive.db'
BACKUP_DIR = 'backups'
ADMIN_IDS = [1474700452]
LOG_FILE = 'bot.log'
BACKUP_INTERVAL = 86400

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    filename=LOG_FILE
)
logger = logging.getLogger(__name__)

class Permission(IntFlag):
    NONE = 0
    ADD_COLOR = auto()
    MANAGE_ROLES = auto()
    VIEW_LOGS = auto()
    ADMIN = auto()

# ==================== DATABASE UTILITIES ====================
def create_database():
    Path(BACKUP_DIR).mkdir(exist_ok=True)
    
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    # Создание таблиц (остается без изменений)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS Colors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE CHECK(length(name) <= 50),
            red INTEGER NOT NULL CHECK(red >= 0 AND red <= 255),
            green INTEGER NOT NULL CHECK(green >= 0 AND green <= 255),
            blue INTEGER NOT NULL CHECK(blue >= 0 AND blue <= 255),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            created_by TEXT
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS Roles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE CHECK(length(name) <= 50),
            color TEXT NOT NULL DEFAULT '🖤' CHECK(length(color) <= 50),
            permissions INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            created_by TEXT,
            FOREIGN KEY (color) REFERENCES Colors(name) ON UPDATE CASCADE ON DELETE SET DEFAULT
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS Users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nickname TEXT NOT NULL UNIQUE CHECK(length(nickname) <= 50),
            tg_username TEXT NOT NULL DEFAULT '-' CHECK(length(tg_username) <= 50),
            registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS UserRoles (
            user_id INTEGER,
            role_id INTEGER,
            assigned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            assigned_by TEXT,
            PRIMARY KEY (user_id, role_id),
            FOREIGN KEY (user_id) REFERENCES Users(id) ON DELETE CASCADE,
            FOREIGN KEY (role_id) REFERENCES Roles(id) ON DELETE CASCADE
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS Logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            action TEXT NOT NULL,
            details TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES Users(id)
        )
    ''')
    
    # Обновленные базовые цвета с эмодзи
    base_colors = [
        ('🖤', 0, 0, 0),        # Чёрный
        ('🤍', 255, 255, 255),  # Белый
        ('🟥', 255, 0, 0),      # Красный
        ('🟩', 0, 255, 0),      # Зелёный
        ('🟦', 0, 0, 255),      # Синий
        ('🟨', 255, 255, 0),    # Жёлтый
        ('🟪', 128, 0, 128)     # Фиолетовый
    ]
    
    for color in base_colors:
        try:
            cursor.execute(
                "INSERT INTO Colors (name, red, green, blue, created_by) VALUES (?, ?, ?, ?, 'system')",
                color
            )
        except sqlite3.IntegrityError:
            pass
    
    # Обновленные базовые роли с эмодзи
    base_roles = [
        ('Администратор', '🟥', Permission.ADMIN),
        ('Модератор', '🟦', Permission.MANAGE_ROLES | Permission.ADD_COLOR),
        ('Пользователь', '🟩', Permission.NONE),
        ('Гость', '🤍', Permission.NONE)
    ]
    
    for role in base_roles:
        try:
            cursor.execute(
                "INSERT INTO Roles (name, color, permissions, created_by) VALUES (?, ?, ?, 'system')",
                (role[0], role[1], role[2])
            )
        except sqlite3.IntegrityError:
            pass
    
    conn.commit()
    conn.close()

def backup_database():
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_name = f"{BACKUP_DIR}/archive_{timestamp}.db"
    shutil.copy2(DB_NAME, backup_name)
    logger.info(f"Created database backup: {backup_name}")

def log_action(user_id: int, action: str, details: str = ""):
    with db_lock:
        conn = sqlite3.connect(DB_NAME)
        try:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO Logs (user_id, action, details) VALUES (?, ?, ?)",
                (user_id, action, details)
            )
            conn.commit()
        except Exception as e:
            logger.error(f"Ошибка при логировании: {str(e)}")
        finally:
            conn.close()

# ==================== SECURITY UTILITIES ====================
async def check_permission(update: Update, required_permission: Permission = Permission.NONE) -> bool:
    user_id = update.effective_user.id
    
    if user_id in ADMIN_IDS:
        return True
        
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    try:
        tg_username = f"@{update.effective_user.username}" if update.effective_user.username else str(user_id)
        
        cursor.execute('''
            SELECT r.permissions 
            FROM UserRoles ur
            JOIN Roles r ON ur.role_id = r.id
            JOIN Users u ON ur.user_id = u.id
            WHERE u.tg_username = ?
        ''', (tg_username,))
        
        combined_permissions = Permission.NONE
        for row in cursor.fetchall():
            combined_permissions |= row[0]
        
        return (combined_permissions & required_permission) == required_permission
    except Exception as e:
        logger.error(f"Permission check error: {str(e)}")
        return False
    finally:
        conn.close()

def get_user_id(update: Update) -> int:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    try:
        tg_username = f"@{update.effective_user.username}" if update.effective_user.username else str(update.effective_user.id)
        cursor.execute(
            "SELECT id FROM Users WHERE tg_username = ?",
            (tg_username,)
        )
        result = cursor.fetchone()
        return result[0] if result else None
    except Exception as e:
        logger.error(f"Error getting user ID: {str(e)}")
        return None
    finally:
        conn.close()

# ==================== COLOR COMMANDS ====================
async def color_add(update: Update, context: CallbackContext):
    if not await check_permission(update, Permission.ADD_COLOR):
        return
        
    if len(context.args) != 4:
        await update.message.reply_text("Ошибка! Используйте: /coloradd <emoji> <r> <g> <b>\nПример: /coloradd 🟥 255 0 0")
        return
    
    conn = sqlite3.connect(DB_NAME)
    try:
        emoji = context.args[0]
        red = int(context.args[1])
        green = int(context.args[2])
        blue = int(context.args[3])
        
        if not (0 <= red <= 255 and 0 <= green <= 255 and 0 <= blue <= 255):
            await update.message.reply_text("Значения RGB должны быть от 0 до 255!")
            return
            
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO Colors (name, red, green, blue, created_by) VALUES (?, ?, ?, ?, ?)",
            (emoji, red, green, blue, f"@{update.effective_user.username}")
        )
        
        conn.commit()
        await update.message.reply_text(f"✅ Цвет {emoji} успешно добавлен!")
        log_action(get_user_id(update), "ADD_COLOR", f"{emoji} ({red},{green},{blue})")
        
    except ValueError:
        await update.message.reply_text("Ошибка! RGB значения должны быть числами!")
    except sqlite3.IntegrityError:
        await update.message.reply_text("Ошибка! Цвет с таким эмодзи уже существует!")
    except Exception as e:
        logger.error(f"Error in color_add: {str(e)}")
        await update.message.reply_text("❌ Произошла ошибка при добавлении цвета!")
    finally:
        conn.close()

async def color_list(chat_id: int, context: CallbackContext):  # Измененная сигнатура
    conn = sqlite3.connect(DB_NAME)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT name, red, green, blue FROM Colors ORDER BY name")
        colors = cursor.fetchall()
        
        response = "🎨 Доступные цвета:\n"
        for color in colors:
            response += f"{color[0]} - RGB({color[1]}, {color[2]}, {color[3]})\n"
        
        await context.bot.send_message(chat_id=chat_id, text=response)
    except Exception as e:
        logger.error(f"Error in color_list: {str(e)}")
        await context.bot.send_message(chat_id=chat_id, text="❌ Ошибка при получении списка цветов")
    finally:
        conn.close()

# ==================== USER COMMANDS ====================
async def myid(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    await update.message.reply_text(f"Ваш Telegram ID: {user_id}")

async def pchangenick(update: Update, context: CallbackContext):
    if not context.args:
        await update.message.reply_text("Ошибка! Используйте: /pchangenick <новый_ник>")
        return
    
    new_nick = context.args[0].replace('@', '')
    tg_username = f"@{update.effective_user.username}" if update.effective_user.username else str(update.effective_user.id)
    
    conn = sqlite3.connect(DB_NAME)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT id, nickname FROM Users WHERE tg_username = ?", (tg_username,))
        user = cursor.fetchone()
        
        if user:
            cursor.execute("UPDATE Users SET nickname = ? WHERE tg_username = ?", (new_nick, tg_username))
            await update.message.reply_text(f"✅ Ник изменён на '{new_nick}'")
            log_action(user[0], "CHANGE_NICK", f"{user[1]} → {new_nick}")
        else:
            cursor.execute(
                "INSERT INTO Users (nickname, tg_username) VALUES (?, ?)",
                (new_nick, tg_username)
            )
            user_id = cursor.lastrowid
            await update.message.reply_text(f"✅ Пользователь '{new_nick}' зарегистрирован!")
            log_action(user_id, "REGISTER", new_nick)
        
        conn.commit()
    except sqlite3.IntegrityError:
        await update.message.reply_text("❌ Этот ник уже занят!")
    except Exception as e:
        logger.error(f"Ошибка в pchangenick: {str(e)}")
        await update.message.reply_text("❌ Произошла ошибка при обработке запроса")
        conn.rollback()
    finally:
        conn.close()

async def send_profile(chat_id: int, context: CallbackContext, tg_username: str):
    conn = sqlite3.connect(DB_NAME)
    try:
        cursor = conn.cursor()
        
        cursor.execute(
            "SELECT id, nickname FROM Users WHERE tg_username = ?",
            (tg_username,)
        )
        user = cursor.fetchone()
        
        if not user:
            await context.bot.send_message(chat_id=chat_id, text="❌ Вы не зарегистрированы! Используйте /pchangenick <ник>")
            return
            
        cursor.execute('''
            SELECT r.name, r.color 
            FROM UserRoles ur
            JOIN Roles r ON ur.role_id = r.id
            WHERE ur.user_id = ?
            ORDER BY r.name
        ''', (user[0],))
        
        roles = cursor.fetchall()
        
        response = "📝 Ваш профиль:\n"
        response += f"Ник: {user[1]}\n"
        if roles:
            role_list = [f"{role[1]} {role[0]}" for role in roles]
            response += "Роли: " + ", ".join(role_list)
        else:
            response += "Роли: нет назначенных ролей"
            
        await context.bot.send_message(chat_id=chat_id, text=response)
    except Exception as e:
        logger.error(f"Error in send_profile: {str(e)}")
        await context.bot.send_message(chat_id=chat_id, text="❌ Ошибка при получении профиля")
    finally:
        conn.close()

# ==================== ROLE MANAGEMENT ====================
async def role_new(update: Update, context: CallbackContext):
    if not await check_permission(update, Permission.MANAGE_ROLES):
        return
        
    if len(context.args) < 2:
        await update.message.reply_text("Ошибка! Используйте: /rolenew <name> <emoji>\nПример: /rolenew Модератор 🟦")
        return
    
    role_name = context.args[0]
    color_emoji = context.args[1]
    
    conn = sqlite3.connect(DB_NAME)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM Colors WHERE name = ?", (color_emoji,))
        if not cursor.fetchone():
            await update.message.reply_text("❌ Указанный цвет не существует! Сначала добавьте его через /coloradd")
            return
            
        cursor.execute(
            "INSERT INTO Roles (name, color, created_by) VALUES (?, ?, ?)",
            (role_name, color_emoji, f"@{update.effective_user.username}")
        )
        
        conn.commit()
        await update.message.reply_text(f"✅ Роль '{color_emoji} {role_name}' создана!")
        log_action(get_user_id(update), "CREATE_ROLE", f"{role_name} ({color_emoji})")
        
    except sqlite3.IntegrityError:
        await update.message.reply_text("❌ Роль с таким именем уже существует!")
    except Exception as e:
        logger.error(f"Error in role_new: {str(e)}")
        await update.message.reply_text("❌ Произошла ошибка при создании роли")
    finally:
        conn.close()

async def role_check(update: Update, context: CallbackContext):
    if not context.args:
        await update.message.reply_text("Ошибка! Используйте: /rolecheck <ник>")
        return
    
    nick = context.args[0].replace('@', '')
    
    conn = sqlite3.connect(DB_NAME)
    try:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT u.nickname, GROUP_CONCAT(r.color || ' ' || r.name, ', ') 
            FROM Users u
            LEFT JOIN UserRoles ur ON u.id = ur.user_id
            LEFT JOIN Roles r ON ur.role_id = r.id
            WHERE u.nickname = ?
            GROUP BY u.id
        ''', (nick,))
        
        user = cursor.fetchone()
        
        if not user:
            await update.message.reply_text(f"❌ Пользователь с ником '{nick}' не найден!")
            return
            
        roles = user[1] if user[1] else "нет ролей"
        await update.message.reply_text(f"👤 {user[0]}\nРоли: {roles}")
    except Exception as e:
        logger.error(f"Error in role_check: {str(e)}")
        await update.message.reply_text("❌ Произошла ошибка при проверке ролей")
    finally:
        conn.close()

async def role_assign(update: Update, context: CallbackContext):
    if not await check_permission(update, Permission.MANAGE_ROLES):
        return
        
    if len(context.args) != 2:
        await update.message.reply_text("Ошибка! Используйте: /roleassign <ник> <роль>")
        return
    
    nick = context.args[0].replace('@', '')
    role_name = context.args[1]
    
    conn = sqlite3.connect(DB_NAME)
    try:
        cursor = conn.cursor()
        
        cursor.execute("SELECT id FROM Roles WHERE name = ?", (role_name,))
        role = cursor.fetchone()
        if not role:
            await update.message.reply_text("❌ Указанная роль не существует!")
            return
            
        cursor.execute("SELECT id, tg_username FROM Users WHERE nickname = ?", (nick,))
        user = cursor.fetchone()
        if not user:
            await update.message.reply_text(f"❌ Пользователь с ником '{nick}' не найден!")
            return
            
        cursor.execute(
            "SELECT 1 FROM UserRoles WHERE user_id = ? AND role_id = ?",
            (user[0], role[0])
        )
        if cursor.fetchone():
            await update.message.reply_text(f"ℹ️ У пользователя '{nick}' уже есть роль '{role_name}'")
            return
            
        cursor.execute(
            "INSERT INTO UserRoles (user_id, role_id, assigned_by) VALUES (?, ?, ?)",
            (user[0], role[0], f"@{update.effective_user.username}")
        )
        await update.message.reply_text(f"✅ Пользователю '{nick}' назначена роль '{role_name}'")
        conn.commit()
        
    except Exception as e:
        logger.error(f"Error in role_assign: {str(e)}")
        await update.message.reply_text("❌ Произошла ошибка при назначении роли")
    finally:
        conn.close()

async def role_remove(update: Update, context: CallbackContext):
    if not await check_permission(update, Permission.MANAGE_ROLES):
        return
        
    if len(context.args) != 2:
        await update.message.reply_text("Ошибка! Используйте: /roleremove <ник> <роль>")
        return
    
    nick = context.args[0].replace('@', '')
    role_name = context.args[1]
    
    conn = sqlite3.connect(DB_NAME)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM Roles WHERE name = ?", (role_name,))
        role = cursor.fetchone()
        
        if not role:
            await update.message.reply_text("❌ Указанная роль не существует!")
            return
            
        cursor.execute("SELECT id FROM Users WHERE nickname = ?", (nick,))
        user = cursor.fetchone()
        
        if not user:
            await update.message.reply_text(f"❌ Пользователь с ником '{nick}' не найден!")
            return
            
        cursor.execute(
            "DELETE FROM UserRoles WHERE user_id = ? AND role_id = ?",
            (user[0], role[0]))
        
        if cursor.rowcount == 0:
            await update.message.reply_text(f"ℹ️ У пользователя '{nick}' нет роли '{role_name}'")
            return
            
        await update.message.reply_text(f"✅ У пользователя '{nick}' удалена роль '{role_name}'")
        log_action(user[0], "REMOVE_ROLE", role_name)
        conn.commit()
    except Exception as e:
        logger.error(f"Error in role_remove: {str(e)}")
        await update.message.reply_text("❌ Произошла ошибка при удалении роли")
    finally:
        conn.close()

async def role_list(chat_id: int, context: CallbackContext):  # Измененная сигнатура
    conn = sqlite3.connect(DB_NAME)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT name, color FROM Roles ORDER BY name")
        roles = cursor.fetchall()
        
        response = "👑 Актуальные роли:\n"
        for role in roles:
            response += f"- {role[1]} {role[0]}\n"
        
        await context.bot.send_message(chat_id=chat_id, text=response)
    except Exception as e:
        logger.error(f"Error in role_list: {str(e)}")
        await context.bot.send_message(chat_id=chat_id, text="❌ Ошибка при получении списка ролей")
    finally:
        conn.close()

# ==================== ADMIN COMMANDS ====================
async def backup_db(update: Update, context: CallbackContext):
    if not await check_permission(update, Permission.ADMIN):
        return
        
    try:
        backup_database()
        await update.message.reply_text("✅ Резервная копия базы данных создана!")
        log_action(get_user_id(update), "DB_BACKUP")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка при создании бэкапа: {str(e)}")
        logger.error(f"Backup failed: {str(e)}")

async def show_logs(update: Update, context: CallbackContext):
    if not await check_permission(update, Permission.VIEW_LOGS):
        return
        
    limit = 10
    if context.args and context.args[0].isdigit():
        limit = min(int(context.args[0]), 50)
    
    conn = sqlite3.connect(DB_NAME)
    try:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT l.timestamp, u.nickname, l.action, l.details 
            FROM Logs l
            LEFT JOIN Users u ON l.user_id = u.id
            ORDER BY l.timestamp DESC
            LIMIT ?
        ''', (limit,))
        
        logs = cursor.fetchall()
        
        if not logs:
            await update.message.reply_text("Логи отсутствуют!")
            return
            
        response = "📜 Последние действия:\n"
        for log in logs:
            response += (f"\n🕒 {log[0]}\n👤 {log[1] or 'System'}\n"
                       f"🔧 Действие: {log[2]}\n"
                       f"📝 Детали: {log[3] or 'нет'}\n")
        
        await update.message.reply_text(response)
    except Exception as e:
        logger.error(f"Error in show_logs: {str(e)}")
        await update.message.reply_text("❌ Произошла ошибка при получении логов")
    finally:
        conn.close()

# ==================== INTERFACE HANDLERS ====================
async def button_handler(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()
    
    try:
        chat_id = query.message.chat_id  # Получаем chat_id из сообщения
        user = query.from_user
        tg_username = f"@{user.username}" if user.username else str(user.id)
        
        if query.data == 'colors':
            await color_list(chat_id, context)
        elif query.data == 'roles':
            await role_list(chat_id, context)
        elif query.data == 'profile':
            await send_profile(chat_id, context, tg_username)
        elif query.data == 'help':
            await show_help(update, context)
            
    except Exception as e:
        logger.error(f"Ошибка обработки кнопки: {str(e)}")
        # Обновляем обработчики команд
async def color_list_command(update: Update, context: CallbackContext):
    await color_list(update.effective_chat.id, context)

async def role_list_command(update: Update, context: CallbackContext):
    await role_list(update.effective_chat.id, context)

async def start(update: Update, context: CallbackContext):
    keyboard = [
        [InlineKeyboardButton("🎨 Цвета", callback_data='colors'),
         InlineKeyboardButton("👑 Роли", callback_data='roles')],
        [InlineKeyboardButton("👤 Профиль", callback_data='profile'),
         InlineKeyboardButton("ℹ️ Помощь", callback_data='help')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("🏛 Добро пожаловать в Архив!", reply_markup=reply_markup)

async def show_help(update: Update, context: CallbackContext):
    help_text = """
📚 Доступные команды:

👤 Основные команды:
/start - Главное меню
/myid - Показать ваш Telegram ID
/pchangenick <ник> - Изменить/создать ваш ник
"Архивариус Кто я" - Показать ваш профиль

🎨 Управление цветами:
/coloradd <emoji> <r> <g> <b> - Добавить цвет (требуются права)
/colorlist - Показать доступные цвета

👑 Управление ролями:
/rolenew <name> <emoji> - Создать роль (требуются права)
/roleassign <ник> <роль> - Назначить роль пользователю
/roleremove <ник> <роль> - Удалить роль у пользователя
/rolelist - Список всех ролей
/rolecheck <ник> - Проверить роли участника

⚙️ Администрирование:
/backup - Создать резервную копию (только для админов)
/logs [n] - Показать последние n логов (по умолчанию 10)

📌 Кнопки в меню:
🎨 Цвета - Показать список цветов
👑 Роли - Показать список ролей
👤 Профиль - Показать ваш профиль
ℹ️ Помощь - Показать это сообщение
"""
    await context.bot.send_message(chat_id=update.effective_chat.id, text=help_text)

async def message_handler(update: Update, context: CallbackContext):
    text = update.message.text.lower()
    user = update.effective_user
    tg_username = f"@{user.username}" if user.username else str(user.id)
    
    if text == "архивариус кто я":
        await send_profile(update.effective_chat.id, context, tg_username)
    elif text == "архивариус помощь":
        await show_help(update, context)

# ==================== MAIN ====================
async def main():
    create_database()
    backup_database()
    TOKEN = os.environ.get('8148255065:AAGlIXBDT5j76yrRlHfJm0OsElpMmIOq4AI')
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("myid", myid))
    app.add_handler(CommandHandler("help", show_help))
    app.add_handler(CommandHandler("coloradd", color_add))
    app.add_handler(CommandHandler("pchangenick", pchangenick))
    app.add_handler(CommandHandler("rolenew", role_new))
    app.add_handler(CommandHandler("colorlist", color_list_command))
    app.add_handler(CommandHandler("rolelist", role_list_command))
    app.add_handler(CommandHandler("roleassign", role_assign))
    app.add_handler(CommandHandler("roleremove", role_remove))
    app.add_handler(CommandHandler("rolelist", role_list))  # Исправленный обработчик
    app.add_handler(CommandHandler("rolecheck", role_check))
    app.add_handler(CommandHandler("backup", backup_db))
    app.add_handler(CommandHandler("logs", show_logs))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    
    job_queue = app.job_queue
    job_queue.run_repeating(
        lambda _: backup_database(),
        interval=BACKUP_INTERVAL,
        first=10
    )
    
    await app.run_polling()

if __name__ == '__main__':
    asyncio.run(main())
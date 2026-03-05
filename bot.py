import asyncio
import logging
import json
import os
import time
import secrets
import hashlib
import sys
import traceback
from datetime import datetime
from threading import Thread, Lock
from flask import Flask, render_template, request, jsonify, redirect, session, make_response
from functools import wraps
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.exceptions import TelegramAPIError
import warnings
warnings.filterwarnings("ignore")

logging.basicConfig(level=logging.CRITICAL)
for lib in ("aiogram", "aiogram.dispatcher", "asyncio"):
    logging.getLogger(lib).setLevel(logging.CRITICAL)

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATA_FILE = "data.json"
ADMINS_FILE = "admins.json"
SECRET_KEY = os.getenv("SECRET_KEY", "change-me-in-production")

# ✅ ПРАВИЛЬНАЯ КОНФИГУРАЦИЯ ДЛЯ BOTHOST
# Порт из переменной окружения (по умолчанию 5000)
PORT = int(os.getenv("PORT", 5000))
# Слушаем на 0.0.0.0 (для Reverse Proxy)
HOST = "0.0.0.0"

_sessions: dict = {}
SESSION_TTL = 60 * 60 * 8
_logs: list = []
_logs_lock = Lock()

# ===== ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ БОТА =====
bot = None
dp = None
bot_event_loop = None

def add_log(level: str, message: str, user_id=None):
    """Добавляет логи с потокобезопасностью"""
    try:
        with _logs_lock:
            _logs.append({
                "time": datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
                "level": level,
                "message": message,
                "user_id": user_id,
            })
            if len(_logs) > 500:
                _logs.pop(0)
    except Exception as e:
        print(f"❌ Ошибка логирования: {e}")

def hash_pw(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

def load_admins() -> list:
    """Загружает администраторов"""
    if not os.path.exists(ADMINS_FILE):
        admins = [{
            "login": "Bludu455",
            "password_hash": hash_pw("gTa8p4S1qWh8I5IQIxS33EHUYlWnyQAu"),
            "created": "по умолчанию",
            "super": True
        }]
        save_admins(admins)
        return admins
    try:
        with open(ADMINS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        add_log("ERROR", f"Ошибка загрузки admins.json: {e}")
        return []

def save_admins(admins: list):
    """Сохраняет администраторов"""
    try:
        with open(ADMINS_FILE, "w", encoding="utf-8") as f:
            json.dump(admins, f, ensure_ascii=False, indent=2)
    except Exception as e:
        add_log("ERROR", f"Ошибка сохранения admins.json: {e}")

def find_admin(login: str) -> dict | None:
    return next((a for a in load_admins() if a["login"] == login), None)

def load_data() -> dict:
    """Загружает данные"""
    if not os.path.exists(DATA_FILE):
        return {
            "channels": [],
            "file_url": "",
            "start_text": "👋 Привет! Подпишись на каналы ниже, чтобы получить файл.",
            "wait_minutes": 0,
            "wait_enabled": False,
            "link_delete_seconds": 0,
            "users": {},
            "banned": [],
        }
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        add_log("ERROR", f"Ошибка загрузки data.json: {e}")
        return {
            "channels": [],
            "file_url": "",
            "start_text": "👋 Привет! Подпишись на каналы ниже, чтобы получить файл.",
            "wait_minutes": 0,
            "wait_enabled": False,
            "link_delete_seconds": 0,
            "users": {},
            "banned": [],
        }

def save_data(data: dict):
    """Сохраняет данные"""
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        add_log("ERROR", f"Ошибка сохранения data.json: {e}")

def get_session_user():
    """Получает текущего пользователя сессии"""
    token = request.cookies.get("session")
    if not token or token not in _sessions:
        return None
    s = _sessions[token]
    if time.time() > s["expires"]:
        del _sessions[token]
        return None
    return s

def require_auth(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not get_session_user():
            if request.path.startswith("/api/"):
                return jsonify({"error": "unauthorized"}), 401
            return redirect("/login")
        return f(*args, **kwargs)
    return decorated_function

app = Flask(__name__)
app.config['JSON_AS_ASCII'] = False
app.secret_key = SECRET_KEY

# ===== ФУНКЦИИ БОТА =====

async def check_subscriptions(user_id: int, channels: list) -> list:
    """Проверяет подписки"""
    if not bot:
        return channels
    not_subbed = []
    for ch in channels:
        try:
            m = await bot.get_chat_member(ch["id"], user_id)
            if m.status in ("left", "kicked", "banned"):
                not_subbed.append(ch)
        except TelegramAPIError as e:
            add_log("WARN", f"Ошибка проверки канала {ch['id']}: {e}")
            not_subbed.append(ch)
        except Exception as e:
            add_log("ERROR", f"Неожиданная ошибка проверки {ch['id']}: {e}")
            not_subbed.append(ch)
    return not_subbed

def build_sub_keyboard(channels: list) -> InlineKeyboardMarkup:
    """Создаёт клавиатуру подписки"""
    rows = []
    for i, ch in enumerate(channels, 1):
        rows.append([InlineKeyboardButton(
            text=f"📢 Подпишись #{i} — {ch.get('name', ch['id'])}",
            url=ch.get("url", f"https://t.me/{ch['id'].lstrip('@')}")
        )])
    rows.append([InlineKeyboardButton(text="✅ Я подписался!", callback_data="check_sub")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def send_file_async(user, data: dict, from_cb=False):
    """Отправляет файл пользователю"""
    if not bot:
        return
    try:
        uid = user["id"] if isinstance(user, dict) else user.id
        file_url = data.get("file_url", "")
        wait_enabled = data.get("wait_enabled", False)
        wait_min = data.get("wait_minutes", 0)
        del_secs = data.get("link_delete_seconds", 0)
        udata = data["users"].get(str(uid), {})

        if wait_enabled and wait_min > 0:
            ws = udata.get("wait_start")
            if not ws:
                data["users"][str(uid)]["wait_start"] = time.time()
                save_data(data)
                await bot.send_message(uid, f"⏳ <b>Почти готово!</b>\n\nФайл будет доступен через <b>{wait_min} мин.</b>\nНажми /start снова, когда время истечёт.", parse_mode="HTML")
                add_log("INFO", f"Таймер ожидания запущен для ID: {uid} на {wait_min} мин.", uid)
                return
            elapsed = (time.time() - ws) / 60
            if elapsed < wait_min:
                rem = wait_min - elapsed
                await bot.send_message(uid, f"⏳ Подождите ещё <b>{rem:.1f} мин.</b>", parse_mode="HTML")
                return
            data["users"][str(uid)]["wait_start"] = None

        data["users"][str(uid)]["subscribed"] = True
        data["users"][str(uid)]["in_channel"] = True
        save_data(data)

        if file_url:
            sent = await bot.send_message(uid, f"✅ <b>Спасибо за подписку!</b>\n\n🔗 Вот ваша ссылка:\n{file_url}", parse_mode="HTML")
            add_log("INFO", f"Ссылка отправлена пользователю ID: {uid}", uid)
            if del_secs and del_secs > 0:
                asyncio.create_task(_delete_later(uid, sent.message_id, del_secs))
        else:
            await bot.send_message(uid, "✅ Вы подписаны! Ссылка пока не задана администратором.")
    except TelegramAPIError as e:
        add_log("ERROR", f"Ошибка отправки файла пользователю: {e}")
    except Exception as e:
        add_log("ERROR", f"Неожиданная ошибка send_file_async: {e}")

async def _delete_later(uid: int, msg_id: int, delay: int):
    """Удаляет сообщение через указанный промежуток времени"""
    if not bot:
        return
    try:
        await asyncio.sleep(delay)
        await bot.delete_message(uid, msg_id)
        await bot.send_message(uid, "⏰ Ссылка была удалена по истечении срока действия.")
        add_log("INFO", f"Ссылка удалена у пользователя ID: {uid} (авто-удаление)", uid)
    except TelegramAPIError as e:
        add_log("WARN", f"Не удалось удалить сообщение у {uid}: {e}")
    except Exception as e:
        add_log("ERROR", f"Ошибка _delete_later: {e}")

async def setup_handlers(dp: Dispatcher):
    """Настраивает обработчики сообщений"""
    
    @dp.message(Command("start"))
    async def cmd_start(message: types.Message):
        try:
            uid = message.from_user.id
            name = message.from_user.username or message.from_user.first_name
            data = load_data()

            if str(uid) not in data["users"]:
                data["users"][str(uid)] = {
                    "id": uid, 
                    "username": name, 
                    "first_name": message.from_user.first_name,
                    "joined": datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
                    "subscribed": False, 
                    "role": "member", 
                    "wait_start": None, 
                    "in_channel": False,
                }
                save_data(data)
                add_log("INFO", f"Новый пользователь: {name} (ID: {uid})", uid)

            if uid in data.get("banned", []):
                await message.answer("🚫 Вы заблокированы и не можете использовать этого бота.")
                add_log("WARN", f"Заблокированный пользователь попытался зайти (ID: {uid})", uid)
                return

            channels = data.get("channels", [])
            if not channels:
                await message.answer("⚙️ Бот ещё не настроен администратором.")
                return

            not_subbed = await check_subscriptions(uid, channels)
            start_text = data.get("start_text", "👋 Подпишись на каналы, чтобы получить файл.")

            if not_subbed:
                kb = build_sub_keyboard(not_subbed)
                await message.answer(start_text, reply_markup=kb, parse_mode="HTML")
                add_log("INFO", f"Пользователь {name} не подписан на {len(not_subbed)} канал(а)", uid)
            else:
                await send_file_async(message.from_user, data)
        except Exception as e:
            add_log("ERROR", f"Ошибка в cmd_start: {e}")
            print(f"❌ Ошибка обработки /start: {e}")

    @dp.callback_query(F.data == "check_sub")
    async def on_check_sub(cb: types.CallbackQuery):
        try:
            uid = cb.from_user.id
            data = load_data()
            if uid in data.get("banned", []):
                await cb.answer("🚫 Вы заблокированы.", show_alert=True)
                return
            not_subbed = await check_subscriptions(uid, data.get("channels", []))
            if not_subbed:
                names = ", ".join(ch.get("name", ch["id"]) for ch in not_subbed)
                await cb.answer(f"❌ Ещё не подписаны на: {names}", show_alert=True)
                return
            await cb.message.delete()
            await send_file_async(cb.from_user, data, from_cb=True)
            await cb.answer()
        except Exception as e:
            add_log("ERROR", f"Ошибка в on_check_sub: {e}")
            print(f"❌ Ошибка обработки callback: {e}")

async def start_bot_polling():
    """Запуск бота с обработкой ошибок и автоперезагрузкой"""
    global bot, dp
    
    retry_count = 0
    max_retries = 10
    
    while True:
        try:
            if bot is None:
                bot = Bot(token=BOT_TOKEN)
                dp = Dispatcher(storage=MemoryStorage())
                await setup_handlers(dp)
                add_log("INFO", "🌸 Диспетчер инициализирован")
                print("✅ Диспетчер инициализирован")

            print(f"🔄 Начинаю polling (попытка {retry_count + 1})...")
            await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
            
        except asyncio.CancelledError:
            print("⏹️  Polling отменён")
            add_log("INFO", "Polling отменён")
            break
            
        except TelegramAPIError as e:
            retry_count += 1
            wait_time = min(2 ** retry_count, 60)
            add_log("WARN", f"Ошибка API Telegram (попытка {retry_count}): {e}")
            print(f"⚠️  Ошибка API: {e}. Повтор через {wait_time} сек...")
            await asyncio.sleep(wait_time)
            
        except Exception as e:
            retry_count += 1
            wait_time = min(2 ** retry_count, 60)
            add_log("ERROR", f"Ошибка polling (попытка {retry_count}): {e}")
            print(f"❌ Ошибка: {e}. Повтор через {wait_time} сек...")
            print(traceback.format_exc())
            await asyncio.sleep(wait_time)
            
            if retry_count >= max_retries:
                add_log("CRITICAL", f"Максимальное количество попыток ({max_retries}) исчерпано")
                print(f"⚠️ Максимальное количество попыток исчерпано, завершаю...")
                break

def run_bot_in_thread():
    """Запускает бота в отдельном потоке с собственным event loop"""
    global bot_event_loop
    
    # Создаём новый event loop для этого потока
    bot_event_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(bot_event_loop)
    
    # Политика для Windows
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    
    try:
        # Запускаем бота с polling
        bot_event_loop.run_until_complete(start_bot_polling())
    except Exception as e:
        add_log("CRITICAL", f"Критическая ошибка потока бота: {e}")
        print(f"❌ Критическая ошибка потока: {e}")
        print(traceback.format_exc())
    finally:
        print("🛑 Закрываю event loop...")
        bot_event_loop.close()

# ===== FLASK МАРШРУТЫ =====

@app.route("/", methods=["GET"])
def page_index():
    sess = get_session_user()
    if not sess:
        return redirect("/login")
    return render_template("index.html")

@app.route("/login", methods=["GET"])
def page_login():
    return render_template("login.html")

@app.route("/api/login", methods=["POST"])
def api_login():
    try:
        data_req = request.get_json()
        login, password = data_req.get("login", "").strip(), data_req.get("password", "")
        admin = find_admin(login)
        if not admin or admin["password_hash"] != hash_pw(password):
            add_log("WARN", f"Неудачная попытка входа: логин «{login}»")
            return jsonify({"ok": False, "error": "Неверный логин или пароль"}), 401
        token = secrets.token_hex(32)
        _sessions[token] = {"login": login, "expires": time.time() + SESSION_TTL, "super": admin.get("super", False)}
        add_log("INFO", f"🔑 Вход выполнен: «{login}»")
        resp = make_response(jsonify({"ok": True}))
        resp.set_cookie("session", token, httponly=True, max_age=SESSION_TTL, path="/", samesite="Lax")
        return resp
    except Exception as e:
        add_log("ERROR", f"Ошибка в api_login: {e}")
        return jsonify({"ok": False, "error": "Ошибка сервера"}), 500

@app.route("/api/logout", methods=["POST"])
def api_logout():
    try:
        token = request.cookies.get("session")
        if token and token in _sessions:
            add_log("INFO", f"🚪 Выход: «{_sessions[token].get('login','?')}»")
            del _sessions[token]
        resp = make_response(redirect("/login"))
        resp.delete_cookie("session")
        return resp
    except Exception as e:
        add_log("ERROR", f"Ошибка в api_logout: {e}")
        return jsonify({"ok": False, "error": "Ошибка сервера"}), 500

@app.route("/api/data", methods=["GET"])
@require_auth
def api_data():
    try:
        return jsonify(load_data())
    except Exception as e:
        add_log("ERROR", f"Ошибка в api_data: {e}")
        return jsonify({"error": "Ошибка загрузки данных"}), 500

@app.route("/api/save", methods=["POST"])
@require_auth
def api_save():
    try:
        data_req = request.get_json()
        data = load_data()
        for key in ("channels","file_url","start_text","wait_minutes","wait_enabled","link_delete_seconds"):
            if key in data_req:
                data[key] = data_req[key]
        save_data(data)
        add_log("INFO", "⚙️ Настройки обновлены через панель")
        return jsonify({"ok": True})
    except Exception as e:
        add_log("ERROR", f"Ошибка в api_save: {e}")
        return jsonify({"ok": False, "error": "Ошибка сохранения"}), 500

@app.route("/api/admins", methods=["GET"])
@require_auth
def api_admins_get():
    try:
        admins = load_admins()
        return jsonify([{"login": a["login"], "created": a.get("created","—"), "super": a.get("super", False)} for a in admins])
    except Exception as e:
        add_log("ERROR", f"Ошибка в api_admins_get: {e}")
        return jsonify({"error": "Ошибка загрузки администраторов"}), 500

@app.route("/api/admins/add", methods=["POST"])
@require_auth
def api_admins_add():
    try:
        sess = get_session_user()
        data_req = request.get_json()
        login, password = data_req.get("login", "").strip(), data_req.get("password", "").strip()
        if not login or not password:
            return jsonify({"ok": False, "error": "Заполните логин и пароль"}), 400
        admins = load_admins()
        if any(a["login"] == login for a in admins):
            return jsonify({"ok": False, "error": "Такой логин уже существует"}), 400
        admins.append({"login": login, "password_hash": hash_pw(password), "created": datetime.now().strftime("%d.%m.%Y %H:%M"), "super": False})
        save_admins(admins)
        add_log("INFO", f"➕ Добавлен администратор «{login}» (автор: {sess['login']})")
        return jsonify({"ok": True})
    except Exception as e:
        add_log("ERROR", f"Ошибка в api_admins_add: {e}")
        return jsonify({"ok": False, "error": "Ошибка сервера"}), 500

@app.route("/api/admins/remove", methods=["POST"])
@require_auth
def api_admins_remove():
    try:
        sess, data_req = get_session_user(), request.get_json()
        login = data_req.get("login", "")
        admins = load_admins()
        target = next((a for a in admins if a["login"] == login), None)
        if not target:
            return jsonify({"ok": False, "error": "Админ не найден"}), 404
        if target.get("super"):
            return jsonify({"ok": False, "error": "Нельзя удалить суперадмина"}), 403
        if login == sess["login"]:
            return jsonify({"ok": False, "error": "Нельзя удалить себя"}), 403
        save_admins([a for a in admins if a["login"] != login])
        add_log("WARN", f"🗑 Администратор «{login}» удалён (автор: {sess['login']})")
        return jsonify({"ok": True})
    except Exception as e:
        add_log("ERROR", f"Ошибка в api_admins_remove: {e}")
        return jsonify({"ok": False, "error": "Ошибка сервера"}), 500

@app.route("/api/admins/change-password", methods=["POST"])
@require_auth
def api_change_pw():
    try:
        sess, data_req = get_session_user(), request.get_json()
        login, new_pw = data_req.get("login", sess["login"]), data_req.get("password", "").strip()
        if not new_pw or len(new_pw) < 4:
            return jsonify({"ok": False, "error": "Пароль должен быть от 4 символов"}), 400
        admins = load_admins()
        for a in admins:
            if a["login"] == login:
                a["password_hash"] = hash_pw(new_pw)
                break
        save_admins(admins)
        add_log("INFO", f"🔑 Смена пароля для «{login}» (автор: {sess['login']})")
        return jsonify({"ok": True})
    except Exception as e:
        add_log("ERROR", f"Ошибка в api_change_pw: {e}")
        return jsonify({"ok": False, "error": "Ошибка сервера"}), 500

@app.route("/api/broadcast", methods=["POST"])
@require_auth
def api_broadcast():
    try:
        if not bot:
            return jsonify({"ok": False, "error": "Бот не инициализирован"}), 500
        data_req = request.get_json()
        text, target = data_req.get("text", ""), data_req.get("target", "all")
        data = load_data()
        targets = ([int(u) for u in data["users"] if int(u) not in data.get("banned", [])] if target == "all" else [int(target)])
        
        async def _broadcast():
            sent = errors = 0
            for uid in targets:
                try:
                    await bot.send_message(uid, text, parse_mode="HTML")
                    sent += 1
                    await asyncio.sleep(0.05)
                except TelegramAPIError as e:
                    errors += 1
                    add_log("ERROR", f"Ошибка рассылки → ID {uid}: {e}")
                except Exception as e:
                    errors += 1
                    add_log("ERROR", f"Неожиданная ошибка при рассылке {uid}: {e}")
            add_log("INFO", f"📨 Рассылка завершена: отправлено {sent}, ошибок {errors}")
            return {"sent": sent, "errors": errors}
        
        try:
            future = asyncio.run_coroutine_threadsafe(_broadcast(), bot_event_loop)
            result = future.result(timeout=60)
            return jsonify({"ok": True, "sent": result["sent"], "errors": result["errors"]})
        except Exception as e:
            add_log("ERROR", f"Ошибка в broadcast: {e}")
            return jsonify({"ok": False, "error": str(e)}), 500
    except Exception as e:
        add_log("ERROR", f"Ошибка в api_broadcast: {e}")
        return jsonify({"ok": False, "error": "Ошибка сервера"}), 500

@app.route("/api/reply", methods=["POST"])
@require_auth
def api_reply():
    try:
        if not bot:
            return jsonify({"ok": False, "error": "Бот не инициализирован"}), 500
        data_req = request.get_json()
        uid, text = int(data_req["user_id"]), data_req.get("text", "")
        
        async def _reply():
            try:
                await bot.send_message(uid, text, parse_mode="HTML")
                add_log("INFO", f"💬 Ответ отправлен пользователю ID: {uid}")
                return True
            except TelegramAPIError as e:
                add_log("ERROR", f"Ошибка ответа → ID {uid}: {e}")
                return False
            except Exception as e:
                add_log("ERROR", f"Неожиданная ошибка при ответе {uid}: {e}")
                return False
        
        try:
            future = asyncio.run_coroutine_threadsafe(_reply(), bot_event_loop)
            result = future.result(timeout=10)
            return jsonify({"ok": result})
        except Exception as e:
            add_log("ERROR", f"Ошибка в reply: {e}")
            return jsonify({"ok": False, "error": str(e)})
    except Exception as e:
        add_log("ERROR", f"Ошибка в api_reply: {e}")
        return jsonify({"ok": False, "error": "Ошибка сервера"}), 500

@app.route("/api/ban", methods=["POST"])
@require_auth
def api_ban():
    try:
        data_req = request.get_json()
        uid, action = int(data_req["user_id"]), data_req.get("action", "ban")
        data = load_data()
        if action == "ban":
            if uid not in data["banned"]:
                data["banned"].append(uid)
            add_log("WARN", f"🚫 Пользователь ID: {uid} заблокирован")
        else:
            data["banned"] = [u for u in data["banned"] if u != uid]
            add_log("INFO", f"✅ Пользователь ID: {uid} разблокирован")
        save_data(data)
        return jsonify({"ok": True})
    except Exception as e:
        add_log("ERROR", f"Ошибка в api_ban: {e}")
        return jsonify({"ok": False, "error": "Ошибка сервера"}), 500

@app.route("/api/logs", methods=["GET"])
@require_auth
def api_logs():
    try:
        with _logs_lock:
            return jsonify(_logs[-200:])
    except Exception as e:
        add_log("ERROR", f"Ошибка в api_logs: {e}")
        return jsonify({"error": "Ошибка загрузки логов"}), 500

@app.route("/api/stats", methods=["GET"])
@require_auth
def api_stats():
    try:
        data = load_data()
        return jsonify({
            "total": len(data["users"]),
            "subscribed": sum(1 for u in data["users"].values() if u.get("subscribed")),
            "in_channel": sum(1 for u in data["users"].values() if u.get("in_channel")),
            "banned": len(data.get("banned", [])),
        })
    except Exception as e:
        add_log("ERROR", f"Ошибка в api_stats: {e}")
        return jsonify({"error": "Ошибка загрузки статистики"}), 500

@app.route("/api/me", methods=["GET"])
@require_auth
def api_me():
    try:
        sess = get_session_user()
        return jsonify({"login": sess["login"], "super": sess.get("super", False)})
    except Exception as e:
        add_log("ERROR", f"Ошибка в api_me: {e}")
        return jsonify({"error": "Ошибка загрузки профиля"}), 500

# ===== ИНИЦИАЛИЗАЦИЯ И ЗАПУСК =====

def init_app():
    """Инициализирует приложение"""
    load_admins()
    print("📱 Приложение загружено")
    print("🚀 Запуск Telegram бота в отдельном потоке...")
    
    # Запускаем бота в отдельном потоке
    bot_thread = Thread(target=run_bot_in_thread, daemon=True, name="BotThread")
    bot_thread.start()
    print("✅ Telegram бот запущен")
    
    return bot_thread

if __name__ == "__main__":
    print("\n" + "="*60)
    print("🌐 TELEGRAM BOT MANAGEMENT SYSTEM")
    print("="*60)
    print(f"🔧 Конфигурация Bothost:")
    print(f"   HOST: {HOST}")
    print(f"   PORT: {PORT}")
    print(f"   URL: http://{HOST}:{PORT}")
    print("="*60 + "\n")
    
    # Инициализируем приложение
    bot_thread = init_app()
    
    print(f"🌐 Flask запущен на {HOST}:{PORT}")
    print(f"📖 Откройте в браузере: https://ваш-домен.bothost.ru")
    print("="*60 + "\n")
    
    try:
        # Запуск Flask - это ОСНОВНОЙ поток приложения
        # ✅ Важно: host="0.0.0.0" для работы с Reverse Proxy
        app.run(
            host=HOST,
            port=PORT,
            debug=False,
            use_reloader=False,
            threaded=True
        )
    except KeyboardInterrupt:
        print("\n🛑 Flask остановлен пользователем")
        add_log("INFO", "Приложение остановлено пользователем")
    except Exception as e:
        print(f"\n❌ Ошибка Flask: {e}")
        print(traceback.format_exc())
        add_log("CRITICAL", f"Ошибка Flask: {e}")
    finally:
        print("✅ Приложение полностью остановлено")

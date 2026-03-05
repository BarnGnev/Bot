import asyncio
import logging
import json
import os
import time
import hashlib
import sys
import traceback
from datetime import datetime
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

BOT_TOKEN = os.getenv("BOT_TOKEN", "8410763877:AAHIJhv7L4HjZBUMund8Nh_VjhV1M0NSq1U")
DATA_FILE = "data.json"
ADMINS_FILE = "admins.json"

_logs: list = []

def add_log(level: str, message: str, user_id=None):
    """Добавляет логи"""
    try:
        _logs.append({
            "time": datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
            "level": level,
            "message": message,
            "user_id": user_id,
        })
        if len(_logs) > 500:
            _logs.pop(0)
        print(f"[{level}] {message}")
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

# ===== ИНИЦИАЛИЗАЦИЯ БОТА =====

bot = None
dp = None

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
            add_log("ERROR", f"Ошибка проверки {ch['id']}: {e}")
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
        add_log("ERROR", f"Ошибка отправки файла: {e}")
    except Exception as e:
        add_log("ERROR", f"Ошибка send_file_async: {e}")

async def _delete_later(uid: int, msg_id: int, delay: int):
    """Удаляет сообщение через указанный промежуток времени"""
    if not bot:
        return
    try:
        await asyncio.sleep(delay)
        await bot.delete_message(uid, msg_id)
        await bot.send_message(uid, "⏰ Ссылка была удалена по истечении срока действия.")
        add_log("INFO", f"Ссылка удалена у пользователя ID: {uid}", uid)
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
                add_log("WARN", f"Заблокированный пользователь (ID: {uid})", uid)
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

async def main():
    """Главная функция - запуск бота с автоперезагрузкой"""
    global bot, dp
    
    retry_count = 0
    max_retries = 10
    
    while True:
        try:
            if bot is None:
                bot = Bot(token=BOT_TOKEN)
                dp = Dispatcher(storage=MemoryStorage())
                await setup_handlers(dp)
                add_log("INFO", "🌸 Бот инициализирован")
                print("✅ Бот инициализирован")

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
                print(f"⚠️ Максимальное количество попыток исчерпано")
                break

if __name__ == "__main__":
    print("\n" + "="*60)
    print("🤖 TELEGRAM BOT (БЕЗ ПОРТОВ)")
    print("="*60)
    
    load_admins()
    print("📱 Бот загружен")
    print("🚀 Запуск polling...")
    print("="*60 + "\n")
    
    try:
        # Запуск бота с asyncio
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 Бот остановлен пользователем")
        add_log("INFO", "Бот остановлен")
    except Exception as e:
        print(f"\n❌ Критическая ошибка: {e}")
        print(traceback.format_exc())
        add_log("CRITICAL", f"Критическая ошибка: {e}")
    finally:
        print("✅ Бот полностью остановлен")
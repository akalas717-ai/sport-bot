import asyncio
import sqlite3
import re
import os
import logging
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiohttp import web
import aiohttp

# ========== НАЛАШТУВАННЯ ==========
TOKEN = "8394512581:AAED1pOf6ZPPgXQ_pKUiq_oVY46eo1cVMgE"
GROUP_ID = -1002216755275
SOLD_KEYWORDS = ["ПРОДАНО", "SOLD", "НЕМАЄ", "ЗАБРАНО", "ПРОДАЛ"]
# =================================

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

PORT = int(os.environ.get("PORT", 10000))
RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL", "")
KEEP_ALIVE_URL = RENDER_URL.rstrip("/") + "/health" if RENDER_URL else None

conn = sqlite3.connect("shop.db")
cursor = conn.cursor()
cursor.execute("""
    CREATE TABLE IF NOT EXISTS products (
        id INTEGER PRIMARY KEY,
        message_id INTEGER,
        text TEXT,
        sizes TEXT
    )
""")
conn.commit()

bot = Bot(token=TOKEN)
dp = Dispatcher()

def extract_sizes(text):
    """Знаходить всі розміри в тексті"""
    sizes_found = []
    # Шукаємо всі можливі розміри (латиниця та кирилиця)
    pattern = r'\b(S|M|L|XL|XXL|М|ХL|ХХL)\b'
    matches = re.findall(pattern, text.upper())
    
    # Конвертуємо кирилицю в латиницю
    convert = {'М': 'M', 'ХL': 'XL', 'ХХL': 'XXL'}
    for m in matches:
        m_converted = convert.get(m, m)
        if m_converted not in sizes_found:
            sizes_found.append(m_converted)
    return sizes_found

def is_sold(text):
    """Перевіряє, чи товар продано"""
    if not text:
        return False
    text_upper = text.upper()
    for keyword in SOLD_KEYWORDS:
        if keyword.upper() in text_upper:
            return True
    return False

@dp.message(Command("start"))
async def start(message: types.Message):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="S", callback_data="size_S"),
         InlineKeyboardButton(text="M", callback_data="size_M"),
         InlineKeyboardButton(text="L", callback_data="size_L")],
        [InlineKeyboardButton(text="XL", callback_data="size_XL"),
         InlineKeyboardButton(text="XXL", callback_data="size_XXL")]
    ])
    await message.answer("👕 Вітаю! Обери свій розмір:", reply_markup=keyboard)

@dp.callback_query(lambda c: c.data.startswith("size_"))
async def show_products(callback: types.CallbackQuery):
    size = callback.data.split("_")[1]
    
    cursor.execute("SELECT message_id, text FROM products WHERE sizes LIKE ?", (f'%{size}%',))
    all_products = cursor.fetchall()
    
    available_products = []
    sold_ids = []
    
    for msg_id, text in all_products:
        if is_sold(text):
            sold_ids.append(msg_id)
            logger.info(f"🗑️ Товар {msg_id} продано, не показуємо")
        else:
            available_products.append((msg_id, text))
    
    if sold_ids:
        cursor.executemany("DELETE FROM products WHERE message_id = ?", [(id,) for id in sold_ids])
        conn.commit()
        logger.info(f"🗑️ Видалено {len(sold_ids)} проданих товарів")
    
    if not available_products:
        await callback.message.answer(f"😕 Товарів з розміром {size} поки немає.")
        await callback.answer()
        return
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[])
    for msg_id, text in available_products[:10]:
        short_name = text[:40] if text else "Товар"
        keyboard.inline_keyboard.append([
            InlineKeyboardButton(text=f"📦 {short_name}", url=f"https://t.me/c/{str(GROUP_ID)[4:]}/{msg_id}")
        ])
    
    await callback.message.answer(f"✅ Знайдено товарів з розміром {size}: {len(available_products)}", reply_markup=keyboard)
    await callback.answer()

@dp.message()
async def catch_group_post(message: types.Message):
    """Обробка нових постів"""
    if message.chat.id != GROUP_ID:
        return
    
    full_text = (message.text or message.caption or "")
    
    if is_sold(full_text):
        logger.info(f"⏩ Пропущено (продано): {message.message_id}")
        return
    
    sizes = extract_sizes(full_text)
    
    if sizes:
        cursor.execute("SELECT id FROM products WHERE message_id = ?", (message.message_id,))
        existing = cursor.fetchone()
        
        if existing:
            cursor.execute("UPDATE products SET text = ?, sizes = ? WHERE message_id = ?",
                           (full_text[:200], ",".join(sizes), message.message_id))
            logger.info(f"🔄 Оновлено товар: {message.message_id} | Розміри: {sizes}")
        else:
            cursor.execute("INSERT INTO products (message_id, text, sizes) VALUES (?, ?, ?)",
                           (message.message_id, full_text[:200], ",".join(sizes)))
            logger.info(f"✅ Збережено товар: {message.message_id} | Розміри: {sizes}")
        conn.commit()
    else:
        logger.info(f"⏩ Пропущено (немає розмірів): {message.message_id}")

@dp.edited_message()
async def catch_edited_post(message: types.Message):
    """ПОВНА СИНХРОНІЗАЦІЯ при редагуванні поста"""
    if message.chat.id != GROUP_ID:
        return
    
    full_text = (message.text or message.caption or "")
    logger.info(f"📝 Відредаговано пост {message.message_id}: {full_text[:50]}...")
    
    # Якщо додали "ПРОДАНО" - видаляємо весь товар
    if is_sold(full_text):
        cursor.execute("DELETE FROM products WHERE message_id = ?", (message.message_id,))
        conn.commit()
        logger.info(f"🗑️ Товар {message.message_id} видалено (додано ПРОДАНО)")
        return
    
    # Отримуємо актуальні розміри з тексту
    new_sizes = extract_sizes(full_text)
    
    if new_sizes:
        # Оновлюємо текст та розміри
        cursor.execute("UPDATE products SET text = ?, sizes = ? WHERE message_id = ?",
                       (full_text[:200], ",".join(new_sizes), message.message_id))
        conn.commit()
        logger.info(f"🔄 Оновлено розміри для поста {message.message_id}: {new_sizes}")
    else:
        # Якщо розмірів більше немає - видаляємо товар з бази
        cursor.execute("DELETE FROM products WHERE message_id = ?", (message.message_id,))
        conn.commit()
        logger.info(f"🗑️ Товар {message.message_id} видалено (немає розмірів у тексті)")
    
    # Додатково: перевіряємо, чи є цей товар в базі для інших розмірів (не потрібно)

# ========== HTTP СЕРВЕР І САМОПІНГ ==========
async def health_check(request):
    return web.Response(text="OK")

async def ping_self():
    while True:
        await asyncio.sleep(600)
        if KEEP_ALIVE_URL:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(KEEP_ALIVE_URL, timeout=10) as resp:
                        logger.info(f"✅ Самопінг: {resp.status}")
            except Exception as e:
                logger.error(f"❌ Помилка самопінгу: {e}")

async def start_http_server():
    app = web.Application()
    app.router.add_get("/health", health_check)
    app.router.add_get("/", health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"🌐 HTTP сервер запущено на порту {PORT}")

async def main():
    await start_http_server()
    asyncio.create_task(ping_self())
    logger.info("🤖 Бот запущено!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
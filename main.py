import asyncio
import aiohttp
import os
import time

# ================= НАСТРОЙКИ (Environment Variables) =================
API_TOKEN = os.environ.get("GIFT_SATELLITE_TOKEN")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# Глобальное состояние бота
state = {
    "collection": os.environ.get("COLLECTION", "PlushPepe"),
    "target_backgrounds": [],  # Если пустой - ищем по всем
    "min_spread": 0.10,        # 10%
    "last_update_id": 0
}

BASE_URL = "https://api.gift-satellite.dev"

# --- Работа с Telegram ---

async def send_tg(session, text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    async with session.post(url, json=payload) as r:
        return await r.json()

async def check_commands(session):
    """Слушает команды: /set_bg, /clear_bg, /status"""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    params = {"offset": state["last_update_id"] + 1, "timeout": 1}
    
    try:
        async with session.get(url, params=params) as r:
            data = await r.json()
            for update in data.get("result", []):
                state["last_update_id"] = update["update_id"]
                msg = update.get("message", {})
                text = msg.get("text", "")
                cid = str(msg.get("chat", {}).get("id", ""))

                if cid != TELEGRAM_CHAT_ID: continue

                if text.startswith("/set_bg"):
                    bgs = [b.strip() for b in text.replace("/set_bg", "").split(",") if b.strip()]
                    state["target_backgrounds"] = bgs
                    await send_tg(session, f"✅ Теперь ищем только фоны: {', '.join(bgs)}")
                
                elif text == "/clear_bg":
                    state["target_backgrounds"] = []
                    await send_tg(session, "✅ Фильтр фонов сброшен. Ищем по всем доступным.")

                elif text == "/status":
                    status = (f"📊 <b>Статус:</b>\nКоллекция: {state['collection']}\n"
                              f"Фоны: {', '.join(state['target_backgrounds']) if state['target_backgrounds'] else 'Все'}\n"
                              f"Спред: {state['min_spread']*100}%")
                    await send_tg(session, status)
    except:
        pass

# --- Логика Парсинга ---

async def fetch_market_data(session, market):
    """Получает топ-50 листингов и группирует их правильно"""
    url = f"{BASE_URL}/search/{market}/{state['collection']}"
    headers = {"Authorization": f"Token {API_TOKEN}"}
    
    market_floors = {} # Ключ: (model, backdrop), Значение: цена
    
    try:
        async with session.get(url, headers=headers) as r:
            if r.status == 200:
                listings = await r.json()
                for item in listings:
                    model = item.get("modelName")
                    bg = item.get("backdropName")
                    price = float(item.get("normalizedPrice", 0))
                    
                    # Если пользователь задал фильтр фонов, пропускаем ненужные
                    if state["target_backgrounds"] and bg not in state["target_backgrounds"]:
                        continue
                    
                    key = (model, bg)
                    # Сохраняем только самую низкую цену для этой пары (API и так сортирует, но для страховки)
                    if key not in market_floors:
                        market_floors[key] = price
            elif r.status == 429:
                await asyncio.sleep(3)
    except Exception as e:
        print(f"Ошибка {market}: {e}")
    
    return market_floors

async def scanner():
    async with aiohttp.ClientSession() as session:
        print("🚀 Сканнер запущен...")
        while True:
            # 1. Слушаем команды
            await check_commands(session)

            # 2. Собираем данные (соблюдаем паузы API)
            print(f"[{time.strftime('%X')}] Сбор данных с рынков...")
            
            tg_data = await fetch_market_data(session, "tg")
            await asyncio.sleep(3)
            
            mrkt_data = await fetch_market_data(session, "mrkt")
            await asyncio.sleep(3)
            
            portals_data = await fetch_market_data(session, "portals")

            # 3. Анализируем арбитраж
            # Собираем все уникальные комбинации (Модель, Фон), которые есть хоть где-то
            all_keys = set(tg_data.keys()) | set(mrkt_data.keys()) | set(portals_data.keys())

            for key in all_keys:
                model, bg = key
                
                # Собираем цены на эту конкретную комбинацию
                prices = {
                    "Telegram Market": tg_data.get(key, float('inf')),
                    "MRKT": mrkt_data.get(key, float('inf')),
                    "Portals": portals_data.get(key, float('inf'))
                }
                
                # Оставляем только те рынки, где этот товар реально есть
                valid = {m: p for m, p in prices.items() if p != float('inf')}
                if len(valid) < 2: continue

                # Ищем самую дешевую и самую дорогую площадку
                best_buy_market = min(valid, key=valid.get)
                buy_price = valid[best_buy_market]
                
                other_prices = [p for m, p in valid.items() if m != best_buy_market]
                best_sell_price = min(other_prices)

                # Проверяем спред
                if buy_price <= best_sell_price * (1 - state["min_spread"]):
                    profit_pct = ((best_sell_price - buy_price) / buy_price) * 100
                    
                    msg = (f"🔥 <b>НАЙДЕН АРБИТРАЖ! (+{profit_pct:.1f}%)</b>\n"
                           f"🎁 <b>{model}</b> | 🖼 {bg}\n\n"
                           f"🛒 КУПИТЬ: <b>{best_buy_market}</b> — {buy_price} TON\n"
                           f"💰 Продать минимум за: {best_sell_price} TON\n\n"
                           f"📊 Цены площадок:\n"
                           f"• TG: {prices['Telegram Market'] if prices['Telegram Market'] != float('inf') else '—'}\n"
                           f"• MRKT: {prices['MRKT'] if prices['MRKT'] != float('inf') else '—'}\n"
                           f"• Portals: {prices['Portals'] if prices['Portals'] != float('inf') else '—'}")
                    
                    await send_tg(session, msg)
                    print(f"!!! Сигнал отправлен: {model} {bg}")

            # Пауза перед следующим общим кругом
            await asyncio.sleep(10)

if __name__ == "__main__":
    asyncio.run(scanner())

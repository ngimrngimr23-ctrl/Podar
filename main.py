import asyncio
import aiohttp
import os
from aiohttp import web

# ================= НАСТРОЙКИ (Environment Variables) =================
API_TOKEN = os.environ.get("GIFT_SATELLITE_TOKEN")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# Глобальное состояние
state = {
    "collections": ["PlushPepe", "Dogs"], # Список отслеживаемых коллекций
    "min_spread": float(os.environ.get("MIN_SPREAD_PCT", 0.05)), # 5% по умолчанию
    "last_update_id": 0
}

BASE_URL = "https://api.gift-satellite.dev"

# --- 1. ВЕБ-СЕРВЕР ДЛЯ RENDER ---
async def handle_ping(request):
    return web.Response(text="Arbitrage Bot is scanning models...")

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"🌐 Сервер пинга запущен на порту {port}")

# --- 2. ТЕЛЕГРАМ УПРАВЛЕНИЕ ---
async def send_tg(session, text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    try:
        async with session.post(url, json=payload) as r: return await r.json()
    except Exception as e:
        print(f"⚠️ Ошибка отправки в TG: {e}")

async def check_commands(session):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    params = {"offset": state["last_update_id"] + 1, "timeout": 1}
    try:
        async with session.get(url, params=params) as r:
            data = await r.json()
            if not data.get("ok"): return
            for update in data.get("result", []):
                state["last_update_id"] = update["update_id"]
                msg = update.get("message", {})
                text = msg.get("text", "")
                uid = str(msg.get("from", {}).get("id", ""))

                if uid != TELEGRAM_CHAT_ID: continue

                if text == "/start" or text == "/status":
                    resp = (f"🚀 <b>Сканнер моделей активен</b>\n\n"
                            f"📦 <b>Коллекции:</b> {', '.join(state['collections'])}\n"
                            f"📈 <b>Мин. спред:</b> {state['min_spread']*100}%\n\n"
                            f"<b>Команды:</b>\n"
                            f"• <code>/add_coll Name</code> — Добавить коллекцию\n"
                            f"• <code>/del_coll Name</code> — Удалить коллекцию\n"
                            f"• <code>/set_spread 5</code> — Установить спред в %")
                    await send_tg(session, resp)

                elif text.startswith("/add_coll"):
                    name = text.replace("/add_coll", "").strip()
                    if name and name not in state["collections"]:
                        state["collections"].append(name)
                        await send_tg(session, f"✅ Добавлена: <b>{name}</b>")

                elif text.startswith("/del_coll"):
                    name = text.replace("/del_coll", "").strip()
                    if name in state["collections"]:
                        state["collections"].remove(name)
                        await send_tg(session, f"❌ Удалена: <b>{name}</b>")

                elif text.startswith("/set_spread"):
                    try:
                        val = float(text.split()[1])
                        state["min_spread"] = val / 100
                        await send_tg(session, f"✅ Спред изменен на <b>{val}%</b>")
                    except: pass
    except: pass

# --- 3. СКАНЕР МОДЕЛЕЙ ---
async def fetch_models_floor(session, market, coll):
    """Находит минимальную цену для каждой модели на конкретном рынке"""
    url = f"{BASE_URL}/search/{market}/{coll}"
    headers = {"Authorization": f"Token {API_TOKEN}"}
    model_floors = {}
    
    print(f"🔎 Сканируем {market} для коллекции {coll}...")
    
    try:
        async with session.get(url, headers=headers) as r:
            if r.status == 200:
                response_data = await r.json()
                
                # Универсальная обработка ответа от API
                items = response_data
                if isinstance(response_data, dict):
                    items = response_data.get("data") or response_data.get("items") or response_data.get("result", [])
                
                # Защита от непредвиденного формата
                if not isinstance(items, list):
                    print(f"⚠️ Формат ответа от {market} не распознан. Вот что прислал сервер:\n{response_data}")
                    return {}

                # Если список пустой - просто выходим
                if len(items) == 0:
                    print(f"ℹ️ {market}: По коллекции {coll} пришел пустой список.")
                    return {}

                # Парсинг данных
                for i in items:
                    if not isinstance(i, dict): continue
                    
                    model = i.get("modelName")
                    try:
                        price = float(i.get("normalizedPrice", 0))
                    except (TypeError, ValueError):
                        continue
                    
                    if model and price > 0:
                        if model not in model_floors or price < model_floors[model]:
                            model_floors[model] = price
                            
                print(f"✅ {market}: Найдено {len(model_floors)} уникальных моделей для {coll}.")
                            
            elif r.status == 404:
                print(f"❌ Ошибка 404: Ссылка {url} не существует. Проверь API_URL!")
            elif r.status == 401 or r.status == 403:
                print(f"❌ Ошибка {r.status}: Проблемы с токеном авторизации для {market}.")
            elif r.status == 429:
                print(f"⚠️ Rate limit на {market}, слишком много запросов. Пауза 5 сек...")
                await asyncio.sleep(5)
            else:
                print(f"❌ Неизвестная ошибка API {market}: статус {r.status}")
                
    except Exception as e:
        print(f"❌ Сетевая ошибка при запросе к {market}: {e}")
        
    return model_floors

async def run_scanner():
    await start_web_server()
    
    async with aiohttp.ClientSession() as session:
        print("🚀 Сканнер моделей запущен!")
        while True:
            await check_commands(session)
            
            for coll in state["collections"]:
                print(f"\n--- 🔄 Начинаем срез по коллекции {coll} ---")
                
                tg_floors = await fetch_models_floor(session, "tg", coll)
                await asyncio.sleep(2) 
                
                mrkt_floors = await fetch_models_floor(session, "mrkt", coll)
                await asyncio.sleep(2)
                
                portals_floors = await fetch_models_floor(session, "portals", coll)

                all_models = set(tg_floors.keys()) | set(mrkt_floors.keys()) | set(portals_floors.keys())

                for model in all_models:
                    prices = {
                        "TG Market": tg_floors.get(model, 999999),
                        "MRKT": mrkt_floors.get(model, 999999),
                        "Portals": portals_floors.get(model, 999999)
                    }
                    
                    valid_prices = {m: p for m, p in prices.items() if p < 999999}
                    if len(valid_prices) < 2: continue

                    best_buy_market = min(valid_prices, key=valid_prices.get)
                    buy_price = valid_prices[best_buy_market]
                    
                    # Ищем самый низкий флор среди остальных рынков (чтобы гарантированно продать)
                    other_prices = [p for m, p in valid_prices.items() if m != best_buy_market]
                    best_sell_price = min(other_prices)

                    # Проверка спреда
                    if buy_price <= best_sell_price * (1 - state["min_spread"]):
                        profit_pct = ((best_sell_price - buy_price) / buy_price) * 100
                        msg = (f"⚡️ <b>АРБИТРАЖ МОДЕЛИ (+{profit_pct:.1f}%)</b>\n"
                               f"📦 Колл: <code>{coll}</code>\n"
                               f"🎁 Модель: <b>{model}</b>\n\n"
                               f"🛒 КУПИТЬ: <b>{best_buy_market}</b> — {buy_price} TON\n"
                               f"💰 ПРОДАТЬ (Floor): {best_sell_price} TON\n\n"
                               f"📊 Срез: TG: {prices['TG Market']} | MRKT: {prices['MRKT']} | Portals: {prices['Portals']}")
                        await send_tg(session, msg)
                        print(f"💸 Найдена связка! {model} в коллекции {coll}. Профит: {profit_pct:.1f}%")

                await asyncio.sleep(3) 

            await asyncio.sleep(10)

if __name__ == "__main__":
    try:
        asyncio.run(run_scanner())
    except (KeyboardInterrupt, SystemExit):
        print("\n🛑 Сканнер остановлен.")
                    

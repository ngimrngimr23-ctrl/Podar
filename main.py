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
    "collections": ["PlushPepe", "Dogs"], 
    "min_spread": float(os.environ.get("MIN_SPREAD_PCT", 0.05)), 
    "last_update_id": 0
}

BASE_URL = "https://api.gift-satellite.dev"

# --- 1. ВЕБ-СЕРВЕР ДЛЯ RENDER (Health Check) ---
async def handle_ping(request):
    return web.Response(text="Arbitrage Bot is active")

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"🌐 Сервер мониторинга запущен на порту {port}")

# --- 2. ТЕЛЕГРАМ ЛОГИКА ---
async def send_tg(session, text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    try:
        async with session.post(url, json=payload) as r: 
            return await r.json()
    except Exception as e:
        print(f"⚠️ Ошибка TG: {e}")

async def check_commands(session):
    """Проверка входящих команд из Telegram"""
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
                    resp = (f"🚀 <b>Сканнер активен</b>\n\n"
                            f"📦 <b>Коллекции:</b> {', '.join(state['collections'])}\n"
                            f"📈 <b>Мин. спред:</b> {state['min_spread']*100}%\n")
                    await send_tg(session, resp)

                elif text.startswith("/set_spread"):
                    try:
                        val = float(text.split()[1])
                        state["min_spread"] = val / 100
                        await send_tg(session, f"✅ Спред: <b>{val}%</b>")
                    except: pass
    except Exception as e:
        print(f"⚠️ Ошибка проверки команд: {e}")

# --- 3. СКАНЕР МОДЕЛЕЙ ---
async def fetch_models_floor(session, market, coll):
    url = f"{BASE_URL}/search/{market}/{coll}"
    headers = {"Authorization": f"Token {API_TOKEN}"}
    model_floors = {}
    
    try:
        async with session.get(url, headers=headers, timeout=10) as r:
            if r.status == 200:
                raw_data = await r.json()
                
                # Гибкий парсинг (если данные вложены в 'data' или 'items')
                items = raw_data
                if isinstance(raw_data, dict):
                    items = raw_data.get("data") or raw_data.get("items") or raw_data.get("result") or []
                
                if not isinstance(items, list):
                    print(f"⚠️ Неверный формат от {market}: {raw_data}")
                    return {}

                for i in items:
                    model = i.get("modelName")
                    price = float(i.get("normalizedPrice", 0))
                    if model and price > 0:
                        if model not in model_floors or price < model_floors[model]:
                            model_floors[model] = price
            else:
                print(f"❌ {market} вернул статус {r.status}")
    except Exception as e:
        print(f"❌ Ошибка запроса к {market}: {e}")
    return model_floors

async def command_listener(session):
    """Отдельный цикл для быстрой реакции на команды"""
    print("🤖 Слушатель команд запущен")
    while True:
        await check_commands(session)
        await asyncio.sleep(1) # Проверка раз в секунду

async def scanner_loop(session):
    """Отдельный цикл для долгого сканирования рынков"""
    print("🔎 Сканнер рынков запущен")
    while True:
        for coll in state["collections"]:
            print(f"🔄 Срез по {coll}...")
            
            # Собираем данные
            tasks = [
                fetch_models_floor(session, "tg", coll),
                fetch_models_floor(session, "mrkt", coll),
                fetch_models_floor(session, "portals", coll)
            ]
            results = await asyncio.gather(*tasks)
            tg_f, mrkt_f, port_f = results

            all_models = set(tg_f.keys()) | set(mrkt_f.keys()) | set(port_f.keys())

            for model in all_models:
                prices = {
                    "TG": tg_f.get(model, 999999),
                    "MRKT": mrkt_f.get(model, 999999),
                    "Portals": port_f.get(model, 999999)
                }
                
                valid = {m: p for m, p in prices.items() if p < 999999}
                if len(valid) < 2: continue

                best_buy_m = min(valid, key=valid.get)
                buy_p = valid[best_buy_m]
                
                # Твоя логика: берем минимальный флор среди остальных
                others = [p for m, p in valid.items() if m != best_buy_m]
                best_sell_p = min(others) 

                if buy_p <= best_sell_p * (1 - state["min_spread"]):
                    profit = ((best_sell_p - buy_p) / buy_p) * 100
                    msg = (f"⚡️ <b>АРБИТРАЖ {profit:.1f}%</b>\n"
                           f"📦 <code>{coll}</code> | 🎁 {model}\n\n"
                           f"🛒 КУПИТЬ: <b>{best_buy_m}</b> — {buy_p} TON\n"
                           f"💰 ПРОДАТЬ: {best_sell_p} TON\n\n"
                           f"📊 Срез: TG:{prices['TG']} | MRKT:{prices['MRKT']} | Port:{prices['Portals']}")
                    await send_tg(session, msg)

            await asyncio.sleep(2) # Пауза между коллекциями
        
        print("💤 Круг завершен, ждем 15 сек...")
        await asyncio.sleep(15)

async def main():
    await start_web_server()
    async with aiohttp.ClientSession() as session:
        # Запускаем две задачи ПАРАЛЛЕЛЬНО
        await asyncio.gather(
            command_listener(session),
            scanner_loop(session)
        )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass

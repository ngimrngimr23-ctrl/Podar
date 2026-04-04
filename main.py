import asyncio
import aiohttp
import os
import time
from aiohttp import web

# ================= НАСТРОЙКИ (Environment Variables) =================
API_TOKEN = os.environ.get("GIFT_SATELLITE_TOKEN")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# Глобальное состояние
state = {
    "collection": os.environ.get("COLLECTION", "PlushPepe"),
    "target_backgrounds": [],  # Если пусто — ищем по всем
    "min_spread": float(os.environ.get("MIN_SPREAD_PCT", 0.10)),
    "last_update_id": 0
}

BASE_URL = "https://api.gift-satellite.dev"

# --- ВЕБ-СЕРВЕР ДЛЯ UPTIMEROBOT ---

async def handle_ping(request):
    """Ответ для UptimeRobot, чтобы сервер не засыпал"""
    return web.Response(text="Bot is awake and scanning!")

async def start_web_server():
    """Запуск веб-сервера на порту Render"""
    app = web.Application()
    app.router.add_get('/', handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    # Render автоматически назначает PORT, по умолчанию 10000
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"🌐 Веб-сервер запущен на порту {port}")

# --- РАБОТА С TELEGRAM ---

async def send_tg(session, text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID, 
        "text": text, 
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    try:
        async with session.post(url, json=payload) as r:
            return await r.json()
    except Exception as e:
        print(f"Ошибка отправки в TG: {e}")

async def check_commands(session):
    """Обработка команд управления через чат бота"""
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

                if cid != TELEGRAM_CHAT_ID:
                    continue

                if text.startswith("/set_bg"):
                    # Пример: /set_bg Forest, Ocean
                    bgs = [b.strip() for b in text.replace("/set_bg", "").split(",") if b.strip()]
                    state["target_backgrounds"] = bgs
                    await send_tg(session, f"✅ Фильтр фонов установлен: <b>{', '.join(bgs)}</b>")
                
                elif text == "/clear_bg":
                    state["target_backgrounds"] = []
                    await send_tg(session, "✅ Фильтр фонов сброшен. Ищем по всем вариантам.")

                elif text == "/status":
                    status = (f"📊 <b>Текущий статус:</b>\n"
                              f"📦 Коллекция: <code>{state['collection']}</code>\n"
                              f"🖼 Фоны: {', '.join(state['target_backgrounds']) if state['target_backgrounds'] else 'Все'}\n"
                              f"📈 Мин. спред: {state['min_spread']*100}%")
                    await send_tg(session, status)
    except Exception as e:
        print(f"Ошибка получения команд: {e}")

# --- ЛОГИКА СКАНЕРА ---

async def fetch_market_data(session, market):
    """Сбор цен и группировка по Модель+Фон"""
    url = f"{BASE_URL}/search/{market}/{state['collection']}"
    headers = {"Authorization": f"Token {API_TOKEN}"}
    
    market_floors = {}
    try:
        async with session.get(url, headers=headers) as r:
            if r.status == 200:
                listings = await r.json()
                for item in listings:
                    model = item.get("modelName")
                    bg = item.get("backdropName")
                    price = float(item.get("normalizedPrice", 0))
                    
                    if state["target_backgrounds"] and bg not in state["target_backgrounds"]:
                        continue
                    
                    key = (model, bg)
                    # Берем только самый дешевый листинг для этой пары
                    if key not in market_floors:
                        market_floors[key] = price
            elif r.status == 429:
                print(f"⚠️ Rate limit на {market}, ожидание...")
                await asyncio.sleep(5)
    except Exception as e:
        print(f"Ошибка парсинга {market}: {e}")
    
    return market_floors

async def scanner_loop():
    """Основной бесконечный цикл арбитража"""
    async with aiohttp.ClientSession() as session:
        # Запускаем веб-сервер внутри цикла один раз
        await start_web_server()
        print("🚀 Сканнер и веб-сервер запущены...")

        while True:
            # Сначала проверяем, не прислал ли пользователь команду
            await check_commands(session)

            print(f"[{time.strftime('%X')}] Запуск проверки цен...")
            
            # Собираем данные с трех рынков с паузами (API лимиты 2-3 сек)
            tg_data = await fetch_market_data(session, "tg")
            await asyncio.sleep(3)
            
            mrkt_data = await fetch_market_data(session, "mrkt")
            await asyncio.sleep(3)
            
            portals_data = await fetch_market_data(session, "portals")

            # Анализируем совпадения
            all_keys = set(tg_data.keys()) | set(mrkt_data.keys()) | set(portals_data.keys())

            for key in all_keys:
                model, bg = key
                prices = {
                    "Telegram Market": tg_data.get(key, float('inf')),
                    "MRKT": mrkt_data.get(key, float('inf')),
                    "Portals": portals_data.get(key, float('inf'))
                }
                
                valid = {m: p for m, p in prices.items() if p != float('inf')}
                if len(valid) < 2:
                    continue

                # Ищем лучшее предложение
                best_buy_market = min(valid, key=valid.get)
                buy_price = valid[best_buy_market]
                
                # Ищем минимальную цену продажи на других рынках
                other_prices = [p for m, p in valid.items() if m != best_buy_market]
                best_sell_price = min(other_prices)

                # Проверка условия арбитража
                if buy_price <= best_sell_price * (1 - state["min_spread"]):
                    profit_pct = ((best_sell_price - buy_price) / buy_price) * 100
                    
                    msg = (f"🔥 <b>АРБИТРАЖ НАЙДЕН! (+{profit_pct:.1f}%)</b>\n"
                           f"🎁 <b>{model}</b>\n"
                           f"🖼 Фон: {bg}\n\n"
                           f"🛒 КУПИТЬ: <b>{best_buy_market}</b> — {buy_price} TON\n"
                           f"💰 Продать минимум за: {best_sell_price} TON\n\n"
                           f"📊 Срез цен:\n"
                           f"• TG: {prices['Telegram Market'] if prices['Telegram Market'] != float('inf') else '—'}\n"
                           f"• MRKT: {prices['MRKT'] if prices['MRKT'] != float('inf') else '—'}\n"
                           f"• Portals: {prices['Portals'] if prices['Portals'] != float('inf') else '—'}")
                    
                    await send_tg(session, msg)

            # Пауза между кругами сканирования
            await asyncio.sleep(20)

if __name__ == "__main__":
    try:
        asyncio.run(scanner_loop())
    except (KeyboardInterrupt, SystemExit):
        pass

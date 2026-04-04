import asyncio
import aiohttp
import os
import time
from aiohttp import web

# ================= НАСТРОЙКИ (Environment Variables) =================
API_TOKEN = os.environ.get("GIFT_SATELLITE_TOKEN")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# Глобальное состояние (управление через ТГ)
state = {
    "collection": os.environ.get("COLLECTION", "PlushPepe"),
    "target_models": [],      # Пусто = все модели
    "target_backgrounds": [],   # Пусто = все фоны
    "min_spread": float(os.environ.get("MIN_SPREAD_PCT", 0.10)),
    "last_update_id": 0
}

BASE_URL = "https://api.gift-satellite.dev"

# --- ВЕБ-СЕРВЕР ДЛЯ UPTIMEROBOT ---
async def handle_ping(request):
    return web.Response(text="Scanner is active")

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()

# --- ТЕЛЕГРАМ ЛОГИКА ---
async def send_tg(session, text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    async with session.post(url, json=payload) as r:
        return await r.json()

async def check_commands(session):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    params = {"offset": state["last_update_id"] + 1, "timeout": 1}
    
    try:
        async with session.get(url, params=params) as r:
            data = await r.json()
            for update in data.get("result", []):
                state["last_update_id"] = update["update_id"]
                msg = update.get("message", {})
                text = msg.get("text", "")
                uid = str(msg.get("from", {}).get("id", ""))

                if uid != TELEGRAM_CHAT_ID: continue

                # Команда /start или /status
                if text == "/start" or text == "/status":
                    m_list = ", ".join(state['target_models']) if state['target_models'] else "Все"
                    bg_list = ", ".join(state['target_backgrounds']) if state['target_backgrounds'] else "Все"
                    resp = (f"⚙️ <b>Настройки сканера:</b>\n\n"
                            f"📦 Коллекция: <code>{state['collection']}</code>\n"
                            f"🎭 Модели: <code>{m_list}</code>\n"
                            f"🖼 Фоны: <code>{bg_list}</code>\n"
                            f"📈 Спред: <b>{state['min_spread']*100}%</b>\n\n"
                            f"<b>Команды управления:</b>\n"
                            f"• <code>/set_coll Name</code> — сменить коллекцию\n"
                            f"• <code>/set_models M1, M2</code> — фильтр моделей\n"
                            f"• <code>/clear_models</code> — искать все модели\n"
                            f"• <code>/set_spread 15</code> — спред 15%\n"
                            f"• <code>/set_bg B1, B2</code> — фильтр фонов\n"
                            f"• <code>/clear_bg</code> — искать все фоны")
                    await send_tg(session, resp)

                elif text.startswith("/set_coll"):
                    new_coll = text.replace("/set_coll", "").strip()
                    if new_coll:
                        state["collection"] = new_coll
                        await send_tg(session, f"✅ Коллекция изменена на: <b>{new_coll}</b>")

                elif text.startswith("/set_models"):
                    models = [m.strip() for m in text.replace("/set_models", "").split(",") if m.strip()]
                    state["target_models"] = models
                    await send_tg(session, f"✅ Модели установлены: {', '.join(models)}")

                elif text == "/clear_models":
                    state["target_models"] = []
                    await send_tg(session, "✅ Теперь ищем <b>все модели</b> в коллекции.")

                elif text.startswith("/set_spread"):
                    try:
                        val = float(text.split()[1]) / 100
                        state["min_spread"] = val
                        await send_tg(session, f"✅ Спред изменен на <b>{val*100}%</b>")
                    except: pass

                elif text.startswith("/set_bg"):
                    bgs = [b.strip() for b in text.replace("/set_bg", "").split(",") if b.strip()]
                    state["target_backgrounds"] = bgs
                    await send_tg(session, f"✅ Фоны установлены: {', '.join(bgs)}")

                elif text == "/clear_bg":
                    state["target_backgrounds"] = []
                    await send_tg(session, "✅ Теперь ищем <b>все фоны</b>.")
    except: pass

# --- СКАНЕР ---
async def fetch_data(session, market):
    url = f"{BASE_URL}/search/{market}/{state['collection']}"
    headers = {"Authorization": f"Token {API_TOKEN}"}
    data_map = {}
    try:
        async with session.get(url, headers=headers) as r:
            if r.status == 200:
                items = await r.json()
                for i in items:
                    m, bg = i.get("modelName"), i.get("backdropName")
                    # Фильтрация моделей и фонов
                    if state["target_models"] and m not in state["target_models"]: continue
                    if state["target_backgrounds"] and bg not in state["target_backgrounds"]: continue
                    
                    key = (m, bg)
                    if key not in data_map: data_map[key] = float(i.get("normalizedPrice", 0))
            elif r.status == 429: await asyncio.sleep(5)
    except: pass
    return data_map

async def main_loop():
    async with aiohttp.ClientSession() as session:
        await start_web_server()
        while True:
            await check_commands(session)
            
            # Сбор данных
            tg = await fetch_data(session, "tg")
            await asyncio.sleep(3)
            mrkt = await fetch_data(session, "mrkt")
            await asyncio.sleep(3)
            portals = await fetch_data(session, "portals")

            all_keys = set(tg.keys()) | set(mrkt.keys()) | set(portals.keys())
            for key in all_keys:
                prices = {"TG": tg.get(key, 999999), "MRKT": mrkt.get(key, 999999), "Portals": portals.get(key, 999999)}
                valid = {m: p for m, p in prices.items() if p < 999999}
                if len(valid) < 2: continue

                buy_m = min(valid, key=valid.get)
                buy_p = valid[buy_m]
                sell_p = min([p for m, p in valid.items() if m != buy_m])

                if buy_p <= sell_p * (1 - state["min_spread"]):
                    profit = ((sell_p - buy_p) / buy_p) * 100
                    msg = (f"🔥 <b>ПРОФИТ {profit:.1f}%</b>\n"
                           f"📦 {state['collection']} | {key[0]} | {key[1]}\n"
                           f"🛒 БУРЕМ: {buy_m} ({buy_p} TON)\n"
                           f"💰 СЛИВАЕМ: {sell_p} TON")
                    await send_tg(session, msg)

            await asyncio.sleep(15)

if __name__ == "__main__":
    asyncio.run(main_loop())
                

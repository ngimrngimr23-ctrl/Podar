import asyncio
import aiohttp
import os
import time
import json
from aiohttp import web

# ================= НАСТРОЙКИ (Environment Variables) =================
API_TOKEN = os.environ.get("GIFT_SATELLITE_TOKEN")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

STATE_FILE = "bot_state.json"

# Глобальное состояние (стартовые значения)
state = {
    "collections": ["Plush Pepe", "Dog"], 
    "ignored_models": [], # Черный список подарков
    "min_spread": float(os.environ.get("MIN_SPREAD_PCT", 0.05)), 
    "density_pct": 0.05,
    "last_update_id": 0,
    "alerts": {}
}

BASE_URL = "https://gift-satellite.dev/api"

# --- 0. СИСТЕМА СОХРАНЕНИЯ НАСТРОЕК ---
def load_state():
    global state
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
                for key in ["collections", "ignored_models", "min_spread", "density_pct", "last_update_id", "alerts"]:
                    if key in saved:
                        state[key] = saved[key]
            print("💾 Настройки успешно загружены из файла!")
        except Exception as e:
            print(f"⚠️ Ошибка загрузки файла настроек: {e}")

def save_state():
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=4)
    except Exception as e:
        print(f"⚠️ Ошибка сохранения в файл: {e}")

# --- 1. ВЕБ-СЕРВЕР ДЛЯ RENDER ---
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
                save_state()
                
                msg = update.get("message", {})
                text = msg.get("text", "")
                uid = str(msg.get("from", {}).get("id", ""))

                if uid != TELEGRAM_CHAT_ID: continue

                if text == "/start" or text == "/help" or text == "/status":
                    resp = (f"🚀 <b>Сканнер арбитража активен</b>\n\n"
                            f"📦 <b>Коллекции:</b> {', '.join(state['collections'])}\n"
                            f"🚫 <b>В игноре:</b> {len(state['ignored_models'])} моделей\n"
                            f"📈 <b>Мин. спред:</b> {state['min_spread']*100}%\n"
                            f"🧱 <b>Порог плотности:</b> {state['density_pct']*100}%\n\n"
                            f"🛠 <b>Команды:</b>\n"
                            f"• <code>/add_coll Имя</code> — Добавить коллекцию\n"
                            f"• <code>/del_coll Имя</code> — Удалить коллекцию\n"
                            f"• <code>/ignore Имя Модели</code> — Не присылать этот подарок\n"
                            f"• <code>/unignore Имя Модели</code> — Убрать из игнора\n"
                            f"• <code>/set_spread 5</code> — Изменить спред (%)\n"
                            f"• <code>/set_density 3</code> — Изменить плотность (%)")
                    await send_tg(session, resp)

                elif text.startswith("/add_coll"):
                    name = text.replace("/add_coll", "").strip()
                    if name and name not in state["collections"]:
                        state["collections"].append(name)
                        save_state()
                        await send_tg(session, f"✅ Добавлена коллекция: <b>{name}</b>")

                elif text.startswith("/del_coll"):
                    name = text.replace("/del_coll", "").strip()
                    if name in state["collections"]:
                        state["collections"].remove(name)
                        save_state()
                        await send_tg(session, f"❌ Удалена коллекция: <b>{name}</b>")
                        
                elif text.startswith("/ignore"):
                    name = text.replace("/ignore", "").strip()
                    if name and name not in state["ignored_models"]:
                        state["ignored_models"].append(name)
                        save_state()
                        await send_tg(session, f"🚫 Подарок скрыт из поиска: <b>{name}</b>")

                elif text.startswith("/unignore"):
                    name = text.replace("/unignore", "").strip()
                    if name in state["ignored_models"]:
                        state["ignored_models"].remove(name)
                        save_state()
                        await send_tg(session, f"✅ Подарок возвращен в поиск: <b>{name}</b>")

                elif text.startswith("/set_spread"):
                    try:
                        val = float(text.split()[1])
                        state["min_spread"] = val / 100
                        save_state()
                        await send_tg(session, f"✅ Спред изменен на <b>{val}%</b>")
                    except: pass

                elif text.startswith("/set_density"):
                    try:
                        val = float(text.split()[1])
                        state["density_pct"] = val / 100
                        save_state()
                        await send_tg(session, f"✅ Порог плотности изменен на <b>{val}%</b>")
                    except: pass
                    
    except Exception as e:
        print(f"⚠️ Ошибка проверки команд: {e}")

# --- 3. СКАНЕР МОДЕЛЕЙ ---
async def fetch_models_prices(session, market, coll):
    url = f"{BASE_URL}/search/{market}/{coll}?limit=1000"
    headers = {"Authorization": f"Token {API_TOKEN}"}
    model_prices = {}
    
    try:
        async with session.get(url, headers=headers, timeout=15) as r:
            if r.status == 200:
                raw_data = await r.json()
                items = raw_data
                if isinstance(raw_data, dict):
                    items = raw_data.get("data") or raw_data.get("items") or raw_data.get("result") or []
                
                if not isinstance(items, list): return {}

                for i in items:
                    raw_model = i.get("modelName")
                    price = float(i.get("normalizedPrice", 0))
                    
                    if raw_model and price > 0:
                        model = str(raw_model).strip()
                        if model not in model_prices:
                            model_prices[model] = []
                        model_prices[model].append(price)
                
                for m in model_prices:
                    model_prices[m].sort()
            else:
                print(f"❌ {market} вернул {r.status} для '{coll}'")
    except Exception as e:
        print(f"❌ Ошибка запроса к {market}: {e}")
    return model_prices

async def command_listener(session):
    print("🤖 Слушатель команд запущен")
    while True:
        await check_commands(session)
        await asyncio.sleep(1)

async def scanner_loop(session):
    print("🔎 Сканнер рынков запущен")
    while True:
        if not state["collections"]:
            await asyncio.sleep(10)
            continue
            
        for coll in state["collections"]:
            print(f"🔄 Срез по {coll}...")
            
            tg_p = await fetch_models_prices(session, "tg", coll)
            await asyncio.sleep(4.5) 
            mrkt_p = await fetch_models_prices(session, "mrkt", coll)
            await asyncio.sleep(3.5) 
            port_p = await fetch_models_prices(session, "portals", coll)
            await asyncio.sleep(3.5) 

            all_models = set(tg_p.keys()) | set(mrkt_p.keys()) | set(port_p.keys())

            for model in all_models:
                # Проверка: если подарок в черном списке, пропускаем его!
                if model in state["ignored_models"]:
                    continue

                prices_dict = {
                    "TG": tg_p.get(model, []),
                    "MRKT": mrkt_p.get(model, []),
                    "Portals": port_p.get(model, [])
                }
                
                valid_markets = {m: p_list for m, p_list in prices_dict.items() if len(p_list) > 0}
                if len(valid_markets) < 3: continue

                floors = {m: p_list[0] for m, p_list in valid_markets.items()}
                best_buy_m = min(floors, key=floors.get)
                buy_p = floors[best_buy_m]
                
                buy_market_prices = valid_markets[best_buy_m]
                if len(buy_market_prices) > 1:
                    price1 = buy_market_prices[0]
                    price2 = buy_market_prices[1]
                    if price2 <= price1 * (1 + state["density_pct"]):
                        continue 

                others = {m: p for m, p in floors.items() if m != best_buy_m}
                best_sell_m = min(others, key=others.get)
                best_sell_p = others[best_sell_m] 

                if buy_p <= best_sell_p * (1 - state["min_spread"]):
                    alert_key = f"{coll}_{model}"
                    if alert_key in state["alerts"]:
                        if buy_p >= state["alerts"][alert_key]["buy_price"]:
                            continue 

                    profit = ((best_sell_p - buy_p) / buy_p) * 100
                    sell_info = [f"{m}: {p} TON" for m, p in others.items()]
                    sell_text = " | ".join(sell_info)

                    # ТЕПЕРЬ НАЗВАНИЕ МОДЕЛИ ТОЖЕ В ТЕГЕ CODE (КОПИРУЕТСЯ ПО КЛИКУ)
                    msg = (f"⚡️ <b>АРБИТРАЖ {profit:.1f}%</b>\n"
                           f"📦 <code>{coll}</code> | 🎁 <code>{model}</code>\n\n"
                           f"🛒 КУПИТЬ: <b>{best_buy_m}</b> — {buy_p} TON\n"
                           f"💰 ПРОДАТЬ: {sell_text}")
                    
                    await send_tg(session, msg)
                    state["alerts"][alert_key] = {"buy_price": buy_p}
                    save_state() # Сохраняем отправленный алерт в файл

        print("💤 Круг завершен, ждем 15 сек...")
        await asyncio.sleep(15)

async def main():
    load_state() # ВОССТАНАВЛИВАЕМ НАСТРОЙКИ ПРИ СТАРТЕ
    await start_web_server()
    async with aiohttp.ClientSession() as session:
        await asyncio.gather(
            command_listener(session),
            scanner_loop(session)
        )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
        

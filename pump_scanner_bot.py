import asyncio
import aiohttp
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command, CommandObject
from aiohttp import web
import time
from collections import deque
import os

# ================= НАСТРОЙКИ =================
# ВАЖНО: токен ТОЛЬКО из переменной окружения.
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Не задана переменная окружения BOT_TOKEN")

settings = {
    # --- Цена ---
    "percent": 3.0,          # ПОТОЛОК роста цены в окне. Если 0 - отключено
    "window_min": 15,        # Окно анализа для "percent" (мин)
    "check_interval": 30,    # Как часто проверять (сек)
    "min_volume": 100000,    # Мин. объём 24ч ($) — отсекаем неликвид

    # Пороги роста (floor = минимум)
    "day_min_rise": 0.0,
    "week_min_rise": 0.0,
    "month_min_rise": 0.0,

    # Потолки роста (ceiling = максимум)
    "day_max_rise": 0.0,
    "week_max_rise": 0.0,
    "month_max_rise": 0.0,

    # --- Объём (ОСНОВНЫЕ ФИЛЬТРЫ) ---
    "vh_percent": 0.0,       # % превышения объёма текущего часа над средним
    "vm_percent": 0.0,       # % превышения объёма последней минуты над средним

    "cooldown_min": 5,       # Мин. пауза от повторного алерта по той же монете
    "chat_id": None,
    "channel_id": None
}

price_history = {}  # symbol -> deque[(ts, price)]
blacklist = set()
alert_memory = {}   # symbol -> {"time": first_alert_ts, "last_msg": ts, "price": price}

# Кэши бейзлайнов объёма
BASELINE_TTL = 900
hour_baseline_cache = {}  # symbol -> {"ts": fetched_at, "avg": float, "cur": float}
day_baseline_cache = {}   # symbol -> {"ts": fetched_at, "avg": float, "cur": float}

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

def fmt_pct(key):
    val = settings[key]
    return "Выкл" if val == 0 else f"{val}%"

# ================= TELEGRAM UI =================

@dp.message(Command("start"))
async def start_cmd(message: types.Message):
    settings["chat_id"] = message.chat.id
    await message.answer(
        "🚀 <b>Pump/Volume-сканер MEXC запущен</b>\n"
        "Теперь фильтры объема (<code>/vh</code>, <code>/vm</code>) работают независимо от цены.\n"
        "Нажми /help для списка команд.",
        parse_mode="HTML"
    )

@dp.message(Command("help"))
async def help_cmd(message: types.Message):
    text = (
        "<b>Доступные команды:</b>\n\n"
        "<b>Объем (Главные фильтры):</b>\n"
        "<code>/vh [число]</code> — Мин. % аномалии объема за час (0 = выкл)\n"
        "<code>/vm [число]</code> — Мин. % аномалии объема за минуту (0 = выкл)\n"
        "<code>/vol [число]</code> — Мин. суточный объем в $ (отсев неликвида)\n\n"
        "<b>Цена (Дополнительные фильтры):</b>\n"
        "<code>/p [число]</code> — Максимальный % роста цены в окне (0 = выкл)\n"
        "<code>/window [число]</code> — Окно анализа цены (в минутах)\n\n"
        "<b>Интервалы и паузы:</b>\n"
        "<code>/ivl [число]</code> — Интервал парсинга (сек)\n"
        "<code>/pause [число]</code> — Пауза между алертами одной монеты (мин)\n\n"
        "<code>/status</code> — Показать текущие настройки"
    )
    await message.answer(text, parse_mode="HTML")

@dp.message(Command("status"))
async def status_cmd(message: types.Message):
    text = (
        "📊 <b>Текущие настройки:</b>\n\n"
        f"<b>Фильтры объёма (ОСНОВНЫЕ):</b>\n"
        f"Аномалия Час (/vh): <b>{fmt_pct('vh_percent')}</b>\n"
        f"Аномалия Минута (/vm): <b>{fmt_pct('vm_percent')}</b>\n"
        f"Мин. 24ч объем (/vol): <b>{settings['min_volume']}$</b>\n\n"
        f"<b>Ценовые фильтры (ДОПОЛНИТЕЛЬНЫЕ):</b>\n"
        f"Потолок роста в окне (/p): <b>{fmt_pct('percent')}</b>\n"
        f"Окно анализа (/window): <b>{settings['window_min']} мин</b>\n\n"
        f"<b>Система:</b>\n"
        f"Интервал парсинга (/ivl): <b>{settings['check_interval']} сек</b>\n"
        f"Кулдаун алертов (/pause): <b>{settings['cooldown_min']} мин</b>\n"
    )
    await message.answer(text, parse_mode="HTML")

@dp.message(Command("vh"))
async def set_vh(message: types.Message, command: CommandObject):
    if command.args:
        try:
            settings["vh_percent"] = float(command.args)
            await message.answer(f"✅ Фильтр часового объема установлен: {fmt_pct('vh_percent')}")
        except ValueError:
            await message.answer("❌ Введи число, например: /vh 150")

@dp.message(Command("vm"))
async def set_vm(message: types.Message, command: CommandObject):
    if command.args:
        try:
            settings["vm_percent"] = float(command.args)
            await message.answer(f"✅ Фильтр минутного объема установлен: {fmt_pct('vm_percent')}")
        except ValueError:
            await message.answer("❌ Введи число, например: /vm 300")

@dp.message(Command("p"))
async def set_percent(message: types.Message, command: CommandObject):
    if command.args:
        try:
            settings["percent"] = float(command.args)
            await message.answer(f"✅ Потолок роста в окне: {fmt_pct('percent')} (0 = выкл)")
        except ValueError:
            await message.answer("❌ Введи число.")

@dp.message(Command("window"))
async def set_window(message: types.Message, command: CommandObject):
    if command.args:
        try:
            settings["window_min"] = int(command.args)
            await message.answer(f"✅ Окно анализа цены: {settings['window_min']} мин.")
        except ValueError:
            await message.answer("❌ Введи целое число.")

@dp.message(Command("vol"))
async def set_vol(message: types.Message, command: CommandObject):
    if command.args:
        try:
            settings["min_volume"] = int(command.args)
            await message.answer(f"✅ Мин. объем 24ч: {settings['min_volume']}$")
        except ValueError:
            await message.answer("❌ Введи целое число.")

@dp.message(Command("ivl"))
async def set_ivl(message: types.Message, command: CommandObject):
    if command.args:
        try:
            settings["check_interval"] = int(command.args)
            await message.answer(f"✅ Интервал парсинга: {settings['check_interval']} сек.")
        except ValueError:
            await message.answer("❌ Введи целое число.")

@dp.message(Command("pause"))
async def set_pause(message: types.Message, command: CommandObject):
    if command.args:
        try:
            settings["cooldown_min"] = int(command.args)
            await message.answer(f"✅ Кулдаун алерта по одной монете: {settings['cooldown_min']} мин.")
        except ValueError:
            await message.answer("❌ Введи целое число.")

@dp.message(Command("setchannel"))
async def set_channel(message: types.Message, command: CommandObject):
    if command.args:
        settings["channel_id"] = command.args.strip()
        await message.answer(f"✅ Канал для алертов установлен: {settings['channel_id']}")


# ================= MEXC API =================

async def fetch_prices():
    url = "https://api.mexc.com/api/v3/ticker/24hr"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=10) as response:
                if response.status == 200:
                    return await response.json()
    except Exception as e:
        print(f"Ошибка API (ticker): {e}", flush=True)
    return []

async def fetch_klines(symbol, interval, limit):
    url = f"https://api.mexc.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=8) as resp:
                if resp.status == 200:
                    return await resp.json()
    except Exception:
        pass
    return []

async def get_long_term_changes(symbol, current_price):
    data = await fetch_klines(symbol, "1d", 31)
    if not data: return 0.0, 0.0
    idx_7 = -8 if len(data) >= 8 else 0
    idx_30 = -31 if len(data) >= 31 else 0
    try:
        p_7 = float(data[idx_7][1])
        p_30 = float(data[idx_30][1])
        c_7 = ((current_price - p_7) / p_7) * 100
        c_30 = ((current_price - p_30) / p_30) * 100
        return c_7, c_30
    except Exception:
        return 0.0, 0.0

async def get_hour_volume_anomaly(symbol):
    cached = hour_baseline_cache.get(symbol)
    now = time.time()
    if cached and (now - cached["ts"]) < BASELINE_TTL:
        return cached["cur"], cached["avg"]

    data = await fetch_klines(symbol, "1h", 192)
    if len(data) < 30: return 0.0, 0.0

    closed = data[:-1]
    last_closed = closed[-1]
    try:
        last_open_ts = int(last_closed[0])
        last_hour = time.gmtime(last_open_ts / 1000).tm_hour
        last_vol = float(last_closed[7]) if len(last_closed) > 7 else float(last_closed[5])
    except Exception:
        return 0.0, 0.0

    same_hour_vols = []
    for bar in closed[:-1]:
        try:
            bar_ts = int(bar[0])
            bar_hour = time.gmtime(bar_ts / 1000).tm_hour
            if bar_hour == last_hour:
                vol = float(bar[7]) if len(bar) > 7 else float(bar[5])
                same_hour_vols.append(vol)
        except Exception:
            continue

    avg = sum(same_hour_vols) / len(same_hour_vols) if same_hour_vols else 0.0
    hour_baseline_cache[symbol] = {"ts": now, "avg": avg, "cur": last_vol}
    return last_vol, avg

async def get_minute_volume_anomaly(symbol):
    cached = day_baseline_cache.get(symbol)
    now = time.time()
    if cached and (now - cached["ts"]) < BASELINE_TTL:
        return cached["cur"], cached["avg"]

    data = await fetch_klines(symbol, "1m", 1000)
    if len(data) < 30: return 0.0, 0.0

    closed = data[:-1]
    try:
        last_vol = float(closed[-1][7]) if len(closed[-1]) > 7 else float(closed[-1][5])
        vols = [float(bar[7]) if len(bar) > 7 else float(bar[5]) for bar in closed[:-1]]
    except Exception:
        return 0.0, 0.0

    avg = sum(vols) / len(vols) if vols else 0.0
    day_baseline_cache[symbol] = {"ts": now, "avg": avg, "cur": last_vol}
    return last_vol, avg

def pct_over_baseline(current, avg):
    if avg <= 0: return 0.0
    return ((current - avg) / avg) * 100

# ================= ОСНОВНОЙ ЦИКЛ =================

async def parser_task():
    print("--- Фоновый парсер (pump/volume) запущен ---", flush=True)
    while True:
        try:
            data = await fetch_prices()
            now = time.time()
            window_sec = settings["window_min"] * 60
            max_pts = max(int((window_sec / settings["check_interval"]) * 1.2), 5)
            cooldown_sec = settings["cooldown_min"] * 60

            for item in data:
                pair = item['symbol']
                if not pair.endswith("USDT") or pair in blacklist:
                    continue

                try:
                    vol = float(item['quoteVolume'])
                    if vol < settings["min_volume"]:
                        continue
                    price = float(item['lastPrice'])
                    ch_24 = float(item['priceChangePercent']) * 100
                except Exception:
                    continue

                # 1. Сбор истории цен (теперь это не блокирует проверку объема!)
                if pair not in price_history or price_history[pair].maxlen != max_pts:
                    price_history[pair] = deque(maxlen=max_pts)
                
                price_history[pair].append((now, price))
                relevant = [p for (t, p) in price_history[pair] if (now - t) <= window_sec]
                have_full_window = (len(price_history[pair]) > 0 and (now - price_history[pair][0][0]) >= window_sec)

                if have_full_window and relevant:
                    min_p = min(relevant)
                    rise = ((price - min_p) / min_p) * 100 if min_p > 0 else 0.0
                else:
                    min_p = price
                    rise = 0.0

                # 2. Проверка кулдауна (чтобы не спамить)
                if pair in alert_memory and (now - alert_memory[pair]["time"]) >= 86400:
                    del alert_memory[pair]

                if pair in alert_memory and (now - alert_memory[pair]["last_msg"]) < cooldown_sec:
                    continue

                # ================= ОСНОВНОЙ ФИЛЬТР: ОБЪЕМ =================
                # Получаем данные по объемам (API защищен кэшированием, так что спама запросов не будет)
                cur_h, avg_h = await get_hour_volume_anomaly(pair)
                vh_pct = pct_over_baseline(cur_h, avg_h)
                
                if settings["vh_percent"] > 0 and vh_pct < settings["vh_percent"]:
                    continue

                cur_m, avg_m = await get_minute_volume_anomaly(pair)
                vm_pct = pct_over_baseline(cur_m, avg_m)
                
                if settings["vm_percent"] > 0 and vm_pct < settings["vm_percent"]:
                    continue

                # ================= ДОПОЛНИТЕЛЬНЫЙ ФИЛЬТР: ЦЕНА =================
                if settings["day_min_rise"] > 0 and ch_24 < settings["day_min_rise"]:
                    continue
                if settings["day_max_rise"] > 0 and ch_24 > settings["day_max_rise"]:
                    continue

                # Проверяем потолок роста цены, ТОЛЬКО если он включен (>0) и собралось "окно" истории
                if settings["percent"] > 0 and have_full_window:
                    if rise > settings["percent"]:
                        continue

                # Долгосрочные свечи дергаем только в самом конце, если прошли все фильтры выше
                ch_7, ch_30 = await get_long_term_changes(pair, price)

                if settings["week_min_rise"] > 0 and ch_7 < settings["week_min_rise"]:
                    continue
                if settings["month_min_rise"] > 0 and ch_30 < settings["month_min_rise"]:
                    continue
                if settings["week_max_rise"] > 0 and ch_7 > settings["week_max_rise"]:
                    continue
                if settings["month_max_rise"] > 0 and ch_30 > settings["month_max_rise"]:
                    continue

                # ================= ОТПРАВКА АЛЕРТА =================
                alert_memory[pair] = {
                    "time": alert_memory[pair]["time"] if pair in alert_memory else now,
                    "price": price,
                    "last_msg": now
                }

                base_coin = pair.replace("USDT", "")
                
                lines = [
                    f"🚀 <b>ПАМП / АНОМАЛИЯ: <code>{base_coin}</code></b>",
                    f"📈 В окне ({settings['window_min']}м): <b>+{rise:.2f}%</b>",
                    f"📊 За 24 часа: <b>{ch_24:.2f}%</b>",
                    f"📆 За 7 дней: <b>{ch_7:.2f}%</b>",
                    f"🗓 За 30 дней: <b>{ch_30:.2f}%</b>",
                    f"🔊 Объём/час vs неделя: <b>{vh_pct:+.1f}%</b>",
                    f"🔊 Объём/мин vs сутки: <b>{vm_pct:+.1f}%</b>",
                    f"💵 Было (мин. в окне): <code>{min_p}</code>",
                    f"💸 Стало (тек): <code>{price}</code>",
                    f"💰 Объём 24ч: <b>{int(vol):,}$</b>"
                ]
                alert_text = "\n".join(lines)

                if settings["chat_id"]:
                    try:
                        await bot.send_message(settings["chat_id"], alert_text, parse_mode="HTML")
                    except Exception as e:
                        print(f"Ошибка отправки админу: {e}", flush=True)

                if settings["channel_id"]:
                    try:
                        await bot.send_message(settings["channel_id"], alert_text, parse_mode="HTML")
                    except Exception as e:
                        print(f"Не удалось отправить в канал {settings['channel_id']}: {e}", flush=True)

        except Exception as e:
            print(f"Ошибка парсера: {e}", flush=True)
        
        await asyncio.sleep(settings["check_interval"])

# ================= WEB & RUN =================

async def handle_ping(request):
    return web.Response(text="OK", status=200)

async def main():
    app = web.Application()
    app.router.add_get('/', handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', int(os.environ.get("PORT", 10000)))
    await site.start()

    await bot.delete_webhook(drop_pending_updates=True)
    asyncio.create_task(parser_task())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
    

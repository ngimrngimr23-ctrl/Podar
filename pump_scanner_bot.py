import asyncio
import aiohttp
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command, CommandObject
from aiohttp import web
import time
from collections import deque
import os

# ================= НАСТРОЙКИ =================
# ВАЖНО: токен ТОЛЬКО из переменной окружения. Никогда не хардкодь его в файле,
# иначе при пуше на GitHub он утечёт даже из приватного репозитория (кэши, форки,
# коллабораторы). На Render: Settings -> Environment -> добавь BOT_TOKEN.
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Не задана переменная окружения BOT_TOKEN")

settings = {
    # --- Цена (опционально, доп. фильтры поверх объёма) ---
    "percent": 3.0,          # ПОТОЛОК роста цены в окне (0 = выключен, не проверяется)
    "window_min": 15,        # Окно анализа для "percent" (мин) — по умолчанию 15 мин
    "check_interval": 30,    # Как часто проверять (сек)
    "min_volume": 100000,    # Мин. объём 24ч ($) — отсекаем неликвид

    # Пороги роста (floor = минимум, чтобы алерт вообще сработал)
    "day_min_rise": 0.0,     # Мин. % роста за 24ч (0 = выкл)
    "week_min_rise": 0.0,    # Мин. % роста за 7 дней (0 = выкл)
    "month_min_rise": 0.0,   # Мин. % роста за 30 дней (0 = выкл)

    # Потолки роста (ceiling = максимум, чтобы отсечь уже "перегретые" монеты)
    "day_max_rise": 0.0,     # Скрыть, если рост за 24ч больше X% (0 = выкл)
    "week_max_rise": 0.0,    # Скрыть, если рост за 7д больше X% (0 = выкл)
    "month_max_rise": 0.0,   # Скрыть, если рост за 30д больше X% (0 = выкл)

    # --- Объём (ПЕРВИЧНЫЙ критерий) ---
    # % запаса, на который текущий (последний закрытый) час должен превысить
    # МАКСИМУМ объёма среди всех часов за предыдущие ~7 дней, чтобы считаться
    # новым рекордом и триггернуть алерт (0 = фильтр выключен вообще).
    "vh_percent": 0.0,       # 0 = выкл
    "vh_price_limit": 0.0,   # доп. лимит на колебание цены (high-low %) внутри этой же часовой свечи (0 = не проверять)

    # % запаса, на который текущая (последняя закрытая) минута должна превысить
    # МАКСИМУМ объёма среди минут за последние ~сутки (0 = фильтр выключен).
    "vm_percent": 0.0,       # 0 = выкл
    "vm_price_limit": 0.0,   # доп. лимит на колебание цены (high-low %) внутри этой же минутной свечи (0 = не проверять)

    # % запаса, на который текущая (последняя закрытая) 4-часовая свеча должна
    # превысить МАКСИМУМ объёма среди 4h-свечей за предыдущие ~15 дней (0 = выкл).
    "v4h_percent": 0.0,      # 0 = выкл
    "v4h_price_limit": 0.0,  # доп. лимит на колебание цены (high-low %) внутри этой же 4h-свечи (0 = не проверять)

    # ВАЖНО: если vh_percent == 0 И vm_percent == 0 И v4h_percent == 0 — алерты
    # не будут срабатывать вообще (нет первичного триггера).

    "cooldown_min": 5,       # Мин. техническая пауза между алертами по одной монете
                             # (реальный анти-спам — правило x2 от предыдущего объёма, см. код)
    "chat_id": None,
    "channel_id": None
}

price_history = {}          # symbol -> deque[(ts, price)]
blacklist = set()
alert_memory = {}            # symbol -> {"time": first_alert_ts, "last_msg": ts, "price": price}

# Кэши бейзлайнов объёма, чтобы не долбить klines-эндпоинт на каждой итерации
BASELINE_TTL = 900           # обновлять раз в 15 минут на пару
hour_baseline_cache = {}     # symbol -> {"ts": fetched_at, "ref": float (макс. за период), "cur": float, "rng": float (high-low % последней свечи)}
day_baseline_cache = {}      # symbol -> {"ts": fetched_at, "ref": float (макс. за период), "cur": float, "rng": float}
four_hour_baseline_cache = {}  # symbol -> {"ts": fetched_at, "ref": float, "cur": float, "rng": float}

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# --- Валидные торговые пары MEXC (фильтр мусорных/несуществующих символов) ---
# /api/v3/ticker/24hr иногда отдаёт "мёртвые"/технические тикеры (например, с
# окончанием "ON" перед USDT), которых по факту нет в реальном списке торгуемых
# пар — из-за этого klines по ним всегда падает с HTTP 400. Сверяемся с
# /api/v3/exchangeInfo и держим только реально торгуемые USDT-пары.
VALID_SYMBOLS_TTL = 6 * 3600  # обновлять раз в 6 часов
valid_symbols = set()
valid_symbols_ts = 0.0


async def refresh_valid_symbols():
    global valid_symbols, valid_symbols_ts
    url = "https://api.mexc.com/api/v3/exchangeInfo"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=15) as resp:
                if resp.status != 200:
                    print(f"exchangeInfo HTTP {resp.status}", flush=True)
                    return
                data = await resp.json()
    except Exception as e:
        print(f"Ошибка exchangeInfo: {e}", flush=True)
        return

    symbols = data.get("symbols", [])
    fresh = set()
    for s in symbols:
        try:
            if s.get("quoteAsset") != "USDT":
                continue
            status = str(s.get("status", "")).upper()
            trading_flag = s.get("isSpotTradingAllowed", None)
            # Разные версии MEXC API называют статус по-разному — считаем пару
            # валидной, если явно указано, что торговля разрешена, либо статус
            # похож на "включено"/"1"/"TRADING"/"ONLINE".
            if trading_flag is True or status in ("1", "ENABLED", "TRADING", "ONLINE"):
                fresh.add(s["symbol"])
        except Exception:
            continue

    if fresh:
        valid_symbols = fresh
        valid_symbols_ts = time.time()
        print(f"--- exchangeInfo обновлён: {len(valid_symbols)} валидных USDT-пар ---", flush=True)

# Статистика последнего прохода сканера — для команды /debug
debug_stats = {
    "ts": 0.0,
    "ticker_ok": False,
    "total_pairs": 0,
    "passed_volume_floor": 0,
    "passed_spam_guard": 0,
    "passed_volume_filter": 0,
    "passed_price_filter": 0,
    "alerts_sent": 0,
    "klines_errors": 0,
    "last_error": None,
}


def fmt_pct(key):
    val = settings[key]
    return "Выкл" if val == 0 else f"{val}%"


def fmt_limit(key):
    val = settings[key]
    return "не проверяется" if val == 0 else f"±{val}%"


# ================= TELEGRAM UI =================

@dp.message(Command("start"))
async def start_cmd(message: types.Message):
    settings["chat_id"] = message.chat.id
    percent_display = "Выкл" if settings["percent"] == 0 else f"{settings['percent']}%"
    await message.answer(
        "🚀 <b>Pump/Volume-сканер MEXC запущен</b>\n"
        "Логика: <b>/vh, /vm и /v4h — первичный критерий</b> (алерт вообще возможен, "
        "только если хотя бы один из них включён). Всё в блоке «ЦЕНА» — "
        "опциональные ДОПОЛНИТЕЛЬНЫЕ фильтры поверх уже сработавшего объёма.\n"
        "У каждой объёмной команды есть 2-й необязательный параметр — лимит "
        "колебания цены (high-low %) <b>внутри той же свечи</b>, где найден рекорд объёма.\n"
        "Ниже — каждая команда, что она делает, пример и <b>текущее значение</b>.\n\n"

        "🔊 <b>ОБЪЁМ (первично)</b>\n"
        f"/vh 5 10 — мин. % превышения нового максимума часа над предыдущим рекордом за ~7 дней; "
        f"опц. 2-й арг — макс. колебание цены (high-low%) внутри этого часа (0=выкл)\n"
        f"   └ сейчас: <b>{fmt_pct('vh_percent')}</b>, лимит цены: <b>{fmt_limit('vh_price_limit')}</b>\n"
        f"/vm 5 10 — мин. % превышения нового максимума минуты над предыдущим рекордом за сутки; "
        f"опц. 2-й арг — макс. колебание цены внутри этой минуты (0=выкл)\n"
        f"   └ сейчас: <b>{fmt_pct('vm_percent')}</b>, лимит цены: <b>{fmt_limit('vm_price_limit')}</b>\n"
        f"/v4h 5 10 — мин. % превышения нового максимума объёма 4-часовой свечи над предыдущим "
        f"рекордом за ~15 дней; опц. 2-й арг — макс. колебание цены внутри этой 4ч-свечи (0=выкл)\n"
        f"   └ сейчас: <b>{fmt_pct('v4h_percent')}</b>, лимит цены: <b>{fmt_limit('v4h_price_limit')}</b>\n"
        f"/v 200000 — мин. объём торгов за 24ч в $, ниже которого пара вообще игнорируется (базовый фильтр ликвидности)\n"
        f"   └ сейчас: <b>{settings['min_volume']:,}$</b>\n\n"

        "📈 <b>ЦЕНА (дополнительно, опционально)</b>\n"
        f"/p 5 — ПОТОЛОК роста в окне: если >0, монета проходит только если рост ещё НЕ превысил это значение (0=выкл, не проверяется)\n"
        f"   └ сейчас: <b>{percent_display}</b>\n"
        f"/t 15 — размер окна для /p, в минутах\n"
        f"   └ сейчас: <b>{settings['window_min']} мин</b>\n"
        f"/d 5 — мин. % роста за 24ч (0=выкл)\n"
        f"   └ сейчас: <b>{fmt_pct('day_min_rise')}</b>\n"
        f"/dmax 20 — потолок роста за 24ч: скрыть, если рост больше (0=выкл)\n"
        f"   └ сейчас: <b>{fmt_pct('day_max_rise')}</b>\n"
        f"/wmin 10 — мин. % роста за 7 дней (0=выкл)\n"
        f"   └ сейчас: <b>{fmt_pct('week_min_rise')}</b>\n"
        f"/w 50 — потолок роста за 7 дней (0=выкл)\n"
        f"   └ сейчас: <b>{fmt_pct('week_max_rise')}</b>\n"
        f"/mmin 20 — мин. % роста за 30 дней (0=выкл)\n"
        f"   └ сейчас: <b>{fmt_pct('month_min_rise')}</b>\n"
        f"/m 100 — потолок роста за 30 дней (0=выкл)\n"
        f"   └ сейчас: <b>{fmt_pct('month_max_rise')}</b>\n\n"

        "⚙️ <b>ПРОЧЕЕ</b>\n"
        f"/b BTC — добавить монету в чёрный список (без алертов)\n"
        f"   └ в ЧС сейчас: <b>{len(blacklist)} шт.</b>\n"
        f"/channel @имя_канала — куда дублировать сигналы (пусто = выкл)\n"
        f"   └ сейчас: <b>{settings['channel_id'] or 'Не задан'}</b>\n"
        f"/s — показать текущий статус всех настроек одной сводкой\n"
        f"/debug — воронка последнего прохода сканера (диагностика, если алертов нет)\n\n"
        "🛡 <b>Анти-спам:</b> повторный алерт по уже уведомлённой монете отправится, "
        "только если новый триггерящий объём минимум в 2 раза больше объёма "
        "предыдущего алерта по ней же — иначе монета молчит."
        , parse_mode="HTML")


@dp.message(Command("channel"))
async def set_channel(message: types.Message, command: CommandObject):
    settings["chat_id"] = message.chat.id
    if command.args:
        settings["channel_id"] = command.args
        await message.answer(f"✅ Канал установлен: <b>{command.args}</b>\n<i>Сделай бота админом канала!</i>", parse_mode="HTML")
    else:
        settings["channel_id"] = None
        await message.answer("✅ Дублирование в канал <b>ОТКЛЮЧЕНО</b>", parse_mode="HTML")


def _parse_float(args):
    return float(args.replace(',', '.'))


def _parse_volume_args(args):
    """
    Парсит аргументы объёмной команды вида '5' или '5 10'.
    Возвращает (percent, price_limit). Второй арг опционален (по умолчанию 0 = выкл).
    """
    if not args:
        raise ValueError("no args")
    parts = args.strip().split()
    percent = abs(_parse_float(parts[0]))
    price_limit = abs(_parse_float(parts[1])) if len(parts) > 1 else 0.0
    return percent, price_limit


@dp.message(Command("p"))
async def set_percent(message: types.Message, command: CommandObject):
    try:
        settings["chat_id"] = message.chat.id
        settings["percent"] = _parse_float(command.args)
        if settings["percent"] == 0:
            await message.answer("✅ Фильтр по цене в окне <b>ВЫКЛЮЧЕН</b> (проверяется только объём и доп. фильтры)", parse_mode="HTML")
        else:
            await message.answer(
                f"✅ Доп. фильтр: потолок роста в окне — <b>{settings['percent']}%</b> (проходят монеты, где рост ещё не превысил это значение)",
                parse_mode="HTML")
    except Exception:
        await message.answer("❌ Ошибка. Пример: /p 5 (0 = выключить)")


@dp.message(Command("t"))
async def set_time(message: types.Message, command: CommandObject):
    settings["chat_id"] = message.chat.id
    if command.args and command.args.isdigit():
        settings["window_min"] = int(command.args)
        await message.answer(f"✅ Окно: <b>{command.args} мин</b>", parse_mode="HTML")
    else:
        await message.answer("❌ Ошибка. Пример: /t 15")


@dp.message(Command("d"))
async def set_day_min(message: types.Message, command: CommandObject):
    try:
        settings["chat_id"] = message.chat.id
        val = _parse_float(command.args)
        settings["day_min_rise"] = 0.0 if val == 0 else abs(val)
        await message.answer(f"✅ Мин. рост за 24ч: <b>{fmt_pct('day_min_rise')}</b>", parse_mode="HTML")
    except Exception:
        await message.answer("❌ Ошибка. Пример: /d 5 (или /d 0 для выключения)")


@dp.message(Command("dmax"))
async def set_day_max(message: types.Message, command: CommandObject):
    try:
        settings["chat_id"] = message.chat.id
        val = abs(_parse_float(command.args))
        settings["day_max_rise"] = val
        await message.answer(f"✅ Потолок роста за 24ч: <b>{fmt_pct('day_max_rise')}</b>", parse_mode="HTML")
    except Exception:
        await message.answer("❌ Ошибка. Пример: /dmax 20 (0 = выключить)")


@dp.message(Command("wmin"))
async def set_week_min(message: types.Message, command: CommandObject):
    try:
        settings["chat_id"] = message.chat.id
        val = _parse_float(command.args)
        settings["week_min_rise"] = 0.0 if val == 0 else abs(val)
        await message.answer(f"✅ Мин. рост за 7д: <b>{fmt_pct('week_min_rise')}</b>", parse_mode="HTML")
    except Exception:
        await message.answer("❌ Ошибка. Пример: /wmin 10")


@dp.message(Command("w"))
async def set_week_max(message: types.Message, command: CommandObject):
    try:
        settings["chat_id"] = message.chat.id
        val = abs(_parse_float(command.args))
        settings["week_max_rise"] = val
        await message.answer(f"✅ Потолок роста за 7д: <b>{fmt_pct('week_max_rise')}</b>", parse_mode="HTML")
    except Exception:
        await message.answer("❌ Ошибка. Пример: /w 50 (0 = выключить)")


@dp.message(Command("mmin"))
async def set_month_min(message: types.Message, command: CommandObject):
    try:
        settings["chat_id"] = message.chat.id
        val = _parse_float(command.args)
        settings["month_min_rise"] = 0.0 if val == 0 else abs(val)
        await message.answer(f"✅ Мин. рост за 30д: <b>{fmt_pct('month_min_rise')}</b>", parse_mode="HTML")
    except Exception:
        await message.answer("❌ Ошибка. Пример: /mmin 20")


@dp.message(Command("m"))
async def set_month_max(message: types.Message, command: CommandObject):
    try:
        settings["chat_id"] = message.chat.id
        val = abs(_parse_float(command.args))
        settings["month_max_rise"] = val
        await message.answer(f"✅ Потолок роста за 30д: <b>{fmt_pct('month_max_rise')}</b>", parse_mode="HTML")
    except Exception:
        await message.answer("❌ Ошибка. Пример: /m 100 (0 = выключить)")


@dp.message(Command("vh"))
async def set_vh(message: types.Message, command: CommandObject):
    try:
        settings["chat_id"] = message.chat.id
        percent, price_limit = _parse_volume_args(command.args)
        settings["vh_percent"] = percent
        settings["vh_price_limit"] = price_limit
        await message.answer(
            f"✅ ПЕРВИЧНЫЙ критерий — новый максимум объёма часа должен превышать предыдущий рекорд (за ~7д) минимум на: <b>{fmt_pct('vh_percent')}</b>\n"
            f"📏 Лимит колебания цены (high-low) внутри этого часа: <b>{fmt_limit('vh_price_limit')}</b>\n"
            f"<i>Напоминание: алерт возможен, только если включён /vh, /vm и/или /v4h.</i>",
            parse_mode="HTML")
    except Exception:
        await message.answer("❌ Ошибка. Пример: /vh 5 (0 = выключить) или /vh 5 10 (5% объём + лимит цены ±10%)")


@dp.message(Command("vm"))
async def set_vm(message: types.Message, command: CommandObject):
    try:
        settings["chat_id"] = message.chat.id
        percent, price_limit = _parse_volume_args(command.args)
        settings["vm_percent"] = percent
        settings["vm_price_limit"] = price_limit
        await message.answer(
            f"✅ ПЕРВИЧНЫЙ критерий — новый максимум объёма минуты должен превышать предыдущий рекорд (за сутки) минимум на: <b>{fmt_pct('vm_percent')}</b>\n"
            f"📏 Лимит колебания цены (high-low) внутри этой минуты: <b>{fmt_limit('vm_price_limit')}</b>\n"
            f"<i>Напоминание: алерт возможен, только если включён /vh, /vm и/или /v4h.</i>",
            parse_mode="HTML")
    except Exception:
        await message.answer("❌ Ошибка. Пример: /vm 5 (0 = выключить) или /vm 5 10 (5% объём + лимит цены ±10%)")


@dp.message(Command("v4h"))
async def set_v4h(message: types.Message, command: CommandObject):
    try:
        settings["chat_id"] = message.chat.id
        percent, price_limit = _parse_volume_args(command.args)
        settings["v4h_percent"] = percent
        settings["v4h_price_limit"] = price_limit
        await message.answer(
            f"✅ ПЕРВИЧНЫЙ критерий — новый максимум объёма 4-часовой свечи должен превышать предыдущий рекорд (за ~15д) минимум на: <b>{fmt_pct('v4h_percent')}</b>\n"
            f"📏 Лимит колебания цены (high-low) внутри этой 4ч-свечи: <b>{fmt_limit('v4h_price_limit')}</b>\n"
            f"<i>Напоминание: алерт возможен, только если включён /vh, /vm и/или /v4h.</i>",
            parse_mode="HTML")
    except Exception:
        await message.answer("❌ Ошибка. Пример: /v4h 5 (0 = выключить) или /v4h 5 10 (5% объём + лимит цены ±10%)")


@dp.message(Command("v"))
async def set_volume(message: types.Message, command: CommandObject):
    settings["chat_id"] = message.chat.id
    if command.args and command.args.isdigit():
        settings["min_volume"] = int(command.args)
        await message.answer(f"✅ Мин. объём 24ч: <b>{settings['min_volume']:,}$</b>", parse_mode="HTML")
    else:
        await message.answer("❌ Ошибка. Пример: /v 200000")


@dp.message(Command("b"))
async def add_blacklist(message: types.Message, command: CommandObject):
    settings["chat_id"] = message.chat.id
    if command.args:
        coin = command.args.upper()
        pair = coin if coin.endswith("USDT") else f"{coin}USDT"
        blacklist.add(pair)
        await message.answer(f"🚫 <b>{pair}</b> в ЧС", parse_mode="HTML")


@dp.message(Command("debug"))
async def debug_cmd(message: types.Message):
    ts = debug_stats["ts"]
    ago = int(time.time() - ts) if ts else None
    ago_str = f"{ago} сек назад" if ago is not None else "ещё не было прохода"

    ticker_status = "✅ ОК" if debug_stats["ticker_ok"] else "❌ Ошибка/пусто"

    lines = [
        "🔍 <b>Воронка последнего прохода сканера</b>",
        f"⏱ Прошёл: {ago_str}",
        f"📡 Тикер MEXC (список всех пар): {ticker_status}",
        f"1️⃣ Всего USDT-пар от биржи: {debug_stats['total_pairs']}",
        f"2️⃣ Прошли мин. объём 24ч + валидность на MEXC (/v): {debug_stats['passed_volume_floor']}",
        f"3️⃣ Прошли фильтр объёма (/vh /vm /v4h): {debug_stats['passed_volume_filter']}",
        f"4️⃣ Прошли анти-спам (первый раз ИЛИ x2 от пред. алерта): {debug_stats['passed_spam_guard']}",
        f"5️⃣ Прошли фильтр цены (/p /d /dmax): {debug_stats['passed_price_filter']}",
        f"📨 Алертов отправлено за этот проход: {debug_stats['alerts_sent']}",
        "",
        f"⚠️ Ошибок запросов klines за проход: {debug_stats['klines_errors']}",
    ]
    if debug_stats["last_error"]:
        lines.append(f"Последняя ошибка: {debug_stats['last_error']}")

    lines.append("")
    lines.append(
        "💡 Если шаг 2→3 обнуляется почти полностью — скорее всего API klines "
        "рвётся по таймауту/рейт-лимиту (смотри ошибку выше), либо ещё не "
        "прогрелся кэш бейзлайнов (подожди пару минут после рестарта). Если шаг "
        "5 сильно меньше шага 4 — вероятно, ещё не накоплено полное окно истории "
        f"цен ({settings['window_min']} мин с момента запуска) — подожди и глянь /debug снова."
    )

    await message.answer("\n".join(lines), parse_mode="HTML")


@dp.message(Command("s"))
async def status_cmd(message: types.Message):
    volume_active = settings["vh_percent"] > 0 or settings["vm_percent"] > 0 or settings["v4h_percent"] > 0
    await message.answer(
        "📊 <b>Статус</b>\n"
        f"🔊 Новый макс. объём/час (запас над рекордом за 7д): {fmt_pct('vh_percent')} | лимит цены: {fmt_limit('vh_price_limit')}\n"
        f"🔊 Новый макс. объём/мин (запас над рекордом за сутки): {fmt_pct('vm_percent')} | лимит цены: {fmt_limit('vm_price_limit')}\n"
        f"🔊 Новый макс. объём/4ч (запас над рекордом за 15д): {fmt_pct('v4h_percent')} | лимит цены: {fmt_limit('v4h_price_limit')}\n"
        f"💰 Мин. объём 24ч: {settings['min_volume']:,}$\n"
        f"{'✅ Первичный триггер активен' if volume_active else '⚠️ Первичный триггер ВЫКЛЮЧЕН — алертов не будет, включи /vh, /vm или /v4h'}\n\n"
        f"📈 Потолок роста в окне: {fmt_pct('percent')} за {settings['window_min']} мин\n"
        f"📅 24ч: мин {fmt_pct('day_min_rise')} / потолок {fmt_pct('day_max_rise')}\n"
        f"📆 7д: мин {fmt_pct('week_min_rise')} / потолок {fmt_pct('week_max_rise')}\n"
        f"🗓 30д: мин {fmt_pct('month_min_rise')} / потолок {fmt_pct('month_max_rise')}\n\n"
        f"📢 Канал: {settings['channel_id'] or 'Не задан'}\n"
        f"🛑 В памяти алертов: {len(alert_memory)}\n"
        f"📡 Отслеживается пар: {len(price_history)}"
        , parse_mode="HTML")


# ================= API =================

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
                debug_stats["klines_errors"] += 1
                debug_stats["last_error"] = f"{symbol} {interval}: HTTP {resp.status}"
    except Exception as e:
        debug_stats["klines_errors"] += 1
        debug_stats["last_error"] = f"{symbol} {interval}: {type(e).__name__} {e}"
    return []


async def get_long_term_changes(symbol, current_price):
    """% изменения цены за 7д и 30д (относительно закрытия свечи 7/30 дней назад)."""
    data = await fetch_klines(symbol, "1d", 31)
    if not data:
        return 0.0, 0.0
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


def _bar_high_low_range_pct(bar):
    """Возвращает (high-low)/low * 100 для одной MEXC-свечи [ts, open, high, low, close, vol, ...]."""
    try:
        high = float(bar[2])
        low = float(bar[3])
        if low <= 0:
            return 0.0
        return ((high - low) / low) * 100
    except Exception:
        return 0.0


def _bar_volume(bar):
    try:
        return float(bar[7]) if len(bar) > 7 else float(bar[5])
    except Exception:
        return 0.0


async def get_hour_volume_anomaly(symbol):
    """
    Берём последнюю ЗАКРЫТУЮ часовую свечу (текущий формирующийся час не считаем,
    т.к. его объём ещё не финальный) и сравниваем с МАКСИМАЛЬНЫМ объёмом среди
    ВСЕХ часовых свечей за предыдущие ~7 дней — т.е. проверяем, это НОВЫЙ РЕКОРД
    объёма за период или нет (а не превышение над средним). Также возвращаем
    high-low % последней закрытой свечи для доп. фильтра по колебанию цены.
    Кэшируем на BASELINE_TTL секунд на пару, чтобы не заваливать API klines-запросами.
    """
    cached = hour_baseline_cache.get(symbol)
    now = time.time()
    if cached and (now - cached["ts"]) < BASELINE_TTL:
        return cached["cur"], cached["ref"], cached["rng"]

    data = await fetch_klines(symbol, "60m", 192)  # 8 дней с запасом. ВАЖНО: у MEXC
    # интервал часа называется "60m", а НЕ "1h" — с "1h" API отдаёт HTTP 400 на
    # каждый запрос без исключения.
    if len(data) < 30:
        return 0.0, 0.0, 0.0

    closed = data[:-1]  # последний бар может быть ещё формирующимся
    last_closed = closed[-1]
    last_vol = _bar_volume(last_closed)
    last_rng = _bar_high_low_range_pct(last_closed)

    prior_vols = []
    for bar in closed[:-1]:  # все предыдущие часы за период, без фильтра по часу суток
        vol = _bar_volume(bar)
        if vol:
            prior_vols.append(vol)

    prior_max = max(prior_vols) if prior_vols else 0.0
    hour_baseline_cache[symbol] = {"ts": now, "ref": prior_max, "cur": last_vol, "rng": last_rng}
    return last_vol, prior_max, last_rng


async def get_minute_volume_anomaly(symbol):
    """
    МАКСИМАЛЬНЫЙ объём минутных свечей за последние ~сутки (сколько отдаст API за
    один запрос, обычно до 1000 баров ≈ 16.6ч). Сравниваем последнюю закрытую
    минуту с этим максимумом — т.е. это новый рекорд объёма за период или нет.
    Также возвращаем high-low % последней закрытой свечи.
    """
    cached = day_baseline_cache.get(symbol)
    now = time.time()
    if cached and (now - cached["ts"]) < BASELINE_TTL:
        return cached["cur"], cached["ref"], cached["rng"]

    data = await fetch_klines(symbol, "1m", 1000)
    if len(data) < 30:
        return 0.0, 0.0, 0.0

    closed = data[:-1]
    last_closed = closed[-1]
    last_vol = _bar_volume(last_closed)
    last_rng = _bar_high_low_range_pct(last_closed)
    prior_vols = [v for v in (_bar_volume(bar) for bar in closed[:-1]) if v]

    prior_max = max(prior_vols) if prior_vols else 0.0
    day_baseline_cache[symbol] = {"ts": now, "ref": prior_max, "cur": last_vol, "rng": last_rng}
    return last_vol, prior_max, last_rng


async def get_four_hour_volume_anomaly(symbol):
    """
    Аналог get_hour_volume_anomaly, но на 4-часовых свечах ("4h" у MEXC) и с
    периодом сравнения ~15 дней (90 свечей по 4ч = 15 суток, берём с запасом).
    Сравниваем последнюю ЗАКРЫТУЮ 4ч-свечу с максимумом объёма среди всех
    предыдущих 4ч-свечей за этот период — новый рекорд объёма или нет.
    Также возвращаем high-low % последней закрытой свечи.
    """
    cached = four_hour_baseline_cache.get(symbol)
    now = time.time()
    if cached and (now - cached["ts"]) < BASELINE_TTL:
        return cached["cur"], cached["ref"], cached["rng"]

    # 15 дней / 4ч = 90 свечей, +запас на "текущую формирующуюся" и погрешности
    data = await fetch_klines(symbol, "4h", 100)
    if len(data) < 20:
        return 0.0, 0.0, 0.0

    closed = data[:-1]  # последняя свеча может быть ещё формирующейся
    last_closed = closed[-1]
    last_vol = _bar_volume(last_closed)
    last_rng = _bar_high_low_range_pct(last_closed)
    prior_vols = [v for v in (_bar_volume(bar) for bar in closed[:-1]) if v]

    prior_max = max(prior_vols) if prior_vols else 0.0
    four_hour_baseline_cache[symbol] = {"ts": now, "ref": prior_max, "cur": last_vol, "rng": last_rng}
    return last_vol, prior_max, last_rng


def pct_over_baseline(current, avg):
    if avg <= 0:
        return 0.0
    return ((current - avg) / avg) * 100


# --- Ротационный "прогрев" объёмных бейзлайнов ---
# Раньше объём считался только для пар, УЖЕ прошедших ценовые фильтры (их мало).
# Теперь объём — первичный критерий, значит бейзлайн нужен для ВСЕХ ликвидных пар
# (их могут быть сотни). Чтобы не заваливать API klines-запросами разом, каждые
# check_interval секунд обновляем ограниченную пачку пар (REFRESH_BATCH), по кругу.
# Между обновлениями конкретной пары используется её последнее кэшированное
# значение (см. hour_baseline_cache / day_baseline_cache / four_hour_baseline_cache),
# т.е. цифра может быть "не идеально свежей" (до BASELINE_TTL секунд) — сознательный
# компромисс между полнотой охвата и лимитами API.
REFRESH_BATCH = 20
_refresh_queue = deque()


async def refresh_volume_baselines(candidates):
    if not _refresh_queue:
        _refresh_queue.extend(candidates)

    batch = []
    while _refresh_queue and len(batch) < REFRESH_BATCH:
        sym = _refresh_queue.popleft()
        if sym in candidates:  # монета всё ещё ликвидна — иначе просто выкидываем
            batch.append(sym)

    tasks = []
    for sym in batch:
        if settings["vh_percent"] > 0:
            tasks.append(get_hour_volume_anomaly(sym))
        if settings["vm_percent"] > 0:
            tasks.append(get_minute_volume_anomaly(sym))
        if settings["v4h_percent"] > 0:
            tasks.append(get_four_hour_volume_anomaly(sym))
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def get_cached_metrics(symbol):
    """Достаёт последние известные % превышения объёма, % колебания цены и сам
    абсолютный объём (cur) из кэша, без нового запроса. Абсолютный объём нужен
    для анти-спам правила (новый алерт разрешён только при x2 от предыдущего)."""
    vh_pct = vm_pct = v4h_pct = None
    vh_rng = vm_rng = v4h_rng = None
    vh_cur = vm_cur = v4h_cur = 0.0
    h = hour_baseline_cache.get(symbol)
    if h:
        vh_pct = pct_over_baseline(h["cur"], h["ref"])
        vh_rng = h.get("rng")
        vh_cur = h.get("cur", 0.0)
    m = day_baseline_cache.get(symbol)
    if m:
        vm_pct = pct_over_baseline(m["cur"], m["ref"])
        vm_rng = m.get("rng")
        vm_cur = m.get("cur", 0.0)
    f = four_hour_baseline_cache.get(symbol)
    if f:
        v4h_pct = pct_over_baseline(f["cur"], f["ref"])
        v4h_rng = f.get("rng")
        v4h_cur = f.get("cur", 0.0)
    return vh_pct, vm_pct, v4h_pct, vh_rng, vm_rng, v4h_rng, vh_cur, vm_cur, v4h_cur


# ================= ОСНОВНОЙ ЦИКЛ =================

async def parser_task():
    print("--- Фоновый парсер (pump/volume) запущен ---", flush=True)
    while True:
        # Сброс статистики воронки на начало прохода
        debug_stats.update({
            "ts": time.time(),
            "ticker_ok": False,
            "total_pairs": 0,
            "passed_volume_floor": 0,
            "passed_spam_guard": 0,
            "passed_volume_filter": 0,
            "passed_price_filter": 0,
            "alerts_sent": 0,
            "klines_errors": 0,
            "last_error": None,
        })
        try:
            data = await fetch_prices()
            debug_stats["ticker_ok"] = bool(data)
            debug_stats["total_pairs"] = len(data)

            if not valid_symbols or (time.time() - valid_symbols_ts) >= VALID_SYMBOLS_TTL:
                await refresh_valid_symbols()

            now = time.time()
            window_sec = settings["window_min"] * 60
            max_pts = max(int((window_sec / settings["check_interval"]) * 1.2), 5)
            cooldown_sec = settings["cooldown_min"] * 60

            # --- Собираем ликвидные пары и прогреваем для них объёмный кэш ---
            liquid_pairs = []
            for item in data:
                pair = item['symbol']
                if not pair.endswith("USDT") or pair in blacklist:
                    continue
                if valid_symbols and pair not in valid_symbols:
                    continue  # мусорный/нереальный тикер — пропускаем
                try:
                    if float(item['quoteVolume']) >= settings["min_volume"]:
                        liquid_pairs.append(pair)
                except Exception:
                    continue
            debug_stats["passed_volume_floor"] = len(liquid_pairs)

            if settings["vh_percent"] > 0 or settings["vm_percent"] > 0 or settings["v4h_percent"] > 0:
                await refresh_volume_baselines(liquid_pairs)

            for item in data:
                pair = item['symbol']
                if not pair.endswith("USDT") or pair in blacklist:
                    continue
                if valid_symbols and pair not in valid_symbols:
                    continue

                try:
                    vol = float(item['quoteVolume'])
                    if vol < settings["min_volume"]:
                        continue
                    price = float(item['lastPrice'])
                    # MEXC отдаёт priceChangePercent долей (0.0787 = 7.87%) — умножаем на 100.
                    ch_24 = float(item['priceChangePercent']) * 100
                except Exception:
                    continue

                if pair not in price_history or price_history[pair].maxlen != max_pts:
                    price_history[pair] = deque(maxlen=max_pts)

                history = price_history[pair]
                relevant = [p for (t, p) in history if (now - t) <= window_sec]

                have_full_window = (
                    len(history) > 0
                    and (now - history[0][0]) >= window_sec
                )

                if have_full_window and relevant:
                    min_p = min(relevant)
                    rise = ((price - min_p) / min_p) * 100 if min_p > 0 else 0.0
                else:
                    min_p = price
                    rise = 0.0

                if pair in alert_memory and (now - alert_memory[pair]["time"]) >= 86400:
                    del alert_memory[pair]

                # ============ ПЕРВИЧНЫЙ КРИТЕРИЙ: ОБЪЁМ ============
                # Хотя бы один из /vh, /vm, /v4h должен быть включён (>0) и выполнен —
                # иначе монета не проходит дальше вообще. Если все три выключены (0),
                # алертов не будет: боту нужен хотя бы один объёмный триггер.
                # У каждого — свой опциональный лимит на колебание цены (high-low %)
                # внутри той самой свечи, где зафиксирован рекорд объёма.
                vh_pct, vm_pct, v4h_pct, vh_rng, vm_rng, v4h_rng, vh_cur, vm_cur, v4h_cur = get_cached_metrics(pair)
                volume_ok = False
                triggered_vols = []

                if settings["vh_percent"] > 0 and vh_pct is not None and vh_pct >= settings["vh_percent"]:
                    if settings["vh_price_limit"] == 0 or (vh_rng is not None and vh_rng <= settings["vh_price_limit"]):
                        volume_ok = True
                        triggered_vols.append(vh_cur)

                if settings["vm_percent"] > 0 and vm_pct is not None and vm_pct >= settings["vm_percent"]:
                    if settings["vm_price_limit"] == 0 or (vm_rng is not None and vm_rng <= settings["vm_price_limit"]):
                        volume_ok = True
                        triggered_vols.append(vm_cur)

                if settings["v4h_percent"] > 0 and v4h_pct is not None and v4h_pct >= settings["v4h_percent"]:
                    if settings["v4h_price_limit"] == 0 or (v4h_rng is not None and v4h_rng <= settings["v4h_price_limit"]):
                        volume_ok = True
                        triggered_vols.append(v4h_cur)

                if not volume_ok:
                    history.append((now, price))
                    continue
                debug_stats["passed_volume_filter"] += 1

                trigger_vol = max(triggered_vols) if triggered_vols else 0.0

                # ============ ШАГ: АНТИ-СПАМ (x2 от предыдущего алерта) ============
                # Раньше было: простой time-based кулдаун, полностью блокирующий
                # повторные алерты на N минут. Теперь: повтор по уже уведомлённой
                # монете разрешён, только если новый триггерящий объём минимум в
                # 2 раза больше объёма, на котором сработал ПРЕДЫДУЩИЙ алерт по
                # этой же монете (порог "уезжает" вверх с каждым разом — так не
                # спамит на одном и том же уровне). cooldown_min остаётся как
                # техническая защита от дублей внутри одного и того же прохода.
                if pair in alert_memory:
                    time_ok = (now - alert_memory[pair]["last_msg"]) >= cooldown_sec
                    prev_vol = alert_memory[pair].get("last_vol", 0.0)
                    vol_ok = prev_vol > 0 and trigger_vol >= 2 * prev_vol
                    if not (time_ok and vol_ok):
                        history.append((now, price))
                        continue
                debug_stats["passed_spam_guard"] += 1

                # ============ ВТОРИЧНЫЕ (ОПЦИОНАЛЬНЫЕ) ФИЛЬТРЫ: ЦЕНА ============
                # /p теперь опционален: 0 = выключен. Если включён — потолок роста
                # в окне (монета ещё не должна была сильно разогнаться).
                price_ok = True
                if settings["percent"] > 0 and not (have_full_window and rise <= settings["percent"]):
                    price_ok = False
                if price_ok and settings["day_min_rise"] > 0 and ch_24 < settings["day_min_rise"]:
                    price_ok = False
                if price_ok and settings["day_max_rise"] > 0 and ch_24 > settings["day_max_rise"]:
                    price_ok = False

                if not price_ok:
                    history.append((now, price))
                    continue
                debug_stats["passed_price_filter"] += 1

                ch_7, ch_30 = await get_long_term_changes(pair, price)

                should_alert = True
                if settings["week_min_rise"] > 0 and ch_7 < settings["week_min_rise"]:
                    should_alert = False
                if should_alert and settings["month_min_rise"] > 0 and ch_30 < settings["month_min_rise"]:
                    should_alert = False
                if should_alert and settings["week_max_rise"] > 0 and ch_7 > settings["week_max_rise"]:
                    should_alert = False
                if should_alert and settings["month_max_rise"] > 0 and ch_30 > settings["month_max_rise"]:
                    should_alert = False

                if should_alert:
                    alert_memory[pair] = {
                        "time": alert_memory[pair]["time"] if pair in alert_memory else now,
                        "price": price,
                        "last_msg": now,
                        "last_vol": trigger_vol,
                    }
                    debug_stats["alerts_sent"] += 1

                    base_coin = pair.replace("USDT", "")
                    lines = [
                        f"🚀 <b>ПАМП: <code>{base_coin}</code></b>",
                        f"📈 В окне ({settings['window_min']}м): <b>+{rise:.2f}%</b>",
                        f"📊 За 24 часа: <b>{ch_24:.2f}%</b>",
                        f"📆 За 7 дней: <b>{ch_7:.2f}%</b>",
                        f"🗓 За 30 дней: <b>{ch_30:.2f}%</b>",
                    ]
                    if vh_pct is not None:
                        lines.append(f"🔊 Новый рекорд объёма/час (vs 7д): <b>{vh_pct:+.1f}%</b>" + (f" (колеб. цены в свече: {vh_rng:.2f}%)" if vh_rng is not None else ""))
                    if vm_pct is not None:
                        lines.append(f"🔊 Новый рекорд объёма/мин (vs сутки): <b>{vm_pct:+.1f}%</b>" + (f" (колеб. цены в свече: {vm_rng:.2f}%)" if vm_rng is not None else ""))
                    if v4h_pct is not None:
                        lines.append(f"🔊 Новый рекорд объёма/4ч (vs 15д): <b>{v4h_pct:+.1f}%</b>" + (f" (колеб. цены в свече: {v4h_rng:.2f}%)" if v4h_rng is not None else ""))
                    lines.append(f"💵 Было (мин. в окне): <code>{min_p}</code>")
                    lines.append(f"💸 Стало (тек): <code>{price}</code>")
                    lines.append(f"💰 Объём 24ч: <b>{int(vol):,}$</b>")
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

                history.append((now, price))
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

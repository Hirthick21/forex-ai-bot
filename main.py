import os
import time
import requests
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()

# ======================================================
# CONFIG
# ======================================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TWELVE_API_KEY = os.getenv("TWELVE_API_KEY")

PAIRS = ["EUR/USD", "GBP/USD", "USD/JPY"]

SCAN_SECONDS = 120
PAIR_DELAY_SECONDS = 5
COOLDOWN_SECONDS = 900

MIN_CONFIDENCE = 78
MIN_RR = 1.8
RR_TARGET = 2.0

LITHUANIA_TZ = ZoneInfo("Europe/Vilnius")
NY_TZ = ZoneInfo("America/New_York")

# ICT uses NY time, UTC-4/NY session logic
SESSION_WINDOWS_NY = {
    "ASIAN": ("20:00", "00:00"),
    "LONDON": ("02:00", "05:00"),
    "NEW_YORK": ("07:00", "10:00"),
}

MIN_SL_DISTANCE = {
    "EUR/USD": 0.0010,
    "GBP/USD": 0.0012,
    "USD/JPY": 0.10,
}

PIP_100_RANGE = {
    "EUR/USD": 0.0100,
    "GBP/USD": 0.0100,
    "USD/JPY": 1.00,
}


# ======================================================
# TELEGRAM
# ======================================================

def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Missing Telegram credentials.")
        return

    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        r = requests.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "HTML",
            },
            timeout=10,
        )
        if not r.ok:
            print("Telegram error:", r.text)
    except Exception as e:
        print("Telegram exception:", e)


# ======================================================
# DATA
# ======================================================

def get_candles(pair, interval="5min", outputsize=300):
    try:
        url = "https://api.twelvedata.com/time_series"
        params = {
            "symbol": pair,
            "interval": interval,
            "outputsize": outputsize,
            "apikey": TWELVE_API_KEY,
        }

        data = requests.get(url, params=params, timeout=10).json()

        if "values" not in data:
            print(f"{pair} Twelve Data error:", data)
            return []

        candles = []
        for c in reversed(data["values"]):
            candles.append({
                "time": c["datetime"],
                "open": float(c["open"]),
                "high": float(c["high"]),
                "low": float(c["low"]),
                "close": float(c["close"]),
                "volume": float(c.get("volume", 1) or 1),
            })

        return candles

    except Exception as e:
        print(f"{pair} candle fetch error:", e)
        return []


def parse_candle_time(c):
    # Twelve Data returns naive exchange time. Treat as UTC approximation for bot logic.
    return datetime.fromisoformat(c["time"]).replace(tzinfo=timezone.utc)


def to_ny_time(c):
    return parse_candle_time(c).astimezone(NY_TZ)


def now_lithuania():
    return datetime.now(LITHUANIA_TZ).strftime("%Y-%m-%d %H:%M:%S")


def now_ny():
    return datetime.now(NY_TZ).strftime("%Y-%m-%d %H:%M:%S")


# ======================================================
# INDICATORS
# ======================================================

def ema(values, period):
    if len(values) < period:
        return None

    k = 2 / (period + 1)
    value = sum(values[:period]) / period

    for price in values[period:]:
        value = price * k + value * (1 - k)

    return round(value, 5)


def sma(values, period):
    if len(values) < period:
        return None
    return round(sum(values[-period:]) / period, 5)


def rsi(values, period=14):
    if len(values) < period + 1:
        return None

    gains, losses = [], []

    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))

    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period

    if avg_loss == 0:
        return 100

    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def atr(candles, period=14):
    if len(candles) < period + 1:
        return None

    trs = []

    for i in range(1, len(candles)):
        high = candles[i]["high"]
        low = candles[i]["low"]
        prev_close = candles[i - 1]["close"]

        tr = max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close),
        )
        trs.append(tr)

    return round(sum(trs[-period:]) / period, 5)


def vwap(candles):
    if not candles:
        return None

    total_pv = 0
    total_vol = 0

    for c in candles[-80:]:
        typical = (c["high"] + c["low"] + c["close"]) / 3
        vol = max(c["volume"], 1)
        total_pv += typical * vol
        total_vol += vol

    if total_vol == 0:
        return None

    return round(total_pv / total_vol, 5)


# ======================================================
# ICT STRUCTURE
# ======================================================

def detect_fvgs(candles, lookback=80):
    fvgs = []
    recent = candles[-lookback:]

    for i in range(2, len(recent)):
        c1 = recent[i - 2]
        c2 = recent[i - 1]
        c3 = recent[i]

        if c3["low"] > c1["high"]:
            fvgs.append({
                "type": "BULLISH",
                "low": round(c1["high"], 5),
                "high": round(c3["low"], 5),
                "mid": round((c1["high"] + c3["low"]) / 2, 5),
                "formed": c2["time"],
            })

        elif c3["high"] < c1["low"]:
            fvgs.append({
                "type": "BEARISH",
                "low": round(c3["high"], 5),
                "high": round(c1["low"], 5),
                "mid": round((c3["high"] + c1["low"]) / 2, 5),
                "formed": c2["time"],
            })

    return fvgs[-10:]


def detect_ifvgs(fvgs, current_close):
    ifvgs = []

    for f in fvgs:
        if f["type"] == "BULLISH" and current_close < f["low"]:
            ifvgs.append({
                "type": "BEARISH_IFVG",
                "low": f["low"],
                "high": f["high"],
                "mid": f["mid"],
            })

        if f["type"] == "BEARISH" and current_close > f["high"]:
            ifvgs.append({
                "type": "BULLISH_IFVG",
                "low": f["low"],
                "high": f["high"],
                "mid": f["mid"],
            })

    return ifvgs[-5:]


def active_5m_fvg_for_entry(fvgs, price, side, atr_value):
    for f in reversed(fvgs):
        near_zone = f["low"] - atr_value <= price <= f["high"] + atr_value

        if side == "BUY" and f["type"] == "BULLISH" and near_zone:
            return f

        if side == "SELL" and f["type"] == "BEARISH" and near_zone:
            return f

    return None


def nearest_swing_levels(candles, price, lookback=80):
    recent = candles[-lookback:]
    swing_highs = []
    swing_lows = []

    for i in range(2, len(recent) - 2):
        c = recent[i]

        if c["high"] > recent[i - 1]["high"] and c["high"] > recent[i + 1]["high"]:
            swing_highs.append(c["high"])

        if c["low"] < recent[i - 1]["low"] and c["low"] < recent[i + 1]["low"]:
            swing_lows.append(c["low"])

    resistance_candidates = [h for h in swing_highs if h > price]
    support_candidates = [l for l in swing_lows if l < price]

    nearest_resistance = min(resistance_candidates) if resistance_candidates else max(c["high"] for c in recent)
    nearest_support = max(support_candidates) if support_candidates else min(c["low"] for c in recent)

    return round(nearest_support, 5), round(nearest_resistance, 5)


def get_session_range(candles, session_name):
    start_str, end_str = SESSION_WINDOWS_NY[session_name]

    start_h, start_m = map(int, start_str.split(":"))
    end_h, end_m = map(int, end_str.split(":"))

    now_ny_dt = datetime.now(NY_TZ)
    session_date = now_ny_dt.date()

    start = datetime(session_date.year, session_date.month, session_date.day, start_h, start_m, tzinfo=NY_TZ)
    end = datetime(session_date.year, session_date.month, session_date.day, end_h, end_m, tzinfo=NY_TZ)

    if end <= start:
        end += timedelta(days=1)

    selected = []

    for c in candles:
        t = to_ny_time(c)

        # Asian session may start previous NY day.
        if session_name == "ASIAN" and t.hour >= 20:
            selected.append(c)
        elif session_name == "ASIAN" and t.hour < 1:
            selected.append(c)
        elif start <= t <= end:
            selected.append(c)

    if not selected:
        return None

    return {
        "high": round(max(c["high"] for c in selected), 5),
        "low": round(min(c["low"] for c in selected), 5),
    }


def session_condition(price, session_range):
    if not session_range:
        return "N/A"

    if price > session_range["high"]:
        return "ABOVE HIGH / Possible buy-side liquidity"
    if price < session_range["low"]:
        return "BELOW LOW / Possible sell-side liquidity"
    return "INSIDE RANGE"


def true_day_open_candle(candles):
    ny_today = datetime.now(NY_TZ).date()

    candidates = []
    for c in candles:
        t = to_ny_time(c)
        if t.date() == ny_today and t.hour == 0:
            candidates.append(c)

    if not candidates:
        return None

    c = candidates[0]

    avg_volume = sum(x["volume"] for x in candles[-50:]) / max(len(candles[-50:]), 1)
    good_volume = c["volume"] >= avg_volume * 1.2

    last_close = candles[-1]["close"]

    if good_volume and last_close > c["high"]:
        bias = "BULLISH CLOSE ABOVE TRUE DAY OPEN HIGH"
    elif good_volume and last_close < c["low"]:
        bias = "BEARISH CLOSE BELOW TRUE DAY OPEN LOW"
    else:
        bias = "NO CLEAR TDO BREAK"

    return {
        "open": round(c["open"], 5),
        "high": round(c["high"], 5),
        "low": round(c["low"], 5),
        "close": round(c["close"], 5),
        "good_volume": good_volume,
        "bias": bias,
    }


def detect_ndog(candles, price, pair):
    ny_today = datetime.now(NY_TZ).date()
    today = []
    previous = []

    for c in candles:
        t = to_ny_time(c)

        if t.date() == ny_today:
            today.append(c)
        elif t.date() < ny_today:
            previous.append(c)

    if not today or not previous:
        return None

    day_open = today[0]["open"]
    prev_close = previous[-1]["close"]
    gap = round(day_open - prev_close, 5)

    zone_low = round(min(day_open, prev_close), 5)
    zone_high = round(max(day_open, prev_close), 5)

    radius = PIP_100_RANGE.get(pair, 0.0100)
    nearby = zone_low - radius <= price <= zone_high + radius

    return {
        "direction": "UP" if gap > 0 else "DOWN",
        "low": zone_low,
        "high": zone_high,
        "gap": gap,
        "nearby_100_pips": nearby,
    }


def detect_nwog(candles, price, pair):
    now_week = datetime.now(NY_TZ).isocalendar().week
    this_week = []
    old = []

    for c in candles:
        t = to_ny_time(c)

        if t.isocalendar().week == now_week:
            this_week.append(c)
        else:
            old.append(c)

    if not this_week or not old:
        return None

    week_open = this_week[0]["open"]
    prev_close = old[-1]["close"]
    gap = round(week_open - prev_close, 5)

    zone_low = round(min(week_open, prev_close), 5)
    zone_high = round(max(week_open, prev_close), 5)

    radius = PIP_100_RANGE.get(pair, 0.0100)
    nearby = zone_low - radius <= price <= zone_high + radius

    return {
        "direction": "UP" if gap > 0 else "DOWN",
        "low": zone_low,
        "high": zone_high,
        "gap": gap,
        "nearby_100_pips": nearby,
    }


def current_session_name():
    h = datetime.now(NY_TZ).hour

    if 20 <= h or h < 1:
        return "ASIAN"
    if 2 <= h < 5:
        return "LONDON"
    if 7 <= h < 10:
        return "NEW_YORK"
    return "OFF-SESSION"


def trade_session_ok():
    return current_session_name() in ["LONDON", "NEW_YORK"]


# ======================================================
# ANALYSIS
# ======================================================

def analyze_tf(candles):
    if len(candles) < 80:
        return None

    closes = [c["close"] for c in candles]
    price = closes[-1]

    ema15 = ema(closes, 15)
    sma9 = sma(closes, 9)
    sma15 = sma(closes, 15)
    rsi14 = rsi(closes, 14)
    atr14 = atr(candles, 14)
    vwap_value = vwap(candles)

    if None in [ema15, sma9, sma15, rsi14, atr14, vwap_value]:
        return None

    if price > vwap_value and price > ema15 and sma9 > sma15:
        trend = "BULLISH"
    elif price < vwap_value and price < ema15 and sma9 < sma15:
        trend = "BEARISH"
    else:
        trend = "NEUTRAL"

    return {
        "price": round(price, 5),
        "ema15": ema15,
        "sma9": sma9,
        "sma15": sma15,
        "vwap": vwap_value,
        "rsi": rsi14,
        "atr": atr14,
        "trend": trend,
    }


def build_signal(pair):
    candles_5m = get_candles(pair, "5min", 300)
    candles_15m = get_candles(pair, "15min", 160)

    if len(candles_5m) < 100 or len(candles_15m) < 80:
        print(f"{pair}: Not enough data.")
        return None

    tf5 = analyze_tf(candles_5m)
    tf15 = analyze_tf(candles_15m)

    if not tf5 or not tf15:
        print(f"{pair}: Analysis failed.")
        return None

    price = tf5["price"]
    atr_value = tf5["atr"]

    fvgs_5m = detect_fvgs(candles_5m, 80)
    fvgs_15m = detect_fvgs(candles_15m, 60)
    ifvgs_15m = detect_ifvgs(fvgs_15m, tf15["price"])

    support, resistance = nearest_swing_levels(candles_5m, price)

    asian = get_session_range(candles_5m, "ASIAN")
    london = get_session_range(candles_5m, "LONDON")
    new_york = get_session_range(candles_5m, "NEW_YORK")

    tdo = true_day_open_candle(candles_5m)
    ndog = detect_ndog(candles_5m, price, pair)
    nwog = detect_nwog(candles_5m, price, pair)

    session = current_session_name()

    print(
        f"{datetime.now()} | {pair} | Price={price} | "
        f"5m={tf5['trend']} 15m={tf15['trend']} | "
        f"Session={session} | RSI={tf5['rsi']} VWAP={tf5['vwap']} ATR={atr_value}"
    )

    if not trade_session_ok():
        print(f"{pair}: Outside London/New York execution window.")
        return None

    confidence = 0
    reasons = []
    warnings = []
    side = None
    active_fvg = None

    # BUY
    if tf15["trend"] == "BULLISH" and tf5["trend"] == "BULLISH":
        side = "BUY"
        confidence += 25
        reasons.append("15m and 5m structure bullish")

        if price > tf5["vwap"]:
            confidence += 12
            reasons.append("Price above VWAP")

        if tf5["sma9"] > tf5["sma15"]:
            confidence += 10
            reasons.append("SMA9 above SMA15")

        if 45 <= tf5["rsi"] <= 68:
            confidence += 10
            reasons.append(f"RSI {tf5['rsi']} supports bullish continuation")

        active_fvg = active_5m_fvg_for_entry(fvgs_5m, price, "BUY", atr_value)

        if active_fvg:
            confidence += 25
            reasons.append(f"5m bullish FVG entry zone active: {active_fvg['low']} - {active_fvg['high']}")
            stop_loss = active_fvg["low"]
        else:
            warnings.append("No valid 5m bullish FVG near price")
            return None

        if ifvgs_15m:
            confidence += 8
            reasons.append("15m IFVG context detected")

        if tdo and "BULLISH" in tdo["bias"]:
            confidence += 8
            reasons.append("True Day Open candle supports bullish bias")

        if ndog and ndog["nearby_100_pips"]:
            confidence += 6
            reasons.append("NDOG within 100 pips")

        if nwog and nwog["nearby_100_pips"]:
            confidence += 6
            reasons.append("NWOG within 100 pips")

        if price > support:
            confidence += 6
            reasons.append(f"Price holding above swing support {support}")

        min_sl = MIN_SL_DISTANCE.get(pair, 0.0010)
        risk = max(abs(price - stop_loss), min_sl)
        stop_loss = round(price - risk, 5)
        take_profit = round(price + risk * RR_TARGET, 5)

    # SELL
    elif tf15["trend"] == "BEARISH" and tf5["trend"] == "BEARISH":
        side = "SELL"
        confidence += 25
        reasons.append("15m and 5m structure bearish")

        if price < tf5["vwap"]:
            confidence += 12
            reasons.append("Price below VWAP")

        if tf5["sma9"] < tf5["sma15"]:
            confidence += 10
            reasons.append("SMA9 below SMA15")

        if 32 <= tf5["rsi"] <= 55:
            confidence += 10
            reasons.append(f"RSI {tf5['rsi']} supports bearish continuation")

        active_fvg = active_5m_fvg_for_entry(fvgs_5m, price, "SELL", atr_value)

        if active_fvg:
            confidence += 25
            reasons.append(f"5m bearish FVG entry zone active: {active_fvg['low']} - {active_fvg['high']}")
            stop_loss = active_fvg["high"]
        else:
            warnings.append("No valid 5m bearish FVG near price")
            return None

        if ifvgs_15m:
            confidence += 8
            reasons.append("15m IFVG context detected")

        if tdo and "BEARISH" in tdo["bias"]:
            confidence += 8
            reasons.append("True Day Open candle supports bearish bias")

        if ndog and ndog["nearby_100_pips"]:
            confidence += 6
            reasons.append("NDOG within 100 pips")

        if nwog and nwog["nearby_100_pips"]:
            confidence += 6
            reasons.append("NWOG within 100 pips")

        if price < resistance:
            confidence += 6
            reasons.append(f"Price trading below swing resistance {resistance}")

        min_sl = MIN_SL_DISTANCE.get(pair, 0.0010)
        risk = max(abs(stop_loss - price), min_sl)
        stop_loss = round(price + risk, 5)
        take_profit = round(price - risk * RR_TARGET, 5)

    else:
        return None

    reward = abs(take_profit - price)
    rr = round(reward / risk, 2) if risk > 0 else 0

    if confidence < MIN_CONFIDENCE:
        print(f"{pair}: Confidence too low: {confidence}%")
        return None

    if rr < MIN_RR:
        print(f"{pair}: RR too low: {rr}")
        return None

    return {
        "pair": pair,
        "side": side,
        "entry": price,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "rr": rr,
        "confidence": confidence,
        "tf5": tf5,
        "tf15": tf15,
        "support": support,
        "resistance": resistance,
        "active_fvg": active_fvg,
        "ifvgs_15m": ifvgs_15m,
        "tdo": tdo,
        "ndog": ndog,
        "nwog": nwog,
        "asian": asian,
        "london": london,
        "new_york": new_york,
        "session": session,
        "reasons": reasons,
        "warnings": warnings,
    }


# ======================================================
# ALERT FORMAT
# ======================================================

def fmt_range(r):
    if not r:
        return "N/A"
    return f"H {r['high']} / L {r['low']}"


def fmt_gap(g):
    if not g:
        return "None"
    near = "within 100 pips" if g["nearby_100_pips"] else "far"
    return f"{g['direction']} {g['low']} - {g['high']} ({near})"


def send_signal(s):
    emoji = "🟢" if s["side"] == "BUY" else "🔴"
    fvg = s["active_fvg"]

    reasons = "\n".join([f"✅ {r}" for r in s["reasons"]])
    warnings = "\n".join([f"⚠️ {w}" for w in s["warnings"]]) or "None"

    ifvg_text = "None"
    if s["ifvgs_15m"]:
        ifvg_text = ", ".join([f"{x['type']} {x['low']}-{x['high']}" for x in s["ifvgs_15m"]])

    tdo = s["tdo"]
    tdo_text = "None"
    if tdo:
        tdo_text = (
            f"O {tdo['open']} / H {tdo['high']} / L {tdo['low']} / C {tdo['close']} "
            f"| {tdo['bias']}"
        )

    message = f"""
{emoji} <b>{s['pair']} {s['side']} ICT ENTRY ZONE</b>

<b>Entry:</b> {s['entry']}
<b>Stop Loss:</b> {s['stop_loss']}
<b>Take Profit:</b> {s['take_profit']}
<b>Risk:Reward:</b> {s['rr']}x
<b>Confidence:</b> {s['confidence']}%

<b>Time:</b>
Lithuania: {now_lithuania()}
New York: {now_ny()}
Session: {s['session']}

<b>Trend:</b>
5m: {s['tf5']['trend']}
15m: {s['tf15']['trend']}

<b>5m Indicators:</b>
EMA15: {s['tf5']['ema15']}
SMA9: {s['tf5']['sma9']}
SMA15: {s['tf5']['sma15']}
VWAP: {s['tf5']['vwap']}
RSI: {s['tf5']['rsi']}
ATR: {s['tf5']['atr']}

<b>ICT Zones:</b>
5m FVG: {fvg['type']} {fvg['low']} - {fvg['high']}
15m IFVG: {ifvg_text}
NDOG: {fmt_gap(s['ndog'])}
NWOG: {fmt_gap(s['nwog'])}
True Day Open: {tdo_text}

<b>Session Highs/Lows:</b>
Asian: {fmt_range(s['asian'])} | {session_condition(s['entry'], s['asian'])}
London: {fmt_range(s['london'])} | {session_condition(s['entry'], s['london'])}
New York: {fmt_range(s['new_york'])} | {session_condition(s['entry'], s['new_york'])}

<b>Swing Levels:</b>
Support: {s['support']}
Resistance: {s['resistance']}

<b>Reasons:</b>
{reasons}

<b>Warnings:</b>
{warnings}

<b>Mode:</b> Paper Alert Only
"""

    send_telegram(message)


# ======================================================
# MAIN LOOP
# ======================================================

def main():
    send_telegram(
        "🤖 <b>Forex Bot V1.4 ONLINE</b>\n\n"
        "Pairs: EUR/USD, GBP/USD, USD/JPY\n"
        "Logic: ICT-style FVG + IFVG + NDOG/NWOG + True Day Open\n"
        "Indicators: EMA15, SMA9, SMA15, VWAP, RSI, ATR\n"
        "Time: Lithuania alerts + New York session logic\n"
        "Mode: Paper alerts only\n"
        "Scan: Every 120 seconds"
    )

    last_signal_time = {}
    last_signal_side = {}

    while True:
        for pair in PAIRS:
            signal = build_signal(pair)

            if signal:
                now = time.time()
                previous_time = last_signal_time.get(pair, 0)
                previous_side = last_signal_side.get(pair)

                cooldown_ok = now - previous_time >= COOLDOWN_SECONDS
                side_changed = signal["side"] != previous_side

                if cooldown_ok or side_changed:
                    send_signal(signal)
                    last_signal_time[pair] = now
                    last_signal_side[pair] = signal["side"]
                else:
                    print(f"{pair}: Signal skipped due to cooldown.")

            time.sleep(PAIR_DELAY_SECONDS)

        time.sleep(SCAN_SECONDS)


if __name__ == "__main__":
    main()

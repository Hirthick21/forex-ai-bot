import os
import time
import requests
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

# ======================================================
# CONFIG
# ======================================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TWELVE_API_KEY = os.getenv("TWELVE_API_KEY")
PAIR = os.getenv("PAIR", "EUR/USD")

SCAN_SECONDS = 60
COOLDOWN_SECONDS = 900

MIN_CONFIDENCE = 75
MIN_RR = 1.8

MIN_SL_DISTANCE = 0.0010  # 10 pips for EUR/USD
RR_TARGET = 2.0

LAST_SIGNAL_TIME = 0
LAST_SIGNAL_SIDE = None


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
# TWELVE DATA
# ======================================================

def get_candles(interval="5min", outputsize=120):
    try:
        url = "https://api.twelvedata.com/time_series"
        params = {
            "symbol": PAIR,
            "interval": interval,
            "outputsize": outputsize,
            "apikey": TWELVE_API_KEY,
        }

        data = requests.get(url, params=params, timeout=10).json()

        if "values" not in data:
            print("Twelve Data error:", data)
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
        print("Candle fetch error:", e)
        return []


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

    gains = []
    losses = []

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

    for c in candles[-60:]:
        typical_price = (c["high"] + c["low"] + c["close"]) / 3
        volume = max(c["volume"], 1)
        total_pv += typical_price * volume
        total_vol += volume

    if total_vol == 0:
        return None

    return round(total_pv / total_vol, 5)


def support_resistance(candles, lookback=50):
    recent = candles[-lookback:]
    support = min(c["low"] for c in recent)
    resistance = max(c["high"] for c in recent)
    return round(support, 5), round(resistance, 5)


def detect_fvg(candles, lookback=40):
    """
    Simple 3-candle Fair Value Gap detection.
    Bullish FVG: candle 3 low > candle 1 high.
    Bearish FVG: candle 3 high < candle 1 low.
    """
    fvgs = []
    recent = candles[-lookback:]

    for i in range(2, len(recent)):
        c1 = recent[i - 2]
        c3 = recent[i]

        if c3["low"] > c1["high"]:
            fvgs.append({
                "type": "BULLISH",
                "low": round(c1["high"], 5),
                "high": round(c3["low"], 5),
            })

        elif c3["high"] < c1["low"]:
            fvgs.append({
                "type": "BEARISH",
                "low": round(c3["high"], 5),
                "high": round(c1["low"], 5),
            })

    return fvgs[-5:]


def detect_ndog(candles):
    """
    New Day Opening Gap.
    Approximate using current UTC day first candle vs previous candle close.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    today_candles = [c for c in candles if c["time"].startswith(today)]
    previous = [c for c in candles if not c["time"].startswith(today)]

    if not today_candles or not previous:
        return None

    day_open = today_candles[0]["open"]
    prev_close = previous[-1]["close"]
    gap = round(day_open - prev_close, 5)

    if abs(gap) < 0.00005:
        return None

    return {
        "direction": "UP" if gap > 0 else "DOWN",
        "low": round(min(day_open, prev_close), 5),
        "high": round(max(day_open, prev_close), 5),
        "gap": gap,
    }


def detect_nwog(candles):
    """
    New Week Opening Gap.
    Simple approximation using ISO week.
    """
    now_week = datetime.now(timezone.utc).isocalendar().week

    this_week = []
    old = []

    for c in candles:
        try:
            candle_week = datetime.fromisoformat(c["time"]).isocalendar().week
            if candle_week == now_week:
                this_week.append(c)
            else:
                old.append(c)
        except Exception:
            continue

    if not this_week or not old:
        return None

    week_open = this_week[0]["open"]
    prev_close = old[-1]["close"]
    gap = round(week_open - prev_close, 5)

    if abs(gap) < 0.00005:
        return None

    return {
        "direction": "UP" if gap > 0 else "DOWN",
        "low": round(min(week_open, prev_close), 5),
        "high": round(max(week_open, prev_close), 5),
        "gap": gap,
    }


def session_ok():
    hour = datetime.now(timezone.utc).hour
    london = 7 <= hour <= 16
    new_york = 12 <= hour <= 21
    return london or new_york


# ======================================================
# SIGNAL ENGINE
# ======================================================

def analyze_timeframe(candles):
    closes = [c["close"] for c in candles]
    price = closes[-1]

    ema15 = ema(closes, 15)
    sma21 = sma(closes, 21)
    rsi14 = rsi(closes, 14)
    atr14 = atr(candles, 14)
    vwap_value = vwap(candles)

    if None in [ema15, sma21, rsi14, atr14, vwap_value]:
        return None

    if price > ema15 > sma21:
        trend = "BULLISH"
    elif price < ema15 < sma21:
        trend = "BEARISH"
    else:
        trend = "NEUTRAL"

    return {
        "price": round(price, 5),
        "ema15": ema15,
        "sma21": sma21,
        "rsi": rsi14,
        "atr": atr14,
        "vwap": vwap_value,
        "trend": trend,
    }


def fvg_near_price(fvgs, price, side, atr_value):
    for f in reversed(fvgs):
        if side == "BUY" and f["type"] == "BULLISH":
            if f["low"] - atr_value <= price <= f["high"] + atr_value:
                return f

        if side == "SELL" and f["type"] == "BEARISH":
            if f["low"] - atr_value <= price <= f["high"] + atr_value:
                return f

    return None


def gap_confluence(gap, price, atr_value):
    if not gap:
        return False

    return gap["low"] - atr_value <= price <= gap["high"] + atr_value


def build_signal():
    candles_5m = get_candles("5min", 120)
    candles_15m = get_candles("15min", 120)

    if len(candles_5m) < 70 or len(candles_15m) < 70:
        print("Not enough candle data.")
        return None

    tf5 = analyze_timeframe(candles_5m)
    tf15 = analyze_timeframe(candles_15m)

    if not tf5 or not tf15:
        print("Indicator analysis failed.")
        return None

    if not session_ok():
        print("Outside London/New York session.")
        return None

    price = tf5["price"]
    atr_value = tf5["atr"]

    support, resistance = support_resistance(candles_5m)
    fvgs = detect_fvg(candles_5m)
    ndog = detect_ndog(candles_5m)
    nwog = detect_nwog(candles_5m)

    confidence = 0
    reasons = []
    warnings = []
    side = None

    print(
        f"{datetime.now()} | {PAIR} | Price={price} | "
        f"5m={tf5['trend']} 15m={tf15['trend']} | "
        f"RSI={tf5['rsi']} VWAP={tf5['vwap']} ATR={atr_value}"
    )

    # BUY SETUP
    if tf15["trend"] == "BULLISH" and tf5["trend"] == "BULLISH":
        side = "BUY"
        confidence += 25
        reasons.append("15m and 5m trend are bullish")

        if price > tf5["vwap"]:
            confidence += 15
            reasons.append("Price is above VWAP")

        if price > tf5["ema15"] > tf5["sma21"]:
            confidence += 15
            reasons.append("EMA15 is above SMA21 with price above both")

        if 45 <= tf5["rsi"] <= 68:
            confidence += 10
            reasons.append(f"RSI {tf5['rsi']} supports bullish momentum")

        active_fvg = fvg_near_price(fvgs, price, "BUY", atr_value)
        if active_fvg:
            confidence += 20
            reasons.append(f"Bullish FVG active: {active_fvg['low']} - {active_fvg['high']}")
        else:
            active_fvg = None
            warnings.append("No nearby bullish FVG")

        if gap_confluence(ndog, price, atr_value):
            confidence += 8
            reasons.append("NDOG zone confluence")

        if gap_confluence(nwog, price, atr_value):
            confidence += 8
            reasons.append("NWOG zone confluence")

        if price > support:
            confidence += 7
            reasons.append(f"Price is holding above support {support}")

        risk_distance = max(atr_value * 1.5, MIN_SL_DISTANCE)
        stop_loss = round(price - risk_distance, 5)
        take_profit = round(price + risk_distance * RR_TARGET, 5)

    # SELL SETUP
    elif tf15["trend"] == "BEARISH" and tf5["trend"] == "BEARISH":
        side = "SELL"
        confidence += 25
        reasons.append("15m and 5m trend are bearish")

        if price < tf5["vwap"]:
            confidence += 15
            reasons.append("Price is below VWAP")

        if price < tf5["ema15"] < tf5["sma21"]:
            confidence += 15
            reasons.append("EMA15 is below SMA21 with price below both")

        if 32 <= tf5["rsi"] <= 55:
            confidence += 10
            reasons.append(f"RSI {tf5['rsi']} supports bearish momentum")

        active_fvg = fvg_near_price(fvgs, price, "SELL", atr_value)
        if active_fvg:
            confidence += 20
            reasons.append(f"Bearish FVG active: {active_fvg['low']} - {active_fvg['high']}")
        else:
            active_fvg = None
            warnings.append("No nearby bearish FVG")

        if gap_confluence(ndog, price, atr_value):
            confidence += 8
            reasons.append("NDOG zone confluence")

        if gap_confluence(nwog, price, atr_value):
            confidence += 8
            reasons.append("NWOG zone confluence")

        if price < resistance:
            confidence += 7
            reasons.append(f"Price is below resistance {resistance}")

        risk_distance = max(atr_value * 1.5, MIN_SL_DISTANCE)
        stop_loss = round(price + risk_distance, 5)
        take_profit = round(price - risk_distance * RR_TARGET, 5)

    else:
        return None

    risk = abs(price - stop_loss)
    reward = abs(take_profit - price)
    rr = round(reward / risk, 2) if risk > 0 else 0

    if confidence < MIN_CONFIDENCE:
        print(f"Confidence too low: {confidence}%")
        return None

    if rr < MIN_RR:
        print(f"RR too low: {rr}")
        return None

    return {
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
        "ndog": ndog,
        "nwog": nwog,
        "reasons": reasons,
        "warnings": warnings,
    }


def send_signal(signal):
    global LAST_SIGNAL_TIME, LAST_SIGNAL_SIDE

    emoji = "🟢" if signal["side"] == "BUY" else "🔴"

    reasons = "\n".join([f"✅ {r}" for r in signal["reasons"]])
    warnings = "\n".join([f"⚠️ {w}" for w in signal["warnings"]]) or "None"

    fvg_text = "None"
    if signal["active_fvg"]:
        f = signal["active_fvg"]
        fvg_text = f"{f['type']} {f['low']} - {f['high']}"

    ndog_text = "None"
    if signal["ndog"]:
        g = signal["ndog"]
        ndog_text = f"{g['direction']} {g['low']} - {g['high']}"

    nwog_text = "None"
    if signal["nwog"]:
        g = signal["nwog"]
        nwog_text = f"{g['direction']} {g['low']} - {g['high']}"

    message = f"""
{emoji} <b>{PAIR} {signal['side']} ENTRY ZONE</b>

<b>Entry:</b> {signal['entry']}
<b>Stop Loss:</b> {signal['stop_loss']}
<b>Take Profit:</b> {signal['take_profit']}
<b>Risk:Reward:</b> {signal['rr']}x
<b>Confidence:</b> {signal['confidence']}%

<b>Trend:</b>
5m: {signal['tf5']['trend']}
15m: {signal['tf15']['trend']}

<b>Indicators 5m:</b>
EMA15: {signal['tf5']['ema15']}
SMA21: {signal['tf5']['sma21']}
VWAP: {signal['tf5']['vwap']}
RSI: {signal['tf5']['rsi']}
ATR: {signal['tf5']['atr']}

<b>Key Zones:</b>
Support: {signal['support']}
Resistance: {signal['resistance']}
FVG: {fvg_text}
NDOG: {ndog_text}
NWOG: {nwog_text}

<b>Reasons:</b>
{reasons}

<b>Warnings:</b>
{warnings}

<b>Mode:</b> Paper Alert Only
<b>Time:</b> {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
"""

    send_telegram(message)


def main():
    global LAST_SIGNAL_TIME, LAST_SIGNAL_SIDE

    send_telegram(
        "🤖 <b>Forex Bot ONLINE</b>\n\n"
        f"Pair: {PAIR}\n"
        "Strategy: EMA15 + SMA21 + VWAP + FVG + NDOG + NWOG\n"
        "Mode: Paper alerts only\n"
        "Scan: Every 60 seconds"
    )

    while True:
        signal = build_signal()

        if signal:
            now = time.time()

            cooldown_ok = now - LAST_SIGNAL_TIME >= COOLDOWN_SECONDS
            side_changed = signal["side"] != LAST_SIGNAL_SIDE

            if cooldown_ok or side_changed:
                send_signal(signal)
                LAST_SIGNAL_TIME = now
                LAST_SIGNAL_SIDE = signal["side"]
            else:
                print("Signal skipped: cooldown active.")

        time.sleep(SCAN_SECONDS)


if __name__ == "__main__":
    main()

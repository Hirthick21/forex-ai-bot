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

PAIRS = ["GBP/USD"]

SCAN_SECONDS = 300
PAIR_DELAY_SECONDS = 10
COOLDOWN_SECONDS = 1800

MIN_CONFIDENCE = 78
MIN_RR = 1.8
RR_TARGET = 2.0

LITHUANIA_TZ = ZoneInfo("Europe/Vilnius")
NY_TZ = ZoneInfo("America/New_York")

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

def get_candles(pair, interval, outputsize=120):
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
            print(f"{pair} {interval} Twelve Data error:", data)
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
        print(f"{pair} {interval} candle fetch error:", e)
        return []


def parse_time(c):
    return datetime.fromisoformat(c["time"]).replace(tzinfo=timezone.utc)


def to_ny(c):
    return parse_time(c).astimezone(NY_TZ)


def lithuania_time():
    return datetime.now(LITHUANIA_TZ).strftime("%Y-%m-%d %H:%M:%S")


def ny_time():
    return datetime.now(NY_TZ).strftime("%Y-%m-%d %H:%M:%S")


# ======================================================
# INDICATORS / HELPERS
# ======================================================

def sma(values, period):
    if len(values) < period:
        return None
    return round(sum(values[-period:]) / period, 5)


def ema(values, period):
    if len(values) < period:
        return None

    k = 2 / (period + 1)
    value = sum(values[:period]) / period

    for price in values[period:]:
        value = price * k + value * (1 - k)

    return round(value, 5)


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

        trs.append(max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close),
        ))

    return round(sum(trs[-period:]) / period, 5)


def vwap(candles, lookback=80):
    recent = candles[-lookback:]

    total_pv = 0
    total_vol = 0

    for c in recent:
        typical = (c["high"] + c["low"] + c["close"]) / 3
        vol = max(c["volume"], 1)
        total_pv += typical * vol
        total_vol += vol

    if total_vol == 0:
        return None

    return round(total_pv / total_vol, 5)


def volume_spike(candles, lookback=30, multiplier=1.3):
    if len(candles) < lookback + 1:
        return False, 1.0

    previous = candles[-lookback - 1:-1]
    current = candles[-1]["volume"]
    avg = sum(c["volume"] for c in previous) / len(previous)

    if avg <= 0:
        return False, 1.0

    ratio = current / avg
    return ratio >= multiplier, round(ratio, 2)


def candle_quality(candle, side):
    body = abs(candle["close"] - candle["open"])
    full_range = candle["high"] - candle["low"]

    if full_range <= 0:
        return False, "Invalid candle"

    body_ratio = body / full_range
    upper_wick = candle["high"] - max(candle["open"], candle["close"])
    lower_wick = min(candle["open"], candle["close"]) - candle["low"]

    upper_ratio = upper_wick / full_range
    lower_ratio = lower_wick / full_range

    if body_ratio < 0.60:
        return False, f"Weak body {round(body_ratio * 100)}%"

    if side == "BUY" and upper_ratio > 0.30:
        return False, f"Upper wick too large {round(upper_ratio * 100)}%"

    if side == "SELL" and lower_ratio > 0.30:
        return False, f"Lower wick too large {round(lower_ratio * 100)}%"

    return True, f"Good displacement candle body {round(body_ratio * 100)}%"


# ======================================================
# ICT / PD ARRAYS
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


def nearest_pd_array(fvgs, ifvgs, price, side, atr_value):
    candidates = []

    for f in fvgs:
        if side == "BUY" and f["type"] == "BULLISH":
            if f["low"] - atr_value <= price <= f["high"] + atr_value:
                candidates.append(("FVG", f))

        if side == "SELL" and f["type"] == "BEARISH":
            if f["low"] - atr_value <= price <= f["high"] + atr_value:
                candidates.append(("FVG", f))

    for f in ifvgs:
        if side == "BUY" and f["type"] == "BULLISH_IFVG":
            if f["low"] - atr_value <= price <= f["high"] + atr_value:
                candidates.append(("IFVG", f))

        if side == "SELL" and f["type"] == "BEARISH_IFVG":
            if f["low"] - atr_value <= price <= f["high"] + atr_value:
                candidates.append(("IFVG", f))

    if not candidates:
        return None, None

    return candidates[-1]


def swing_levels(candles, price, lookback=100):
    recent = candles[-lookback:]
    swing_highs = []
    swing_lows = []

    for i in range(2, len(recent) - 2):
        c = recent[i]

        if c["high"] > recent[i - 1]["high"] and c["high"] > recent[i + 1]["high"]:
            swing_highs.append(c["high"])

        if c["low"] < recent[i - 1]["low"] and c["low"] < recent[i + 1]["low"]:
            swing_lows.append(c["low"])

    resistance_candidates = [x for x in swing_highs if x > price]
    support_candidates = [x for x in swing_lows if x < price]

    resistance = min(resistance_candidates) if resistance_candidates else max(c["high"] for c in recent)
    support = max(support_candidates) if support_candidates else min(c["low"] for c in recent)

    return round(support, 5), round(resistance, 5)


def premium_discount(candles, price, lookback=80):
    recent = candles[-lookback:]
    high = max(c["high"] for c in recent)
    low = min(c["low"] for c in recent)
    mid = (high + low) / 2

    if price > mid:
        zone = "PREMIUM"
    elif price < mid:
        zone = "DISCOUNT"
    else:
        zone = "EQUILIBRIUM"

    return {
        "high": round(high, 5),
        "low": round(low, 5),
        "mid": round(mid, 5),
        "zone": zone,
    }


def previous_day_levels(daily_candles):
    if len(daily_candles) < 2:
        return None

    prev = daily_candles[-2]

    return {
        "high": round(prev["high"], 5),
        "low": round(prev["low"], 5),
        "close": round(prev["close"], 5),
    }


def daily_bias(daily):
    if len(daily) < 20:
        return None

    closes = [c["close"] for c in daily]
    last = daily[-1]
    prev = daily[-2]

    ema15 = ema(closes, 15)
    sma9 = sma(closes, 9)
    sma15 = sma(closes, 15)
    pd = premium_discount(daily, last["close"], 20)
    prev_day = previous_day_levels(daily)

    score_bull = 0
    score_bear = 0
    reasons = []

    if last["close"] > last["open"]:
        score_bull += 1
        reasons.append("Daily candle currently bullish")
    else:
        score_bear += 1
        reasons.append("Daily candle currently bearish")

    if last["close"] > ema15:
        score_bull += 1
        reasons.append("Daily close above EMA15")
    else:
        score_bear += 1
        reasons.append("Daily close below EMA15")

    if sma9 and sma15 and sma9 > sma15:
        score_bull += 1
        reasons.append("Daily SMA9 above SMA15")
    elif sma9 and sma15 and sma9 < sma15:
        score_bear += 1
        reasons.append("Daily SMA9 below SMA15")

    if prev_day:
        if last["close"] > prev_day["high"]:
            score_bull += 1
            reasons.append("Price trading above previous day high")
        elif last["close"] < prev_day["low"]:
            score_bear += 1
            reasons.append("Price trading below previous day low")

    if score_bull > score_bear:
        bias = "BULLISH"
        assumption = "Prefer longs. Expect price to seek buy-side liquidity or rebalance bullish PD arrays."
    elif score_bear > score_bull:
        bias = "BEARISH"
        assumption = "Prefer shorts. Expect price to seek sell-side liquidity or rebalance bearish PD arrays."
    else:
        bias = "NEUTRAL"
        assumption = "No strong daily directional assumption. Trade only very clean intraday displacement."

    return {
        "bias": bias,
        "assumption": assumption,
        "reasons": reasons,
        "pd": pd,
        "prev_day": prev_day,
        "ema15": ema15,
        "sma9": sma9,
        "sma15": sma15,
    }


def tf_context(candles, label):
    if len(candles) < 80:
        return None

    closes = [c["close"] for c in candles]
    price = closes[-1]

    ema15_value = ema(closes, 15)
    sma9_value = sma(closes, 9)
    sma15_value = sma(closes, 15)
    rsi_value = rsi(closes, 14)
    atr_value = atr(candles, 14)
    vwap_value = vwap(candles)

    fvgs = detect_fvgs(candles, 80)
    ifvgs = detect_ifvgs(fvgs, price)
    pd = premium_discount(candles, price, 80)
    support, resistance = swing_levels(candles, price)

    if price > vwap_value and price > ema15_value and sma9_value > sma15_value:
        structure = "BULLISH"
    elif price < vwap_value and price < ema15_value and sma9_value < sma15_value:
        structure = "BEARISH"
    else:
        structure = "NEUTRAL"

    return {
        "label": label,
        "price": round(price, 5),
        "structure": structure,
        "ema15": ema15_value,
        "sma9": sma9_value,
        "sma15": sma15_value,
        "rsi": rsi_value,
        "atr": atr_value,
        "vwap": vwap_value,
        "fvgs": fvgs,
        "ifvgs": ifvgs,
        "pd": pd,
        "support": support,
        "resistance": resistance,
    }


# ======================================================
# SESSION / GAP LOGIC
# ======================================================

def current_session():
    h = datetime.now(NY_TZ).hour

    if 19 <= h or h < 1:
        return "ASIAN"
    if 2 <= h < 6:
        return "LONDON"
    if 7 <= h < 11:
        return "NEW_YORK"
    return "OFF-SESSION"


def execution_session_ok():
    return current_session() in ["LONDON", "NEW_YORK"]


def session_range(candles, name):
    windows = {
        "ASIAN": (19, 0),
        "LONDON": (2, 6),
        "NEW_YORK": (7, 11),
    }

    start_h, end_h = windows[name]
    selected = []

    for c in candles:
        t = to_ny(c)
        h = t.hour

        if name == "ASIAN":
            if h >= 19 or h < 1:
                selected.append(c)
        elif start_h <= h < end_h:
            selected.append(c)

    if not selected:
        return None

    return {
        "high": round(max(c["high"] for c in selected), 5),
        "low": round(min(c["low"] for c in selected), 5),
    }


def session_position(price, r):
    if not r:
        return "N/A"

    if price > r["high"]:
        return "ABOVE HIGH"
    if price < r["low"]:
        return "BELOW LOW"
    return "INSIDE RANGE"


def ndog(candles, price, pair):
    today = datetime.now(NY_TZ).date()
    today_c = []
    previous = []

    for c in candles:
        t = to_ny(c)

        if t.date() == today:
            today_c.append(c)
        elif t.date() < today:
            previous.append(c)

    if not today_c or not previous:
        return None

    open_price = today_c[0]["open"]
    prev_close = previous[-1]["close"]

    low = round(min(open_price, prev_close), 5)
    high = round(max(open_price, prev_close), 5)
    gap = round(open_price - prev_close, 5)
    radius = PIP_100_RANGE.get(pair, 0.0100)

    return {
        "direction": "UP" if gap > 0 else "DOWN",
        "low": low,
        "high": high,
        "gap": gap,
        "nearby": low - radius <= price <= high + radius,
    }


def nwog(candles, price, pair):
    this_week = datetime.now(NY_TZ).isocalendar().week
    current = []
    old = []

    for c in candles:
        t = to_ny(c)

        if t.isocalendar().week == this_week:
            current.append(c)
        else:
            old.append(c)

    if not current or not old:
        return None

    open_price = current[0]["open"]
    prev_close = old[-1]["close"]

    low = round(min(open_price, prev_close), 5)
    high = round(max(open_price, prev_close), 5)
    gap = round(open_price - prev_close, 5)
    radius = PIP_100_RANGE.get(pair, 0.0100)

    return {
        "direction": "UP" if gap > 0 else "DOWN",
        "low": low,
        "high": high,
        "gap": gap,
        "nearby": low - radius <= price <= high + radius,
    }


# ======================================================
# SIGNAL BUILDER
# ======================================================

def aligned_with_bias(side, daily, h4, h1, m15):
    needed = "BULLISH" if side == "BUY" else "BEARISH"

    score = 0
    reasons = []

    if daily["bias"] == needed:
        score += 25
        reasons.append(f"Daily bias is {needed}")

    if h4["structure"] == needed:
        score += 20
        reasons.append(f"4H PD array structure is {needed}")

    if h1["structure"] == needed:
        score += 20
        reasons.append(f"1H PD array structure is {needed}")

    if m15["structure"] == needed:
        score += 15
        reasons.append(f"15m execution context is {needed}")

    return score, reasons


def build_signal(pair):
    daily_c = get_candles(pair, "1day", 60)
    h4_c = get_candles(pair, "4h", 120)
    h1_c = get_candles(pair, "1h", 160)
    m15_c = get_candles(pair, "15min", 160)
    m5_c = get_candles(pair, "5min", 200)

    if min(len(daily_c), len(h4_c), len(h1_c), len(m15_c), len(m5_c)) < 50:
        print(f"{pair}: Not enough HTF data.")
        return None

    daily = daily_bias(daily_c)
    h4 = tf_context(h4_c, "4H")
    h1 = tf_context(h1_c, "1H")
    m15 = tf_context(m15_c, "15m")
    m5 = tf_context(m5_c, "5m")

    if not all([daily, h4, h1, m15, m5]):
        print(f"{pair}: HTF analysis failed.")
        return None

    price = m5["price"]
    current = current_session()

    print(
        f"{datetime.now()} | {pair} | Price={price} | "
        f"D={daily['bias']} 4H={h4['structure']} 1H={h1['structure']} "
        f"15m={m15['structure']} 5m={m5['structure']} Session={current}"
    )

    if not execution_session_ok():
        print(f"{pair}: Outside London/New York execution window.")
        return None

    asian = session_range(m5_c, "ASIAN")
    london = session_range(m5_c, "LONDON")
    newyork = session_range(m5_c, "NEW_YORK")

    nd = ndog(m5_c, price, pair)
    nw = nwog(m5_c, price, pair)

    spike, vol_ratio = volume_spike(m5_c)
    good_buy_candle, buy_candle_note = candle_quality(m5_c[-1], "BUY")
    good_sell_candle, sell_candle_note = candle_quality(m5_c[-1], "SELL")

    signals = []

    for side in ["BUY", "SELL"]:
        confidence, reasons = aligned_with_bias(side, daily, h4, h1, m15)
        warnings = []

        pd_type, pd_array = nearest_pd_array(
            m5["fvgs"],
            m5["ifvgs"],
            price,
            side,
            m5["atr"],
        )

        if not pd_array:
            continue

        confidence += 20
        reasons.append(f"5m {pd_type} entry zone active: {pd_array['low']} - {pd_array['high']}")

        if side == "BUY":
            if price > m5["vwap"]:
                confidence += 8
                reasons.append("Price above VWAP")
            if m5["sma9"] > m5["sma15"]:
                confidence += 8
                reasons.append("SMA9 above SMA15")
            if good_buy_candle:
                confidence += 8
                reasons.append(buy_candle_note)
            else:
                warnings.append(buy_candle_note)

            stop_base = pd_array["low"]
            risk = max(abs(price - stop_base), MIN_SL_DISTANCE.get(pair, 0.0010))
            stop_loss = round(price - risk, 5)
            take_profit = round(price + risk * RR_TARGET, 5)

        else:
            if price < m5["vwap"]:
                confidence += 8
                reasons.append("Price below VWAP")
            if m5["sma9"] < m5["sma15"]:
                confidence += 8
                reasons.append("SMA9 below SMA15")
            if good_sell_candle:
                confidence += 8
                reasons.append(sell_candle_note)
            else:
                warnings.append(sell_candle_note)

            stop_base = pd_array["high"]
            risk = max(abs(stop_base - price), MIN_SL_DISTANCE.get(pair, 0.0010))
            stop_loss = round(price + risk, 5)
            take_profit = round(price - risk * RR_TARGET, 5)

        if spike:
            confidence += 6
            reasons.append(f"Good volume/tick-volume candle: {vol_ratio}x average")
        else:
            warnings.append(f"No strong volume confirmation: {vol_ratio}x average")

        if nd and nd["nearby"]:
            confidence += 5
            reasons.append("NDOG within 100 pips")

        if nw and nw["nearby"]:
            confidence += 5
            reasons.append("NWOG within 100 pips")

        rr = round(abs(take_profit - price) / abs(price - stop_loss), 2)

        if confidence >= MIN_CONFIDENCE and rr >= MIN_RR:
            signals.append({
                "pair": pair,
                "side": side,
                "entry": price,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "rr": rr,
                "confidence": confidence,
                "daily": daily,
                "h4": h4,
                "h1": h1,
                "m15": m15,
                "m5": m5,
                "pd_type": pd_type,
                "pd_array": pd_array,
                "asian": asian,
                "london": london,
                "newyork": newyork,
                "ndog": nd,
                "nwog": nw,
                "volume_ratio": vol_ratio,
                "session": current,
                "reasons": reasons,
                "warnings": warnings,
            })

    if not signals:
        return None

    return max(signals, key=lambda x: x["confidence"])


# ======================================================
# FORMAT ALERT
# ======================================================

def fmt_range(r):
    if not r:
        return "N/A"
    return f"H {r['high']} / L {r['low']}"


def fmt_gap(g):
    if not g:
        return "None"
    status = "within 100 pips" if g["nearby"] else "far"
    return f"{g['direction']} {g['low']} - {g['high']} ({status})"


def send_signal(s):
    emoji = "🟢" if s["side"] == "BUY" else "🔴"

    reasons = "\n".join([f"✅ {r}" for r in s["reasons"]])
    warnings = "\n".join([f"⚠️ {w}" for w in s["warnings"]]) or "None"

    message = f"""
{emoji} <b>{s['pair']} {s['side']} HTF NARRATIVE ENTRY</b>

<b>Entry:</b> {s['entry']}
<b>Stop Loss:</b> {s['stop_loss']}
<b>Take Profit:</b> {s['take_profit']}
<b>Risk:Reward:</b> {s['rr']}x
<b>Confidence:</b> {s['confidence']}%

<b>Time:</b>
Lithuania: {lithuania_time()}
New York: {ny_time()}
Session: {s['session']}

<b>Daily Assumption:</b>
Bias: {s['daily']['bias']}
{s['daily']['assumption']}

<b>Top-Down Checklist:</b>
Daily Bias: {s['daily']['bias']}
4H PD Array: {s['h4']['structure']} | PD: {s['h4']['pd']['zone']}
1H PD Array: {s['h1']['structure']} | PD: {s['h1']['pd']['zone']}
15m PD Array: {s['m15']['structure']} | PD: {s['m15']['pd']['zone']}
5m Entry Model: {s['pd_type']} {s['pd_array']['low']} - {s['pd_array']['high']}

<b>5m Entry Indicators:</b>
EMA15: {s['m5']['ema15']}
SMA9: {s['m5']['sma9']}
SMA15: {s['m5']['sma15']}
VWAP: {s['m5']['vwap']}
RSI: {s['m5']['rsi']}
ATR: {s['m5']['atr']}
Volume Ratio: {s['volume_ratio']}x

<b>Session Highs/Lows:</b>
Asian: {fmt_range(s['asian'])} | {session_position(s['entry'], s['asian'])}
London: {fmt_range(s['london'])} | {session_position(s['entry'], s['london'])}
New York: {fmt_range(s['newyork'])} | {session_position(s['entry'], s['newyork'])}

<b>Gap Logic:</b>
NDOG: {fmt_gap(s['ndog'])}
NWOG: {fmt_gap(s['nwog'])}

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
        "🤖 <b>Forex Bot V2.0 HTF Narrative ONLINE</b>\n\n"
        "Checklist:\n"
        "Daily Bias → 4H PD Array → 1H PD Array → 15m PD Array → 5m Entry\n\n"
        "Pairs: GBP/USD\n"
        "Mode: Paper alerts only\n"
        "Scan: Every 5 minutes"
    )

    last_signal_time = {}
    last_signal_side = {}

    while True:
        for pair in PAIRS:
            try:
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

            except Exception as e:
                print(f"{pair}: Error: {e}")

        time.sleep(SCAN_SECONDS)


if __name__ == "__main__":
    main()

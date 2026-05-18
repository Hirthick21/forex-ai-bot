import requests
import time
import json
from datetime import datetime, timezone
from openai import OpenAI

# ======================================================
# CONFIG — FILL THESE IN BEFORE RUNNING
# ======================================================

TELEGRAM_TOKEN    = "8731089787:AAEnfvCK4x75qNK8rt_bolM05Luc9lE4pyU"
TELEGRAM_CHAT_ID  = "7489428510"
TWELVE_API_KEY    = "aa504ba1e1544594bb3bf04f6f241380"
OPENAI_API_KEY    = "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"

PAIRS = ["EUR/USD", "GBP/USD", "USD/JPY", "XAU/USD"]

COMMAND_CHECK_SECONDS   = 3
MARKET_SCAN_SECONDS     = 60
SIGNAL_COOLDOWN_SECONDS = 900    # 15 min per pair

MIN_CONFIDENCE = 72              # AI must be this confident to alert
MIN_RR         = 1.8             # minimum risk:reward

MIN_SL = {
    "EUR/USD": 0.0010,
    "GBP/USD": 0.0012,
    "USD/JPY": 0.10,
    "XAU/USD": 1.00,
}
RR_TARGET = 2.2

# ======================================================
# GLOBAL STATE
# ======================================================

BOT_ACTIVE     = True
LAST_UPDATE_ID = 0
LAST_SIGNALS   = {}   # pair → {time, side, confidence}
openai_client  = OpenAI(api_key=OPENAI_API_KEY)

# ======================================================
# TELEGRAM
# ======================================================

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id":    TELEGRAM_CHAT_ID,
            "text":       message,
            "parse_mode": "HTML"
        }, timeout=10)
        if not r.ok:
            print("Telegram Error:", r.text)
    except Exception as e:
        print("Telegram Exception:", e)


def get_updates():
    global LAST_UPDATE_ID
    try:
        data = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
            params={"offset": LAST_UPDATE_ID + 1, "timeout": 2},
            timeout=8
        ).json()
        return data.get("result", [])
    except:
        return []


def handle_commands():
    global BOT_ACTIVE, LAST_UPDATE_ID
    for update in get_updates():
        LAST_UPDATE_ID = update["update_id"]
        text = update.get("message", {}).get("text", "").strip().lower()

        if text == "/start":
            BOT_ACTIVE = True
            send_telegram(
                "✅ <b>Forex AI Bot Started</b>\n\n"
                "Scanning EUR/USD · GBP/USD · USD/JPY · XAU/USD\n"
                "AI Engine: GPT-4o powered analysis\n"
                "Indicators: EMA15 · SMA21 · VWAP · RSI · ATR · MACD · FVG · NDOG · NWOG\n"
                "Every 60 seconds — London/NY session only"
            )
        elif text == "/stop":
            BOT_ACTIVE = False
            send_telegram("🔴 <b>Bot Stopped</b>\nSend /start to resume.")
        elif text == "/status":
            status = "ACTIVE ✅" if BOT_ACTIVE else "STOPPED 🔴"
            last_sigs = "\n".join(
                f"  {p}: {v['side']} ({v['confidence']}%) at {v['time']}"
                for p, v in LAST_SIGNALS.items()
            ) or "  None yet"
            send_telegram(
                f"📊 <b>Bot Status</b>\n\n"
                f"Status: {status}\n"
                f"AI Engine: GPT-4o\n"
                f"Min Confidence: {MIN_CONFIDENCE}%\n"
                f"Min R:R: {MIN_RR}x\n\n"
                f"<b>Last Signals:</b>\n{last_sigs}"
            )
        elif text == "/price":
            lines = []
            for p in PAIRS:
                price = get_price(p)
                lines.append(f"  <b>{p}:</b>  {price if price else 'N/A'}")
            send_telegram("💱 <b>Live Prices</b>\n\n" + "\n".join(lines))
        elif text == "/test":
            send_telegram(
                "🧪 <b>TEST SIGNAL — EUR/USD</b>\n\n"
                "Signal: BUY\nEntry: 1.08420\nSL: 1.08310\nTP: 1.08662\n"
                "R:R: 2.2x | Confidence: 81%\n\n"
                "FVG: 1.08390 – 1.08430 (Bullish)\n"
                "NDOG Gap: 1.08350 – 1.08410 (UP)\n"
                "VWAP: 1.08405 | EMA15: 1.08415\n\n"
                "<i>This is a test alert only.</i>"
            )
        elif text == "/help":
            send_telegram(
                "📖 <b>Commands</b>\n"
                "══════════════════\n"
                "/start  — Start scanning\n"
                "/stop   — Stop scanning\n"
                "/status — Status + last signals\n"
                "/price  — Live prices\n"
                "/test   — Test alert\n"
                "/help   — This menu\n\n"
                "<b>AI Indicators:</b>\n"
                "• EMA 15 · SMA 21 (trend stack)\n"
                "• VWAP (institutional level)\n"
                "• RSI 14 · MACD (momentum)\n"
                "• ATR 14 (volatility/SL sizing)\n"
                "• FVG — Fair Value Gaps\n"
                "• NDOG — New Day Opening Gap\n"
                "• NWOG — New Week Opening Gap\n"
                "• Support / Resistance levels\n"
                "• Session filter: London + NY"
            )

# ======================================================
# TWELVE DATA
# ======================================================

def get_price(pair):
    try:
        data = requests.get(
            "https://api.twelvedata.com/price",
            params={"symbol": pair, "apikey": TWELVE_API_KEY},
            timeout=10
        ).json()
        return round(float(data["price"]), 5) if "price" in data else None
    except:
        return None


def get_candles(pair, interval, outputsize=100):
    try:
        data = requests.get(
            "https://api.twelvedata.com/time_series",
            params={"symbol": pair, "interval": interval,
                    "outputsize": outputsize, "apikey": TWELVE_API_KEY},
            timeout=10
        ).json()
        if "values" not in data:
            return []
        return [{
            "time":   c["datetime"],
            "open":   float(c["open"]),
            "high":   float(c["high"]),
            "low":    float(c["low"]),
            "close":  float(c["close"]),
            "volume": float(c.get("volume", 1)),
        } for c in reversed(data["values"])]
    except:
        return []

# ======================================================
# INDICATORS
# ======================================================

def calc_ema(closes, period):
    if len(closes) < period:
        return None
    k, val = 2 / (period + 1), sum(closes[:period]) / period
    for p in closes[period:]:
        val = p * k + val * (1 - k)
    return round(val, 5)


def calc_sma(closes, period):
    if len(closes) < period:
        return None
    return round(sum(closes[-period:]) / period, 5)


def calc_rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    gains = [max(closes[i] - closes[i-1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i-1] - closes[i], 0) for i in range(1, len(closes))]
    ag = sum(gains[-period:]) / period
    al = sum(losses[-period:]) / period
    return 100.0 if al == 0 else round(100 - 100 / (1 + ag / al), 2)


def calc_macd(closes, fast=12, slow=26, signal=9):
    if len(closes) < slow + signal:
        return None, None, None
    ema_fast = calc_ema(closes, fast)
    ema_slow = calc_ema(closes, slow)
    if not ema_fast or not ema_slow:
        return None, None, None
    macd_line = round(ema_fast - ema_slow, 5)
    # Calculate signal line from recent MACD values
    macd_series = []
    for i in range(slow, len(closes)):
        ef = calc_ema(closes[:i+1], fast)
        es = calc_ema(closes[:i+1], slow)
        if ef and es:
            macd_series.append(ef - es)
    if len(macd_series) < signal:
        return macd_line, None, None
    signal_line = round(sum(macd_series[-signal:]) / signal, 5)
    histogram   = round(macd_line - signal_line, 5)
    return macd_line, signal_line, histogram


def calc_atr(candles, period=14):
    if len(candles) < period + 1:
        return None
    trs = [max(
        candles[i]["high"] - candles[i]["low"],
        abs(candles[i]["high"] - candles[i-1]["close"]),
        abs(candles[i]["low"]  - candles[i-1]["close"])
    ) for i in range(1, len(candles))]
    return round(sum(trs[-period:]) / period, 5)


def calc_vwap(candles):
    today = datetime.now(timezone.utc).date()
    session = [c for c in candles if c["time"][:10] == str(today)] or candles[-30:]
    if not session:
        return None
    cum_tp_vol = sum(((c["high"]+c["low"]+c["close"])/3) * max(c["volume"], 1) for c in session)
    cum_vol    = sum(max(c["volume"], 1) for c in session)
    return round(cum_tp_vol / cum_vol, 5)


def calc_ndog(candles):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_c = [c for c in candles if c["time"].startswith(today)]
    prev_c  = [c for c in candles if not c["time"].startswith(today)]
    if not today_c or not prev_c:
        return None
    gap = round(today_c[0]["open"] - prev_c[-1]["close"], 5)
    if abs(gap) < 0.00003:
        return None
    return {
        "day_open":   round(today_c[0]["open"], 5),
        "prev_close": round(prev_c[-1]["close"], 5),
        "gap_size":   gap,
        "direction":  "UP" if gap > 0 else "DOWN",
        "zone_low":   round(min(today_c[0]["open"], prev_c[-1]["close"]), 5),
        "zone_high":  round(max(today_c[0]["open"], prev_c[-1]["close"]), 5),
    }


def calc_nwog(candles):
    now = datetime.now(timezone.utc)
    this_week = [c for c in candles
                 if datetime.fromisoformat(c["time"]).isocalendar()[1] == now.isocalendar()[1]]
    last_week = [c for c in candles
                 if datetime.fromisoformat(c["time"]).isocalendar()[1] != now.isocalendar()[1]]
    if not this_week or not last_week:
        return None
    gap = round(this_week[0]["open"] - last_week[-1]["close"], 5)
    if abs(gap) < 0.00003:
        return None
    return {
        "week_open":  round(this_week[0]["open"], 5),
        "prev_close": round(last_week[-1]["close"], 5),
        "gap_size":   gap,
        "direction":  "UP" if gap > 0 else "DOWN",
        "zone_low":   round(min(this_week[0]["open"], last_week[-1]["close"]), 5),
        "zone_high":  round(max(this_week[0]["open"], last_week[-1]["close"]), 5),
    }


def calc_fvg(candles, lookback=40):
    fvgs = []
    recent = candles[-lookback:]
    for i in range(2, len(recent)):
        c0, c1, c2 = recent[i-2], recent[i-1], recent[i]
        if c2["low"] > c0["high"]:
            fvgs.append({"type": "BULLISH", "low": round(c0["high"], 5),
                         "high": round(c2["low"], 5), "time": c1["time"]})
        elif c2["high"] < c0["low"]:
            fvgs.append({"type": "BEARISH", "low": round(c2["high"], 5),
                         "high": round(c0["low"], 5), "time": c1["time"]})
    return fvgs[-5:]


def support_resistance(candles, lookback=50):
    recent = candles[-lookback:]
    return (
        round(min(c["low"]  for c in recent), 5),
        round(max(c["high"] for c in recent), 5)
    )


def forex_session_ok():
    h = datetime.now(timezone.utc).hour
    return (7 <= h <= 16) or (12 <= h <= 21)


def analyze_tf(candles):
    if len(candles) < 60:
        return None
    closes = [c["close"] for c in candles]
    price  = closes[-1]
    ema15  = calc_ema(closes, 15)
    sma21  = calc_sma(closes, 21)
    rsi14  = calc_rsi(closes, 14)
    atr14  = calc_atr(candles, 14)
    vwap   = calc_vwap(candles)
    macd, macd_sig, macd_hist = calc_macd(closes)

    if None in [ema15, sma21, rsi14, atr14]:
        return None

    trend = ("BULLISH" if price > ema15 > sma21
             else "BEARISH" if price < ema15 < sma21
             else "NEUTRAL")

    return {
        "price": round(price, 5), "ema15": ema15, "sma21": sma21,
        "rsi": rsi14, "atr": atr14, "vwap": vwap,
        "macd": macd, "macd_signal": macd_sig, "macd_hist": macd_hist,
        "vwap_bias": ("ABOVE" if vwap and price > vwap else "BELOW" if vwap else None),
        "trend": trend,
    }

# ======================================================
# AI BRAIN — GPT-4o DEEP ANALYSIS
# ======================================================

AI_SYSTEM_PROMPT = """You are a world-class professional forex and gold trader specialising in 
Smart Money Concepts (SMC) and ICT methodology. You have 20 years of experience reading 
institutional order flow, fair value gaps, liquidity zones, and multi-timeframe confluence.

You will receive a full market snapshot for a forex/gold pair including:
- Multi-timeframe data (1m, 5m, 15m)
- All key indicators: EMA15, SMA21, VWAP, RSI, MACD, ATR
- Fair Value Gaps (FVG), New Day Opening Gap (NDOG), New Week Opening Gap (NWOG)
- Support and resistance levels
- Current session context

Your job is to perform the deepest possible analysis and return ONLY a valid JSON object.
No markdown. No preamble. No explanation outside the JSON.

Return exactly this schema:
{
  "signal": "BUY" | "SELL" | "WAIT",
  "confidence": <integer 0-100>,
  "entry": <float>,
  "stop_loss": <float>,
  "take_profit": <float>,
  "rr_ratio": <float>,
  "trend_bias": "BULLISH" | "BEARISH" | "RANGING",
  "entry_zone": { "low": <float>, "high": <float> },
  "key_support": <float>,
  "key_resistance": <float>,
  "fvg_in_play": true | false,
  "gap_in_play": "NDOG" | "NWOG" | "BOTH" | "NONE",
  "vwap_confluence": true | false,
  "momentum": "STRONG" | "MODERATE" | "WEAK",
  "session_quality": "HIGH" | "MEDIUM" | "LOW",
  "risk_level": "LOW" | "MEDIUM" | "HIGH",
  "summary": "<3-4 sentence elite trader analysis covering: dominant bias, key level in play, entry reason, and risk>",
  "reasons": ["<reason 1>", "<reason 2>", "..."],
  "warnings": ["<warning if any>"]
}

Rules you MUST follow:
- confidence < 65 → signal MUST be "WAIT"
- stop_loss must use ATR × 1.5 minimum from entry
- take_profit must give at least 1.8 R:R
- entry must be within current ATR of the close price
- Be brutally honest. Do not force signals. WAIT is a valid and often correct answer.
- Prioritise VWAP + FVG + gap confluence for highest confidence signals
- If NDOG or NWOG zone aligns with FVG and VWAP, confidence can be 85-95
- If only trend aligns with no confluence, keep confidence 55-65 (WAIT)
"""


def ai_analyse(pair, tf1, tf5, tf15, fvgs, ndog, nwog, support, resistance):
    """Send full market context to GPT-4o and get back a structured signal."""

    fvg_text = "\n".join(
        f"  {f['type']} FVG: {f['low']} – {f['high']} (formed {f['time']})"
        for f in fvgs
    ) or "  None detected"

    ndog_text = (
        f"  Day Open: {ndog['day_open']} | Prev Close: {ndog['prev_close']}\n"
        f"  Gap: {ndog['direction']} {abs(ndog['gap_size']):.5f}\n"
        f"  Zone: {ndog['zone_low']} – {ndog['zone_high']}"
    ) if ndog else "  No meaningful NDOG today"

    nwog_text = (
        f"  Week Open: {nwog['week_open']} | Prev Week Close: {nwog['prev_close']}\n"
        f"  Gap: {nwog['direction']} {abs(nwog['gap_size']):.5f}\n"
        f"  Zone: {nwog['zone_low']} – {nwog['zone_high']}"
    ) if nwog else "  No meaningful NWOG this week"

    def tf_block(label, tf):
        if not tf:
            return f"{label}: insufficient data"
        return (
            f"{label} | Trend: {tf['trend']} | Price: {tf['price']}\n"
            f"  EMA15: {tf['ema15']} | SMA21: {tf['sma21']}\n"
            f"  RSI: {tf['rsi']} | ATR: {tf['atr']}\n"
            f"  VWAP: {tf['vwap']} ({tf['vwap_bias']})\n"
            f"  MACD: {tf['macd']} | Signal: {tf['macd_signal']} | Hist: {tf['macd_hist']}"
        )

    user_msg = f"""
=== MARKET SNAPSHOT: {pair} ===
Session UTC Hour: {datetime.now(timezone.utc).hour}
Day of Week: {datetime.now(timezone.utc).strftime('%A')}

--- TIMEFRAME ANALYSIS ---
{tf_block('15m (Bias/Structure)', tf15)}

{tf_block('5m  (Entry Direction)', tf5)}

{tf_block('1m  (Entry Trigger)', tf1)}

--- KEY LEVELS ---
Support:    {support}
Resistance: {resistance}

--- FAIR VALUE GAPS (FVG) ---
{fvg_text}

--- NEW DAY OPENING GAP (NDOG) ---
{ndog_text}

--- NEW WEEK OPENING GAP (NWOG) ---
{nwog_text}

=== TASK ===
Perform deep SMC/ICT analysis on {pair}.
Consider all confluences: FVG alignment, gap fill probability,
VWAP magnetism, multi-timeframe trend stack, RSI/MACD momentum,
support/resistance respect, and session timing.
Return ONLY the JSON signal object. No markdown or extra text.
"""

    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": AI_SYSTEM_PROMPT},
                {"role": "user",   "content": user_msg},
            ],
            temperature=0.15,
            max_tokens=800,
        )
        raw = response.choices[0].message.content.strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        result = json.loads(raw)
        result["pair"] = pair
        return result
    except json.JSONDecodeError as e:
        print(f"[AI] JSON parse error for {pair}: {e}")
        return None
    except Exception as e:
        print(f"[AI] OpenAI error for {pair}: {e}")
        return None

# ======================================================
# SIGNAL BUILDER
# ======================================================

def build_signal(pair):
    c1m  = get_candles(pair, "1min",  120)
    c5m  = get_candles(pair, "5min",  100)
    c15m = get_candles(pair, "15min", 100)

    if len(c1m) < 70 or len(c5m) < 60 or len(c15m) < 60:
        print(f"[{pair}] Not enough data.")
        return None

    tf1  = analyze_tf(c1m)
    tf5  = analyze_tf(c5m)
    tf15 = analyze_tf(c15m)

    if not tf1 or not tf5 or not tf15:
        print(f"[{pair}] Analysis failed.")
        return None

    if not forex_session_ok():
        print(f"[{pair}] Outside London/NY session.")
        return None

    fvgs               = calc_fvg(c5m, 40)
    ndog               = calc_ndog(c1m)
    nwog               = calc_nwog(c1m)
    support, resistance = support_resistance(c5m, 50)

    print(f"[{pair}] Sending to GPT-4o... Price={tf1['price']} | "
          f"1m={tf1['trend']} 5m={tf5['trend']} 15m={tf15['trend']} | "
          f"RSI={tf1['rsi']} VWAP={tf1['vwap']} FVGs={len(fvgs)}")

    ai = ai_analyse(pair, tf1, tf5, tf15, fvgs, ndog, nwog, support, resistance)

    if not ai:
        print(f"[{pair}] AI returned nothing.")
        return None

    print(f"[{pair}] AI → {ai.get('signal')} ({ai.get('confidence')}%) | {ai.get('trend_bias')}")

    if ai.get("signal") == "WAIT":
        print(f"[{pair}] AI says WAIT.")
        return None

    if ai.get("confidence", 0) < MIN_CONFIDENCE:
        print(f"[{pair}] Confidence {ai['confidence']}% below threshold {MIN_CONFIDENCE}%.")
        return None

    if ai.get("rr_ratio", 0) < MIN_RR:
        print(f"[{pair}] R:R {ai['rr_ratio']} below minimum {MIN_RR}.")
        return None

    return {**ai, "tf1": tf1, "tf5": tf5, "tf15": tf15,
            "fvgs": fvgs, "ndog": ndog, "nwog": nwog,
            "support": support, "resistance": resistance}


# ======================================================
# TELEGRAM SIGNAL FORMATTER
# ======================================================

def format_signal(s):
    emoji   = "🟢" if s["signal"] == "BUY" else "🔴"
    tf1     = s["tf1"]
    reasons = "\n".join(f"  • {r}" for r in s.get("reasons", []))
    warnings = "\n".join(f"  ⚠️ {w}" for w in s.get("warnings", [])) or "  None"

    # FVG in play
    fvg_line = ""
    for f in reversed(s.get("fvgs", [])):
        price = s["entry"]
        if f["low"] - 0.001 <= price <= f["high"] + 0.001:
            fvg_line = f"\n🕳 <b>FVG Active:</b> {f['low']} – {f['high']} ({f['type']})"
            break

    # Gap lines
    ndog = s.get("ndog")
    nwog = s.get("nwog")
    ndog_line = f"\n📅 <b>NDOG Zone:</b> {ndog['zone_low']} – {ndog['zone_high']} ({ndog['direction']} gap)" if ndog else ""
    nwog_line = f"\n📆 <b>NWOG Zone:</b> {nwog['zone_low']} – {nwog['zone_high']} ({nwog['direction']} gap)" if nwog else ""

    # Risk tag
    risk_tag = {"LOW": "🟢 Low", "MEDIUM": "🟡 Medium", "HIGH": "🔴 High"}.get(s.get("risk_level"), "—")
    momentum_tag = {"STRONG": "💪 Strong", "MODERATE": "👌 Moderate", "WEAK": "⚡ Weak"}.get(s.get("momentum"), "—")
    session_tag = {"HIGH": "🔥 Prime", "MEDIUM": "✅ Good", "LOW": "⚠️ Low"}.get(s.get("session_quality"), "—")

    ez = s.get("entry_zone", {})
    ez_str = f"{ez.get('low', '—')} – {ez.get('high', '—')}"

    return (
        f"{emoji}{emoji} <b>{s['pair']} {s['signal']} SIGNAL</b> {emoji}{emoji}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"\n"
        f"<b>Entry Zone:</b>   {ez_str}\n"
        f"<b>Stop Loss:</b>    {s['stop_loss']}\n"
        f"<b>Take Profit:</b>  {s['take_profit']}\n"
        f"<b>Risk:Reward:</b>  {s['rr_ratio']}x\n"
        f"<b>Confidence:</b>   {s['confidence']}%\n"
        f"\n"
        f"<b>📊 Indicators (1m)</b>\n"
        f"EMA 15:  {tf1['ema15']}   SMA 21: {tf1['sma21']}\n"
        f"VWAP:    {tf1['vwap'] or 'N/A'}   RSI: {tf1['rsi']}\n"
        f"MACD:    {tf1['macd']}   Hist: {tf1['macd_hist']}\n"
        f"ATR:     {tf1['atr']}\n"
        f"\n"
        f"<b>📈 Multi-Timeframe Trend</b>\n"
        f"1m: {s['tf1']['trend']}  |  5m: {s['tf5']['trend']}  |  15m: {s['tf15']['trend']}\n"
        f"\n"
        f"<b>🏗 Key Levels</b>\n"
        f"Support:    {s['support']}\n"
        f"Resistance: {s['resistance']}"
        f"{fvg_line}"
        f"{ndog_line}"
        f"{nwog_line}\n"
        f"\n"
        f"<b>⚡ Signal Quality</b>\n"
        f"Momentum: {momentum_tag}   Risk: {risk_tag}   Session: {session_tag}\n"
        f"\n"
        f"<b>🧠 AI Analysis</b>\n"
        f"{s.get('summary', '')}\n"
        f"\n"
        f"<b>✅ Confluence Reasons</b>\n"
        f"{reasons}\n"
        f"\n"
        f"<b>⚠️ Warnings</b>\n"
        f"{warnings}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🤖 <i>Forex AI Bot — GPT-4o + SMC/ICT</i>\n"
        f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )


# ======================================================
# MAIN LOOP
# ======================================================

def main():
    send_telegram(
        "🚀 <b>Forex AI Bot — GPT-4o + SMC/ICT — ONLINE</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "<b>Pairs:</b> EUR/USD · GBP/USD · USD/JPY · XAU/USD\n\n"
        "<b>AI Engine:</b> GPT-4o (world-class trader brain)\n\n"
        "<b>Indicators:</b>\n"
        "  • EMA 15 + SMA 21 (trend stack)\n"
        "  • VWAP (institutional magnet)\n"
        "  • RSI 14 + MACD (momentum)\n"
        "  • ATR 14 (volatility sizing)\n"
        "  • FVG — Fair Value Gaps\n"
        "  • NDOG — New Day Opening Gap\n"
        "  • NWOG — New Week Opening Gap\n"
        "  • Support / Resistance (50-bar)\n\n"
        "<b>Session Filter:</b> London + New York only\n"
        "<b>Min Confidence:</b> 72%  |  <b>Min R:R:</b> 1.8x\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Send /help for commands 📖"
    )

    last_scan = 0

    while True:
        handle_commands()
        now = time.time()

        if BOT_ACTIVE and now - last_scan >= MARKET_SCAN_SECONDS:
            print(f"\n{'='*60}")
            print(f"  SCANNING ALL PAIRS — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            print(f"{'='*60}")

            for pair in PAIRS:
                try:
                    signal = build_signal(pair)

                    if signal:
                        last = LAST_SIGNALS.get(pair, {})
                        cooldown_ok   = now - last.get("time", 0) >= SIGNAL_COOLDOWN_SECONDS
                        side_changed  = signal["signal"] != last.get("side")

                        if cooldown_ok or side_changed:
                            send_telegram(format_signal(signal))
                            LAST_SIGNALS[pair] = {
                                "time": now,
                                "side": signal["signal"],
                                "confidence": signal["confidence"],
                                "time_str": datetime.now().strftime("%H:%M:%S")
                            }
                            print(f"  ✅ Alert sent — {pair} {signal['signal']} ({signal['confidence']}%)")
                        else:
                            print(f"  ⏳ {pair}: Same signal, in cooldown.")
                    else:
                        print(f"  — {pair}: No signal.")

                except Exception as e:
                    print(f"  ✗ Error on {pair}: {e}")

                time.sleep(3)   # Avoid API rate limits between pairs

            last_scan = now

        time.sleep(COMMAND_CHECK_SECONDS)


if __name__ == "__main__":
    main()

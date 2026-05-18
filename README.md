# Forex AI Bot

Telegram forex alert bot using Twelve Data API.

## Strategy
- Pair: EUR/USD
- Timeframes: 5m and 15m
- EMA15
- SMA21
- VWAP
- RSI
- ATR
- FVG
- NDOG
- NWOG
- Support / resistance

## Setup

Create `.env` file:

```env
TELEGRAM_TOKEN=your_token
TELEGRAM_CHAT_ID=your_chat_id
TWELVE_API_KEY=your_key
PAIR=EUR/USD

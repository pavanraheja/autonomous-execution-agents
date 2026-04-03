# ─────────────────────────────────────────
# CONFIG TEMPLATE — Copy to config.py and fill in your keys
# Never commit config.py — it contains live credentials
# ─────────────────────────────────────────

# Binance API Keys (USDⓈ-M Futures)
BINANCE_API_KEY    = "your_read_only_api_key"
BINANCE_API_SECRET = "your_read_only_api_secret"

LIVE_BINANCE_API_KEY    = "your_live_trading_api_key"
LIVE_BINANCE_API_SECRET = "your_live_trading_api_secret"

# Alchemy (Ethereum RPC — Whale Tracker)
ALCHEMY_API_KEY = "your_alchemy_api_key"

# Arkham Intelligence API (Whale Tracker)
ARKHAM_API_KEY = "your_arkham_api_key"

# Telegram Bot (optional — for trade alerts)
TELEGRAM_BOT_TOKEN = "your_telegram_bot_token"
TELEGRAM_CHAT_ID   = "your_telegram_chat_id"

# Scanner Settings
MFI_LENGTH      = 14    # Standard MFI period
MFI_OVERBOUGHT  = 80    # Overbought threshold
TIMEFRAME       = "6h"  # 6 hour candles
CANDLE_LIMIT    = 50    # Candles to fetch per coin

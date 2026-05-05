#!/usr/bin/env python3
"""
📡 AI Trading Signal Bot — Telegram
─────────────────────────────────────────────────────────────
Send any of these messages to get signals:
  "any signals" / "signal" / "scan" / /signal

The bot will:
1. Scan 20+ markets with full technical analysis
2. Pick the single best setup across all timeframes
3. Send you a clean signal card with entry, TP, SL, reasoning

Works with any exchange — Kraken, Binance, Bybit, etc.
You place the trade manually.
─────────────────────────────────────────────────────────────
"""

import os, json, math, logging, requests, asyncio
from datetime import datetime, timezone
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import anthropic

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("SignalBot")

# ─── CONFIG ───────────────────────────────────────────────────────────────────
def load_config():
    # Railway: reads from environment variables
    # Local: reads from config_signal.json
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    api_key = os.environ.get("ANTHROPIC_API_KEY")

    if token and api_key:
        return {"telegram_bot_token": token, "anthropic_api_key": api_key}

    # Fallback to local config file
    path = os.path.join(os.path.dirname(__file__), "config_signal.json")
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)

    print("ERROR: Set TELEGRAM_BOT_TOKEN and ANTHROPIC_API_KEY environment variables")
    exit(1)

HL_URL = "https://api.hyperliquid.xyz/info"

# ─── MARKETS ──────────────────────────────────────────────────────────────────
TOP_MARKETS = [
    "BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA",
    "AVAX", "LINK", "DOT", "UNI", "ATOM", "LTC",
    "NEAR", "APT", "ARB", "OP", "INJ", "SUI", "MATIC"
]

def get_market_data() -> list:
    try:
        r = requests.post(HL_URL, json={"type": "metaAndAssetCtxs"}, timeout=10)
        data = r.json()
        universe = data[0].get("universe", [])
        ctxs = data[1]
        markets = []
        for i, asset in enumerate(universe):
            name = asset.get("name", "")
            if name not in TOP_MARKETS or i >= len(ctxs):
                continue
            ctx = ctxs[i]
            mid = float(ctx.get("midPx") or ctx.get("markPx") or 0)
            if mid <= 0:
                continue
            markets.append({
                "name": name,
                "price": mid,
                "funding": float(ctx.get("funding") or 0),
                "volume_24h": float(ctx.get("dayNtlVlm") or 0),
                "open_interest": float(ctx.get("openInterest") or 0),
            })
        return sorted(markets, key=lambda m: m["volume_24h"], reverse=True)
    except Exception as e:
        log.error(f"Market data error: {e}")
        return []

# ─── TECHNICALS ───────────────────────────────────────────────────────────────
def fetch_candles(coin, interval, n=60):
    ms = {"1m":60000,"5m":300000,"15m":900000,"1h":3600000,"4h":14400000}
    period = ms.get(interval, 3600000)
    now = int(datetime.now(timezone.utc).timestamp() * 1000)
    start = now - (n + 5) * period
    try:
        r = requests.post(HL_URL, json={"type":"candleSnapshot","req":{"coin":coin,"interval":interval,"startTime":start,"endTime":now}}, timeout=10)
        raw = r.json()
        return [{"t":int(c["t"]),"o":float(c["o"]),"h":float(c["h"]),"l":float(c["l"]),"c":float(c["c"]),"v":float(c["v"])} for c in raw][-n:]
    except:
        return []

def rsi(closes, p=14):
    if len(closes) < p+1: return None
    gains = [max(closes[i]-closes[i-1],0) for i in range(1,len(closes))]
    losses = [max(closes[i-1]-closes[i],0) for i in range(1,len(closes))]
    ag = sum(gains[-p:])/p; al = sum(losses[-p:])/p
    return round(100-(100/(1+ag/al)) if al else 100, 1)

def ema(vals, p):
    if len(vals) < p: return [None]*len(vals)
    k = 2/(p+1); res = [None]*(p-1); res.append(sum(vals[:p])/p)
    for v in vals[p:]: res.append(v*k+res[-1]*(1-k))
    return res

def macd_signal(closes):
    e12=ema(closes,12); e26=ema(closes,26)
    ml=[a-b if a and b else None for a,b in zip(e12,e26)]
    valid=[v for v in ml if v is not None]
    if len(valid)<9: return "unknown", None
    sl=ema(valid,9)
    h = valid[-1]-sl[-1]
    prev_h = valid[-2]-sl[-2] if len(valid)>=2 else 0
    cross = "🔼 BULLISH CROSS" if prev_h<0 and h>0 else "🔽 BEARISH CROSS" if prev_h>0 and h<0 else None
    return "bullish" if h>0 else "bearish", cross

def bollinger(closes, p=20):
    if len(closes)<p: return None, None, "inside"
    w=closes[-p:]; mid=sum(w)/p; std=math.sqrt(sum((x-mid)**2 for x in w)/p)
    upper=mid+2*std; lower=mid-2*std; price=closes[-1]
    pct_b=(price-lower)/(upper-lower) if upper>lower else 0.5
    pos="🔴 ABOVE UPPER" if price>upper else "🟢 BELOW LOWER" if price<lower else "inside"
    return round(upper,4), round(lower,4), pos

def vwap(candles):
    if not candles: return None
    pv=sum(((c["h"]+c["l"]+c["c"])/3)*c["v"] for c in candles)
    tv=sum(c["v"] for c in candles)
    return round(pv/tv,4) if tv else None

def atr(candles, p=14):
    if len(candles)<p+1: return None
    trs=[max(c["h"]-c["l"],abs(c["h"]-candles[i-1]["c"]),abs(c["l"]-candles[i-1]["c"])) for i,c in enumerate(candles[1:],1)]
    return round(sum(trs[-p:])/p, 4)

def build_ta(coin):
    c1h = fetch_candles(coin, "1h", 60)
    c15m = fetch_candles(coin, "15m", 60)
    c5m = fetch_candles(coin, "5m", 40)
    c4h = fetch_candles(coin, "4h", 40)

    cl1h=[c["c"] for c in c1h]; cl15m=[c["c"] for c in c15m]
    cl5m=[c["c"] for c in c5m]; cl4h=[c["c"] for c in c4h]

    r1h=rsi(cl1h); r15m=rsi(cl15m); r5m=rsi(cl5m,7); r4h=rsi(cl4h)
    m1h_trend, m1h_cross = macd_signal(cl1h)
    m15m_trend, m15m_cross = macd_signal(cl15m)
    bb_upper, bb_lower, bb_pos = bollinger(cl1h)
    vw = vwap(c1h[-24:] if len(c1h)>=24 else c1h)
    at = atr(c1h)
    price = cl1h[-1] if cl1h else 0

    # Count signals
    bull=0; bear=0; sigs=[]
    if r1h and r1h<40: bull+=2; sigs.append(f"RSI 1h oversold ({r1h})")
    elif r1h and r1h>65: bear+=2; sigs.append(f"RSI 1h overbought ({r1h})")
    if r15m and r15m<38: bull+=2; sigs.append(f"RSI 15m oversold ({r15m})")
    elif r15m and r15m>65: bear+=2; sigs.append(f"RSI 15m overbought ({r15m})")
    if r4h and r4h<40: bull+=2; sigs.append(f"RSI 4h oversold ({r4h})")
    elif r4h and r4h>70: bear+=3; sigs.append(f"RSI 4h overbought ({r4h})")
    if m1h_trend=="bullish": bull+=1
    elif m1h_trend=="bearish": bear+=1
    if m1h_cross: sigs.append(f"MACD 1h: {m1h_cross}")
    if m15m_cross: sigs.append(f"MACD 15m: {m15m_cross}")
    if bb_pos=="🟢 BELOW LOWER": bull+=2; sigs.append("BB: Price below lower band (oversold)")
    elif bb_pos=="🔴 ABOVE UPPER": bear+=2; sigs.append("BB: Price above upper band (overbought)")
    if vw and price:
        if price>vw: bull+=1; sigs.append(f"Above VWAP (${vw:,.2f})")
        else: bear+=1; sigs.append(f"Below VWAP (${vw:,.2f})")
    # Volume
    if len(c1h)>=6:
        rv=sum(c["v"] for c in c1h[-3:])/3; pv=sum(c["v"] for c in c1h[-6:-3])/3
        if rv>pv*1.15: bull+=1; sigs.append("Volume expanding")
        elif rv<pv*0.85: sigs.append("Volume shrinking ⚠️")

    return {
        "rsi_1h": r1h, "rsi_15m": r15m, "rsi_5m": r5m, "rsi_4h": r4h,
        "macd_1h": m1h_trend, "macd_cross_1h": m1h_cross,
        "macd_15m": m15m_trend, "macd_cross_15m": m15m_cross,
        "bb_pos": bb_pos, "bb_upper": bb_upper, "bb_lower": bb_lower,
        "vwap": vw, "atr": at, "price": price,
        "bull": bull, "bear": bear, "signals": sigs,
        "bias": "BULLISH" if bull>bear+2 else "BEARISH" if bear>bull+2 else "MIXED"
    }

# ─── AI SIGNAL ────────────────────────────────────────────────────────────────
SYSTEM = """You are an elite crypto trading signal analyst. You analyze multiple markets and find the SINGLE best trade setup.

Requirements:
- Only signal LONG or SHORT if confidence ≥80%
- Need at least 3 confirming signals across timeframes
- Consider funding rate: high positive = longs expensive (favor shorts), high negative = shorts expensive (favor longs)
- Use ATR for stop loss and take profit placement
- Risk:reward must be at least 1:1.5

Respond ONLY with valid JSON, no markdown:
{
  "signal": "LONG" | "SHORT" | "HOLD",
  "coin": "BTC",
  "confidence": 85,
  "timeframe": "4h",
  "entry": 94500.0,
  "take_profit_1": 96000.0,
  "take_profit_2": 97500.0,
  "stop_loss": 93000.0,
  "leverage_suggested": 3,
  "edge": "mean_reversion | momentum | breakout | funding_arb",
  "risk_reward": 2.1,
  "reasoning": "3 sentences max — be specific about which indicators confirm",
  "key_levels": [93000, 94500, 96000],
  "invalidation": "What would invalidate this trade",
  "time_in_trade": "4-8 hours"
}"""

def build_prompt(markets, technicals):
    lines = ["=== MARKET SCAN RESULTS ===\n"]
    for m in markets[:12]:
        ta = technicals.get(m["name"], {})
        fund_str = "⚠️HIGH+" if m["funding"]>0.001 else "⚠️HIGH-" if m["funding"]<-0.001 else "neutral"
        lines.append(
            f"{m['name']}: ${m['price']:,.4f} | "
            f"RSI 4h:{ta.get('rsi_4h','?')} 1h:{ta.get('rsi_1h','?')} 15m:{ta.get('rsi_15m','?')} 5m:{ta.get('rsi_5m','?')} | "
            f"MACD 1h:{ta.get('macd_1h','?')} | BB:{ta.get('bb_pos','?')} | "
            f"Funding:{m['funding']:.5f}({fund_str}) | "
            f"MTF:{ta.get('bias','?')} Bull:{ta.get('bull',0)} Bear:{ta.get('bear',0)}"
        )
        sigs = ta.get("signals", [])
        if sigs:
            lines.append(f"  ✓ {' | '.join(sigs)}")
    lines.append(f"\nTime UTC: {datetime.now(timezone.utc).strftime('%H:%M')}")
    lines.append("Find the SINGLE best trade or return HOLD if nothing qualifies.")
    return "\n".join(lines)

async def get_ai_signal(markets, technicals, api_key):
    client = anthropic.Anthropic(api_key=api_key)
    prompt = build_prompt(markets, technicals)
    try:
        resp = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=600,
            system=SYSTEM,
            messages=[{"role":"user","content":prompt}],
        )
        raw = resp.content[0].text.strip().replace("```json","").replace("```","").strip()
        return json.loads(raw)
    except Exception as e:
        log.error(f"AI error: {e}")
        return None

# ─── SIGNAL CARD FORMATTER ────────────────────────────────────────────────────
def format_signal_card(sig, ta) -> str:
    if sig["signal"] == "HOLD":
        return (
            "🔍 *SIGNAL SCAN COMPLETE*\n\n"
            "⏸ *NO TRADE — HOLD*\n\n"
            "Market conditions don't meet the 80%+ confidence threshold right now.\n"
            "No setup with sufficient edge found across 20 markets.\n\n"
            "_Try again in 1-2 hours when conditions change._"
        )

    direction = "🟢 LONG" if sig["signal"] == "LONG" else "🔴 SHORT"
    coin = sig["coin"]
    conf = sig["confidence"]
    conf_bar = "🟩" * (conf // 10) + "⬜" * (10 - conf // 10)

    stars = "⭐⭐⭐" if conf >= 85 else "⭐⭐" if conf >= 80 else "⭐"

    entry = sig["entry"]
    tp1 = sig["take_profit_1"]
    tp2 = sig.get("take_profit_2", tp1 * 1.01)
    sl = sig["stop_loss"]
    rr = sig.get("risk_reward", 0)
    lev = sig.get("leverage_suggested", 2)

    # Calculate % moves
    if sig["signal"] == "LONG":
        tp1_pct = (tp1 - entry) / entry * 100
        tp2_pct = (tp2 - entry) / entry * 100
        sl_pct = (entry - sl) / entry * 100
    else:
        tp1_pct = (entry - tp1) / entry * 100
        tp2_pct = (entry - tp2) / entry * 100
        sl_pct = (sl - entry) / entry * 100

    # Key signals
    sigs = ta.get(coin, {}).get("signals", [])
    sig_text = "\n".join(f"  ✅ {s}" for s in sigs[:5]) if sigs else "  Multiple timeframe confluence"

    card = f"""
📡 *AI TRADING SIGNAL* {stars}

{direction} *{coin}/USDT*
━━━━━━━━━━━━━━━━━━━━━
🎯 *Confidence:* {conf}% {conf_bar}
⚡ *Edge:* {sig.get('edge','').replace('_',' ').title()}
⏱ *Time in trade:* {sig.get('time_in_trade','4-8h')}

━━━━━━━━━━━━━━━━━━━━━
📊 *LEVELS*
Entry:      `${entry:,.4f}`
TP1:        `${tp1:,.4f}` (+{tp1_pct:.2f}%)
TP2:        `${tp2:,.4f}` (+{tp2_pct:.2f}%)
Stop Loss:  `${sl:,.4f}` (-{sl_pct:.2f}%)
R:R Ratio:  *{rr:.1f}x* 🏆

━━━━━━━━━━━━━━━━━━━━━
💡 *SUGGESTED SIZING*
• Safe: 2-3x leverage, risk 3-5% balance
• Moderate: {lev}x leverage, risk 5% balance
• Aggressive: {min(lev+1, 5)}x leverage, risk 8% balance

━━━━━━━━━━━━━━━━━━━━━
📈 *CONFIRMING SIGNALS*
{sig_text}

━━━━━━━━━━━━━━━━━━━━━
🧠 *AI REASONING*
_{sig.get('reasoning', 'Multiple timeframe confluence detected.')}_

⚠️ *INVALIDATION*
_{sig.get('invalidation', 'Price breaks stop loss level.')}_

━━━━━━━━━━━━━━━━━━━━━
_Place manually on Kraken/Binance/Bybit_
_Always use a stop loss. Never risk more than you can afford to lose._
"""
    return card.strip()

# ─── TELEGRAM HANDLERS ────────────────────────────────────────────────────────
TRIGGER_WORDS = [
    "signal", "signals", "any signals", "scan", "analyse",
    "analyze", "trade", "check", "find", "show me", "what's good",
    "anything", "go", "run", "now", "yes", "ok"
]

async def run_signal_scan(update: Update, cfg: dict):
    chat_id = update.effective_chat.id
    msg = await update.message.reply_text(
        "🔍 *Scanning 20 markets...*\nAnalyzing RSI · MACD · BB · VWAP · ATR across 4 timeframes...",
        parse_mode="Markdown"
    )

    markets = get_market_data()
    if not markets:
        await msg.edit_text("❌ Failed to fetch market data. Try again.")
        return

    await msg.edit_text(
        f"📊 *Found {len(markets)} markets. Computing technicals...*\n"
        "Fetching candle data for top 12 coins...",
        parse_mode="Markdown"
    )

    technicals = {}
    for m in markets[:12]:
        technicals[m["name"]] = build_ta(m["name"])

    await msg.edit_text(
        "🤖 *Asking Claude AI for best signal...*",
        parse_mode="Markdown"
    )

    signal = await get_ai_signal(markets, technicals, cfg["anthropic_api_key"])

    if not signal:
        await msg.edit_text("❌ AI analysis failed. Try again in a moment.")
        return

    card = format_signal_card(signal, technicals)
    await msg.delete()
    await update.message.reply_text(card, parse_mode="Markdown")

async def signal_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = context.bot_data["cfg"]
    await run_signal_scan(update, cfg)

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.lower().strip() if update.message.text else ""
    if any(t in text for t in TRIGGER_WORDS):
        cfg = context.bot_data["cfg"]
        await run_signal_scan(update, cfg)
    else:
        await update.message.reply_text(
            "👋 Say *any signals*, *scan*, or */signal* to get a trade signal!\n\n"
            "I'll analyze 20 markets and find the best setup for you.",
            parse_mode="Markdown"
        )

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📡 *AI Trading Signal Bot*\n\n"
        "I scan 20+ crypto markets using:\n"
        "• RSI on 4h/1h/15m/5m timeframes\n"
        "• MACD with crossover detection\n"
        "• Bollinger Bands squeeze/breakout\n"
        "• VWAP position analysis\n"
        "• ATR-based stop loss sizing\n"
        "• Funding rate sentiment\n\n"
        "💬 Just say *any signals* or */signal* anytime!\n\n"
        "_You place trades manually — works with Kraken, Binance, Bybit, etc._",
        parse_mode="Markdown"
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📋 *Commands:*\n\n"
        "/signal — Run a full market scan\n"
        "/start — Show intro\n\n"
        "Or just type: *any signals*, *scan*, *trade*, *analyse*\n\n"
        "The bot scans 20 markets, finds the best setup, and sends you a complete signal card with:\n"
        "• Entry price\n"
        "• Two take profit levels\n"
        "• Stop loss\n"
        "• Risk/reward ratio\n"
        "• AI reasoning\n"
        "• Suggested leverage",
        parse_mode="Markdown"
    )

# ─── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    cfg = load_config()
    print("""
╔══════════════════════════════════════════════╗
║       AI TRADING SIGNAL BOT — TELEGRAM       ║
║   Say "any signals" to get a trade signal    ║
╚══════════════════════════════════════════════╝
""")
    log.info("Bot starting...")

    app = Application.builder().token(cfg["telegram_bot_token"]).build()
    app.bot_data["cfg"] = cfg

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("signal", signal_command))
    app.add_handler(CommandHandler("scan", signal_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    log.info("Bot is running! Open Telegram and send 'any signals'")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

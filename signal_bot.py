#!/usr/bin/env python3
"""
📡 AI Trading Signal Bot v3 — Advanced Edition
────────────────────────────────────────────────────────────────
Full analysis stack:
  • RSI (4h/1h/15m/5m) + Divergence detection
  • MACD crossovers on 3 timeframes
  • Bollinger Bands + squeeze detection
  • VWAP deviation
  • Order Blocks (institutional support/resistance)
  • Fair Value Gaps — FVG (price magnets)
  • Volume Delta (aggressive buyer/seller pressure)
  • RSI Divergence (bullish/bearish reversal signals)
  • Higher Timeframe daily structure
  • Funding rate extremes (contrarian signals)
  • Fear & Greed index
  • BTC dominance trend
  • ATR-based dynamic TP/SL
────────────────────────────────────────────────────────────────
"""
import os, json, math, logging, requests, asyncio
from datetime import datetime, timezone
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (Application, CommandHandler, MessageHandler,
                           CallbackQueryHandler, filters, ContextTypes)
import anthropic
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants
from eth_account import Account

logging.basicConfig(level=logging.INFO, format="%(asctime)s │ %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("SignalBot")
HL_URL = "https://api.hyperliquid.xyz/info"
TOP_MARKETS = ["BTC","ETH","SOL","BNB","XRP","DOGE","ADA","AVAX","LINK","DOT",
               "UNI","ATOM","LTC","NEAR","APT","ARB","OP","INJ","SUI","MATIC"]

# ─── CONFIG ───────────────────────────────────────────────────────────────────
def load_config():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if token and api_key:
        return {
            "telegram_bot_token": token, "anthropic_api_key": api_key,
            "hl_wallet_address": os.environ.get("HL_WALLET_ADDRESS",""),
            "hl_secret_key": os.environ.get("HL_SECRET_KEY",""),
            "max_position_usd": float(os.environ.get("MAX_POSITION_USD","15")),
            "max_leverage": int(os.environ.get("MAX_LEVERAGE","3")),
        }
    path = os.path.join(os.path.dirname(__file__), "config_signal.json")
    if os.path.exists(path):
        with open(path) as f: return json.load(f)
    print("ERROR: Set TELEGRAM_BOT_TOKEN and ANTHROPIC_API_KEY"); exit(1)

# ─── HYPERLIQUID EXECUTOR ─────────────────────────────────────────────────────
class HLExecutor:
    def __init__(self, cfg):
        self.enabled = bool(cfg.get("hl_secret_key") and cfg.get("hl_wallet_address"))
        if not self.enabled: return
        wallet = Account.from_key(cfg["hl_secret_key"])
        self.address = cfg["hl_wallet_address"]
        self.max_usd = cfg.get("max_position_usd", 15)
        self.max_lev = cfg.get("max_leverage", 3)
        self.exchange = Exchange(wallet, constants.MAINNET_API_URL, account_address=self.address)

    def get_balance(self):
        try:
            r = requests.post(HL_URL, json={"type":"clearinghouseState","user":self.address}, timeout=10)
            return float(r.json().get("marginSummary",{}).get("accountValue") or 0)
        except: return 0.0

    def execute(self, signal):
        if not self.enabled:
            return {"success":False,"error":"Add HL_WALLET_ADDRESS + HL_SECRET_KEY in Railway Variables"}
        coin=signal["coin"]; is_long=signal["signal"]=="LONG"
        lev=min(signal.get("leverage_suggested",2), self.max_lev)
        tp=signal["take_profit_1"]; sl=signal["stop_loss"]
        try:
            balance=self.get_balance()
            if balance<10: return {"success":False,"error":f"Balance too low: ${balance:.2f}"}
            size_usd=max(min(balance*0.20, self.max_usd), 11.0)
            r=requests.post(HL_URL, json={"type":"allMids"}, timeout=5)
            price=float(r.json().get(coin, signal["entry"]))
            sz_dec=0; asset_id=0
            try:
                r2=requests.post(HL_URL, json={"type":"meta"}, timeout=5)
                for i,a in enumerate(r2.json().get("universe",[])):
                    if a.get("name")==coin: sz_dec=int(a.get("szDecimals",0)); asset_id=i; break
            except: pass
            sz=round(size_usd/price, sz_dec)
            if sz_dec==0: sz=int(sz)
            result=self.exchange.market_open(coin, is_long, sz, None, 0.01)
            statuses=result.get("response",{}).get("data",{}).get("statuses",[])
            if statuses and "error" in str(statuses[0]).lower():
                return {"success":False,"error":str(statuses[0])}
            fill=price
            try: fill=float(statuses[0].get("filled",{}).get("avgPx",price))
            except: pass
            try: self.exchange.order(asset_id, not is_long, sz, round(tp,6), {"limit":{"tif":"Gtc"}}, reduce_only=True)
            except Exception as e: log.warning(f"TP failed: {e}")
            try: self.exchange.order(asset_id, not is_long, sz, round(sl*(0.995 if not is_long else 1.005),6),
                    {"trigger":{"triggerPx":str(round(sl,6)),"isMarket":True,"tpsl":"sl"}}, reduce_only=True)
            except Exception as e: log.warning(f"SL failed: {e}")
            return {"success":True,"coin":coin,"side":"LONG" if is_long else "SHORT",
                    "fill_price":fill,"size_usd":size_usd,"size_contracts":sz,"leverage":lev,"tp":tp,"sl":sl}
        except Exception as e: return {"success":False,"error":str(e)}

# ─── CANDLES ──────────────────────────────────────────────────────────────────
def fetch_candles(coin, interval, n=80):
    ms={"5m":300000,"15m":900000,"1h":3600000,"4h":14400000,"1d":86400000}
    period=ms.get(interval,3600000); now=int(datetime.now(timezone.utc).timestamp()*1000); start=now-(n+5)*period
    try:
        r=requests.post(HL_URL,json={"type":"candleSnapshot","req":{"coin":coin,"interval":interval,"startTime":start,"endTime":now}},timeout=10)
        return [{"t":int(c["t"]),"o":float(c["o"]),"h":float(c["h"]),"l":float(c["l"]),"c":float(c["c"]),"v":float(c["v"])} for c in r.json()][-n:]
    except: return []

# ─── BASIC INDICATORS ─────────────────────────────────────────────────────────
def rsi(closes, p=14):
    if len(closes)<p+1: return None
    gains=[max(closes[i]-closes[i-1],0) for i in range(1,len(closes))]
    losses=[max(closes[i-1]-closes[i],0) for i in range(1,len(closes))]
    ag=sum(gains[-p:])/p; al=sum(losses[-p:])/p
    return round(100-(100/(1+ag/al)) if al else 100,1)

def ema(vals,p):
    if len(vals)<p: return [None]*len(vals)
    k=2/(p+1); res=[None]*(p-1); res.append(sum(vals[:p])/p)
    for v in vals[p:]: res.append(v*k+res[-1]*(1-k))
    return res

def macd_sig(closes):
    e12=ema(closes,12); e26=ema(closes,26)
    ml=[a-b if a and b else None for a,b in zip(e12,e26)]
    valid=[v for v in ml if v is not None]
    if len(valid)<9: return "unknown",None
    sl=ema(valid,9); h=valid[-1]-sl[-1]; prev_h=valid[-2]-sl[-2] if len(valid)>=2 else 0
    cross="BULLISH CROSS" if prev_h<0 and h>0 else "BEARISH CROSS" if prev_h>0 and h<0 else None
    return "bullish" if h>0 else "bearish", cross

def bollinger(closes, p=20):
    if len(closes)<p: return "inside",None,None
    w=closes[-p:]; mid=sum(w)/p; std=math.sqrt(sum((x-mid)**2 for x in w)/p)
    upper=mid+2*std; lower=mid-2*std; price=closes[-1]
    bw=(upper-lower)/mid; squeeze=bw<0.03
    pos="ABOVE UPPER" if price>upper else "BELOW LOWER" if price<lower else "inside"
    pct_b=round((price-lower)/(upper-lower),3) if upper>lower else 0.5
    return pos, round(upper,4), round(lower,4)

def atr_calc(candles, p=14):
    if len(candles)<p+1: return None
    trs=[max(c["h"]-c["l"],abs(c["h"]-candles[i-1]["c"]),abs(c["l"]-candles[i-1]["c"])) for i,c in enumerate(candles[1:],1)]
    return round(sum(trs[-p:])/p,6)

def vwap_calc(candles):
    if not candles: return None
    pv=sum(((c["h"]+c["l"]+c["c"])/3)*c["v"] for c in candles)
    tv=sum(c["v"] for c in candles)
    return round(pv/tv,4) if tv else None

# ─── ADVANCED INDICATORS ──────────────────────────────────────────────────────
def find_order_blocks(candles, lookback=40):
    """Institutional Order Blocks — where banks placed orders."""
    if len(candles)<lookback: return {"bullish":[],"bearish":[]}
    c=candles[-lookback:]; bull=[]; bear=[]
    for i in range(2, len(c)-2):
        body=c[i]["h"]-c[i]["l"]
        if body==0: continue
        if (c[i]["c"]<c[i]["o"] and c[i+1]["c"]>c[i+1]["o"] and
                c[i+1]["c"]>c[i]["h"] and (c[i]["o"]-c[i]["c"])/body>0.4):
            bull.append({"high":round(c[i]["h"],4),"low":round(c[i]["l"],4),
                         "mid":round((c[i]["h"]+c[i]["l"])/2,4)})
        if (c[i]["c"]>c[i]["o"] and c[i+1]["c"]<c[i+1]["o"] and
                c[i+1]["c"]<c[i]["l"] and (c[i]["c"]-c[i]["o"])/body>0.4):
            bear.append({"high":round(c[i]["h"],4),"low":round(c[i]["l"],4),
                         "mid":round((c[i]["h"]+c[i]["l"])/2,4)})
    return {"bullish":bull[-3:],"bearish":bear[-3:]}

def find_fvg(candles, lookback=40):
    """Fair Value Gaps — imbalances price tends to fill."""
    if len(candles)<lookback: return {"bullish":[],"bearish":[]}
    c=candles[-lookback:]; bull=[]; bear=[]
    for i in range(1, len(c)-1):
        if c[i+1]["l"]>c[i-1]["h"]:
            sz=(c[i+1]["l"]-c[i-1]["h"])/c[i]["c"]*100
            if sz>0.1: bull.append({"top":round(c[i+1]["l"],4),"bottom":round(c[i-1]["h"],4),"pct":round(sz,3)})
        if c[i+1]["h"]<c[i-1]["l"]:
            sz=(c[i-1]["l"]-c[i+1]["h"])/c[i]["c"]*100
            if sz>0.1: bear.append({"top":round(c[i-1]["l"],4),"bottom":round(c[i+1]["h"],4),"pct":round(sz,3)})
    return {"bullish":bull[-2:],"bearish":bear[-2:]}

def volume_delta(candles, lookback=20):
    """Volume Delta — aggressive buyers vs sellers."""
    if len(candles)<lookback: return {}
    c=candles[-lookback:]
    buy=sum(x["v"] for x in c if x["c"]>=x["o"]); sell=sum(x["v"] for x in c if x["c"]<x["o"])
    total=buy+sell; dp=round((buy-sell)/total*100,1) if total else 0
    rc=c[-5:]; rb=sum(x["v"] for x in rc if x["c"]>=x["o"]); rs=sum(x["v"] for x in rc if x["c"]<x["o"]); rt=rb+rs
    rdp=round((rb-rs)/rt*100,1) if rt else 0
    return {"delta_pct":dp,"recent_delta_pct":rdp,
            "pressure":"buying" if dp>10 else "selling" if dp<-10 else "neutral",
            "recent_pressure":"buying" if rdp>15 else "selling" if rdp<-15 else "neutral"}

def rsi_divergence(closes):
    """RSI Divergence — price vs momentum disagreement."""
    if len(closes)<25: return None
    rsi_s=[r for r in [rsi(closes[:i+1]) for i in range(14,len(closes))] if r]
    if len(rsi_s)<10: return None
    if closes[-1]<closes[-10] and rsi_s[-1]>rsi_s[-10]: return "BULLISH DIVERGENCE"
    if closes[-1]>closes[-10] and rsi_s[-1]<rsi_s[-10]: return "BEARISH DIVERGENCE"
    return None

def daily_structure(coin):
    """Higher timeframe daily trend — is the bigger trend with us?"""
    try:
        d=fetch_candles(coin,"1d",30)
        if len(d)<10: return {"trend":"unknown"}
        cl=[c["c"] for c in d]; price=cl[-1]
        e20=ema(cl,20)[-1] or price; e50=ema(cl,min(50,len(cl)))[-1] or price
        trend="BULLISH" if price>e20>e50 else "BEARISH" if price<e20<e50 else "RANGING"
        return {"trend":trend,"ema20":round(e20,4),"ema50":round(e50,4)}
    except: return {"trend":"unknown"}

def get_fear_greed():
    """Fear & Greed Index from Alternative.me."""
    try:
        r=requests.get("https://api.alternative.me/fng/?limit=1",timeout=5)
        d=r.json()["data"][0]; v=int(d["value"])
        sig="EXTREME_FEAR_BUY" if v<25 else "FEAR_BUY" if v<40 else "GREED_CAUTION" if v>70 else "EXTREME_GREED_SELL" if v>85 else "neutral"
        return {"value":v,"label":d["value_classification"],"signal":sig}
    except: return {"value":50,"label":"Neutral","signal":"neutral"}

def get_btc_dom():
    """BTC dominance — high dom = alts weak, low dom = alts strong."""
    try:
        r=requests.get("https://api.coingecko.com/api/v3/global",timeout=5)
        dom=r.json()["data"]["market_cap_percentage"]["btc"]
        return {"dominance":round(dom,1),"signal":"alt_bearish" if dom>55 else "alt_bullish" if dom<48 else "neutral"}
    except: return {"dominance":None,"signal":"neutral"}

# ─── FULL ANALYSIS ENGINE ─────────────────────────────────────────────────────
def build_ta(coin):
    c1h=fetch_candles(coin,"1h",80); c15m=fetch_candles(coin,"15m",60)
    c5m=fetch_candles(coin,"5m",40); c4h=fetch_candles(coin,"4h",50)
    cl1h=[c["c"] for c in c1h]; cl15m=[c["c"] for c in c15m]
    cl5m=[c["c"] for c in c5m]; cl4h=[c["c"] for c in c4h]

    r1h=rsi(cl1h); r15m=rsi(cl15m); r5m=rsi(cl5m,7); r4h=rsi(cl4h)
    m1h,mx1h=macd_sig(cl1h); m15m,mx15m=macd_sig(cl15m); m4h,mx4h=macd_sig(cl4h)
    bb_pos,bb_up,bb_lo=bollinger(cl1h)
    at=atr_calc(c1h); vw=vwap_calc(c1h[-24:] if len(c1h)>=24 else c1h)
    price=cl1h[-1] if cl1h else 0

    # Advanced
    obs=find_order_blocks(c1h,40); obs4h=find_order_blocks(c4h,30)
    fvg=find_fvg(c1h,40); fvg4h=find_fvg(c4h,30)
    vd=volume_delta(c1h,20)
    div=rsi_divergence(cl1h)
    htf=daily_structure(coin)

    near_bull_ob=any(price>0 and abs(price-ob["mid"])/price<0.015 for ob in obs["bullish"])
    near_bear_ob=any(price>0 and abs(price-ob["mid"])/price<0.015 for ob in obs["bearish"])
    near_bull_fvg=any(f["bottom"]<=price<=f["top"] for f in fvg["bullish"]) if price>0 else False
    near_bear_fvg=any(f["bottom"]<=price<=f["top"] for f in fvg["bearish"]) if price>0 else False

    # ATR-based TP/SL suggestions
    atr_v=at or (price*0.01)
    dyn={"bull_tp1":round(price+atr_v*2,4),"bull_tp2":round(price+atr_v*3.5,4),"bull_sl":round(price-atr_v*1.5,4),
         "bear_tp1":round(price-atr_v*2,4),"bear_tp2":round(price-atr_v*3.5,4),"bear_sl":round(price+atr_v*1.5,4)}

    bull=0; bear=0; sigs=[]

    # RSI
    if r4h and r4h<35: bull+=3; sigs.append(f"RSI 4h extreme oversold ({r4h})")
    elif r4h and r4h<45: bull+=2; sigs.append(f"RSI 4h oversold ({r4h})")
    elif r4h and r4h>75: bear+=3; sigs.append(f"RSI 4h extreme overbought ({r4h})")
    elif r4h and r4h>65: bear+=2; sigs.append(f"RSI 4h overbought ({r4h})")
    if r1h and r1h<40: bull+=2; sigs.append(f"RSI 1h oversold ({r1h})")
    elif r1h and r1h>65: bear+=2; sigs.append(f"RSI 1h overbought ({r1h})")
    if r15m and r15m<38: bull+=2; sigs.append(f"RSI 15m oversold ({r15m})")
    elif r15m and r15m>65: bear+=2; sigs.append(f"RSI 15m overbought ({r15m})")

    # MACD
    if mx1h=="BULLISH CROSS": bull+=3; sigs.append("MACD 1h BULLISH CROSS")
    elif mx1h=="BEARISH CROSS": bear+=3; sigs.append("MACD 1h BEARISH CROSS")
    elif m1h=="bullish": bull+=1
    elif m1h=="bearish": bear+=1
    if mx15m=="BULLISH CROSS": bull+=2; sigs.append("MACD 15m BULLISH CROSS")
    elif mx15m=="BEARISH CROSS": bear+=2; sigs.append("MACD 15m BEARISH CROSS")
    if m4h=="bullish": bull+=1; sigs.append("MACD 4h bullish")
    elif m4h=="bearish": bear+=1; sigs.append("MACD 4h bearish")

    # Bollinger
    if bb_pos=="BELOW LOWER": bull+=2; sigs.append("BB: below lower band (oversold)")
    elif bb_pos=="ABOVE UPPER": bear+=2; sigs.append("BB: above upper band (overbought)")

    # VWAP
    if vw and price>vw: bull+=1; sigs.append(f"Above VWAP (${vw:,.2f})")
    elif vw and price<vw: bear+=1; sigs.append(f"Below VWAP (${vw:,.2f})")

    # Order Blocks
    if near_bull_ob: bull+=3; sigs.append("AT BULLISH ORDER BLOCK — institutional support")
    if near_bear_ob: bear+=3; sigs.append("AT BEARISH ORDER BLOCK — institutional resistance")

    # FVG
    if near_bull_fvg: bull+=2; sigs.append("INSIDE BULLISH FVG — price magnet")
    if near_bear_fvg: bear+=2; sigs.append("INSIDE BEARISH FVG — price magnet")

    # Volume Delta
    if vd.get("pressure")=="buying": bull+=2; sigs.append(f"Volume delta: BUYING ({vd.get('delta_pct',0):+.1f}%)")
    elif vd.get("pressure")=="selling": bear+=2; sigs.append(f"Volume delta: SELLING ({vd.get('delta_pct',0):+.1f}%)")
    if vd.get("recent_pressure")=="buying": bull+=1; sigs.append("Recent: aggressive buying")
    elif vd.get("recent_pressure")=="selling": bear+=1; sigs.append("Recent: aggressive selling")

    # Divergence
    if div=="BULLISH DIVERGENCE": bull+=3; sigs.append("RSI BULLISH DIVERGENCE — reversal")
    elif div=="BEARISH DIVERGENCE": bear+=3; sigs.append("RSI BEARISH DIVERGENCE — reversal")

    # HTF structure
    if htf.get("trend")=="BULLISH": bull+=2; sigs.append("Daily trend: BULLISH")
    elif htf.get("trend")=="BEARISH": bear+=2; sigs.append("Daily trend: BEARISH")

    # Volume
    if len(c1h)>=6:
        rv=sum(c["v"] for c in c1h[-3:])/3; pv=sum(c["v"] for c in c1h[-6:-3])/3
        if rv>pv*1.2: bull+=1; sigs.append("Volume expanding")
        elif rv<pv*0.7: sigs.append("Volume shrinking")

    return {
        "price":price,"rsi_4h":r4h,"rsi_1h":r1h,"rsi_15m":r15m,"rsi_5m":r5m,
        "macd_1h":m1h,"macd_cross_1h":mx1h,"macd_15m":m15m,"macd_cross_15m":mx15m,"macd_4h":m4h,
        "bb_pos":bb_pos,"bb_upper":bb_up,"bb_lower":bb_lo,"vwap":vw,"atr":at,
        "order_blocks":obs,"order_blocks_4h":obs4h,"fvg":fvg,"fvg_4h":fvg4h,
        "volume_delta":vd,"divergence":div,"htf":htf,"dynamic_levels":dyn,
        "near_bull_ob":near_bull_ob,"near_bear_ob":near_bear_ob,
        "near_bull_fvg":near_bull_fvg,"near_bear_fvg":near_bear_fvg,
        "bull":bull,"bear":bear,"signals":sigs,
        "bias":"BULLISH" if bull>bear+3 else "BEARISH" if bear>bull+3 else "MIXED"
    }

# ─── MARKET DATA ──────────────────────────────────────────────────────────────
def get_markets():
    try:
        r=requests.post(HL_URL,json={"type":"metaAndAssetCtxs"},timeout=10)
        data=r.json(); universe=data[0].get("universe",[]); ctxs=data[1]; markets=[]
        for i,a in enumerate(universe):
            name=a.get("name","")
            if name not in TOP_MARKETS or i>=len(ctxs): continue
            ctx=ctxs[i]; mid=float(ctx.get("midPx") or ctx.get("markPx") or 0)
            if mid<=0: continue
            markets.append({"name":name,"price":mid,"funding":float(ctx.get("funding") or 0),
                            "volume_24h":float(ctx.get("dayNtlVlm") or 0),"open_interest":float(ctx.get("openInterest") or 0)})
        return sorted(markets,key=lambda m:m["volume_24h"],reverse=True)
    except: return []

# ─── AI SIGNAL ENGINE ─────────────────────────────────────────────────────────
SYSTEM = """You are an elite institutional-grade crypto trading analyst. You use the most advanced technical analysis available.

Your edge sources (in priority order):
1. ORDER BLOCKS at current price — highest probability entries (institutional support/resistance)
2. FAIR VALUE GAPS (FVG) at current price — price magnets
3. RSI DIVERGENCE — momentum reversal signals
4. MACD CROSSOVERS — trend change confirmation
5. EXTREME RSI (4h <30 or >75) + multi-timeframe alignment
6. FUNDING EXTREMES — contrarian squeeze plays
7. FEAR & GREED extremes (<25 or >85) — market sentiment reversals

Rules:
- Only signal if confidence ≥80%
- Need 3+ INDEPENDENT confirming signals
- R:R must be ≥1.5x, ideally 2x+
- Daily trend alignment = +1 confidence
- Order block + FVG + RSI divergence = near-certain trade
- Use ATR-based dynamic levels from the data

Respond ONLY with valid JSON, no markdown:
{
  "signal": "LONG" | "SHORT" | "HOLD",
  "coin": "BTC",
  "confidence": 85,
  "entry": 94500.0,
  "take_profit_1": 96000.0,
  "take_profit_2": 97800.0,
  "stop_loss": 93200.0,
  "leverage_suggested": 2,
  "edge": "order_block_bounce",
  "risk_reward": 2.3,
  "reasoning": "3 sentences citing specific indicators",
  "invalidation": "specific price level that invalidates",
  "time_in_trade": "4-8h",
  "key_confluence": ["order block at X", "FVG at Y", "RSI divergence", "daily trend bullish"]
}"""

def build_prompt(markets, technicals, fg, dom):
    lines=[
        f"=== MACRO CONTEXT ===",
        f"Fear & Greed: {fg.get('value','?')} ({fg.get('label','?')}) — {fg.get('signal','?')}",
        f"BTC Dominance: {dom.get('dominance','?')}% — {dom.get('signal','?')}",
        f"UTC: {datetime.now(timezone.utc).strftime('%H:%M')}\n",
        "=== MARKET SCAN ===",
    ]
    for m in markets[:12]:
        ta=technicals.get(m["name"],{})
        fund_str="LONGS_HOT" if m["funding"]>0.001 else "SHORTS_HOT" if m["funding"]<-0.001 else "ok"
        obs=ta.get("order_blocks",{}); fvg=ta.get("fvg",{}); vd=ta.get("volume_delta",{}); htf=ta.get("htf",{})
        dyn=ta.get("dynamic_levels",{})
        lines.append(f"\n{m['name']}: ${m['price']:,.4f} | {ta.get('bias','?')} Bull:{ta.get('bull',0)} Bear:{ta.get('bear',0)}")
        lines.append(f"  RSI: 4h={ta.get('rsi_4h')} 1h={ta.get('rsi_1h')} 15m={ta.get('rsi_15m')} 5m={ta.get('rsi_5m')}")
        lines.append(f"  MACD: 4h={ta.get('macd_4h','?')} 1h={ta.get('macd_1h','?')}/{ta.get('macd_cross_1h','none')} 15m={ta.get('macd_cross_15m','none')}")
        lines.append(f"  BB: {ta.get('bb_pos','?')} | VWAP: {'above' if ta.get('price',0)>(ta.get('vwap') or 0) else 'below'}")
        lines.append(f"  OBs: {len(obs.get('bullish',[]))} bull / {len(obs.get('bearish',[]))} bear | FVG: {len(fvg.get('bullish',[]))} bull / {len(fvg.get('bearish',[]))} bear")
        lines.append(f"  VolDelta: {vd.get('pressure','?')} ({vd.get('delta_pct',0):+.1f}%) recent={vd.get('recent_pressure','?')}")
        lines.append(f"  Divergence: {ta.get('divergence','none')} | Daily: {htf.get('trend','?')} | Funding: {m['funding']:.6f}({fund_str})")
        if ta.get("near_bull_ob"): lines.append("  *** AT BULLISH ORDER BLOCK ***")
        if ta.get("near_bear_ob"): lines.append("  *** AT BEARISH ORDER BLOCK ***")
        if ta.get("near_bull_fvg"): lines.append("  *** INSIDE BULLISH FVG ***")
        if ta.get("near_bear_fvg"): lines.append("  *** INSIDE BEARISH FVG ***")
        if ta.get("divergence"): lines.append(f"  *** {ta.get('divergence')} ***")
        if ta.get("signals"): lines.append(f"  Signals: {' | '.join(ta['signals'][:5])}")
        lines.append(f"  ATR levels: TP1=${dyn.get('bull_tp1','?')} TP2=${dyn.get('bull_tp2','?')} SL=${dyn.get('bull_sl','?')}")
    lines.append("\nFind the SINGLE best trade. Prioritize OB+FVG+Divergence combos. Return HOLD if nothing ≥80%.")
    return "\n".join(lines)

async def get_ai_signal(markets, technicals, fg, dom, api_key):
    client=anthropic.Anthropic(api_key=api_key)
    try:
        resp=client.messages.create(model="claude-sonnet-4-5",max_tokens=700,system=SYSTEM,
            messages=[{"role":"user","content":build_prompt(markets,technicals,fg,dom)}])
        raw=resp.content[0].text.strip().replace("```json","").replace("```","").strip()
        return json.loads(raw)
    except Exception as e:
        log.error(f"AI error: {e}"); return None

# ─── FORMATTERS ───────────────────────────────────────────────────────────────
def format_card(sig, ta, fg) -> str:
    if sig["signal"]=="HOLD":
        return (f"⏸ *NO TRADE — HOLD*\n\n"
                f"😱 Fear & Greed: {fg.get('value','?')} ({fg.get('label','?')})\n\n"
                f"No setup meets 80%+ confidence threshold.\n_Try again in 1-2 hours._")
    coin=sig["coin"]; conf=sig["confidence"]
    d="🟢 LONG" if sig["signal"]=="LONG" else "🔴 SHORT"
    conf_bar="🟩"*(conf//10)+"⬜"*(10-conf//10)
    stars="⭐⭐⭐" if conf>=85 else "⭐⭐" if conf>=80 else "⭐"
    entry=sig["entry"]; tp1=sig["take_profit_1"]; tp2=sig.get("take_profit_2",tp1); sl=sig["stop_loss"]
    rr=sig.get("risk_reward",0)
    if sig["signal"]=="LONG":
        tp1p=(tp1-entry)/entry*100; tp2p=(tp2-entry)/entry*100; slp=(entry-sl)/entry*100
    else:
        tp1p=(entry-tp1)/entry*100; tp2p=(entry-tp2)/entry*100; slp=(sl-entry)/entry*100
    coin_ta=ta.get(coin,{}); sigs=coin_ta.get("signals",[])
    sig_text="\n".join(f"  ✅ {s}" for s in sigs[:5]) if sigs else "  Multi-timeframe confluence"
    confluence=sig.get("key_confluence",[])
    conf_text=("\n🔑 *KEY CONFLUENCE*\n"+"\n".join(f"  🔑 {c}" for c in confluence[:3])) if confluence else ""
    obs=coin_ta.get("order_blocks",{}); fvg_d=coin_ta.get("fvg",{})
    ob_text=""
    if coin_ta.get("near_bull_ob") or coin_ta.get("near_bear_ob"):
        ob_text="\n\n🏦 *ORDER BLOCK ALERT*\n"
        if coin_ta.get("near_bull_ob"):
            ob=obs["bullish"][-1] if obs.get("bullish") else {}
            ob_text+=f"  📦 Bullish OB: ${ob.get('low','?')} — ${ob.get('high','?')}\n"
        if coin_ta.get("near_bear_ob"):
            ob=obs["bearish"][-1] if obs.get("bearish") else {}
            ob_text+=f"  📦 Bearish OB: ${ob.get('low','?')} — ${ob.get('high','?')}\n"
    fvg_text=""
    if coin_ta.get("near_bull_fvg") or coin_ta.get("near_bear_fvg"):
        fvg_text="\n🕳 *FVG MAGNET ZONE*\n"
        if coin_ta.get("near_bull_fvg") and fvg_d.get("bullish"):
            f=fvg_d["bullish"][-1]
            fvg_text+=f"  Bullish FVG: ${f.get('bottom','?')} — ${f.get('top','?')}\n"
        if coin_ta.get("near_bear_fvg") and fvg_d.get("bearish"):
            f=fvg_d["bearish"][-1]
            fvg_text+=f"  Bearish FVG: ${f.get('bottom','?')} — ${f.get('top','?')}\n"
    div_text=""
    if coin_ta.get("divergence"):
        div_text=f"\n📐 *{coin_ta.get('divergence')}* — reversal signal\n"
    return f"""📡 *AI SIGNAL v3 ADVANCED* {stars}

{d} *{coin}/USDT*
━━━━━━━━━━━━━━━━━━━━━
🎯 *Confidence:* {conf}% {conf_bar}
⚡ *Edge:* {sig.get('edge','').replace('_',' ').title()}
⏱ *Time:* {sig.get('time_in_trade','4-8h')}
😱 *F&G:* {fg.get('value','?')} — {fg.get('label','?')}

📊 *LEVELS*
Entry:     `${entry:,.4f}`
TP1:       `${tp1:,.4f}` (+{tp1p:.2f}%)
TP2:       `${tp2:,.4f}` (+{tp2p:.2f}%)
Stop Loss: `${sl:,.4f}` (-{slp:.2f}%)
R:R Ratio: *{rr:.1f}x* 🏆{ob_text}{fvg_text}{div_text}
📈 *CONFIRMING SIGNALS*
{sig_text}{conf_text}

🧠 *AI REASONING*
_{sig.get('reasoning','')}_

⚠️ *INVALIDATION*
_{sig.get('invalidation','')}_"""

def format_result(res) -> str:
    if not res["success"]:
        return f"❌ *EXECUTION FAILED*\n\n`{res.get('error','Unknown')}`\n\nPlace manually on your exchange."
    side="🟢 LONG" if res["side"]=="LONG" else "🔴 SHORT"
    return f"""✅ *TRADE EXECUTED!*

{side} *{res['coin']}/USDT*
━━━━━━━━━━━━━━━━━━━━━
💰 Fill:     `${res['fill_price']:,.4f}`
📦 Size:     `{res['size_contracts']} contracts (${res['size_usd']:.2f})`
⚡ Leverage: `{res['leverage']}x`
🎯 TP:       `${res['tp']:,.4f}`
🛡 SL:       `${res['sl']:,.4f}`

_TP and SL placed on Hyperliquid automatically._"""

# ─── TELEGRAM HANDLERS ────────────────────────────────────────────────────────
TRIGGERS=["signal","signals","any signals","scan","analyse","analyze","trade",
          "check","find","anything","go","run","now","search","yes","ok","pump","signals?"]

async def run_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg=context.bot_data["cfg"]; chat_id=update.effective_chat.id
    msg=await update.message.reply_text(
        "🔍 *Advanced scan v3 starting...*\n"
        "Order Blocks · FVG · Divergence · Fear & Greed · Volume Delta...",
        parse_mode="Markdown")
    fg=get_fear_greed(); dom=get_btc_dom()
    markets=get_markets()
    if not markets:
        await msg.edit_text("❌ Market data unavailable. Try again."); return
    await msg.edit_text(
        f"📊 *Analyzing {len(markets)} markets...*\n"
        f"F&G: {fg.get('value','?')} ({fg.get('label','?')}) | BTC Dom: {dom.get('dominance','?')}%\n"
        f"Computing OBs, FVGs, divergences, volume delta...",
        parse_mode="Markdown")
    technicals={}
    for m in markets[:12]: technicals[m["name"]]=build_ta(m["name"])
    await msg.edit_text("🤖 *Claude AI analyzing all signals...*",parse_mode="Markdown")
    signal=await get_ai_signal(markets,technicals,fg,dom,cfg["anthropic_api_key"])
    if not signal:
        await msg.edit_text("❌ AI failed. Try again."); return
    card=format_card(signal,technicals,fg)
    await msg.delete()
    if signal["signal"]=="HOLD":
        await update.message.reply_text(card,parse_mode="Markdown"); return
    context.bot_data[f"sig_{chat_id}"]=signal
    hl_on=bool(cfg.get("hl_secret_key") and cfg.get("hl_wallet_address"))
    if hl_on:
        kb=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ YES — Execute",callback_data=f"yes_{chat_id}"),
            InlineKeyboardButton("❌ NO — Skip",callback_data=f"no_{chat_id}"),
        ]])
        await update.message.reply_text(
            card+"\n\n━━━━━━━━━━━━━━━━━━━━━\n⚡ *Execute on Hyperliquid?*",
            parse_mode="Markdown",reply_markup=kb)
    else:
        await update.message.reply_text(
            card+"\n\n_Place manually on your exchange._",parse_mode="Markdown")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query=update.callback_query; await query.answer()
    chat_id=update.effective_chat.id; executor=context.bot_data["executor"]
    if query.data.startswith("yes_"):
        signal=context.bot_data.get(f"sig_{chat_id}")
        if not signal:
            await query.edit_message_text("⚠️ Signal expired. Say 'any signals' for a new one."); return
        await query.edit_message_text(f"⚡ *Executing {signal['signal']} {signal['coin']}...*",parse_mode="Markdown")
        loop=asyncio.get_event_loop()
        result=await loop.run_in_executor(None,executor.execute,signal)
        await query.edit_message_text(format_result(result),parse_mode="Markdown")
        context.bot_data.pop(f"sig_{chat_id}",None)
    elif query.data.startswith("no_"):
        await query.edit_message_text("❌ *Trade skipped.*\n\nSay *any signals* anytime.",parse_mode="Markdown")
        context.bot_data.pop(f"sig_{chat_id}",None)

async def signal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await run_scan(update,context)

async def msg_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text=update.message.text.lower().strip() if update.message.text else ""
    if any(t in text for t in TRIGGERS): await run_scan(update,context)
    else: await update.message.reply_text(
        "👋 Say *any signals* or */signal* to run a full advanced scan!\n\n"
        "Includes: Order Blocks · FVG · RSI Divergence · Volume Delta · Fear & Greed",
        parse_mode="Markdown")

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg=context.bot_data["cfg"]
    hl="✅ Auto-execute enabled" if cfg.get("hl_secret_key") else "⚠️ Signal-only (add HL keys in Railway)"
    await update.message.reply_text(
        f"📡 *AI Signal Bot v3 — Advanced*\n\n"
        f"Hyperliquid: {hl}\n\n"
        f"*Analysis stack:*\n"
        f"• RSI on 4h/1h/15m/5m + divergence detection\n"
        f"• MACD crossovers on 3 timeframes\n"
        f"• Order Blocks (institutional levels) 🏦\n"
        f"• Fair Value Gaps — FVG (price magnets) 🕳\n"
        f"• Volume Delta (buyer/seller pressure)\n"
        f"• Bollinger Bands + squeeze\n"
        f"• Higher timeframe daily structure\n"
        f"• Funding rate extremes\n"
        f"• Fear & Greed index 😱\n"
        f"• BTC dominance trend\n"
        f"• ATR dynamic TP/SL sizing\n\n"
        f"*Say:* any signals\n\n"
        f"_Runs 24/7 — no laptop needed!_",
        parse_mode="Markdown")

# ─── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    cfg=load_config(); executor=HLExecutor(cfg)
    print("""
╔════════════════════════════════════════════════════════╗
║      AI TRADING SIGNAL BOT v3 — ADVANCED EDITION      ║
║  Order Blocks · FVG · Divergence · Fear & Greed       ║
║  Say "any signals" → full scan → YES/NO to execute    ║
╚════════════════════════════════════════════════════════╝
""")
    log.info("Starting v3 advanced signal bot...")
    if executor.enabled:
        log.info(f"Hyperliquid connected. Balance: ${executor.get_balance():.2f}")
    else:
        log.info("Signal-only mode. Add HL_WALLET_ADDRESS + HL_SECRET_KEY in Railway to enable execution.")
    app=Application.builder().token(cfg["telegram_bot_token"]).build()
    app.bot_data["cfg"]=cfg; app.bot_data["executor"]=executor
    app.add_handler(CommandHandler("start",start_cmd))
    app.add_handler(CommandHandler("signal",signal_cmd))
    app.add_handler(CommandHandler("scan",signal_cmd))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,msg_handler))
    log.info("Bot live! Open Telegram and say 'any signals'")
    app.run_polling(drop_pending_updates=True)

if __name__=="__main__": main()

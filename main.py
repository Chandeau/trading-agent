# ═══════════════════════════════════════════════════════════════════
# YISROEL & YAKOV — AI TRADING AGENT v2
# Fixes: real momentum calc, real scale-out, honest stop labeling,
#        real crypto Hunter signals from price history
# ═══════════════════════════════════════════════════════════════════
 
import os, time, json, threading, requests
from datetime import datetime
from collections import deque
from flask import Flask, jsonify, render_template_string
import anthropic
 
PAPER      = os.environ.get("PAPER_MODE", "true").lower() == "true"
ALPACA_KEY = os.environ.get("ALPACA_KEY", "")
ALPACA_SEC = os.environ.get("ALPACA_SECRET", "")
CLAUDE_KEY = os.environ.get("ANTHROPIC_KEY", "")
 
BASE  = "https://paper-api.alpaca.markets" if PAPER else "https://api.alpaca.markets"
HEADS = {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SEC, "Content-Type": "application/json"}
 
ASSETS = [
    {"sym": "BTCUSD", "label": "BTC", "trail_pct": 0.033},
    {"sym": "ETHUSD", "label": "ETH", "trail_pct": 0.045},
    {"sym": "SOLUSD", "label": "SOL", "trail_pct": 0.065},
]
 
STD_PCT   = 0.20
HUNT_PCT  = 0.30
HUNT2_PCT = 0.40
STD_MULT  = 1.5
HUNT_MULT = 2.0
 
PRICE_HISTORY = {a["label"]: deque(maxlen=120) for a in ASSETS}
 
S = {
    "positions": {},
    "trades":    [],
    "log":       [],
    "prices":    {},
    "last_ai":   0,
    "cash":      0.0,
    "status":    "connecting",
    "cycles":    0,
    "signals":   {a["label"]: {"hunter": False, "reason": "", "mom_1h": 0, "mom_15m": 0} for a in ASSETS},
}
 
def log(msg, t="info"):
    e = {"msg": msg, "type": t, "ts": datetime.utcnow().strftime("%H:%M:%S")}
    S["log"] = [e] + S["log"][:79]
    print(f"[{e['ts']}] {msg}")
 
def get_momentum(label):
    h = PRICE_HISTORY[label]
    if len(h) < 2:
        return 0.0, 0.0, False
    cur = h[-1]
    idx_1h = max(0, len(h) - 60)
    price_1h = h[idx_1h]
    mom_1h = (cur - price_1h) / price_1h * 100 if price_1h > 0 else 0.0
    idx_15m = max(0, len(h) - 15)
    price_15m = h[idx_15m]
    mom_15m = (cur - price_15m) / price_15m * 100 if price_15m > 0 else 0.0
    moves = []
    step = 15
    for i in range(step, len(h), step):
        if i < len(h):
            m = abs(h[i] - h[i-step]) / h[i-step] * 100
            moves.append(m)
    avg_move = sum(moves) / len(moves) if moves else 0.5
    is_volatile = abs(mom_15m) > avg_move * 2.5
    return round(mom_1h, 2), round(mom_15m, 2), is_volatile
 
def check_hunter_signals():
    for a in ASSETS:
        label = a["label"]
        if label not in S["prices"]:
            continue
        mom_1h, mom_15m, is_volatile = get_momentum(label)
        signal = False
        reason = ""
        if abs(mom_1h) >= 4.0:
            signal = True
            direction = "UP" if mom_1h > 0 else "DOWN"
            reason = f"Strong 1h momentum {mom_1h:+.1f}% ({direction})"
        elif is_volatile and abs(mom_15m) >= 2.0:
            signal = True
            reason = f"Volatility spike: {mom_15m:+.1f}% in 15min"
        elif abs(mom_15m) >= 1.5 and (mom_15m * mom_1h > 0) and abs(mom_1h) >= 2.0:
            signal = True
            reason = f"Accelerating: 1h {mom_1h:+.1f}%, 15m {mom_15m:+.1f}%"
        prev = S["signals"][label]["hunter"]
        S["signals"][label] = {"hunter": signal, "reason": reason, "mom_1h": mom_1h, "mom_15m": mom_15m}
        if signal and not prev:
            log(f"🎯 HUNTER SIGNAL {label}: {reason}", "hunter")
 
def get_prices():
    try:
        url = "https://data.alpaca.markets/v1beta3/crypto/us/latest/quotes?symbols=BTC%2FUSD,ETH%2FUSD,SOL%2FUSD"
        r = requests.get(url, headers=HEADS, timeout=10)
        if r.ok:
            d = r.json().get("quotes", {})
            mapping = {"BTC/USD": "BTC", "ETH/USD": "ETH", "SOL/USD": "SOL"}
            prices = {}
            for sym, label in mapping.items():
                if sym in d:
                    ask = float(d[sym].get("ap", 0))
                    bid = float(d[sym].get("bp", 0))
                    if ask > 0 and bid > 0:
                        price = (ask + bid) / 2
                        prices[label] = {"price": price, "chg24h": 0}
                        PRICE_HISTORY[label].append(price)
            if len(prices) >= 2:
                S["prices"] = prices
                S["status"] = "live"
                S["cycles"] += 1
                for label in prices:
                    mom_1h, mom_15m, _ = get_momentum(label)
                    S["prices"][label]["mom_1h"] = mom_1h
                    S["prices"][label]["mom_15m"] = mom_15m
                check_hunter_signals()
                return prices
        else:
            log(f"Price feed {r.status_code}", "error")
    except Exception as e:
        log(f"Price error: {str(e)[:80]}", "error")
    S["status"] = "error"
    return None
 
def calc_trail_stop(label, price):
    a = next(x for x in ASSETS if x["label"] == label)
    return price * a["trail_pct"]
 
def get_cash():
    try:
        r = requests.get(f"{BASE}/v2/account", headers=HEADS, timeout=10)
        if r.ok:
            cash = float(r.json().get("cash", 0))
            S["cash"] = cash
            return cash
    except Exception as e:
        log(f"Account error: {e}", "error")
    return S.get("cash", 100000.0)
 
def buy_notional(sym, notional):
    try:
        payload = {"symbol": sym, "notional": str(round(notional, 2)),
                   "side": "buy", "type": "market", "time_in_force": "gtc"}
        r = requests.post(f"{BASE}/v2/orders", headers=HEADS, json=payload, timeout=10)
        if r.ok:
            return r.json()
        log(f"Buy rejected: {r.text[:100]}", "error")
    except Exception as e:
        log(f"Buy error: {e}", "error")
    return None
 
def sell_qty(sym, qty):
    try:
        payload = {"symbol": sym, "qty": str(round(qty, 8)),
                   "side": "sell", "type": "market", "time_in_force": "gtc"}
        r = requests.post(f"{BASE}/v2/orders", headers=HEADS, json=payload, timeout=10)
        if r.ok:
            return r.json()
        log(f"Partial sell rejected: {r.text[:100]}", "error")
    except Exception as e:
        log(f"Partial sell error: {e}", "error")
    return None
 
def sell_all(sym):
    try:
        r = requests.delete(f"{BASE}/v2/positions/{sym}", headers=HEADS, timeout=10)
        return r.ok
    except:
        return False
 
def check_stops():
    prices = S["prices"]
    if not prices:
        return
    for label in list(S["positions"].keys()):
        pos = S["positions"][label]
        cur = prices.get(label, {}).get("price")
        if not cur:
            continue
        trail = calc_trail_stop(label, cur)
        mult = HUNT_MULT if pos["mode"] == "hunter" else STD_MULT
        new_stop = cur - trail * mult
        pos["stop"] = max(pos["stop"], new_stop)
        be_thresh = 1.08 if pos["mode"] == "hunter" else 1.05
        if cur >= pos["entry"] * be_thresh and not pos.get("be"):
            pos["stop"] = max(pos["stop"], pos["entry"])
            pos["be"] = True
            log(f"⚓ Break-even {label} @ ${pos['entry']:,.0f}", "info")
        scale_thresh = 1.30 if pos["mode"] == "hunter" else 1.20
        if cur >= pos["entry"] * scale_thresh and not pos.get("so"):
            sell_pct = 0.20 if pos["mode"] == "hunter" else 0.30
            partial_qty = pos["qty"] * sell_pct
            asset = next(a for a in ASSETS if a["label"] == label)
            order = sell_qty(asset["sym"], partial_qty)
            if order:
                profit = (cur - pos["entry"]) * partial_qty
                pos["qty"] -= partial_qty
                pos["cost"] *= (1 - sell_pct)
                pos["so"] = True
                log(f"📊 SCALE OUT {label} {sell_pct*100:.0f}% @${cur:,.0f} → +${profit:.2f} LOCKED ✅", "win")
            else:
                log(f"⚠️ Scale-out failed {label}", "error")
        if cur <= pos["stop"]:
            asset = next(a for a in ASSETS if a["label"] == label)
            ok = sell_all(asset["sym"])
            if ok:
                pnl  = (cur - pos["entry"]) * pos["qty"]
                pnlP = (cur - pos["entry"]) / pos["entry"] * 100
                S["trades"] = [{"asset": label, "mode": pos["mode"],
                    "entry": pos["entry"], "exit": cur,
                    "pnl": pnl, "pnl_pct": pnlP,
                    "ts": datetime.utcnow().strftime("%H:%M")}] + S["trades"][:49]
                del S["positions"][label]
                emoji = "✅" if pnl >= 0 else "🔴"
                log(f"{emoji} STOP {label} @${cur:,.0f} → {'+'if pnl>=0 else ''}${pnl:.2f} ({pnlP:+.1f}%)",
                    "win" if pnl >= 0 else "loss")
 
def ai_decide(cash):
    now = time.time()
    if now - S["last_ai"] < 180:
        return
    if not S["prices"] or len(S["prices"]) < 2:
        return
    S["last_ai"] = now
    try:
        client = anthropic.Anthropic(api_key=CLAUDE_KEY)
        mkt_lines = []
        for a in ASSETS:
            label = a["label"]
            if label not in S["prices"]:
                continue
            p = S["prices"][label]
            sig = S["signals"].get(label, {})
            hunter_flag = "🎯 HUNTER SIGNAL" if sig.get("hunter") else ""
            mkt_lines.append(
                f"{label}: ${p['price']:,.2f} | 1h: {p.get('mom_1h',0):+.2f}% | "
                f"15m: {p.get('mom_15m',0):+.2f}% {hunter_flag}"
            )
        open_pos = "\n".join(
            f"{lbl}: entry ${p['entry']:,.2f} now ${S['prices'].get(lbl,{}).get('price',0):,.2f} stop ${p['stop']:.0f} [{p['mode']}]"
            for lbl, p in S["positions"].items()
        ) or "None"
        active_signals = [f"{lbl}: {S['signals'][lbl]['reason']}" for lbl in S["signals"] if S["signals"][lbl].get("hunter")]
        signal_text = "\n".join(active_signals) if active_signals else "None active"
        prompt = f"""Crypto trading agent. Paper: {PAPER}
ACCOUNT: Cash ${cash:,.2f} | Open: {len(S['positions'])}/2 max
MARKET (real momentum from price history):
{chr(10).join(mkt_lines)}
HUNTER SIGNALS: {signal_text}
POSITIONS: {open_pos}
RULES: BUY if 1h momentum >3% OR active hunter signal. HUNTER mode if >6% OR signal active. Standard 20%, Hunter 30-40%. Max 2 positions.
JSON only: {{"action":"BUY or HOLD","asset":"BTC or ETH or SOL or null","mode":"standard or hunter or null","confidence":7,"reason":"max 10 words"}}"""
        msg = client.messages.create(
            model="claude-sonnet-4-5", max_tokens=100,
            system="Crypto trading agent. JSON only. No markdown.",
            messages=[{"role": "user", "content": prompt}]
        )
        raw = msg.content[0].text.strip().replace("```json","").replace("```","")
        dec = json.loads(raw)
        if dec.get("action") == "BUY" and dec.get("asset") and len(S["positions"]) < 2:
            label = dec["asset"]
            asset = next((a for a in ASSETS if a["label"] == label), None)
            if asset and label not in S["positions"] and label in S["prices"]:
                ih = dec.get("mode") == "hunter"
                sig = S["signals"].get(label, {})
                double = ih and sig.get("hunter") and abs(S["prices"][label].get("mom_1h", 0)) >= 6
                pct = HUNT2_PCT if double else (HUNT_PCT if ih else STD_PCT)
                notional = cash * pct
                if notional >= 10:
                    price = S["prices"][label]["price"]
                    trail = calc_trail_stop(label, price)
                    stop = price - trail * (HUNT_MULT if ih else STD_MULT)
                    qty = notional / price
                    order = buy_notional(asset["sym"], notional)
                    if order:
                        S["positions"][label] = {"entry": price, "stop": stop, "qty": qty,
                            "cost": notional, "mode": "hunter" if ih else "standard", "so": False, "be": False}
                        prefix = "🎯 HUNTER" if ih else "📈 STANDARD"
                        log(f"{prefix} BUY {label} @${price:,.2f} | {pct*100:.0f}% | stop ${stop:,.0f} | {dec.get('reason','')}", "hunter" if ih else "buy")
        else:
            log(f"💭 HOLD — {dec.get('reason','no setup')} ({dec.get('confidence',7)}/10)", "info")
    except Exception as e:
        log(f"AI error: {str(e)[:80]}", "error")
 
def trading_loop():
    log("🚀 Agent v2 started — bugs fixed: real momentum, real scale-out, real signals")
    log(f"{'📄 PAPER' if PAPER else '💰 LIVE'} trading mode")
    time.sleep(3)
    while True:
        try:
            prices = get_prices()
            if prices:
                cash = get_cash()
                check_stops()
                ai_decide(cash)
        except Exception as e:
            log(f"Loop error: {e}", "error")
        time.sleep(60)
 
app = Flask(__name__)
 
@app.route("/")
def index():
    return render_template_string(HTML)
 
@app.route("/api")
def api():
    prices = S["prices"]
    pos_out = {}
    for lbl, pos in S["positions"].items():
        cur = prices.get(lbl, {}).get("price", pos["entry"])
        pnl = (cur - pos["entry"]) * pos["qty"]
        pos_out[lbl] = {**pos, "current": cur, "pnl": pnl, "pnl_pct": pnl / pos["cost"] * 100 if pos["cost"] else 0}
    total_pos = sum(p["qty"] * prices.get(lbl, {}).get("price", p["entry"]) for lbl, p in S["positions"].items())
    return jsonify({"prices": prices, "positions": pos_out, "trades": S["trades"][:20],
        "log": S["log"][:40], "cash": S["cash"], "pos_value": total_pos,
        "paper": PAPER, "status": S["status"], "cycles": S["cycles"], "signals": S["signals"]})
 
HTML = """<!DOCTYPE html>
<html><head>
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Y&Y Agent v2</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0A0D14;color:#E8EDF5;font-family:'SF Mono','Fira Code',monospace;max-width:500px;margin:0 auto;padding-bottom:70px}
.banner{padding:8px 14px;display:flex;justify-content:space-between;font-size:9px;border-bottom:1px solid #1C2333}
.head{padding:14px;border-bottom:1px solid #1C2333}
.brand{font-size:10px;color:#00C896;letter-spacing:3px;font-weight:bold;margin-bottom:10px}
.stats{display:flex;gap:8px}
.stat{flex:1;background:#111520;border-radius:8px;border:1px solid #1C2333;padding:9px 10px}
.sl{font-size:7px;color:#5A6478;letter-spacing:2px;margin-bottom:3px}
.sv{font-size:15px;font-weight:bold;line-height:1}
.ss{font-size:8px;color:#5A6478;margin-top:3px}
.tabs{display:flex;border-bottom:1px solid #1C2333;position:sticky;top:0;background:#0A0D14;z-index:10}
.tab{flex:1;padding:10px 4px;font-size:9px;letter-spacing:1px;text-align:center;cursor:pointer;color:#5A6478;border:none;background:none;border-bottom:2px solid transparent;font-family:inherit;outline:none}
.tab.on{color:#00C896;border-bottom-color:#00C896}
.body{padding:12px 14px}
.lbl{font-size:7px;color:#5A6478;letter-spacing:2px;margin-bottom:7px;margin-top:12px}
.card{background:#111520;border-radius:8px;border:1px solid #1C2333;padding:11px 13px;margin-bottom:9px}
.ch{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}
.kv{display:flex;justify-content:space-between;margin-bottom:4px}
.k{font-size:9px;color:#5A6478}.v{font-size:9px;font-weight:bold}
.g{color:#00C896}.r{color:#EF4444}.o{color:#FF6B35}.m{color:#5A6478}
.badge{font-size:7px;padding:2px 6px;border-radius:3px}
.hr{border-top:1px solid #1C2333;margin:7px 0}
.le{padding:6px 0;border-bottom:1px solid #1C233322;font-size:9px;line-height:1.5}
.lt{color:#5A6478;font-size:8px;margin-right:6px}
.empty{text-align:center;padding:28px 0;color:#5A6478;font-size:10px}
.sig{padding:10px 0;border-bottom:1px solid #1C2333}
.sn{font-size:10px;font-weight:bold}.sd{font-size:8px;color:#5A6478;margin-top:3px}
.st{font-size:7px;padding:2px 7px;border-radius:3px;background:#1C2333;color:#5A6478}
.st.on{background:#FF6B3530;color:#FF6B35;font-weight:bold}
</style></head>
<body><div id="app"><div style="text-align:center;padding:60px 20px;color:#5A6478;font-size:11px">Loading v2...</div></div>
<script>
let tab='market',data={};
const fmt=n=>'$'+Math.abs(n).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});
const pct=n=>(n>=0?'+':'')+n.toFixed(2)+'%';
const mom=n=>n===0?'—':(n>=0?'+':'')+n.toFixed(2)+'%';
async function load(){try{const r=await fetch('/api');data=await r.json();draw();}catch(e){}}
function draw(){
  if(!data.prices)return;
  const total=(data.cash||0)+(data.pos_value||0),pnl=total-1000,pnlP=pnl/1000*100;
  const wins=(data.trades||[]).filter(t=>t.pnl>0).length,wr=data.trades?.length?Math.round(wins/data.trades.length*100):0;
  const pc=pnl>=0?'g':'r',live=data.status==='live',cc=live?'#00C896':data.status==='error'?'#EF4444':'#F5A623';
  let h=`<div class="banner" style="background:${cc}15;border-bottom:1px solid ${cc}40;color:${cc}">
    <div><span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${cc};margin-right:6px"></span>
    ${live?(data.paper?'PAPER TRADING · Alpaca':'LIVE TRADING · Alpaca'):'CONNECTING...'}</div>
    <span class="m">v2 · cycle #${data.cycles||0}</span></div>
  <div class="head"><div class="brand">YISROEL & YAKOV</div><div class="stats">
    <div class="stat"><div class="sl">TOTAL VALUE</div><div class="sv ${pc}">${fmt(total)}</div><div class="ss">started $1,000</div></div>
    <div class="stat"><div class="sl">P & L</div><div class="sv ${pc}">${pnl>=0?'+':''}${fmt(pnl)}</div><div class="ss ${pc}">${pct(pnlP)}</div></div>
    <div class="stat"><div class="sl">WIN RATE</div><div class="sv ${data.trades?.length?'g':'m'}">${wr}%</div><div class="ss">${data.trades?.length||0} trades</div></div>
  </div></div>
  <div class="tabs">${['market','trades','hunter','log'].map(t=>`<button class="tab${tab===t?' on':''}" onclick="go('${t}')">${t.toUpperCase()}</button>`).join('')}</div>
  <div class="body">`;
  if(tab==='market'){
    h+=`<div class="lbl" style="margin-top:0">LIVE PRICES + MOMENTUM</div>`;
    ['BTC','ETH','SOL'].forEach(id=>{
      const pr=data.prices?.[id],pos=data.positions?.[id],sig=data.signals?.[id];
      if(!pr)return;
      const hasSignal=sig?.hunter;
      h+=`<div class="card" style="${hasSignal?'border-color:#FF6B3560':pos?'border-color:#00C89660':''}">
        <div class="ch"><div style="display:flex;align-items:center;gap:8px">
          <span style="font-size:15px;font-weight:bold">${id}</span>
          ${hasSignal?`<span class="badge" style="background:#FF6B3520;color:#FF6B35">🎯 SIGNAL</span>`:''}
          ${pos?`<span class="badge" style="background:${pos.mode==='hunter'?'#FF6B3520':'#00C89620'};color:${pos.mode==='hunter'?'#FF6B35':'#00C896'}">${pos.mode.toUpperCase()}</span>`:''}
        </div><span class="v">$${pr.price?.toLocaleString('en-US',{maximumFractionDigits:2})}</span></div>
        <div class="kv"><span class="k">1h momentum</span><span class="v ${(pr.mom_1h||0)>=0?'g':'r'}">${mom(pr.mom_1h||0)}</span></div>
        <div class="kv"><span class="k">15m momentum</span><span class="v ${(pr.mom_15m||0)>=0?'g':'r'}">${mom(pr.mom_15m||0)}</span></div>
        ${hasSignal?`<div class="kv"><span class="k">Signal</span><span class="v o">${sig.reason}</span></div>`:''}`;
      if(pos)h+=`<div class="hr"></div>
        <div class="kv"><span class="k">Entry</span><span class="v">$${pos.entry?.toLocaleString('en-US',{maximumFractionDigits:2})}</span></div>
        <div class="kv"><span class="k">Trailing stop</span><span class="v r">$${pos.stop?.toFixed(2)}</span></div>
        <div class="kv"><span class="k">P&L</span><span class="v ${pos.pnl>=0?'g':'r'}">${pos.pnl>=0?'+':''}${fmt(pos.pnl||0)}</span></div>`;
      h+=`</div>`;
    });
    h+=`<div class="card"><div class="kv"><span class="k">Cash (Alpaca)</span><span class="v">${fmt(data.cash||0)}</span></div>
      <div class="kv"><span class="k">In positions</span><span class="v">${fmt(data.pos_value||0)}</span></div>
      <div class="kv"><span class="k">Mode</span><span class="v ${data.paper?'g':'o'}">${data.paper?'● PAPER':'● LIVE'}</span></div></div>`;
  }
  if(tab==='trades'){
    const op=Object.entries(data.positions||{});
    if(op.length){h+=`<div class="lbl" style="margin-top:0">OPEN POSITIONS</div>`;
      op.forEach(([lbl,pos])=>{h+=`<div class="card" style="border-color:${pos.mode==='hunter'?'#FF6B3560':'#00C89640'}">
        <div class="ch"><div style="display:flex;gap:8px;align-items:center"><span style="font-size:14px;font-weight:bold">${lbl}</span>
          <span class="badge" style="background:${pos.mode==='hunter'?'#FF6B3520':'#00C89620'};color:${pos.mode==='hunter'?'#FF6B35':'#00C896'}">${pos.mode?.toUpperCase()}</span>
        </div><span class="sv ${pos.pnl>=0?'g':'r'}" style="font-size:11px">${pos.pnl>=0?'+':''}${fmt(pos.pnl||0)}</span></div>
        <div class="kv"><span class="k">Entry</span><span class="v">$${pos.entry?.toLocaleString('en-US',{maximumFractionDigits:2})}</span></div>
        <div class="kv"><span class="k">Current</span><span class="v ${pos.pnl>=0?'g':'r'}">$${pos.current?.toLocaleString('en-US',{maximumFractionDigits:2})}</span></div>
        <div class="kv"><span class="k">Trailing stop</span><span class="v r">$${pos.stop?.toFixed(2)}</span></div>
        <div class="kv"><span class="k">Return</span><span class="v ${pos.pnl>=0?'g':'r'}">${pct(pos.pnl_pct||0)}</span></div>
      </div>`;});}
    h+=`<div class="lbl" style="margin-top:${op.length?'12px':'0'}">CLOSED TRADES</div>`;
    if(!data.trades?.length)h+=`<div class="empty">No closed trades yet</div>`;
    else data.trades.slice(0,15).forEach(t=>{h+=`<div class="card">
      <div class="ch"><div style="display:flex;gap:8px;align-items:center"><span style="font-size:12px;font-weight:bold">${t.asset}</span>
        <span class="badge" style="background:${t.mode==='hunter'?'#FF6B3520':'#00C89620'};color:${t.mode==='hunter'?'#FF6B35':'#00C896'}">${(t.mode||'standard').toUpperCase()}</span>
      </div><span class="sv ${t.pnl>=0?'g':'r'}" style="font-size:11px">${t.pnl>=0?'+':''}${fmt(t.pnl)}</span></div>
      <div class="kv"><span class="k">Entry → Exit</span><span class="v m">$${t.entry?.toLocaleString()} → $${t.exit?.toLocaleString()}</span></div>
      <div class="kv"><span class="k">Return</span><span class="v ${t.pnl>=0?'g':'r'}">${pct(t.pnl_pct||0)}</span></div>
      <div style="font-size:8px;color:#5A6478;margin-top:3px">${t.ts} UTC</div></div>`;});
  }
  if(tab==='hunter'){
    const sigs=data.signals||{},anyActive=Object.values(sigs).some(s=>s.hunter);
    h+=`<div class="card" style="border-color:${anyActive?'#FF6B35':'#1C2333'}">
      <div class="ch"><span style="font-size:13px;font-weight:bold;color:#FF6B35">🎯 Hunter Mode</span>
        <span style="font-size:8px;color:${anyActive?'#FF6B35':'#5A6478'}">${anyActive?'● SIGNAL ACTIVE':'● WATCHING'}</span></div>
      <div style="font-size:9px;color:#5A6478;line-height:1.7;margin-bottom:10px">Real signals from live price history. Fires on 1h momentum >4%, volatility spikes, or accelerating moves.</div>
      ${[['1h momentum threshold','>4% triggers signal'],['Volatility spike','2.5x normal 15m move'],['Acceleration','1h >2% + 15m >1.5% same dir'],['Position size','30% single / 40% double confirmed'],['Trailing stop','2x wider than standard']].map(([k,v])=>`<div class="kv"><span class="k">${k}</span><span class="v o">${v}</span></div>`).join('')}
    </div>
    <div class="lbl">LIVE SIGNAL STATUS</div>
    <div class="card" style="padding:0 13px">
      ${['BTC','ETH','SOL'].map(lbl=>{const s=sigs[lbl]||{},active=s.hunter;
        return `<div class="sig"><div style="display:flex;justify-content:space-between;align-items:center">
          <span class="sn">${lbl}</span><span class="st ${active?'on':''}">${active?'🎯 ACTIVE':'WATCHING'}</span></div>
          <div class="sd">${active?s.reason:`1h: ${mom(s.mom_1h||0)} | 15m: ${mom(s.mom_15m||0)}`}</div></div>`;}).join('')}
    </div>`;
  }
  if(tab==='log'){
    h+=`<div class="lbl" style="margin-top:0">AGENT LOG (UTC)</div>`;
    if(!data.log?.length)h+=`<div class="empty">Starting up...</div>`;
    else data.log.forEach(e=>{
      const c=e.type==='win'?'#00C896':e.type==='loss'?'#EF4444':e.type==='hunter'?'#FF6B35':e.type==='buy'?'#00C896':'#5A6478';
      h+=`<div class="le" style="color:${c}"><span class="lt">${e.ts}</span>${e.msg}</div>`;
    });
  }
  h+='</div>';document.getElementById('app').innerHTML=h;
}
function go(t){tab=t;draw();}
window.go=go;
load();setInterval(load,30000);
</script></body></html>"""
 
if __name__ == "__main__":
    t = threading.Thread(target=trading_loop, daemon=True)
    t.start()
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=False)

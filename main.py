# ═══════════════════════════════════════════════════════════════════
# YISROEL & YAKOV — AI TRADING AGENT
# Deploy on Replit · Paper trading first · One config change to go live
# ═══════════════════════════════════════════════════════════════════

import os, time, json, threading, requests
from datetime import datetime
from flask import Flask, jsonify, render_template_string
import anthropic

# ── CONFIG (change PAPER = False to go live) ─────────────────────────
PAPER        = os.environ.get("PAPER_MODE", "true").lower() == "true"
ALPACA_KEY   = os.environ.get("ALPACA_KEY", "")
ALPACA_SEC   = os.environ.get("ALPACA_SECRET", "")
CLAUDE_KEY   = os.environ.get("ANTHROPIC_KEY", "")

BASE    = "https://paper-api.alpaca.markets" if PAPER else "https://api.alpaca.markets"
HEADS   = {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SEC, "Content-Type": "application/json"}
START   = 1000.0

ASSETS = [
    {"cg":"bitcoin",  "sym":"BTCUSD", "label":"BTC", "atr":0.033},
    {"cg":"ethereum", "sym":"ETHUSD", "label":"ETH", "atr":0.045},
    {"cg":"solana",   "sym":"SOLUSD", "label":"SOL", "atr":0.065},
]

STD_PCT    = 0.20
HUNT_PCT   = 0.30
HUNT2_PCT  = 0.40
STD_ATR    = 1.5
HUNT_ATR   = 2.0

S = {
    "positions": {},
    "trades":    [],
    "log":       [],
    "prices":    {},
    "last_ai":   0,
    "cash":      0.0,
    "status":    "connecting",
    "cycles":    0,
}

def log(msg, t="info"):
    e = {"msg":msg, "type":t, "ts":datetime.now().strftime("%H:%M:%S")}
    S["log"] = [e] + S["log"][:59]
    print(f"[{e['ts']}] {msg}")

def get_prices():
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price"
            "?ids=bitcoin,ethereum,solana&vs_currencies=usd&include_24hr_change=true",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10
        )
        if r.ok:
            d = r.json()
            prices = {}
            for a in ASSETS:
                prices[a["label"]] = {
                    "price":  d[a["cg"]]["usd"],
                    "chg24h": round(d[a["cg"]].get("usd_24h_change", 0), 2),
                }
            S["prices"] = prices
            S["status"] = "live"
            S["cycles"] += 1
            return prices
    except Exception as e:
        log(f"Price feed error: {e}", "error")

    try:
        log("Falling back to Coinbase price feed", "info")
        prices = {}
        for a in ASSETS:
            cb_pair = a["label"][:3] + "-USD"
            spot = requests.get(
                f"https://api.coinbase.com/v2/prices/{cb_pair}/spot",
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=10
            )
            yday = requests.get(
                f"https://api.coinbase.com/v2/prices/{cb_pair}/spot?date="
                f"{datetime.utcnow().date().isoformat()}",
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=10
            )
            if not spot.ok:
                raise RuntimeError(f"Coinbase {cb_pair} failed")
            price = float(spot.json()["data"]["amount"])
            chg = 0.0
            if yday.ok:
                try:
                    y = float(yday.json()["data"]["amount"])
                    if y:
                        chg = round((price - y) / y * 100, 2)
                except Exception:
                    pass
            prices[a["label"]] = {"price": price, "chg24h": chg}
        S["prices"] = prices
        S["status"] = "live"
        S["cycles"] += 1
        return prices
    except Exception as e:
        log(f"Coinbase fallback error: {e}", "error")

    S["status"] = "error"
    return None

def calc_atr(label):
    a = next(x for x in ASSETS if x["label"] == label)
    return S["prices"].get(label, {}).get("price", 0) * a["atr"]

def get_cash():
    try:
        r = requests.get(f"{BASE}/v2/account", headers=HEADS, timeout=10)
        if r.ok:
            d = r.json()
            cash = float(d.get("cash", 0))
            S["cash"] = cash
            return cash
    except Exception as e:
        log(f"Account error: {e}", "error")
    return S.get("cash", START)

def buy(sym, notional):
    try:
        payload = {
            "symbol":        sym,
            "notional":      str(round(notional, 2)),
            "side":          "buy",
            "type":          "market",
            "time_in_force": "gtc",
        }
        r = requests.post(f"{BASE}/v2/orders", headers=HEADS, json=payload, timeout=10)
        if r.ok:
            return r.json()
        else:
            log(f"Order rejected: {r.text[:80]}", "error")
    except Exception as e:
        log(f"Order error: {e}", "error")
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
        atr  = calc_atr(label)
        mult = HUNT_ATR if pos["mode"] == "hunter" else STD_ATR
        new_stop = cur - atr * mult
        pos["stop"] = max(pos["stop"], new_stop)
        be = 1.08 if pos["mode"] == "hunter" else 1.05
        if cur >= pos["entry"] * be and not pos.get("be"):
            pos["stop"] = max(pos["stop"], pos["entry"])
            pos["be"] = True
            log(f"⚓ Break-even set {label}", "info")
        if cur >= pos["entry"] * 1.20 and not pos.get("so"):
            pos["so"] = True
            profit = (cur - pos["entry"]) * pos["qty"] * 0.30
            log(f"📊 SCALE OUT {label} 30% at +20% → +${profit:.2f} locked", "win")
        if cur <= pos["stop"]:
            asset = next(a for a in ASSETS if a["label"] == label)
            ok = sell_all(asset["sym"])
            if ok:
                pnl  = (cur - pos["entry"]) * pos["qty"]
                pnlP = (cur - pos["entry"]) / pos["entry"] * 100
                S["trades"] = [{
                    "asset": label, "mode": pos["mode"],
                    "entry": pos["entry"], "exit": cur,
                    "pnl": pnl, "pnl_pct": pnlP,
                    "ts": datetime.now().strftime("%H:%M"),
                }] + S["trades"][:49]
                del S["positions"][label]
                emoji = "✅" if pnl >= 0 else "🔴"
                log(f"{emoji} STOP HIT {label} @${cur:,.2f} → {'+'if pnl>=0 else ''}${pnl:.2f} ({pnlP:+.2f}%)",
                    "win" if pnl >= 0 else "loss")

def ai_decide(cash):
    now = time.time()
    if now - S["last_ai"] < 180:
        return
    if not S["prices"]:
        return
    S["last_ai"] = now
    try:
        client = anthropic.Anthropic(api_key=CLAUDE_KEY)
        mkt = "\n".join(
            f"{a['label']}: ${S['prices'][a['label']]['price']:,.2f} "
            f"| 24h: {S['prices'][a['label']]['chg24h']:+.2f}% "
            f"| ATR: ${calc_atr(a['label']):.0f}"
            for a in ASSETS if a["label"] in S["prices"]
        )
        open_pos = "\n".join(
            f"{lbl}: entry ${p['entry']:,.2f} now ${S['prices'].get(lbl,{}).get('price',0):,.2f} "
            f"stop ${p['stop']:.0f} [{p['mode']}]"
            for lbl, p in S["positions"].items()
        ) or "None"
        prompt = f"""Crypto trading agent. Paper mode: {PAPER}

ACCOUNT: Cash ${cash:.2f} | Positions: {len(S['positions'])}/2 max

MARKET:
{mkt}

OPEN POSITIONS:
{open_pos}

RULES: BUY if 24h momentum >3% and not already in asset. Hunter if >8% or strong signals (30%, 40% double-confirmed). Standard=20%. Max 2 positions.

JSON only: {{"action":"BUY or HOLD","asset":"BTC or ETH or SOL or null","mode":"standard or hunter or null","confidence":7,"reason":"8 words max","hunter_signal":"SHORT_SQUEEZE or MOMENTUM or GAP or LOW_FLOAT or NEWS or null","double_confirmed":false}}"""

        msg = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=120,
            system="Trading agent. JSON only. No markdown.",
            messages=[{"role":"user","content":prompt}]
        )
        raw = msg.content[0].text.strip().replace("```json","").replace("```","")
        dec = json.loads(raw)
        if dec.get("action") == "BUY" and dec.get("asset") and len(S["positions"]) < 2:
            label = dec["asset"]
            asset = next((a for a in ASSETS if a["label"] == label), None)
            if asset and label not in S["positions"] and label in S["prices"]:
                ih      = dec.get("mode") == "hunter"
                pct_sz  = HUNT2_PCT if (ih and dec.get("double_confirmed")) else (HUNT_PCT if ih else STD_PCT)
                notional= cash * pct_sz
                if notional > 10:
                    price  = S["prices"][label]["price"]
                    atr    = calc_atr(label)
                    stop   = price - atr * (HUNT_ATR if ih else STD_ATR)
                    qty    = notional / price
                    order  = buy(asset["sym"], notional)
                    if order:
                        S["positions"][label] = {
                            "entry": price, "stop": stop, "qty": qty,
                            "cost": notional, "mode": "hunter" if ih else "standard",
                            "so": False, "be": False,
                        }
                        sz = ("40%" if dec.get("double_confirmed") else "30%") if ih else "20%"
                        log(f"{'🎯 HUNTER' if ih else '📈 STANDARD'} BUY {label} @${price:,.2f} | {sz} | stop ${stop:.0f} | {dec.get('reason','')}", "hunter" if ih else "buy")
        else:
            log(f"💭 HOLD — {dec.get('reason','no setup')} ({dec.get('confidence',7)}/10)", "info")
    except Exception as e:
        log(f"AI error: {str(e)[:60]}", "error")

def trading_loop():
    log(f"🚀 Agent started — {'PAPER' if PAPER else 'LIVE'} trading mode")
    log(f"📡 Alpaca {'paper' if PAPER else 'live'} API connected")
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
        pos_out[lbl] = {**pos, "current": cur, "pnl": pnl, "pnl_pct": pnl / pos["cost"] * 100}
    total_pos = sum(
        p["qty"] * prices.get(lbl, {}).get("price", p["entry"])
        for lbl, p in S["positions"].items()
    )
    return jsonify({
        "prices": prices, "positions": pos_out,
        "trades": S["trades"][:20], "log": S["log"][:30],
        "cash": S["cash"], "pos_value": total_pos,
        "paper": PAPER, "status": S["status"], "cycles": S["cycles"],
    })

HTML = """<!DOCTYPE html>
<html><head>
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Y&Y Agent</title>
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
</style></head>
<body><div id="app"><div style="text-align:center;padding:60px 20px;color:#5A6478;font-size:11px">Loading...</div></div>
<script>
let tab='market',data={};
const $=n=>'$'+Math.abs(n).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});
const p=n=>(n>=0?'+':'')+n.toFixed(2)+'%';
async function load(){try{const r=await fetch('/api');data=await r.json();draw();}catch(e){}}
function draw(){
  if(!data.prices)return;
  const total=(data.cash||0)+(data.pos_value||0),pnl=total-1000,pnlP=pnl/1000*100;
  const wins=(data.trades||[]).filter(t=>t.pnl>0).length,wr=data.trades?.length?Math.round(wins/data.trades.length*100):0;
  const pc=pnl>=0?'g':'r',live=data.status==='live',cc=live?'#00C896':data.status==='error'?'#EF4444':'#F5A623';
  let h=`<div class="banner" style="background:${cc}15;border-bottom:1px solid ${cc}40;color:${cc}">
    <div><span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${cc};margin-right:6px"></span>
    ${live?(data.paper?'PAPER TRADING · Alpaca':'LIVE TRADING · Alpaca'):'CONNECTING...'}</div>
    <span class="m">cycle #${data.cycles||0}</span></div>
  <div class="head"><div class="brand">YISROEL & YAKOV</div><div class="stats">
    <div class="stat"><div class="sl">TOTAL VALUE</div><div class="sv ${pc}">${$(total)}</div><div class="ss">started $1,000</div></div>
    <div class="stat"><div class="sl">P & L</div><div class="sv ${pc}">${pnl>=0?'+':''}${$(pnl)}</div><div class="ss ${pc}">${p(pnlP)}</div></div>
    <div class="stat"><div class="sl">WIN RATE</div><div class="sv ${data.trades?.length?'g':'m'}">${wr}%</div><div class="ss">${data.trades?.length||0} trades</div></div>
  </div></div>
  <div class="tabs">${['market','trades','hunter','log'].map(t=>`<button class="tab${tab===t?' on':''}" onclick="go('${t}')">${t.toUpperCase()}</button>`).join('')}</div>
  <div class="body">`;
  if(tab==='market'){
    h+=`<div class="lbl" style="margin-top:0">LIVE PRICES</div>`;
    ['BTC','ETH','SOL'].forEach(id=>{
      const pr=data.prices?.[id],pos=data.positions?.[id];if(!pr)return;
      h+=`<div class="card" style="${pos?'border-color:#FF6B3560':''}"><div class="ch">
        <div style="display:flex;align-items:center;gap:8px"><span style="font-size:15px;font-weight:bold">${id}</span>
        ${pos?`<span class="badge" style="background:${pos.mode==='hunter'?'#FF6B3520':'#00C89620'};color:${pos.mode==='hunter'?'#FF6B35':'#00C896'}">${pos.mode.toUpperCase()}</span>`:''}</div>
        <span class="sv ${pr.chg24h>=0?'g':'r'}" style="font-size:11px">${p(pr.chg24h||0)}</span></div>
        <div class="kv"><span class="k">Price</span><span class="v">$${pr.price?.toLocaleString('en-US',{maximumFractionDigits:2})}</span></div>`;
      if(pos)h+=`<div class="hr"></div>
        <div class="kv"><span class="k">Entry</span><span class="v">$${pos.entry?.toLocaleString('en-US',{maximumFractionDigits:2})}</span></div>
        <div class="kv"><span class="k">ATR stop</span><span class="v r">$${pos.stop?.toFixed(2)}</span></div>
        <div class="kv"><span class="k">P&L</span><span class="v ${pos.pnl>=0?'g':'r'}">${pos.pnl>=0?'+':''}${$(pos.pnl||0)}</span></div>`;
      h+=`</div>`;
    });
    h+=`<div class="card">
      <div class="kv"><span class="k">Cash (Alpaca)</span><span class="v">${$(data.cash||0)}</span></div>
      <div class="kv"><span class="k">In positions</span><span class="v">${$(data.pos_value||0)}</span></div>
      <div class="kv"><span class="k">Mode</span><span class="v ${data.paper?'g':'o'}">${data.paper?'● PAPER':'● LIVE'}</span></div>
    </div>`;
  }
  if(tab==='trades'){
    const op=Object.entries(data.positions||{});
    if(op.length){h+=`<div class="lbl" style="margin-top:0">OPEN POSITIONS</div>`;
      op.forEach(([lbl,pos])=>{h+=`<div class="card" style="border-color:${pos.mode==='hunter'?'#FF6B3560':'#1C2333'}">
        <div class="ch"><div style="display:flex;gap:8px;align-items:center"><span style="font-size:14px;font-weight:bold">${lbl}</span>
          <span class="badge" style="background:${pos.mode==='hunter'?'#FF6B3520':'#00C89620'};color:${pos.mode==='hunter'?'#FF6B35':'#00C896'}">${pos.mode?.toUpperCase()}</span></div>
          <span class="sv ${pos.pnl>=0?'g':'r'}" style="font-size:11px">${pos.pnl>=0?'+':''}${$(pos.pnl||0)}</span></div>
        <div class="kv"><span class="k">Entry</span><span class="v">$${pos.entry?.toLocaleString('en-US',{maximumFractionDigits:2})}</span></div>
        <div class="kv"><span class="k">Current</span><span class="v ${pos.pnl>=0?'g':'r'}">$${pos.current?.toLocaleString('en-US',{maximumFractionDigits:2})}</span></div>
        <div class="kv"><span class="k">ATR stop</span><span class="v r">$${pos.stop?.toFixed(2)}</span></div>
        <div class="kv"><span class="k">Return</span><span class="v ${pos.pnl>=0?'g':'r'}">${p(pos.pnl_pct||0)}</span></div>
      </div>`;});}
    h+=`<div class="lbl" style="margin-top:${op.length?'12px':'0'}">CLOSED TRADES</div>`;
    if(!data.trades?.length)h+=`<div class="empty">No closed trades yet</div>`;
    else data.trades.slice(0,15).forEach(t=>{h+=`<div class="card">
      <div class="ch"><div style="display:flex;gap:8px;align-items:center"><span style="font-size:12px;font-weight:bold">${t.asset}</span>
        <span class="badge" style="background:${t.mode==='hunter'?'#FF6B3520':'#00C89620'};color:${t.mode==='hunter'?'#FF6B35':'#00C896'}">${(t.mode||'standard').toUpperCase()}</span></div>
        <span class="sv ${t.pnl>=0?'g':'r'}" style="font-size:11px">${t.pnl>=0?'+':''}${$(t.pnl)}</span></div>
      <div class="kv"><span class="k">Entry → Exit</span><span class="v m">$${t.entry?.toLocaleString()} → $${t.exit?.toLocaleString()}</span></div>
      <div class="kv"><span class="k">Return</span><span class="v ${t.pnl>=0?'g':'r'}">${p(t.pnl_pct||0)}</span></div>
      <div style="font-size:8px;color:#5A6478;margin-top:3px">${t.ts}</div>
    </div>`;});
  }
  if(tab==='hunter'){
    h+=`<div class="card" style="border-color:#FF6B3550">
      <div class="ch"><span style="font-size:13px;font-weight:bold;color:#FF6B35">🎯 Hunter Mode</span><span style="font-size:8px;color:#00C896">● ACTIVE</span></div>
      <div style="font-size:9px;color:#5A6478;line-height:1.7;margin-bottom:10px">Explosive move scanner. ATR stop on every trade.</div>
      ${[['Single signal','30%','#FF6B35'],['2+ confirmed','40%','#EF4444'],['ATR stop','2x wider','#5A6478'],['Break-even','+8%','#5A6478'],['Options','Top trades','#F5A623']].map(([k,v,c])=>`<div class="kv"><span class="k">${k}</span><span style="font-size:9px;font-weight:bold;color:${c}">${v}</span></div>`).join('')}
    </div>
    <div class="lbl">SCANNERS</div><div class="card" style="padding:0 13px">
      ${[['01','Short Squeeze','Float >20% shorted + catalyst'],['02','Pre-Market Gap','Gap >4% + volume'],['03','Crypto Funding','Extreme negative funding'],['04','Low Float','Float <10M + spike'],['05','News','FDA / earnings / listing']].map(([n,nm,d2])=>`<div class="sig"><div style="display:flex;justify-content:space-between"><span class="sn">${n} · ${nm}</span><span class="st">WATCHING</span></div><div class="sd">${d2}</div></div>`).join('')}
    </div>`;
  }
  if(tab==='log'){
    h+=`<div class="lbl" style="margin-top:0">AGENT LOG</div>`;
    if(!data.log?.length)h+=`<div class="empty">Starting up...</div>`;
    else data.log.forEach(e=>{
      const c=e.type==='win'?'#00C896':e.type==='loss'?'#EF4444':e.type==='hunter'?'#FF6B35':e.type==='buy'?'#00C896':'#5A6478';
      h+=`<div class="le" style="color:${c}"><span class="lt">${e.ts}</span>${e.msg}</div>`;
    });
  }
  h+='</div>';
  document.getElementById('app').innerHTML=h;
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

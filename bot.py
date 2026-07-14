#!/usr/bin/env python3
"""
HyperStats -> Telegram Bot (ereignis-gesteuert)

Meldet:
  1) Wal oeffnet / schliesst / dreht eine grosse Position (BTC, ETH, HYPE)
  2) Long/Short-Bias kippt (Long <-> Short)
  3) Grosse Liquidationen (aus dem HyperStats-Alerts-Feed)
  4) Zusaetzlich alle ~30 Min ein kompakter Ueberblick

Der Bot merkt sich den letzten Stand in state.json (wird vom GitHub-Workflow
automatisch zurueck ins Repo gespeichert). Beim allerersten Lauf wird nur die
Ausgangslage gespeichert - es kommt keine Alert-Flut.

Umgebungsvariablen (GitHub-Secrets):
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""

import os
import json
import html
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import requests

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Europe/Berlin")
except Exception:
    TZ = None

HL_API = "https://api.hyperliquid.xyz/info"
ALERTS_RSS = "https://hyperstats.org/feeds/alerts.xml"
TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TG_CHAT = os.environ["TELEGRAM_CHAT_ID"]
STATE_FILE = "state.json"

# ---- Einstellungen (nach Belieben anpassen) -------------------------------
COINS = ["BTC", "ETH", "HYPE"]
BIG_POSITION_USD = 500_000     # ab dieser Groesse gilt eine Position als "gross"
DIGEST_EVERY_MIN = 30          # Abstand des kompakten Ueberblicks
MAX_SEEN = 300                 # wie viele Liquidations-IDs gemerkt werden

# ---- Wale-Liste (heutige HyperStats-Top-Adressen, editierbar) -------------
WHALES = [
    "0x0ddf9bae2af4b874b96d287a5ad42eb47138a902",
    "0xd6e56265890b76413d1d527eb9b75e334c0c5b42",
    "0x7fba7e745bd97f828c824589680749b55a8c04ab",
    "0x5b5d51203a0f9079f8aeb098a6523a13f298c060",
    "0x152e41f0b83e6cad4b5dc730c1d6279b7d67c9dc",
    "0xa1830e8d9f019feb448478a171bb37cc6c4c0482",
    "0x218a65e21eddeece7a9df38c6bbdd89f692b7da2",
    "0x32008fcb6bbd16532afc83ca8b6c920dde22c407",
    "0xcf5343ba750a6e30afbb1dadda08bc78f8c8ee11",
    "0x3440f23a87f1950e7a88cd248fd270e92d1132c5",
    "0xa312114b5795dff9b8db50474dd57701aa78ad1e",
    "0x8af700ba841f30e0a3fcb0ee4c4a9d223e1efa05",
    "0xad227f63d34e7251c1d0ab65e64eeea07aee4e44",
    "0x6859da14835424957a1e6b397d8026b1d9ff7e1e",
    "0xac487c027ffe32021bbba77e30786f8c8f353201",
    "0x66f889094739dbb7d20aa60f645acd88feba75a9",
    "0x6daec5ff434924e0839358e710e6ae5f158590de",
    "0xd21d931890d27b6e7e2e668f27931e17698e90f1",
]


# ---------------------------------------------------------------------------
def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f), True
    except (FileNotFoundError, json.JSONDecodeError):
        return {"positions": {}, "bias": {}, "seen_alerts": [], "last_digest": None}, False


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def hl_state(addr):
    r = requests.post(HL_API, json={"type": "clearinghouseState", "user": addr}, timeout=20)
    r.raise_for_status()
    return r.json()


def fmt_usd(x):
    ax = abs(x)
    sign = "-" if x < 0 else ""
    if ax >= 1e9:
        return f"{sign}${ax/1e9:.2f}B"
    if ax >= 1e6:
        return f"{sign}${ax/1e6:.2f}M"
    if ax >= 1e3:
        return f"{sign}${ax/1e3:.1f}K"
    return f"{sign}${ax:.0f}"


def short_addr(a):
    return a[:6] + "\u2026" + a[-4:]


def snapshot():
    """Aktuelle Positionen je Wal + aggregiertes Long/Short je Coin."""
    long_not = {c: 0.0 for c in COINS}
    short_not = {c: 0.0 for c in COINS}
    positions = {}
    whales = []

    for addr in WHALES:
        try:
            st = hl_state(addr)
        except Exception as e:
            print(f"skip {addr}: {e}")
            continue

        acct = float(st.get("marginSummary", {}).get("accountValue", 0) or 0)
        pos = {}
        main = None
        main_val = 0.0
        tot_upnl = 0.0

        for ap in st.get("assetPositions", []):
            p = ap.get("position", {})
            coin = p.get("coin")
            szi = float(p.get("szi", 0) or 0)
            pv = abs(float(p.get("positionValue", 0) or 0))
            upnl = float(p.get("unrealizedPnl", 0) or 0)
            tot_upnl += upnl
            side = "Long" if szi > 0 else "Short"

            if szi != 0 and coin in COINS:
                pos[coin] = {"side": side, "notional": pv}
                (long_not if szi > 0 else short_not)[coin] += pv

            if pv > main_val:
                main_val = pv
                main = (coin, side, upnl)

        positions[addr] = pos
        whales.append({"addr": addr, "acct": acct, "upnl": tot_upnl, "main": main})

    whales.sort(key=lambda w: w["acct"], reverse=True)

    bias = {}
    for c in COINS:
        tot = long_not[c] + short_not[c]
        if tot == 0:
            bias[c] = "none"
        else:
            lp = long_not[c] / tot * 100
            bias[c] = "Long" if lp >= 55 else "Short" if lp <= 45 else "neutral"

    return long_not, short_not, positions, bias, whales


def diff_positions(prev, curr):
    alerts = []
    for addr in WHALES:
        pv = prev.get(addr, {})
        cv = curr.get(addr, {})
        sa = short_addr(addr)
        for c in COINS:
            a = pv.get(c)
            b = cv.get(c)
            an = a["notional"] if a else 0.0
            bn = b["notional"] if b else 0.0
            if not a and b and bn >= BIG_POSITION_USD:
                e = "\U0001F7E2" if b["side"] == "Long" else "\U0001F534"
                alerts.append(f"{e} <code>{sa}</code> hat <b>{c} {b['side']}</b> ge\u00f6ffnet ({fmt_usd(bn)})")
            elif a and not b and an >= BIG_POSITION_USD:
                alerts.append(f"\u26AA\uFE0F <code>{sa}</code> hat <b>{c} {a['side']}</b> geschlossen ({fmt_usd(an)})")
            elif a and b and a["side"] != b["side"] and max(an, bn) >= BIG_POSITION_USD:
                e = "\U0001F7E2" if b["side"] == "Long" else "\U0001F534"
                alerts.append(f"{e} <code>{sa}</code> hat <b>{c}</b> von {a['side']} auf {b['side']} gedreht ({fmt_usd(bn)})")
    return alerts


def diff_bias(prev, curr):
    alerts = []
    for c in COINS:
        p = prev.get(c)
        n = curr.get(c)
        if p in ("Long", "Short") and n in ("Long", "Short") and p != n:
            e = "\U0001F7E2" if n == "Long" else "\U0001F534"
            alerts.append(f"{e} <b>{c}</b>-Bias gekippt: {p} \u2192 <b>{n}</b>")
    return alerts


def liquidation_alerts(seen):
    """Neue Liquidations-Meldungen aus dem HyperStats-Alerts-Feed."""
    out = []
    current_ids = []
    try:
        r = requests.get(ALERTS_RSS, timeout=20)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            guid = (item.findtext("guid") or item.findtext("link") or title).strip()
            desc = (item.findtext("description") or "").strip()
            if "liquidat" not in (title + " " + desc).lower():
                continue
            current_ids.append(guid)
            if guid not in seen:
                out.append("\U0001F4A5 " + html.escape(title))
    except Exception as e:
        print(f"rss error: {e}")
    return out, current_ids


def market_ctx():
    """Open Interest, 24h-Volumen und Funding je Coin (Hyperliquid)."""
    out = {}
    try:
        r = requests.post(HL_API, json={"type": "metaAndAssetCtxs"}, timeout=20)
        r.raise_for_status()
        meta, ctxs = r.json()
        idx = {a["name"]: i for i, a in enumerate(meta.get("universe", []))}
        for c in COINS:
            if c in idx and idx[c] < len(ctxs):
                ctx = ctxs[idx[c]]
                mark = float(ctx.get("markPx", 0) or 0)
                out[c] = {
                    "oi": float(ctx.get("openInterest", 0) or 0) * mark,
                    "vol": float(ctx.get("dayNtlVlm", 0) or 0),
                    "funding": float(ctx.get("funding", 0) or 0) * 100,  # % pro Stunde
                }
    except Exception as e:
        print(f"ctx error: {e}")
    return out


def build_digest(long_not, short_not, whales, ctx):
    now = datetime.now(TZ).strftime("%d.%m. %H:%M") if TZ else datetime.utcnow().strftime("%d.%m. %H:%M UTC")
    lines = [f"\U0001F40B <b>HyperStats-\u00dcberblick</b> \u2014 {now}", "", "<b>\U0001F4CA Long/Short (Top-Wale)</b>"]
    for c in COINS:
        L, S = long_not[c], short_not[c]
        tot = L + S
        if tot == 0:
            lines.append(f"\u2022 {c}: keine offene Position")
            continue
        lp = L / tot * 100
        if lp >= 55:
            tag = f"\U0001F7E2 {lp:.0f}% Long"
        elif lp <= 45:
            tag = f"\U0001F534 {100-lp:.0f}% Short"
        else:
            tag = f"\u26AA\uFE0F neutral ({lp:.0f}% Long)"
        lines.append(f"\u2022 {c}: {tag}  (L {fmt_usd(L)} / S {fmt_usd(S)})")

    if ctx:
        lines += ["", "<b>\U0001F4C8 Markt (OI \u00b7 Vol24h \u00b7 Funding/h)</b>"]
        for c in COINS:
            m = ctx.get(c)
            if not m:
                continue
            fsign = "+" if m["funding"] >= 0 else ""
            lines.append(f"\u2022 {c}: OI {fmt_usd(m['oi'])} \u00b7 Vol {fmt_usd(m['vol'])} \u00b7 Funding {fsign}{m['funding']:.4f}%")

    lines += ["", "<b>\U0001F3C6 Gr\u00f6\u00dfte Wale (live)</b>"]
    for i, w in enumerate(whales[:8], 1):
        if w["main"] and w["main"][0]:
            coin, side, _ = w["main"]
            emoji = "\U0001F7E2" if side == "Long" else "\U0001F534"
            main_s = f"{coin} {side} {emoji}"
        else:
            main_s = "keine Position"
        lines.append(f"{i}. <code>{short_addr(w['addr'])}</code> \u00b7 {fmt_usd(w['acct'])} \u00b7 {main_s} \u00b7 uPnL {fmt_usd(w['upnl'])}")

    lines += ["", "<i>Daten: Hyperliquid-API \u00b7 Auswahl: HyperStats Top-Wale</i>"]
    return "\n".join(lines)


def send(text):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    r = requests.post(url, json={
        "chat_id": TG_CHAT, "text": text,
        "parse_mode": "HTML", "disable_web_page_preview": True,
    }, timeout=20)
    print(r.status_code, r.text[:300])
    r.raise_for_status()


def main():
    state, warm = load_state()
    long_not, short_not, positions, bias, whales = snapshot()

    events = []
    if warm:
        events += diff_positions(state.get("positions", {}), positions)
        events += diff_bias(state.get("bias", {}), bias)
        liq, current_ids = liquidation_alerts(set(state.get("seen_alerts", [])))
        events += liq
    else:
        # Kaltstart: nur Ausgangslage merken, keine Alert-Flut
        _, current_ids = liquidation_alerts(set())
        send("\u2705 HyperStats-Bot ist aktiv. Ab jetzt bekommst du Live-Alerts.")

    if events:
        now = datetime.now(TZ).strftime("%H:%M") if TZ else datetime.utcnow().strftime("%H:%M UTC")
        send(f"\u26A1 <b>Wale-Alert</b> \u2014 {now}\n\n" + "\n".join(events))

    # 30-Min-Ueberblick (zeitgesteuert ueber gespeicherten Zeitstempel)
    now_utc = datetime.now(timezone.utc)
    last = state.get("last_digest")
    due = True
    if last:
        try:
            due = (now_utc - datetime.fromisoformat(last)).total_seconds() >= DIGEST_EVERY_MIN * 60 - 60
        except Exception:
            due = True
    if due and whales:
        send(build_digest(long_not, short_not, whales, market_ctx()))
        state["last_digest"] = now_utc.isoformat()

    # State aktualisieren + speichern
    state["positions"] = positions
    state["bias"] = bias
    merged = list(dict.fromkeys(state.get("seen_alerts", []) + current_ids))[-MAX_SEEN:]
    state["seen_alerts"] = merged
    save_state(state)


if __name__ == "__main__":
    main()

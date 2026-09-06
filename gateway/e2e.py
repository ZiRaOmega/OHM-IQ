#!/usr/bin/env python3
"""E2E test suite for the IQ gateway — every feature, REAL pipeline.

Monkeypatches ONLY the network send (last mile) so tests don't spam the chat,
but exercises: state loading, handler routing, every callback button, every
command, view generation, and the send() call path (args, markdown fallback).
Run: python3 e2e_iq_gateway.py
"""
import sys, json, time, importlib, os
sys.path.insert(0, '/home/omega/.hermes/iqbench')

import iq_gateway as gw
importlib.reload(gw)

PASS, FAIL = [], []

# ── fake last-mile: capture what would REALLY be sent to Telegram ──
SENT = []
def fake_tg(method, **kw):
    SENT.append((method, kw))
    if method == "sendMessage":
        return {"ok": True, "result": {"message_id": len(SENT)}}
    return {"ok": True, "result": {}}
gw.tg = fake_tg

OWNER = int(os.environ.get("IQGW_OWNER","0"))
st = json.load(open(os.environ.get("IQGW_STATE", "iq_gateway_state.json")))

def upd_cb(data):
    return {"update_id": int(time.time() * 1000) % 10**9,
            "callback_query": {"id": f"c{len(SENT)}", "data": data,
                               "from": {"id": OWNER},
                               "message": {"chat": {"id": OWNER}}}}

def upd_msg(text):
    return {"update_id": int(time.time() * 1000) % 10**9,
            "message": {"chat": {"id": OWNER}, "from": {"id": OWNER}, "text": text}}

def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"  {'✅' if cond else '❌'} {name}" + (f" — {detail}" if detail and not cond else ""))

def last_text():
    for m, kw in reversed(SENT):
        if m == "sendMessage":
            return kw.get("text", "")
    return ""

# ══ 1. MENU ══
print("══ 1. Menu & commandes ══")
SENT.clear(); gw.handle_update(upd_msg("/start"), st)
check("/start affiche le menu", "IQ RESEARCH" in last_text() or "menu" in last_text().lower())

SENT.clear(); gw.handle_update(upd_cb("menu"), st)
check("bouton menu", "IQ RESEARCH" in last_text() or "pipeline" in last_text().lower())

# ══ 2. SESSIONS ══
print("══ 2. Sessions ══")
SENT.clear(); gw.handle_update(upd_cb("runs"), st)
check("bouton runs liste les sessions", "Sessions" in last_text() or "SESSIONS" in last_text())
sids = list(st.get("sessions", {}).keys())
check(f"state contient des sessions ({len(sids)})", len(sids) > 0)

for sid in sids:
    SENT.clear(); gw.handle_update(upd_cb(f"sess:{sid}"), st)
    ok = len(last_text()) > 30
    check(f"sess:{sid[:40]}", ok, f"text={last_text()[:60]!r}")

# ══ 3. LIVE VIEWS ══
print("══ 3. Live ══")
for sid in sids:
    SENT.clear(); gw.handle_update(upd_cb(f"live:{sid}:0"), st)
    t = last_text()
    check(f"live:{sid[:40]}", len(t) > 30, f"vide: {t[:60]!r}")

# ══ 4. RÉSULTATS ══
print("══ 4. Résultats ══")
for sid in sids:
    SENT.clear(); gw.handle_update(upd_cb(f"res:{sid}"), st)
    t = last_text()
    check(f"res:{sid[:40]}", len(t) > 10, f"vide: {t[:60]!r}")

# ══ 5. MÉTA BRAIN ══
print("══ 5. Méta ══")
for sid in [s for s in sids if st["sessions"][s].get("kind") == "meta"]:
    SENT.clear(); gw.handle_update(upd_cb(f"mbrain:{sid}"), st)
    check(f"mbrain:{sid[:40]}", len(last_text()) > 10)

# ══ 6. SÉCURITÉ: non-owner ignoré ══
print("══ 6. Sécurité ══")
SENT.clear()
fake_other = upd_cb("menu")
fake_other["callback_query"]["from"]["id"] = 999999
fake_other["callback_query"]["message"]["chat"]["id"] = 999999
gw.handle_update(fake_other, st)
sent_msgs = [k for m, k in SENT if m == "sendMessage"]
check("non-owner → rien envoyé", len(sent_msgs) == 0)

# ══ 7. send() en plain text (v3: plus de parse_mode → plus de 400 Markdown) ══
print("══ 7. Send plain-text ══")
calls = []
def tg_capture(method, **kw):
    calls.append(kw)
    return {"ok": True, "result": {"message_id": 1}}
gw.tg = tg_capture
gw.send(OWNER, "text avec _markdown cassé *incomplet")
check("send n'utilise jamais parse_mode", all(c.get("parse_mode") is None for c in calls))
check("send texte préservé tel quel", calls and calls[0].get("text") == "text avec _markdown cassé *incomplet")
gw.tg = fake_tg

# ══ RÉSUMÉ ══
print(f"\n{'='*50}")
print(f"RÉSULTAT: {len(PASS)} pass / {len(FAIL)} fail")
if FAIL:
    print("ÉCHECS:")
    for n, d in FAIL:
        print(f"  ❌ {n} {d}")
    sys.exit(1)
print("TOUS LES TESTS PASSENT ✓")

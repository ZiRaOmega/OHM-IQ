#!/usr/bin/env python3
"""IQ research gateway v3 — exact run paths, safe control, bounded research."""
import json
import os
import secrets
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import gateway_runtime as runtime
import gateway_views as views

IQDIR = Path(__file__).resolve().parent
STATE_F = IQDIR / "iq_gateway_state.json"
OWNER = 6303646457
TOKEN = None

# ── code-staleness watchdog ──────────────────────────────────────────────────
# Rule (workspace hard rule): any patch to a module the gateway imports is
# INVISIBLE to the running process — Python keeps the old bytecode in memory.
# This watchdog fingerprints every loaded module file at startup and warns the
# owner on Telegram the moment one changes on disk, so "forgot to restart"
# can never silently serve stale code again.


def _module_fingerprints():
    import hashlib
    fps = {}
    for name, mod in list(sys.modules.items()):
        path = getattr(mod, "__file__", None)
        if path and path.startswith(str(IQDIR)) and path.endswith(".py"):
            try:
                fps[path] = hashlib.sha256(Path(path).read_bytes()).hexdigest()[:12]
            except OSError:
                pass
    return fps


def check_code_staleness(baseline, chat):
    """Alert once when any loaded module file changes after startup."""
    current = _module_fingerprints()
    changed = [p for p, h in baseline.items() if current.get(p) != h]
    if not changed:
        return baseline
    msg = ("⚠️ CODE STALE — restart requis\n"
           "Des fichiers chargés par le gateway ont été modifiés après son démarrage :\n"
           + "\n".join(f"• {Path(p).name}" for p in sorted(changed))
           + "\n\nLe gateway tourne sur l'ancien code. Règle workspace : kill + relance directe."
           + "\n(vérification automatique à chaque cycle poll)")
    print("[staleness] " + " ".join(Path(p).name for p in changed), flush=True)
    try:
        tg("sendMessage", chat_id=chat, text=msg)
    except Exception:
        pass
    # re-arm with the NEW hashes so the same change is never reported twice
    return {**baseline, **{p: current[p] for p in changed}}
CLIENT = None
MAIN_KB = {"inline_keyboard": [
    [{"text":"🧠 Test raisonnement", "callback_data":"bench"}, {"text":"⚡ Boosters", "callback_data":"boost"}],
    [{"text":"🧬 Méta contrôlé", "callback_data":"meta"}, {"text":"🚀 ARC public", "callback_data":"agi"}],
    [{"text":"🎛 Sessions", "callback_data":"runs"}, {"text":"🏆 Résultats", "callback_data":"lb"}],
    [{"text":"📋 Protocole", "callback_data":"protocol"}, {"text":"💡 Découvertes", "callback_data":"discoveries"}],
    [{"text":"🩺 Diagnostic", "callback_data":"health"}, {"text":"💰 Budget", "callback_data":"settings"}],
    [{"text":"⚙️ Modèle", "callback_data":"models"}]
]}


def load_state():
    return runtime.read_state(STATE_F)


def save_state(st):
    runtime.write_state(STATE_F, st)


def available_models():
    from iq_bench import load_endpoints
    return sorted(name for name, endpoint in load_endpoints().items()
                  if (endpoint[1] if isinstance(endpoint, tuple) else endpoint.get("key")))


def tg(method, **kwargs):
    global TOKEN, CLIENT
    if TOKEN is None:
        TOKEN = (IQDIR / "tg_token.txt").read_text().strip()
    if CLIENT is None:
        CLIENT = httpx.Client(transport=httpx.HTTPTransport(local_address="0.0.0.0", retries=1), timeout=45)
    response = CLIENT.post(f"https://api.telegram.org/bot{TOKEN}/{method}", json=kwargs)
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError("Telegram: " + str(data.get("description", "échec API")))
    return data


def send(chat, text, kb=None):
    # Plain text: raw model output can never break Telegram markup parsing.
    if not str(text).strip():
        text = "Aucune donnée disponible."
    payload = {"chat_id":chat, "text":str(text)[:3800], "disable_web_page_preview":True}
    if kb:
        payload["reply_markup"] = kb
    return tg("sendMessage", **payload)


def senddoc(chat, path, caption=""):
    global TOKEN
    if TOKEN is None:
        TOKEN = (IQDIR/"tg_token.txt").read_text().strip()
    with Path(path).open("rb") as stream, httpx.Client(transport=httpx.HTTPTransport(local_address="0.0.0.0"), timeout=60) as client:
        response = client.post(f"https://api.telegram.org/bot{TOKEN}/sendDocument", data={"chat_id":str(chat), "caption":caption[:900]}, files={"document":(Path(path).name, stream)})
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError("Envoi du document refusé par Telegram")
    return data


def answer_cb(callback_id, text=""):
    return tg("answerCallbackQuery", callback_query_id=callback_id, text=text)


def sess_alive(rec):
    return runtime.is_alive(rec)


def sess_paused(rec):
    return runtime.is_paused(rec)


def sess_live_file(rec):
    return views.live_file(rec)


def live_view(sid, st, offset=0):
    return views.live_view(sid, st["sessions"][sid], offset)


def mbrain_view(sid, st, page=0):
    return views.mbrain_view(sid, st["sessions"][sid], page)


def prompts_view(sid, st, page=0):
    return views.prompts_view(sid, st["sessions"][sid], page)


def res_view(sid, st, page=0):
    return views.result_view(sid, st["sessions"][sid], page)


def help_text(st):
    return (f"★ IQ RESEARCH v3\nModèle : {st.get('model','non choisi')}\nBudget : {st.get('profile','quick')}\n\n"
            "Comparer des méthodes de raisonnement, pas fabriquer un QI humain.\n"
            "🧠 Test : baseline seule. ⚡ Boosters : comparaison contrôlée.\n"
            "🧬 Méta : analyse et proposition testable. 🚀 ARC : corpus public, non certifié.\n"
            "Chaque nouveau run archive protocole, prompts, coûts et validation séparée.\n\n"
            "/bench /boost /meta /agi /runs /leaderboard\n"
            "/protocol /discoveries /health /settings /models\n"
            "Dans Sessions : live intégral, prompts exacts, résultats, pause/reprise/stop/restart.")


def record_status(rec):
    if sess_paused(rec):
        return "⏸️ Pause"
    if sess_alive(rec):
        return "🟢 En cours"
    result = views.load_json(Path(rec["dir"])/"result.json", {})
    if result:
        return "📊 " + str(result.get("status", "résultats présents"))
    if rec.get("stopped"):
        return "⏹️ Arrêté — checkpoint conservé"
    return "⚪ Processus absent — fin non certifiée"


def list_sessions(st, page=0):
    records = sorted(st.get("sessions", {}).items(), key=lambda kv:kv[1].get("started", ""), reverse=True)
    pages = max(1, (len(records)+7)//8)
    page = max(0,min(int(page),pages-1))
    keys = []
    lines = [f"🎛 Sessions — {len(records)} enregistrées — page {page+1}/{pages}"]
    for sid, rec in records[page*8:page*8+8]:
        text = f"{rec['kind']} · {rec['model']} · {record_status(rec)}"
        lines.append(text)
        keys.append([{"text":text[:90], "callback_data":f"sess:{sid}"}])
    nav = []
    if page:
        nav.append({"text":"⬅️", "callback_data":f"runs:{page-1}"})
    if page+1<pages:
        nav.append({"text":"➡️", "callback_data":f"runs:{page+1}"})
    if nav:
        keys.append(nav)
    keys.append([{"text":"🏠 Menu", "callback_data":"menu"}])
    return "\n\n".join(lines), {"inline_keyboard":keys}


def session_view(sid, st):
    rec = st["sessions"][sid]
    rows = views.read_events(sess_live_file(rec))
    txt = (f"🎛 {sid}\n{rec['kind']} · {rec['model']}\n{record_status(rec)}\n"
           f"Événements : {len(rows)}\nDébut : {rec.get('started','?')}\n"
           f"Options : {json.dumps(rec.get('options', {}), ensure_ascii=False)}")
    if rows:
        last = {k:v for k,v in rows[-1].items() if k in ('event','phase','status','candidate','item_id','latency_s')}
        txt += "\nDernier événement : " + json.dumps(last, ensure_ascii=False)
    kb = [[{"text":"🔬 Live intégral", "callback_data":f"live:{sid}:0"}, {"text":"📊 Résultats", "callback_data":f"res:{sid}:0"}],
          [{"text":"🧠 Méta intégral", "callback_data":f"mbrain:{sid}:0"}, {"text":"📝 Prompts exacts", "callback_data":f"prompts:{sid}:0"}],
          [{"text":"⏸️ Pause", "callback_data":f"ctl:pause:{sid}"}, {"text":"▶️ Resume", "callback_data":f"ctl:resume:{sid}"}],
          [{"text":"⏹️ Stop", "callback_data":f"ctl:stop:{sid}"}, {"text":"🔁 Restart", "callback_data":f"ctl:restart:{sid}"}],
          [{"text":"📋 Protocole du run", "callback_data":f"manifest:{sid}:0"}, {"text":"🩺 Diagnostic", "callback_data":"health"}],
          [{"text":"🎛 Sessions", "callback_data":"runs"}, {"text":"🏠 Menu", "callback_data":"menu"}]]
    return txt, {"inline_keyboard":kb}


def start_research(chat, st, kind, *, options=None, directory=None, resume=False):
    model = st.get("model")
    if model not in available_models():
        raise ValueError("Modèle non configuré : choisis un modèle disponible dans ⚙️ Modèle")
    for sid, rec in st["sessions"].items():
        if rec.get("kind") == kind and rec.get("model") == model and sess_alive(rec):
            send(chat, "Session déjà active : double lancement empêché.\n"+sid, session_view(sid,st)[1])
            return sid
    if options is None:
        profile = st.get("profile", "quick")
        if profile not in runtime.PROFILE_BUDGETS:
            raise ValueError("Profil inconnu")
        # Engine budgets (research_core.PROFILES) are the source of truth: max_tokens
        # is the TOTAL run budget, not per-call (gateway used to pass 16000 → runs
        # died budget_exhausted after ~6-10 questions).
        from research_core import PROFILES
        options = {"profile":profile, "seed":secrets.randbelow(2147483647),
                   "max_calls":PROFILES[profile]["max_calls"], "max_tokens":PROFILES[profile]["max_tokens"]}
        if st.get("meta_model"):
            options["meta_model"] = st["meta_model"]
    sid, rec = runtime.spawn_record(IQDIR, kind, model, options, directory, resume)
    st["sessions"][sid] = rec
    save_state(st)
    send(chat, f"★ {kind} lancé : {model}\n{sid}\nPlafond : {options['max_calls']} appels · {options['max_tokens']:,} tokens (budget total du run).\nLe checkpoint est conservé. Aucun gain scientifique affirmé avant validation.".replace(",", " "), session_view(sid,st)[1])
    return sid


def cmd_bench(chat, st):
    return start_research(chat, st, "bench")


def cmd_boost(chat, st):
    return start_research(chat, st, "boost")


def cmd_meta(chat, st):
    return start_research(chat, st, "meta")


def cmd_agi(chat, st):
    return start_research(chat, st, "agi")


def cmd_ctl(chat, sid, st, action):
    rec = st["sessions"][sid]
    alive = sess_alive(rec)
    if action == "pause":
        runtime.signal_record(rec,"pause")
    elif action == "stop":
        if alive:
            runtime.stop_record(rec)
        rec["stopped"] = datetime.now(timezone.utc).isoformat()
    elif action == "resume" and alive:
        runtime.signal_record(rec,"resume")
    elif action in ("resume", "restart"):
        if alive:
            runtime.stop_record(rec)
        if action == "resume":
            result = views.load_json(Path(rec["dir"])/"result.json", {})
            if result.get("status") in ("complete", "completed"):
                return send(chat,"Cette expérience est terminée : aucun nouvel appel. Restart crée une nouvelle expérience.", session_view(sid,st)[1])
            if rec.get("protocol") != "research-v1":
                raise ValueError("Ce run historique n'a pas de checkpoint compatible. Restart démarre un nouveau protocole sans effacer les données.")
        selection = st.get("model")
        st["model"] = rec["model"]
        try:
            new_sid = start_research(chat, st, rec["kind"], options=rec.get("options"), directory=rec["dir"] if action=="resume" else None, resume=action=="resume")
        finally:
            st["model"] = selection
        rec["resumed_as" if action=="resume" else "restarted_as"] = new_sid
        st["sessions"][new_sid]["parent_session"] = sid
    else:
        raise ValueError("Action inconnue")
    save_state(st)
    send(chat, f"Contrôle {action} enregistré. Les données restent sur disque.", session_view(sid,st)[1])


def protocol_text():
    return ("📋 Protocole de recherche\n\n"
            "• Pas de QI humain calibré, pas de preuve d'AGI. ARC = corpus public, contamination possible, non certifié.\n"
            "• Manifeste avant les appels : seed, items, split, prompts exacts + hash, versions et budget.\n"
            "• Optimisation sur TRAIN seulement ; sélection sur validation ; champion figé puis comparé à une baseline sur TEST séparé.\n"
            "• Un seul champion testé. Test final jamais transmis au méta. Baseline et candidat voient les mêmes items et budgets.\n"
            "• Erreur API, réponse vide, troncature, erreur de format et réponse incorrecte sont distinguées.\n"
            "• Scores appariés, intervalles, discordances, latence et tokens. Usage absent = inconnu, pas zéro.\n"
            "• Reprise du checkpoint, sans rejouer les appels terminés. L'efficacité ne se juge pas au seul score.\n"
            "• Les règles du méta restent des hypothèses ; pas de publication automatique non validée.\n\n"
            "Le manifeste de chaque session donne les paramètres effectivement exécutés ; les petits profils sont des tests techniques.")


def cmd_protocol(chat, st):
    return send(chat,protocol_text(),MAIN_KB)


def cmd_settings(chat, st):
    text = "💰 Budget d'expérience\nActuel : " + st.get("profile","quick") + "\n\n"
    text += "\n".join(f"{name} : plafond {calls} appels" for name,calls in runtime.PROFILE_BUDGETS.items())
    text += "\n16 000 tokens max par appel. Durée et coût monétaire inconnus avant exécution. Les appels méta comptent aussi. Smoke = vérification technique uniquement."
    kb = [[{"text":name, "callback_data":"profile:"+name}] for name in runtime.PROFILE_BUDGETS]
    kb.append([{"text":"🏠 Menu", "callback_data":"menu"}])
    return send(chat,text,{"inline_keyboard":kb})


def cmd_discoveries(chat, st, page=0):
    data = views.load_json(IQDIR/"DISCOVERIES.json", [])
    if isinstance(data,dict):
        data = data.get("discoveries",data.get("rules",[]))
    text = "💡 Registre des découvertes\nHypothèse ≠ gain prouvé. Les anciennes entrées sans preuve sont non validées.\n\n"
    text += json.dumps(data,ensure_ascii=False,indent=2) if data else "Aucune découverte enregistrée."
    text,kb = views.paged("💡 Registre", text, "discoveries", "registry", page)
    kb["inline_keyboard"][-1] = [{"text":"🏠 Menu", "callback_data":"menu"}]
    return send(chat,text,kb)


def cmd_health(chat, st):
    health = views.load_json(IQDIR/"gateway_health.json", {})
    live = [sid for sid,r in st.get("sessions",{}).items() if sess_alive(r)]
    text = f"🩺 Diagnostic IQ\nGateway PID : {os.getpid()}\nDernier poll : {health.get('last_poll','pas encore mesuré')}\nDernier update : {health.get('last_update','aucun')}\nErreurs poll : {health.get('poll_errors',0)}\nSessions actives : {len(live)}\n"
    for sid in live:
        rec=st["sessions"][sid]; path=sess_live_file(rec)
        age = round(time.time()-path.stat().st_mtime) if path and path.exists() else None
        text += f"\n{sid} · {rec['model']}\nFlux : {str(age)+' s' if age is not None else 'pas encore écrit'} · {record_status(rec)}"
    text += "\n\nUn PID seul ne prouve pas la progression. Les appels longs peuvent temporairement laisser le flux immobile."
    return send(chat,text,MAIN_KB)


def cmd_leaderboard(chat, st):
    text = "🏆 Résultats — comparables seulement à leur baseline et protocole\n"
    for sid,rec in sorted(st.get("sessions",{}).items(), key=lambda kv:kv[1].get("started", ""),reverse=True)[:12]:
        result=views.load_json(Path(rec["dir"])/"result.json", {})
        text += f"\n{sid} · {rec['model']}\n{result.get('status', 'historique/non terminé')}"
        if result:
            text += "\n" + json.dumps({k:v for k,v in result.items() if k in ('conclusion','paired','summary','champion')},ensure_ascii=False)[:350]
    text += "\n\nDétails et erreurs dans Sessions → Résultats. Aucun classement de QI humain."
    return send(chat,text,MAIN_KB)


def cmd_models(chat, st):
    kb=[[{"text":name, "callback_data":f"model:{i}"}] for i,name in enumerate(available_models())]
    kb.append([{"text":"🏠 Menu", "callback_data":"menu"}])
    return send(chat,"⚙️ Modèles configurés — sélection actuelle : "+st.get("model","?"),{"inline_keyboard":kb})


def handle_update(up, st):
    st.setdefault("sessions",{})
    callback = up.get("callback_query")
    message = callback.get("message",{}) if callback else up.get("message",{})
    sender = callback.get("from",{}) if callback else message.get("from",{})
    chat = message.get("chat",{}).get("id")
    if sender.get("id") != OWNER or chat != OWNER:
        if callback:
            answer_cb(callback["id"],"Accès refusé")
        return
    if callback:
        answer_cb(callback["id"])
        data=callback.get("data", "")
    else:
        raw=message.get("text","").split()
        data=raw[0].lstrip("/").split("@")[0] if raw else "menu"
    print(f"[update] {data[:100]}",flush=True)
    data={"start":"menu", "help":"menu", "leaderboard":"lb", "sessions":"runs", "status":"health"}.get(data,data)
    handlers={"bench":cmd_bench,"boost":cmd_boost,"meta":cmd_meta,"agi":cmd_agi,"lb":cmd_leaderboard,"protocol":cmd_protocol,"health":cmd_health,"settings":cmd_settings,"discoveries":cmd_discoveries,"models":cmd_models}
    try:
        if data=="menu":
            send(chat,help_text(st),MAIN_KB)
        elif data in handlers:
            handlers[data](chat,st)
        elif data.startswith("runs"):
            page=int(data.split(":")[1]) if ":" in data else 0
            send(chat,*list_sessions(st,page))
        elif data.startswith("profile:"):
            value=data.split(":",1)[1]
            if value not in runtime.PROFILE_BUDGETS:
                raise ValueError("Profil inconnu")
            st["profile"]=value; save_state(st); cmd_settings(chat,st)
        elif data.startswith("model:") or data.startswith("setmodel:"):
            value=data.split(":",1)[1]
            models=available_models()
            selected=models[int(value)] if data.startswith("model:") else value
            if selected not in models:
                raise ValueError("Modèle non configuré")
            st["model"]=selected; save_state(st); send(chat,help_text(st),MAIN_KB)
        elif data.startswith("discoveries:"):
            cmd_discoveries(chat,st,int(data.split(":")[-1]))
        elif data.startswith("event:"):
            _,sid,index,page=data.split(":")
            send(chat,*views.event_view(sid,st["sessions"][sid],int(index),int(page)))
        elif data.startswith("ctl:"):
            _,action,sid=data.split(":",2); cmd_ctl(chat,sid,st,action)
        elif data.startswith(("sess:","live:","mbrain:","prompts:","res:","manifest:")):
            fields=data.split(":"); action,sid=fields[:2]; page=int(fields[2]) if len(fields)>2 else 0
            if action=="sess":
                send(chat,*session_view(sid,st))
            elif action=="manifest":
                manifest=views.load_json(Path(st["sessions"][sid]["dir"])/"manifest.json", {})
                send(chat,*views.paged("📋 Manifeste", json.dumps(manifest,ensure_ascii=False,indent=2) if manifest else "Run historique : manifeste non disponible.","manifest",sid,page))
            else:
                renderer={"live":live_view,"mbrain":mbrain_view,"prompts":prompts_view,"res":res_view}[action]
                send(chat,*renderer(sid,st,page))
        else:
            send(chat,help_text(st),MAIN_KB)
    except (ValueError,KeyError,IndexError) as exc:
        send(chat,"⚠️ Action non exécutée : "+str(exc)[:250],MAIN_KB)


def main():
    # A second IQ poller exits without ever calling Telegram.
    with runtime.file_lock(IQDIR/"iq_gateway.lock",blocking=False):
        load_state()  # Corrupt state fails closed; never replace it with an empty file.
        health=views.load_json(IQDIR/"gateway_health.json", {})
        offset=int(health.get("offset",0))
        for attempt in range(5):
            try:
                me=tg("getMe")["result"]
                print(f"★ IQ GATEWAY v3 online: @{me['username']} PID={os.getpid()}",flush=True)
                break
            except Exception as exc:
                print("startup retry: "+type(exc).__name__,flush=True)
                if attempt==4:
                    raise RuntimeError("Telegram inaccessible après 5 essais") from None
                time.sleep(min(2**attempt,15))
        health.update({"pid":os.getpid(),"started":datetime.now(timezone.utc).isoformat(),"poll_errors":0})
        code_baseline = _module_fingerprints()
        print(f"[staleness] baseline: {len(code_baseline)} modules chargés fingerprintés",flush=True)
        while True:
            try:
                updates=tg("getUpdates",offset=offset,timeout=25,allowed_updates=["message","callback_query"])["result"]
                for update in updates:
                    try:
                        handle_update(update,load_state())
                    except Exception as exc:
                        print("handler err: "+type(exc).__name__,flush=True)
                    offset=max(offset,update["update_id"]+1)
                    health["last_update"]=datetime.now(timezone.utc).isoformat()
                runtime.reap_children()
                code_baseline = check_code_staleness(code_baseline, OWNER)
                health.update({"offset":offset,"last_poll":datetime.now(timezone.utc).isoformat()})
                runtime.atomic_json(IQDIR/"gateway_health.json",health)
                print(f"[poll] ok updates={len(updates)}",flush=True)
            except Exception as exc:
                health["poll_errors"]=health.get("poll_errors",0)+1
                print("poll err: "+type(exc).__name__,flush=True)
                time.sleep(3)


if __name__=="__main__":
    main()

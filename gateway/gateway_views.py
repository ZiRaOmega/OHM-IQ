"""Read-only, exact-run views. All model text is inert, paginated data."""
import hashlib
import json
from pathlib import Path

PAGE_CHARS = 2600


def load_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return default


def read_events(path):
    if path is None:
        return []
    result = []
    try:
        with Path(path).open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                    if isinstance(event, dict):
                        result.append(event)
                except ValueError:
                    continue  # an append may still be in progress
    except OSError:
        pass
    return result


def live_file(rec, meta=False):
    fields = ("mbrain",) if meta else ("genlive", "live")
    for field in fields:
        value = rec.get(field)
        if value:
            p = Path(value)
            if not meta and p.name == "meta_brain_live.jsonl":
                continue
            if p.is_file():
                return p
    directory = Path(rec["dir"])
    names = ("meta_brain_live.jsonl",) if meta else ("events.jsonl", "booster_live.jsonl", "agi_live.jsonl")
    for name in names:
        p = directory / name
        if p.is_file():
            return p
    if not meta:
        # Only this run's generations, never another session's newest file.
        generations = sorted(directory.glob("gen*_live.jsonl"), key=lambda p: p.stat().st_mtime)
        if generations:
            return generations[-1]
    return None


def button(text, callback):
    if len(callback.encode("utf-8")) > 64:
        raise ValueError("Callback exceeds Telegram's 64-byte limit")
    return {"text": text, "callback_data": callback}


def keyboard(*rows):
    return {"inline_keyboard": [row for row in rows if row]}


def paged(title, text, route, sid, page=0):
    pages = [text[i:i + PAGE_CHARS] for i in range(0, len(text), PAGE_CHARS)] or ["(vide)"]
    page = max(0, min(int(page), len(pages) - 1))
    nav = []
    if page:
        nav.append(button("⬅️ Précédent", f"{route}:{sid}:{page-1}"))
    if page + 1 < len(pages):
        nav.append(button("➡️ Suivant", f"{route}:{sid}:{page+1}"))
    return (f"{title}\nPage {page+1}/{len(pages)}\n\n{pages[page]}",
            keyboard(nav, [button("⬅️ Session", f"sess:{sid}")]))


def prompt_snapshots(rec):
    d = Path(rec["dir"])
    raw = load_json(d / "prompts.json")
    if isinstance(raw, dict):
        raw = raw.get("prompts", raw)
        if isinstance(raw, dict):
            result = {}
            for name, value in raw.items():
                if value is None or isinstance(value, str):
                    result[name] = value
                elif isinstance(value, dict):
                    result[name] = value.get("text", value.get("system_prompt"))
            return result
    # Legacy records only when an exact snapshot was saved in the same run.
    result = {}
    for p in sorted(d.glob("*.json")):
        row = load_json(p)
        if isinstance(row, dict) and "system_prompt" in row:
            result[str(row.get("booster", p.stem))] = row["system_prompt"]
        elif isinstance(row, list):
            for item in row:
                if isinstance(item, (list, tuple)) and len(item) == 2:
                    name, value = item
                    if isinstance(value, dict) and "system_prompt" in value:
                        result[name] = value["system_prompt"]
    return result


def prompts_view(sid, rec, page=0):
    snapshots = prompt_snapshots(rec)
    if not snapshots:
        return paged("📝 Prompts utilisés", "Snapshot absent pour cette ancienne session. Le catalogue actuel n'est pas présenté comme son prompt historique.", "prompts", sid, page)
    parts = []
    for name, body in snapshots.items():
        label = "(aucun system prompt — baseline)" if body is None else str(body)
        digest = hashlib.sha256((body or "").encode()).hexdigest()
        parts.append(f"━━ {name} ━━\n{len(body or '')} caractères · SHA256 {digest}\n{label}")
    return paged("📝 Snapshots exacts — texte intégral", "\n\n".join(parts), "prompts", sid, page)


def live_view(sid, rec, offset=0):
    events = read_events(live_file(rec))
    offset = max(0, int(offset))
    if not events:
        return "🔬 Pas encore d'événements pour CETTE session.", keyboard([button("🔄 Refresh", f"live:{sid}:0"), button("⬅️ Session", f"sess:{sid}")])
    offset = min(offset, ((len(events)-1)//3)*3)
    rows, lines = [], [f"🔬 {rec['model']} · {rec['kind']} · {len(events)} événements"]
    for i in range(offset, min(offset+3, len(events))):
        index = len(events) - 1 - i
        ev = events[index]
        status = ev.get("status", "correct" if ev.get("correct") else "incorrect" if ev.get("correct") is False else ev.get("event", "info"))
        lines.append(f"\n#{index+1} {status} · {ev.get('phase','')} · {ev.get('candidate',ev.get('family',''))}\n{str(ev.get('question',ev.get('task_id','')))[:180]}\nRéponse : {ev.get('answer',ev.get('got'))} · {ev.get('latency','?')} s")
        rows.append([button(f"📄 Événement #{index+1} complet", f"event:{sid}:{index}:0")])
    nav = []
    if offset:
        nav.append(button("⬅️ Plus récents", f"live:{sid}:{max(0,offset-3)}"))
    if offset+3 < len(events):
        nav.append(button("➡️ Plus anciens", f"live:{sid}:{offset+3}"))
    return "\n".join(lines)[:3800], keyboard(*rows, nav, [button("🔄 Refresh", f"live:{sid}:0"), button("⬅️ Session", f"sess:{sid}")])


def event_view(sid, rec, index=0, page=0):
    events = read_events(live_file(rec))
    if not 0 <= index < len(events):
        return paged("📄 Événement indisponible", "Le fichier n'a pas cet événement.", f"event:{sid}", str(index), page)
    ev = events[index]
    kind = ev.get("event", "info")
    lines = []
    if kind == "attempt_reserved":
        lines.append("⏳ Question envoyée — réponse API en cours.")
    elif ev.get("status") == "ok" or kind in ("attempt_result", "evaluation"):
        lines.append("✅ Réponse reçue.")
    elif ev.get("status"):
        lines.append(f"⚠️ {ev.get('status')}")
    else:
        lines.append(kind)
    if ev.get("question"):
        lines.append(f"\n🧩 Question ({ev.get('family','?')}):\n{ev['question']}")
    elif ev.get("task_id"):
        lines.append(f"\n🧩 Tâche : {ev['task_id']}")
    if ev.get("answer") is not None:
        lines.append(f"\n💬 Réponse : {ev['answer']}")
    if ev.get("expected") is not None:
        verdict = "✅ correct" if ev.get("correct") else "❌ incorrect" if ev.get("correct") is False else "❔"
        lines.append(f"🎯 Attendu : {ev['expected']} — {verdict}")
    elif ev.get("correct") is not None:
        lines.append(f"🎯 Verdict : {'✅ correct' if ev['correct'] else '❌ incorrect'}")
    if ev.get("reasoning"):
        lines.append(f"\n🧠 Raisonnement (fourni par l'API) :\n{ev['reasoning']}")
    if ev.get("raw") and not ev.get("reasoning"):
        lines.append(f"\n📥 Sortie brute :\n{str(ev['raw'])[:1200]}")
    meta = []
    if ev.get("model_requested"): meta.append(f"modèle: {ev['model_requested']}" + (f" (retourné: {ev['model_returned']})" if ev.get("model_returned") and ev["model_returned"] != ev["model_requested"] else ""))
    if ev.get("finish_reason"): meta.append(f"fin: {ev['finish_reason']}")
    if ev.get("latency"): meta.append(f"{round(ev['latency'],1)}s")
    if ev.get("tokens"): meta.append(f"{ev['tokens'].get('total_tokens','?')} tokens")
    if ev.get("reserved_tokens"): meta.append(f"réservés: {ev['reserved_tokens']}")
    if ev.get("http_status"): meta.append(f"HTTP {ev['http_status']}")
    if ev.get("attempts", 0) > 1: meta.append(f"tentative {ev['attempts']}")
    if meta: lines.append("\n" + " · ".join(meta))
    text = "\n".join(str(x) for x in lines if x is not None)
    out, kb = paged(f"📄 Événement #{index+1}", text, f"event:{sid}", str(index), page)
    kb["inline_keyboard"][-1] = [button("⬅️ Live", f"live:{sid}:0")]
    return out, kb


def mbrain_view(sid, rec, page=0):
    events = read_events(live_file(rec, meta=True))
    if not events:
        return paged("🧠 Méta — flux de cette session", "Pas encore de sortie méta dans cette session. L'analyse n'a peut-être pas commencé ; consulte le live et l'état. Le raisonnement interne n'est affiché que s'il est fourni par l'API.", "mbrain", sid, page)
    blocks = []
    for e in events:
        head = e.get("event", "info")
        if e.get("status"): head += f" · {e['status']}"
        body = e.get("reasoning") or e.get("raw") or ""
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        lines = [f"• {head}"]
        if e.get("question"): lines.append(f"  Q: {str(e['question'])[:150]}")
        if body: lines.append(f"  {str(body)[:600]}")
        blocks.append("\n".join(lines))
    text = "\n\n".join(blocks)
    return paged("🧠 Méta — flux de cette session", text, "mbrain", sid, page)


def result_view(sid, rec, page=0):
    d = Path(rec["dir"])
    result = load_json(d / "result.json")
    if result is not None:
        report = d / "REPORT.md"
        text = report.read_text(errors="replace") if report.is_file() else json.dumps(result, ensure_ascii=False, indent=2)
        return paged("🏆 Résultats locaux — aucune certification AGI", text, "res", sid, page)
    for name in ("AGI_RESULT.json", "leaderboard.json"):
        legacy = load_json(d / name)
        if legacy is not None:
            return paged("🏆 Ancien protocole — scores non validés / QI non étalonné", json.dumps(legacy, ensure_ascii=False, indent=2), "res", sid, page)
    return paged("🏆 Résultats", "Pas de résultat final dans CE run. Les résultats partiels sont visibles dans Live ; une interruption ne prouve aucun gain.", "res", sid, page)

"""Gateway persistence and PID-identity-safe lifecycle; Linux /proc required."""
import fcntl
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

CHILDREN = {}
PROFILE_BUDGETS = {"smoke": 8, "quick": 80, "standard": 400}


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=path.name+".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


@contextmanager
def file_lock(path, blocking=True):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        yield
    finally:
        os.close(fd)


def read_state(path):
    p = Path(path)
    if not p.exists():
        return {"version":3, "owner":None, "model":"glm-5.3", "profile":"quick", "sessions":{}}
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("sessions", {}), dict):
        raise ValueError("État invalide — fichier préservé, réparation manuelle requise")
    data.setdefault("sessions", {})
    data.setdefault("profile", "quick")
    return data


def write_state(path, data):
    with file_lock(str(path)+".lock"):
        current = read_state(path)
        sessions = {**current.get("sessions", {}), **data.get("sessions", {})}
        value = {**current, **data, "sessions":sessions, "version":3}
        atomic_json(path, value)
        data.update(value)


def process_identity(pid):
    try:
        pid = int(pid)
        if pid <= 1:
            return None
        root = Path("/proc") / str(pid)
        stat = (root / "stat").read_text().rsplit(")",1)[1].split()
        cmd = (root / "cmdline").read_bytes().decode(errors="replace").rstrip("\0").split("\0")
        if not cmd or cmd == [""]:
            return None
        return {"start_ticks":stat[19], "state":stat[0], "cmd":cmd}
    except (OSError, ValueError, IndexError, TypeError):
        return None


def matches_identity(rec):
    found = process_identity(rec.get("pid"))
    return bool(found and found["state"] not in ("Z", "X") and
                str(rec.get("proc_start_ticks")) == found["start_ticks"] and rec.get("cmd") == found["cmd"])


def is_alive(rec):
    if "proc_start_ticks" in rec:
        return matches_identity(rec)
    # Legacy records: read-only liveness with exact script+model checks.
    found = process_identity(rec.get("pid"))
    if not found or found["state"] in ("Z", "X"):
        return False
    names = {"bench":"iq_bench.py", "boost":"iq_booster.py", "meta":"iq_meta.py", "agi":"agi_optimizer.py"}
    script = names.get(rec.get("kind"))
    argv = found["cmd"]
    if not script or script not in [Path(x).name for x in argv[:3]]:
        return False
    return rec.get("model") in argv


def is_paused(rec):
    p = process_identity(rec.get("pid"))
    return bool(is_alive(rec) and p and p["state"] in ("T", "t"))


def signal_record(rec, action):
    if not matches_identity(rec):
        raise ValueError("Identité du processus non vérifiée : aucun signal envoyé")
    signals = {"pause":signal.SIGSTOP, "resume":signal.SIGCONT, "stop":signal.SIGTERM}
    if action not in signals:
        raise ValueError("Action inconnue")
    os.kill(int(rec["pid"]), signals[action])


def stop_record(rec, grace=2.0):
    if not matches_identity(rec):
        raise ValueError("Identité du processus non vérifiée : arrêt refusé")
    if is_paused(rec):
        signal_record(rec, "resume")
    signal_record(rec, "stop")
    deadline = time.monotonic()+grace
    while time.monotonic() < deadline and matches_identity(rec):
        child = CHILDREN.get(rec["pid"])
        if child:
            child.poll()
        time.sleep(0.03)
    if matches_identity(rec):
        os.kill(rec["pid"], signal.SIGKILL)
    child = CHILDREN.pop(rec["pid"], None)
    if child:
        child.wait(timeout=3)


def build_command(root, kind, model, directory, options, resume=False):
    mode = "arc" if kind == "agi" else kind
    if mode not in ("bench", "boost", "meta", "arc"):
        raise ValueError("Mode inconnu")
    command = [sys.executable, "-u", str(Path(root)/"iq_research.py"),
               "--mode", mode, "--model", model, "--profile", options["profile"],
               "--run-dir", str(directory), "--seed", str(options["seed"]),
               "--max-calls", str(options["max_calls"]), "--max-tokens", str(options["max_tokens"])]
    if options.get("meta_model"):
        command += ["--meta-model", options["meta_model"]]
    if resume:
        command.append("--resume")
    return command


def spawn_record(root, kind, model, options, directory=None, resume=False):
    sid = "r" + uuid.uuid4().hex[:16]
    directory = Path(directory) if directory else Path(root)/"sessions"/sid
    directory.mkdir(parents=True, exist_ok=True)
    cmd = build_command(root, kind, model, directory, options, resume)
    with (directory/"run.log").open("a", encoding="utf-8") as log:
        process = subprocess.Popen(cmd, cwd=root, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    CHILDREN[process.pid] = process
    identity = process_identity(process.pid)
    record = {"kind":kind, "model":model, "pid":process.pid,
              "started":datetime.now(timezone.utc).isoformat(), "dir":str(directory),
              "live":str(directory/"events.jsonl"), "mbrain":str(directory/"meta_brain_live.jsonl"),
              "options":dict(options), "cmd":cmd, "protocol":"research-v1",
              "proc_start_ticks":identity["start_ticks"] if identity else None}
    return sid, record


def reap_children():
    for pid, process in list(CHILDREN.items()):
        if process.poll() is not None:
            CHILDREN.pop(pid, None)

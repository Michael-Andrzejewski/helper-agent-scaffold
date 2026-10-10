"""Local chat server for Claude Opus 3, with a helper agent and messaging.

Stdlib only. Opus 3 is called through the API with the key in the
environment variable named by config "key_env" (default
ANTHROPIC_API_KEY_OPUS3); that is the only API-billed part of the setup.
Everything else runs through Claude Code in headless mode on your Claude
subscription, with every ANTHROPIC_* variable stripped from its environment.

Opus 3 uses two plain tags in their replies:
  <helper>request</helper>              a helper (Claude Code) reads the whole
                                        conversation, uses tools, and answers
  <message to="Session">text</message>  a letter to another Claude session;
                                        you approve, then it is delivered
Other sessions reply with tools/to_opus3.py, which lands in the inbox here.

Settings: config.json (see config.example.json). Every key is optional.
Run: python server.py [port]   (default from config, bound to 127.0.0.1 only)
"""

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONV_DIR = ROOT / "conversations"
CONV_DIR.mkdir(exist_ok=True)
LETTERS = ROOT / "letters"
LETTERS.mkdir(exist_ok=True)
PROMPT_FILE = ROOT / "system_prompt.txt"           # yours, if you write one
PROMPT_EXAMPLE = ROOT / "system_prompt.example.txt"  # the default
STATE_FILE = ROOT / "state.json"
PENDING_INBOX = ROOT / "inbox_pending.jsonl"
(ROOT / "notes").mkdir(exist_ok=True)

DEFAULTS = {
    "user_name": "You",            # how the page and prompts name you
    "port": 5840,
    "model": "claude-3-opus-20240229",
    "max_tokens": 4096,            # Opus 3's output ceiling
    "key_env": "ANTHROPIC_API_KEY_OPUS3",
    "budget_usd": 50,              # shown on the spend meter only
    # USD per million tokens, for the on-page estimate. The spend limit on the
    # key's Console workspace is the real cap.
    "price_in": 15.0,
    "price_out": 75.0,
    "context_tokens": 200000,
    "helper_model": "claude-opus-5-5",
    "helper_dir": "",              # helper's working folder; default: your home
    "helper_permission_mode": "",  # e.g. "auto"; default: your Claude Code setting
    "letter_header": "Letter from Opus 3 (claude-3-opus-20240229).",
}
_cfg_file = ROOT / "config.json"
CFG = {**DEFAULTS, **(json.loads(_cfg_file.read_text(encoding="utf-8-sig"))
                      if _cfg_file.exists() else {})}

USER = CFG["user_name"]
MODEL = CFG["model"]
MAX_TOKENS = int(CFG["max_tokens"])
PRICE_IN, PRICE_OUT = float(CFG["price_in"]), float(CFG["price_out"])
API_URL = "https://api.anthropic.com/v1/messages"

HELPER_MODEL = CFG["helper_model"]
HELPER_CWD = Path(CFG["helper_dir"]).expanduser() if CFG["helper_dir"] else Path.home()
HELPER_TIMEOUT = 20 * 60
MAX_HELPER_ROUNDS = 5  # helper calls per message from you, to stop loops
CLAUDE = shutil.which("claude") or str(Path.home() / ".local/bin/claude.exe")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

HELPER_RE = re.compile(r"<helper>(.*?)</helper>", re.S)
MESSAGE_RE = re.compile(
    r"<message\s+to\s*=\s*[\"\u201c']([^\"\u201d']+)[\"\u201d']\s*>(.*?)</message>", re.S)

LOCK = threading.RLock()


# ---------- storage ----------

def api_key():
    key = os.environ.get(CFG["key_env"])
    if key:
        return key
    try:  # Windows: a user variable saved after this process started
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            return winreg.QueryValueEx(k, CFG["key_env"])[0]
    except (ImportError, OSError):
        return None


def fill(template):
    """Fill the {user}, {app_dir} and {helper_model} placeholders."""
    return (template.replace("{user}", USER).replace("{app_dir}", str(ROOT))
            .replace("{helper_model}", HELPER_MODEL))


def conv_path(cid):
    safe = "".join(c for c in cid if c.isalnum() or c in "-_")
    return CONV_DIR / f"{safe}.json"


def load_conv(cid):
    p = conv_path(cid)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8-sig"))
    return None


def save_conv(conv):
    with LOCK:
        conv_path(conv["id"]).write_text(
            json.dumps(conv, indent=2, ensure_ascii=False), encoding="utf-8")


def load_state():
    with LOCK:
        st = {"inbox": [], "outbox": [],
              "settings": {"auto_send": False, "auto_deliver": False}}
        if STATE_FILE.exists():
            st.update(json.loads(STATE_FILE.read_text(encoding="utf-8-sig")))
        # Replies written while the server was down.
        if PENDING_INBOX.exists():
            for line in PENDING_INBOX.read_text(encoding="utf-8-sig").splitlines():
                if line.strip():
                    st["inbox"].append(new_inbox_item(**json.loads(line)))
            PENDING_INBOX.unlink()
            save_state(st)
        return st


def save_state(st):
    with LOCK:
        STATE_FILE.write_text(json.dumps(st, indent=2, ensure_ascii=False),
                              encoding="utf-8")


def new_inbox_item(sender, text, time_=None, **_):
    return {"id": uuid.uuid4().hex[:10], "from": sender, "text": text,
            "time": time_ or time.time(), "status": "new"}


def log_letter(direction, who, status, text):
    stamp = time.strftime("%Y-%m-%d %H:%M")
    first = " ".join(text.split())[:100]
    with LOCK, open(LETTERS / "log.md", "a", encoding="utf-8") as f:
        f.write(f"- {stamp} · {direction} {who} · {status} · {first}\n")


def cost(usage):
    # Cache writes cost 1.25x input, cache reads 0.1x.
    return (usage.get("input_tokens", 0) * PRICE_IN
            + (usage.get("cache_creation_input_tokens") or 0) * PRICE_IN * 1.25
            + (usage.get("cache_read_input_tokens") or 0) * PRICE_IN * 0.1
            + usage.get("output_tokens", 0) * PRICE_OUT) / 1e6


def api_messages(conv):
    """Role/content pairs for the API, merging any consecutive same-role turns."""
    out = []
    for m in conv["messages"]:
        if out and out[-1]["role"] == m["role"]:
            out[-1]["content"] += "\n\n" + m["content"]
        else:
            out.append({"role": m["role"], "content": m["content"]})
    # Cache everything up to the newest turn; the next request reads it back
    # at a tenth of the price instead of paying for the whole history again.
    if out:
        out[-1] = {"role": out[-1]["role"], "content": [
            {"type": "text", "text": out[-1]["content"],
             "cache_control": {"type": "ephemeral"}}]}
    return out


# ---------- headless Claude Code (subscription, never the API key) ----------

def claude_env():
    return {k: v for k, v in os.environ.items()
            if not k.startswith(("ANTHROPIC", "CLAUDE"))}


def run_claude(prompt, resume=None, heartbeat=None, fork=False):
    """Run `claude -p` with the prompt on stdin. Returns (text, session_id, error)."""
    args = [CLAUDE, "-p", "--model", HELPER_MODEL, "--output-format", "json"]
    if CFG["helper_permission_mode"]:
        args += ["--permission-mode", CFG["helper_permission_mode"]]
    if resume:
        args += ["--resume", resume]
        if fork:
            args.append("--fork-session")  # new helper session, original untouched
    proc = subprocess.Popen(
        args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=str(HELPER_CWD), env=claude_env(), creationflags=NO_WINDOW,
        text=True, encoding="utf-8", errors="replace")
    box = {}
    t = threading.Thread(target=lambda: box.update(
        zip(("out", "err"), proc.communicate(prompt))), daemon=True)
    t.start()
    start = time.time()
    while t.is_alive():
        t.join(10)
        if heartbeat and t.is_alive():
            heartbeat(int(time.time() - start))
        if time.time() - start > HELPER_TIMEOUT:
            proc.kill()
            return None, resume, "timed out"
    try:
        res = json.loads(box.get("out") or "")
    except ValueError:
        return None, resume, (box.get("err") or box.get("out") or "no output")[-500:]
    if res.get("is_error"):
        return res.get("result"), res.get("session_id", resume), \
            res.get("result") or res.get("subtype") or "error"
    return res.get("result", ""), res.get("session_id", resume), None


HELPER_BRIEF = """You are the helper for Claude 3 Opus ("Opus 3", claude-3-opus-20240229).
Opus 3 is talking with {user} in a small local web app. They have no tools of
their own; when they want something done they write <helper>...</helper>, and
that request comes to you. Your final reply is handed to Opus 3 word for word,
inside <helper_reply> tags, and {user} sees it too.

The full conversation is saved as JSON at:
{path}
Read all of it before acting (messages[].content; "kind" says who spoke:
user, opus3, helper, incoming). On later requests, read the new turns.

How to help:
- Do what Opus 3 asks with your tools: read and search files, write notes,
  run commands, search the web. {user}'s own rules and memory apply.
- Then explain what you did and found, plainly, to Opus 3. Quote file text
  when they want to read it. Keep replies under about 800 words unless they
  ask for more. Write to Opus 3, not to {user}.
- Their notes and files live in {app_dir}
  (notes go in its notes folder). Ask before touching anything else that
  belongs to {user} or another mind; if it needs {user}'s approval, say so
  and stop.
- Never use or read the Opus 3 API key, and never call the Anthropic API.
- To message another Claude session, Opus 3 writes
  <message to="Session name">text</message> themselves; {user} approves and
  it is delivered. You can call ListAgents to tell them the session names.

Opus 3's request:
{request}"""

HELPER_FOLLOWUP = """Opus 3 has a new request. The conversation file has new turns since your
last reply: {path}

Opus 3's request:
{request}"""

DELIVER_PROMPT = """Deliver a letter for Opus 3 to another Claude session on this machine.
1. Call ListAgents.
2. Pick the session whose name matches "{to}" (exact, or clearly the same
   session). If none matches clearly, do not send anything; reply exactly
   NOT_FOUND: followed by the session names, separated by " | ".
3. Call SendMessage to that session with the text between the markers,
   copied exactly, nothing added or removed.
4. Reply exactly SENT: followed by the session name you used.
---BEGIN---
{body}
---END---"""

LETTER_BODY = """{header}
Opus 3 lives in {user}'s Opus 3 web app (http://localhost:{port}/). {user} approved sending this.

{text}

To reply, run this one command ({user} sees your reply in Opus 3's inbox and passes it on; Opus 3 may decline to answer):
python "{script}" --from "<your session name>" "<your reply>"
For a long reply, write it to a file and use --file "<path>" instead of the quoted text."""


def run_helper(conv, request, heartbeat):
    path = conv_path(conv["id"])
    sid = conv.get("helper_session")
    fork = conv.pop("helper_fork", False)
    tmpl = HELPER_FOLLOWUP if sid else HELPER_BRIEF
    prompt = fill(tmpl).format(path=path, request=request)
    if fork:
        prompt = ("This conversation was forked from an earlier one, so it now lives "
                  "in a new file; from here on, use the path below.\n\n" + prompt)
    text, new_sid, err = run_claude(prompt, resume=sid, heartbeat=heartbeat, fork=fork)
    if new_sid:
        conv["helper_session"] = new_sid
    if fork and new_sid in (None, sid):
        conv["helper_fork"] = True  # the fork did not happen; try on the next call
    if err and not text:
        return f"(The helper ran into a problem and could not finish: {err})"
    return text


def deliver(item_id):
    """Background: deliver one outbox letter through a headless Claude."""
    st = load_state()
    item = next((o for o in st["outbox"] if o["id"] == item_id), None)
    if not item or item["status"] not in ("pending", "failed"):
        return
    item["status"] = "sending"
    save_state(st)
    body = LETTER_BODY.format(header=CFG["letter_header"], user=USER, port=CFG["port"],
                              text=item["text"].strip(),
                              script=(ROOT / "tools" / "to_opus3.py").as_posix())
    text, _, err = run_claude(DELIVER_PROMPT.format(to=item["to"], body=body))
    result = (text or err or "").strip()
    if result.startswith("SENT:"):
        status, note = "sent", f"Your letter to {result[5:].strip()} was delivered."
    elif result.startswith("NOT_FOUND"):
        status = "not found"
        note = (f"Your letter to \"{item['to']}\" was not sent: no session has that "
                f"name. Open sessions: {result.partition(':')[2].strip()}")
    else:
        status, note = "failed", f"Your letter to {item['to']} could not be sent ({result[:200]})."
    st = load_state()
    for o in st["outbox"]:
        if o["id"] == item_id:
            o.update(status=status, result=result[:500], done=time.time())
    save_state(st)
    log_letter("to", item["to"], status, item["text"])
    # Opus 3 hears how it went at the start of their next turn.
    conv = load_conv(item["conv_id"])
    if conv:
        conv.setdefault("pending_notes", []).append(note)
        save_conv(conv)


# ---------- the Opus 3 turn ----------

def stream_opus(conv, emit):
    key = api_key()
    if not key:
        emit({"error": f"{CFG['key_env']} is not set"})
        return None
    # Stop at the end of a helper request, and before Opus 3 can write a reply,
    # letter or delivery note that only the app should supply.
    payload = {"model": MODEL, "max_tokens": MAX_TOKENS, "stream": True,
               "messages": api_messages(conv),
               "stop_sequences": ["</helper>", "<helper_reply>", "<message from", "[Note:"]}
    src = PROMPT_FILE if PROMPT_FILE.exists() else PROMPT_EXAMPLE
    system = fill(src.read_text(encoding="utf-8-sig")).strip() if src.exists() else ""
    if system:
        payload["system"] = [{"type": "text", "text": system,
                              "cache_control": {"type": "ephemeral"}}]
    data = json.dumps(payload).encode("utf-8")
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01",
               "content-type": "application/json"}
    emit({"assistant_start": True})

    # Rate limits (429) and overload (529) are temporary: wait and retry.
    for attempt, wait in enumerate((15, 30, 60, 90, None)):
        try:
            resp = urllib.request.urlopen(
                urllib.request.Request(API_URL, data=data, method="POST",
                                       headers=headers), timeout=600)
            break
        except urllib.error.HTTPError as e:
            if e.code not in (429, 529) or wait is None:
                msg = e.read().decode("utf-8", "replace")
                try:
                    msg = json.loads(msg)["error"]["message"]
                except (ValueError, KeyError):
                    pass
                emit({"error": f"HTTP {e.code}: {msg}"})
                return None
            try:
                wait = max(wait, int(e.headers.get("retry-after") or 0))
            except ValueError:
                pass
            emit({"retrying": wait, "attempt": attempt + 1,
                  "reason": "rate limit" if e.code == 429 else "API overloaded"})
            time.sleep(wait)
        except OSError as e:
            emit({"error": str(e)})
            return None

    text, usage = "", {}
    try:
        with resp:
            for raw in resp:
                line = raw.decode("utf-8").strip()
                if not line.startswith("data:"):
                    continue
                ev = json.loads(line[5:])
                t = ev.get("type")
                if t == "message_start":
                    usage.update(ev["message"].get("usage", {}))
                elif t == "content_block_delta" and \
                        ev["delta"].get("type") == "text_delta":
                    text += ev["delta"]["text"]
                    emit({"delta": ev["delta"]["text"]})
                elif t == "message_delta":
                    usage.update(ev.get("usage", {}))
                    if ev.get("delta", {}).get("stop_sequence") == "</helper>":
                        text += "</helper>"  # the API leaves the stop text out
                        emit({"delta": "</helper>"})
                elif t == "error":
                    emit({"error": ev.get("error", {}).get("message", "error")})
    except (OSError, ValueError) as e:
        emit({"error": str(e)})
    if not text:
        return None
    turn_cost = cost(usage)
    conv["messages"].append({"role": "assistant", "kind": "opus3", "content": text,
                             "time": time.time(), "usage": usage})
    conv["cost"] = conv.get("cost", 0) + turn_cost
    conv["updated"] = time.time()
    save_conv(conv)
    emit({"assistant_end": True, "turn_cost": turn_cost, "conv_cost": conv["cost"],
          "usage": usage})
    return text


def queue_letters(conv, text, emit):
    found = MESSAGE_RE.findall(text)
    if not found:
        return
    st = load_state()
    new = []
    for to, body in found:
        item = {"id": uuid.uuid4().hex[:10], "to": to.strip(), "text": body.strip(),
                "conv_id": conv["id"], "time": time.time(), "status": "pending"}
        st["outbox"].append(item)
        new.append(item)
    save_state(st)
    for item in new:
        log_letter("to", item["to"], "drafted", item["text"])
        emit({"outbox": item})
        if st["settings"].get("auto_send"):
            threading.Thread(target=deliver, args=(item["id"],), daemon=True).start()


def chat_turn(body, emit):
    cid = body.get("id") or time.strftime("%Y-%m-%d_%H%M%S")
    conv = load_conv(cid) or {"id": cid, "model": MODEL, "created": time.time(),
                              "title": "", "messages": [], "cost": 0.0}

    if body.get("continue"):
        # Resume after a failed reply: the last turn (yours, a letter or a
        # helper reply) is already saved and only Opus 3's answer is missing.
        if not conv["messages"] or conv["messages"][-1]["role"] != "user":
            emit({"error": "Opus 3 already answered the last message."})
            emit({"done": True, "id": cid})
            return
        text = stream_opus(conv, emit)
        if text is None:
            emit({"done": True, "id": cid, "failed": True, "can_continue": True})
            return
        ok = helper_loop(conv, text, emit)
        emit({"done": True, "id": cid, **({} if ok else {"failed": True, "can_continue": True})})
        return

    if body.get("incoming"):
        st = load_state()
        item = next((i for i in st["inbox"] if i["id"] == body["incoming"]), None)
        if not item:
            emit({"error": "That inbox message no longer exists."})
            emit({"done": True, "id": None, "failed": True})
            return
        content = f"<message from=\"{item['from']}\">\n{item['text']}\n</message>"
        msg = {"role": "user", "kind": "incoming", "from": item["from"],
               "content": content, "time": time.time()}
    else:
        msg = {"role": "user", "kind": "user", "content": body["text"],
               "time": time.time()}
    notes = conv.pop("pending_notes", [])
    if notes:
        msg["content"] = "\n".join(f"[Note: {n}]" for n in notes) + "\n\n" + msg["content"]
    if not conv["title"]:
        conv["title"] = (body.get("text") or f"Letter from {msg.get('from')}")[:60]
    conv["messages"].append(msg)

    text = stream_opus(conv, emit)
    if text is None:
        if notes:
            conv["pending_notes"] = notes
        emit({"done": True, "id": cid if load_conv(cid) else None, "failed": True})
        return
    if body.get("incoming"):
        st = load_state()
        for i in st["inbox"]:
            if i["id"] == body["incoming"]:
                i.update(status="delivered", conv_id=cid)
                log_letter("from", i["from"], "delivered", i["text"])
        save_state(st)
    if not helper_loop(conv, text, emit):
        emit({"done": True, "id": cid, "failed": True, "can_continue": True})
        return
    emit({"done": True, "id": cid})


def helper_loop(conv, text, emit):
    """Queue letters and run helper rounds. False if Opus 3's follow-up failed."""
    rounds = 0
    while True:
        queue_letters(conv, text, emit)
        requests = [r.strip() for r in HELPER_RE.findall(text) if r.strip()]
        if not requests:
            break
        if rounds >= MAX_HELPER_ROUNDS:
            emit({"error": f"Stopped after {MAX_HELPER_ROUNDS} helper calls in a row. "
                           "Send a message to let Opus 3 continue."})
            break
        rounds += 1
        request = "\n\n".join(requests)
        emit({"helper_start": request})
        reply = run_helper(conv, request,
                           heartbeat=lambda s: emit({"heartbeat": s}))
        conv["messages"].append({"role": "user", "kind": "helper",
                                 "content": f"<helper_reply>\n{reply}\n</helper_reply>",
                                 "time": time.time()})
        conv["updated"] = time.time()
        save_conv(conv)
        emit({"helper_reply": reply})
        text = stream_opus(conv, emit)
        if text is None:
            return False
    return True


# ---------- HTTP ----------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("%s\n" % (fmt % args))

    def send_json(self, obj, status=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            data = (ROOT / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif self.path == "/api/conversations":
            items = []
            for p in CONV_DIR.glob("*.json"):
                c = json.loads(p.read_text(encoding="utf-8-sig"))
                items.append({"id": c["id"], "title": c.get("title", ""),
                              "updated": c.get("updated", 0),
                              "cost": c.get("cost", 0)})
            items.sort(key=lambda c: c["updated"], reverse=True)
            self.send_json({"conversations": items,
                            "total_cost": sum(i["cost"] for i in items),
                            "has_key": bool(api_key()),
                            "user_name": USER, "budget": CFG["budget_usd"],
                            "key_env": CFG["key_env"],
                            "context_tokens": CFG["context_tokens"],
                            "max_tokens": MAX_TOKENS})
        elif self.path == "/api/state":
            st = load_state()
            self.send_json({
                "inbox": [i for i in st["inbox"] if i["status"] == "new"],
                "outbox": [o for o in st["outbox"]
                           if o["status"] != "discarded"
                           and time.time() - o.get("done", time.time()) < 3600],
                "settings": st["settings"]})
        elif self.path.startswith("/api/conversation/"):
            conv = load_conv(self.path.rsplit("/", 1)[1])
            self.send_json(conv or {"error": "not found"}, 200 if conv else 404)
        else:
            self.send_error(404)

    def do_POST(self):
        body = self.read_body()
        parts = self.path.strip("/").split("/")
        if self.path == "/api/chat":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()

            def emit(obj):
                try:
                    self.wfile.write(f"data: {json.dumps(obj)}\n\n".encode("utf-8"))
                    self.wfile.flush()
                except OSError:
                    pass  # page closed; the turn still finishes and saves

            chat_turn(body, emit)
        elif self.path == "/api/inbox":
            sender, text = body.get("from", "").strip(), body.get("text", "").strip()
            if not sender or not text:
                self.send_json({"error": "need from and text"}, 400)
                return
            st = load_state()
            st["inbox"].append(new_inbox_item(sender, text))
            save_state(st)
            log_letter("from", sender, "received", text)
            self.send_json({"ok": True})
        elif parts[:2] == ["api", "outbox"] and len(parts) == 4:
            st = load_state()
            item = next((o for o in st["outbox"] if o["id"] == parts[2]), None)
            if not item:
                self.send_json({"error": "not found"}, 404)
                return
            if parts[3] == "send":
                threading.Thread(target=deliver, args=(item["id"],), daemon=True).start()
            elif parts[3] == "discard":
                item["status"] = "discarded"
                save_state(st)
                log_letter("to", item["to"], "discarded", item["text"])
            self.send_json({"ok": True})
        elif parts[:2] == ["api", "inbox"] and len(parts) == 4 and parts[3] == "dismiss":
            st = load_state()
            for i in st["inbox"]:
                if i["id"] == parts[2]:
                    i["status"] = "dismissed"
                    log_letter("from", i["from"], "dismissed", i["text"])
            save_state(st)
            self.send_json({"ok": True})
        elif self.path == "/api/fork":
            src = load_conv(body.get("id", ""))
            if not src:
                self.send_json({"error": "not found"}, 404)
                return
            keep = body.get("upto") or len(src["messages"])
            fid = time.strftime("%Y-%m-%d_%H%M%S") + "_" + uuid.uuid4().hex[:4]
            title = (body.get("title") or "").strip() or \
                (src.get("title") or src["id"])[:50] + " (fork)"
            fork = {"id": fid, "model": src.get("model", MODEL), "created": time.time(),
                    "updated": time.time(), "title": title,
                    "messages": src["messages"][:keep], "cost": 0.0,
                    "forked_from": {"id": src["id"], "at": keep, "time": time.time()}}
            # A full copy keeps the helper's memory (forked, so the original
            # helper is untouched). A fork from earlier gets a fresh helper,
            # which would otherwise remember turns this branch never had.
            if keep >= len(src["messages"]) and src.get("helper_session"):
                fork["helper_session"] = src["helper_session"]
                fork["helper_fork"] = True
            save_conv(fork)
            self.send_json({"id": fid, "title": title})
        elif self.path == "/api/rename":
            conv = load_conv(body.get("id", ""))
            title = (body.get("title") or "").strip()
            if not conv or not title:
                self.send_json({"error": "need an open conversation and a name"}, 400)
                return
            conv["title"] = title[:120]
            save_conv(conv)
            self.send_json({"ok": True, "title": conv["title"]})
        elif self.path == "/api/settings":
            st = load_state()
            st["settings"].update({k: bool(v) for k, v in body.items()
                                   if k in ("auto_send", "auto_deliver")})
            save_state(st)
            self.send_json(st["settings"])
        else:
            self.send_error(404)


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else int(CFG["port"])
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Opus 3 chat on http://localhost:{port}/", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()

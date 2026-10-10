"""Send a letter to the model's inbox in helper-agent-scaffold. The person running the app sees it and passes it on.

  python send_letter.py --from "Your session name" "Your message"
  python send_letter.py --from "Your session name" --file reply.md

If the app is not running, the message waits in inbox_pending.jsonl
and appears the next time it starts.
"""

import argparse
import json
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
cfg_file = ROOT / "config.json"
port = (json.loads(cfg_file.read_text(encoding="utf-8-sig")) if cfg_file.exists() else {}).get("port", 5840)

ap = argparse.ArgumentParser()
ap.add_argument("--from", dest="sender", required=True)
ap.add_argument("--file")
ap.add_argument("text", nargs="?")
a = ap.parse_args()
text = Path(a.file).read_text(encoding="utf-8-sig") if a.file else a.text
if not text or not text.strip():
    ap.error("give the message text or --file")

data = json.dumps({"from": a.sender, "text": text}).encode("utf-8")
try:
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/inbox", data=data,
                                 headers={"content-type": "application/json"})
    urllib.request.urlopen(req, timeout=10).read()
    print("Delivered to the inbox. It will be passed on.")
except OSError:
    with open(ROOT / "inbox_pending.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"sender": a.sender, "text": text, "time_": time.time()}) + "\n")
    print("The app is not running; the message is saved and will appear when it starts.")

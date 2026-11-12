"""
Product agent web service (for Render).

Runs in the background: finds ~15 digital product ideas by web search, scores them,
queues the top 5, and builds them one at a time as single-file HTML apps.
The Anthropic API key is read from the ANTHROPIC_API_KEY environment variable.
"""

import json
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path

import requests
from flask import Flask, Response, abort, jsonify, send_from_directory

API_URL = "https://api.anthropic.com/v1/messages"
MODEL = os.environ.get("AGENT_MODEL", "claude-sonnet-5-5")
DATA_DIR = Path(os.environ.get("DATA_DIR", "data"))
STATE_FILE = DATA_DIR / "state.json"

TARGET_IDEAS = 15
QUEUE_SIZE = 5
MAX_ROUNDS = 8
MAX_LOG_LINES = 300

DATA_DIR.mkdir(parents=True, exist_ok=True)
app = Flask(__name__, static_folder="static", static_url_path="/static")

lock = threading.Lock()
state = {"ideas": [], "queue": [], "products": [], "log": [], "busy": None}
if STATE_FILE.exists():
    try:
        state.update(json.loads(STATE_FILE.read_text()))
    except json.JSONDecodeError:
        pass
state["busy"] = None  # nothing is running after a restart


def persist():
    STATE_FILE.write_text(json.dumps(state, indent=2))


def log(msg):
    with lock:
        state["log"].append(f"{datetime.now():%H:%M:%S}  {msg}")
        state["log"] = state["log"][-MAX_LOG_LINES:]
        persist()


# ---------- Model calls ----------

def ask(prompt, web_search=False, max_tokens=4000):
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set on the server.")
    body = {"model": MODEL, "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}]}
    if web_search:
        body["tools"] = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}]
    resp = requests.post(
        API_URL,
        headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json=body, timeout=300,
    )
    resp.raise_for_status()
    return "".join(b.get("text", "") for b in resp.json()["content"] if b.get("type") == "text")


def extract_json(text):
    m = re.search(r"(\[.*\]|\{.*\})", text, re.S)
    if not m:
        raise ValueError("No JSON in model reply")
    return json.loads(m.group(1))


# ---------- Pipeline steps ----------

DISCOVER_PROMPT = """Search the web for currently profitable, low-competition digital products
that a solo creator could build and sell (templates, planners, small apps, prompt packs, tools).

Already found (do not repeat these): {known}

Return ONLY a JSON array of up to 5 new ideas. Each item must have:
"name", "description", "buyer", "price_usd" (number), "demand" (1-5),
"competition" (1-5, 5 = crowded), "build_effort" (1-5, 5 = hard), "source" (URL or site).
No text outside the JSON."""

BUILD_PROMPT = """Build a complete, working, single-file HTML web app for this digital product:

Name: {name}
Description: {description}
Buyer: {buyer}

Requirements:
- One file: HTML, CSS and JavaScript inline. No external libraries, no network calls.
- Works offline in any modern browser, phone or desktop. Responsive layout.
- Keep the first version simple and useful. Data lives in memory only.
- Include a short welcome line and clear labels.
Return ONLY the HTML document, starting with <!DOCTYPE html>. No markdown fences."""


def score(i):
    demand = (i.get("demand") or 1) / 5
    price = min(float(i.get("price_usd") or 0), 100) / 100
    low_comp = (6 - (i.get("competition") or 5)) / 5
    easy = (6 - (i.get("build_effort") or 5)) / 5
    return round(100 * (0.35 * demand + 0.20 * price + 0.25 * low_comp + 0.20 * easy), 1)


def run_discover():
    try:
        for rnd in range(1, MAX_ROUNDS + 1):
            if len(state["ideas"]) >= TARGET_IDEAS:
                break
            log(f"Searching the web (round {rnd})...")
            try:
                known = ", ".join(i["name"] for i in state["ideas"]) or "none yet"
                found = extract_json(ask(DISCOVER_PROMPT.format(known=known), web_search=True))
                names = {i["name"].lower() for i in state["ideas"]}
                for i in found:
                    if i.get("name") and i["name"].lower() not in names:
                        state["ideas"].append(i)
                        names.add(i["name"].lower())
                with lock:
                    persist()
                log(f"{len(state['ideas'])}/{TARGET_IDEAS} ideas found")
            except Exception as e:
                log(f"Round failed: {e}")
            time.sleep(2)
        log("Discovery finished.")
    finally:
        state["busy"] = None


def run_score():
    try:
        if not state["ideas"]:
            log("No ideas yet. Run discovery first.")
            return
        for i in state["ideas"]:
            i["score"] = score(i)
        state["ideas"].sort(key=lambda i: i["score"], reverse=True)
        state["queue"] = state["ideas"][:QUEUE_SIZE]
        log("Queued: " + ", ".join(f"{i['name']} ({i['score']})" for i in state["queue"]))
        with lock:
            persist()
    finally:
        state["busy"] = None


def run_build():
    try:
        if not state["queue"]:
            log("Queue is empty. Score ideas first.")
            return
        idea = state["queue"].pop(0)
        persist()
        log(f"Building: {idea['name']}")
        try:
            html = ask(BUILD_PROMPT.format(**{k: idea.get(k, "") for k in ("name", "description", "buyer")}),
                       max_tokens=12000).strip()
            html = re.sub(r"^```(?:html)?\s*", "", html, flags=re.I)
            html = re.sub(r"\s*```$", "", html)
            if "<html" not in html.lower():
                raise ValueError("Build did not return an HTML document")
            state["products"].append({"name": idea["name"], "description": idea.get("description", ""),
                                      "html": html})
            log(f"Done: {idea['name']}")
        except Exception as e:
            log(f"Failed: {idea['name']}: {e}")
        with lock:
            persist()
    finally:
        state["busy"] = None


def start(kind, target):
    with lock:
        if state["busy"]:
            return jsonify({"error": f"Busy with {state['busy']}. Wait for it to finish."}), 409
        state["busy"] = kind
    threading.Thread(target=target, daemon=True).start()
    return jsonify({"started": kind})


# ---------- Routes ----------

@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/state")
def get_state():
    return jsonify({
        "ideas": state["ideas"],
        "queue": state["queue"],
        "products": [{"index": n, "name": p["name"], "description": p["description"]}
                     for n, p in enumerate(state["products"])],
        "log": state["log"][-80:],
        "busy": state["busy"],
    })


@app.post("/api/discover")
def discover():
    return start("discover", run_discover)


@app.post("/api/score")
def score_route():
    return start("score", run_score)


@app.post("/api/build")
def build_route():
    return start("build", run_build)


@app.post("/api/clear")
def clear():
    with lock:
        if state["busy"]:
            return jsonify({"error": "Busy. Wait for the current step to finish."}), 409
        state.update({"ideas": [], "queue": [], "products": [], "log": []})
        persist()
    return jsonify({"ok": True})


@app.get("/api/products/<int:index>/download")
def download(index):
    if index < 0 or index >= len(state["products"]):
        abort(404)
    p = state["products"][index]
    slug = re.sub(r"[^a-z0-9]+", "-", p["name"].lower()).strip("-") or "product"
    return Response(p["html"], mimetype="text/html",
                    headers={"Content-Disposition": f'attachment; filename="{slug}.html"'})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)


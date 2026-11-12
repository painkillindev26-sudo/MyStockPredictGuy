#!/usr/bin/env python3
"""
AD AGENT - one package, end to end.

  SYNC   pull your products from Gumroad + public GitHub repos
  LEARN  read Pinterest analytics + Gumroad sales, find what actually works
  VIDEO  short vertical ad videos (needs ffmpeg), posted as Pinterest video pins
  PLAN   web research -> Claude drafts soft-sell campaigns -> critic blocks pushy copy
         (informed by what it learned). Pin images are generated automatically.
  POST   publish to every channel you configured: Pinterest, Bluesky, Mastodon, Telegram
         (caps + spacing, optionally after your approval; any one channel is enough)

One command does the whole loop:
    python ad_agent.py run            # asks you to approve each pin
    python ad_agent.py run --auto     # fully hands-off (use with cron once you trust it)

Individual steps: sync | learn | plan | review | post | report

FREE ONLINE HOSTING: see SETUP.md (runs on GitHub Actions, no server needed).

Setup (run on your own computer):
    pip install requests anthropic pillow
    ffmpeg must be installed for video (brew install ffmpeg / apt install ffmpeg)
    export ANTHROPIC_API_KEY=...
    export GUMROAD_TOKEN=...        # Gumroad > Settings > Advanced > Applications
    export GITHUB_TOKEN=...         # GitHub fine-grained token, read-only on your repos
    # Configure any channels you have (each is optional):
    export BLUESKY_HANDLE=you.bsky.social  BLUESKY_APP_PASSWORD=...
    export MASTODON_URL=https://mastodon.social  MASTODON_TOKEN=...
    export TELEGRAM_BOT_TOKEN=...  TELEGRAM_CHAT_ID=@yourchannel
    export PINTEREST_TOKEN=...  PINTEREST_BOARD_ID=...   # needs Pinterest approval

Hands-off schedule (cron). Posts are spaced >=1h apart, max 3/day, so run `post` hourly:
    0 8 * * *   cd /path/to/agent && python ad_agent.py run --auto    # research + plan + first post
    0 10-20 * * * cd /path/to/agent && python ad_agent.py post --auto  # releases the rest, spaced out
"""
import base64, io, json, os, re, shutil, subprocess, sys, tempfile, time, datetime as dt
from pathlib import Path
from urllib.parse import urlencode
import requests

DATA = Path("data"); DATA.mkdir(exist_ok=True)
PRODUCTS, QUEUE, PLANS = DATA / "products.json", DATA / "queue.json", DATA / "plans.json"
HISTORY, INSIGHTS = DATA / "history.json", DATA / "insights.json"
MODEL = "claude-sonnet-5-5"
DAILY_PIN_CAP = 3            # never flood a feed
MIN_GAP_SECONDS = 3600       # space posts out
REPLAN_DAYS = 7              # refresh each product's campaign weekly
PINTEREST = "https://api.pinterest.com/v5"


def load(p, default):
    return json.loads(p.read_text()) if p.exists() else default


def save(p, obj):
    p.write_text(json.dumps(obj, indent=2))


def need(name):
    v = os.environ.get(name)
    if not v:
        sys.exit(f"Missing env var {name}")
    return v


def pin_headers():
    return {"Authorization": f"Bearer {need('PINTEREST_TOKEN')}"}


class Permanent(Exception):
    """The platform rejected this specific post; do not retry that channel."""


class Transient(Exception):
    """Login, rate limit or outage; stop now and retry on the next run."""


def _check(r, what):
    if r.ok:
        return r
    if r.status_code in (401, 403, 408, 429) or r.status_code >= 500:
        raise Transient(f"{what}: {r.status_code} {r.text[:120]}")
    raise Permanent(f"{what}: {r.status_code} {r.text[:120]}")


# ---------------- SYNC ----------------
def sync():
    old = load(PRODUCTS, [])
    items, ok = [], 0
    try:
        items += fetch_gumroad(); ok += 1
    except Exception as e:
        print(f"Gumroad sync failed ({e}); keeping previously saved Gumroad products.")
        items += [i for i in old if i["source"] == "gumroad"]
    try:
        items += fetch_github(); ok += 1
    except Exception as e:
        print(f"GitHub sync failed ({e}); keeping previously saved GitHub products.")
        items += [i for i in old if i["source"] == "github"]
    if not ok:
        sys.exit("Both Gumroad and GitHub failed. Check your tokens.")
    save(PRODUCTS, items)
    # snapshot sales counts so we can see which products moved after campaigns
    hist = load(HISTORY, [])
    hist.append({"date": dt.date.today().isoformat(),
                 "sales": {i["id"]: i["sales"] for i in items}})
    save(HISTORY, hist[-90:])
    print(f"Synced {len(items)} products")


def fetch_gumroad():
    items = []
    r = requests.get("https://api.gumroad.com/v2/products",
                     params={"access_token": need("GUMROAD_TOKEN")}, timeout=30)
    r.raise_for_status()
    for p in r.json().get("products", []):
        items.append({
            "id": f"gumroad:{p['id']}", "source": "gumroad", "name": p.get("name"),
            "description": clean(p.get("description"))[:3000],
            "price": p.get("formatted_price") or p.get("price"),
            "url": p.get("short_url"), "image": p.get("thumbnail_url") or p.get("preview_url"),
            "tags": p.get("tags", []), "sales": p.get("sales_count") or 0,
            "published": p.get("published", True),
        })
    return items


def fetch_github():
    items = []
    gh = {"Authorization": f"Bearer {need('GITHUB_TOKEN')}", "Accept": "application/vnd.github+json"}
    r = requests.get("https://api.github.com/user/repos",
                     params={"per_page": 100, "affiliation": "owner"}, headers=gh, timeout=30)
    r.raise_for_status()
    for repo in r.json():
        if repo.get("fork") or repo.get("private"):
            continue  # only public, original work gets advertised
        rd = requests.get(f"https://api.github.com/repos/{repo['full_name']}/readme",
                          headers={**gh, "Accept": "application/vnd.github.raw+json"}, timeout=30)
        items.append({
            "id": f"github:{repo['full_name']}", "source": "github", "name": repo["name"],
            "description": ((repo.get("description") or "") + "\n" + (rd.text[:3000] if rd.ok else "")),
            "price": None, "url": repo["html_url"], "image": None,
            "tags": repo.get("topics", []), "sales": repo.get("stargazers_count") or 0,
            "published": True,
        })
    return items


# ---------------- LEARN ----------------
def tracked_link(url, product_id, angle):
    slug = re.sub(r"\W+", "-", product_id)[:40]
    q = urlencode({"utm_source": "pinterest", "utm_medium": "social",
                   "utm_campaign": slug, "utm_content": angle or "na"})
    return f"{url}{'&' if '?' in url else '?'}{q}"


def learn():
    queue = load(QUEUE, [])
    today = dt.date.today()
    for q in queue:
        if q["status"] != "posted" or not q.get("pin_id"):
            continue
        posted = dt.datetime.fromisoformat(q["posted_at"]).date()
        if (today - posted).days < 2:
            continue  # too fresh to judge
        r = requests.get(f"{PINTEREST}/pins/{q['pin_id']}/analytics", headers=pin_headers(),
                         params={"start_date": posted.isoformat(), "end_date": today.isoformat(),
                                 "metric_types": "IMPRESSION,SAVE,PIN_CLICK,OUTBOUND_CLICK"},
                         timeout=30)
        if r.ok:
            q["metrics"] = r.json().get("all", {}).get("summary_metrics", {})
    save(QUEUE, queue)

    by_angle, by_product = {}, {}
    for q in queue:
        m = q.get("metrics")
        if not m:
            continue
        for key, bucket in ((q.get("angle"), by_angle), (q["product_id"], by_product)):
            b = bucket.setdefault(key, {"pins": 0, "impressions": 0, "saves": 0, "clicks": 0})
            b["pins"] += 1
            b["impressions"] += m.get("IMPRESSION", 0)
            b["saves"] += m.get("SAVE", 0)
            b["clicks"] += m.get("OUTBOUND_CLICK", 0)
    for bucket in (by_angle, by_product):
        for b in bucket.values():
            b["click_rate"] = round(b["clicks"] / b["impressions"], 4) if b["impressions"] else 0

    hist = load(HISTORY, [])
    sales_delta = {}
    if len(hist) >= 2:
        first, last = hist[0]["sales"], hist[-1]["sales"]
        sales_delta = {k: last.get(k, 0) - first.get(k, 0) for k in last}

    winners = sorted((q for q in queue if q.get("metrics")),
                     key=lambda q: q["metrics"].get("OUTBOUND_CLICK", 0) + q["metrics"].get("SAVE", 0),
                     reverse=True)
    save(INSIGHTS, {"by_angle": by_angle, "by_product": by_product, "sales_delta": sales_delta,
                    "top_titles": [q["title"] for q in winners[:5]],
                    "weak_titles": [q["title"] for q in winners[-3:]] if len(winners) > 5 else []})
    print("Learned from", sum(1 for q in queue if q.get("metrics")), "pins")


# ---------------- TOOLS ----------------
def _json(text):
    """Pull the first JSON object/array out of a model reply (handles fences and chatter)."""
    t = text.strip()
    starts = [i for i in (t.find("{"), t.find("[")) if i != -1]
    if not starts:
        return t
    a = min(starts)
    b = max(t.rfind("}"), t.rfind("]"))
    return t[a:b + 1]


def clean(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html or "")).strip()


def research(client, p):
    """Tool 1: live web research (search keywords, competitors, seasonal timing)."""
    try:
        msg = client.messages.create(
            model=MODEL, max_tokens=1500,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 4}],
            messages=[{"role": "user", "content":
                f"Research for a Pinterest campaign. Product: {p['name']} - {(p['description'] or '')[:500]}\n"
                "Find: (1) keywords/topics people search in this niche, (2) what competing products "
                "do well and where they are weak, (3) seasonal timing. "
                "Reply with 6 concise bullet points, no fluff."}])
        return " ".join(b.text for b in msg.content if b.type == "text")
    except Exception as e:
        return f"(research unavailable: {e})"


def critic(client, product, pins):
    """Tool 2: second Claude pass that blocks pushy, hypey or unsupported-claim pins."""
    try:
        msg = client.messages.create(
            model=MODEL, max_tokens=1200,
            system=('You review ad copy. Score each pin 0-10 for pushiness (hype, urgency, pressure, '
                    'clickbait) and flag any claim the product description does not support. '
                    'Return ONLY JSON: [{"i": int, "pushy": int, "unsupported_claim": bool}]'),
            messages=[{"role": "user", "content": json.dumps({
                "product": (product["description"] or "")[:1500],
                "pins": [{"i": i, "title": x["title"], "description": x["description"]}
                         for i, x in enumerate(pins)]})}])
        return {r["i"]: r for r in json.loads(_json(msg.content[0].text))}
    except Exception:
        return {}  # fail CLOSED: pins with no score are blocked, never posted unreviewed


def make_image(title, subtitle):
    """Tool 3: renders a clean 1000x1500 pin image (needs Pillow). Returns base64 PNG."""
    import textwrap
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (1000, 1500), "#F6F1EA")
    d = ImageDraw.Draw(img)
    try:
        big, small = ImageFont.truetype("DejaVuSans-Bold.ttf", 78), ImageFont.truetype("DejaVuSans.ttf", 40)
    except OSError:
        big = small = ImageFont.load_default()
    y = 400
    for line in textwrap.wrap(title, 20):
        d.text((80, y), line, fill="#222222", font=big); y += 100
    d.rectangle((80, y + 30, 300, y + 38), fill="#C8553D")
    d.text((80, y + 80), subtitle[:40], fill="#555555", font=small)
    buf = io.BytesIO(); img.save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()


def media_source(q):
    if q.get("image"):
        return {"source_type": "image_url", "url": q["image"]}
    return {"source_type": "image_base64", "content_type": "image/png",
            "data": make_image(q["title"], q.get("product_name", ""))}


VIDEO_DIR = DATA / "videos"
PALETTES = [("#F6F1EA", "#222222", "#C8553D"), ("#1F2A44", "#FFFFFF", "#F2B84B"),
            ("#E8F1EF", "#1B3B36", "#2E8B74"), ("#2B2B2B", "#FAFAFA", "#FF6B6B")]


def scene_png(text, path, pal):
    import textwrap
    from PIL import Image, ImageDraw, ImageFont
    bg, fg, accent = pal
    img = Image.new("RGB", (1080, 1920), bg); d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", 88)
    except OSError:
        font = ImageFont.load_default()
    lines = textwrap.wrap(text, 16)
    y = 960 - len(lines) * 60
    for line in lines:
        d.text((90, y), line, fill=fg, font=font); y += 120
    d.rectangle((90, y + 20, 330, y + 30), fill=accent)
    img.save(path)


def make_video(q):
    """Tool 4: renders a short vertical ad video (animated text scenes) with ffmpeg."""
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is not installed")
    VIDEO_DIR.mkdir(exist_ok=True)
    slug = re.sub(r"\W+", "-", (q.get("product_name", "") + "-" + q["title"]).lower())[:60]
    out = VIDEO_DIR / f"{slug}.mp4"
    pal = PALETTES[sum(map(ord, slug)) % len(PALETTES)]
    with tempfile.TemporaryDirectory() as tmp:
        clips = []
        for i, sc in enumerate(q["scenes"]):
            png, clip = f"{tmp}/s{i}.png", f"{tmp}/c{i}.mp4"
            scene_png(sc["text"], png, pal)
            secs = max(2, min(5, int(sc.get("seconds", 3))))
            subprocess.run(["ffmpeg", "-y", "-loop", "1", "-i", png, "-t", str(secs), "-vf",
                            f"zoompan=z='min(zoom+0.0008,1.12)':d={secs * 25}:s=1080x1920:fps=25,format=yuv420p",
                            "-c:v", "libx264", "-r", "25", clip], check=True, capture_output=True)
            clips.append(clip)
        lst = f"{tmp}/list.txt"
        Path(lst).write_text("".join(f"file '{c}'\n" for c in clips))
        subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", str(out)],
                       check=True, capture_output=True)
    # caption + link saved beside the file so you can also post it on TikTok / Reels / Shorts
    (VIDEO_DIR / f"{slug}.txt").write_text(q["description"] + "\n\n" + q["link"])
    return out


def upload_video(q, path):
    """Uploads the rendered video to Pinterest; returns a media_source (raises on failure)."""
    m = _check(requests.post(f"{PINTEREST}/media", headers=pin_headers(), json={"media_type": "video"},
                             timeout=30), "Pinterest video register").json()
    with open(path, "rb") as f:
        _check(requests.post(m["upload_url"], data=m["upload_parameters"], files={"file": f}, timeout=300),
               "Pinterest video upload")
    for _ in range(30):
        st = requests.get(f"{PINTEREST}/media/{m['media_id']}", headers=pin_headers(), timeout=30).json().get("status")
        if st == "succeeded":
            return {"source_type": "video_id", "media_id": m["media_id"], "cover_image_key_frame_time": 1000}
        if st == "failed":
            raise Permanent("Pinterest rejected the video")
        time.sleep(10)
    raise Transient("Pinterest is still processing the video")


# ---------------- PLAN ----------------
SYSTEM = """You are a senior marketing strategist for a solo seller of digital products.
Rules: value first (about 80% useful/inspiring content, 20% direct offer). Never pushy,
no fake urgency, no hype, no false claims. Pinterest is a search engine: write pins people
would save for later, with natural keywords. Be honest about what the product is.
If INSIGHTS are provided, lean toward the angles and styles that earned saves and clicks,
and move away from weak ones, while still testing one new idea.
Return ONLY valid JSON:
{"audience": str, "positioning": str,
 "pins": [{"title": str(<=90 chars), "description": str(<=450 chars, natural keywords),
           "image_idea": str, "angle": "educate|inspire|proof|offer"}],
 "other_platforms": [{"platform": str, "why": str, "first_post": str}],
 "two_week_calendar": [{"day": int, "platform": str, "content": str}],
 "video": {"title": str, "caption": str(<=300 chars), "scenes": [{"text": str(<=10 words), "seconds": 2-4}]}}
Give 4 pins, at most 1 with angle "offer".
Video: 5-6 scenes. Scene 1 is a scroll-stopping hook built on a real problem or true surprising
fact (no clickbait). Middle scenes teach or show real value. The last scene is a calm,
no-pressure invitation to take a look. No fake urgency, no unsupported claims."""


def plan():
    import anthropic
    client = anthropic.Anthropic()
    products, queue, plans = load(PRODUCTS, []), load(QUEUE, []), load(PLANS, {})
    insights = load(INSIGHTS, {})
    if not products:
        sys.exit("Run `sync` first.")
    existing = {q["title"].lower() for q in queue}
    for p in products:
        if not p.get("published") or not p.get("url"):
            continue
        last = plans.get(p["id"], {}).get("planned_on")
        if last and (dt.date.today() - dt.date.fromisoformat(last)).days < REPLAN_DAYS:
            continue
        payload = {"product": p, "RESEARCH": research(client, p), "INSIGHTS": {
            "angles": insights.get("by_angle"), "this_product": insights.get("by_product", {}).get(p["id"]),
            "top_titles": insights.get("top_titles"), "weak_titles": insights.get("weak_titles"),
            "recent_sales_change": insights.get("sales_delta", {}).get(p["id"])}}
        try:
            msg = client.messages.create(model=MODEL, max_tokens=3000, system=SYSTEM,
                                         messages=[{"role": "user", "content": json.dumps(payload)}])
            out = json.loads(_json(msg.content[0].text))
        except Exception as e:
            print(f"Skipped {p['name']}: {type(e).__name__} {str(e)[:100]}"); continue
        out["planned_on"] = dt.date.today().isoformat()
        plans[p["id"]] = out
        pins = out.get("pins", [])
        scores = critic(client, p, pins)
        for i, pin in enumerate(pins):
            s_ = scores.get(i)
            if s_ is None or s_.get("pushy", 10) > 4 or s_.get("unsupported_claim"):
                print("  Critic blocked:", pin["title"]); continue
            if pin["title"].lower() in existing:
                continue
            existing.add(pin["title"].lower())
            queue.append({"product_id": p["id"], "product_name": p["name"], "title": pin["title"][:100],
                          "description": pin["description"][:800],
                          "link": tracked_link(p["url"], p["id"], pin.get("angle")),
                          "image": p.get("image"), "image_idea": pin.get("image_idea"),
                          "angle": pin.get("angle"), "status": "pending"})
        vid = out.get("video")
        if vid and vid.get("scenes"):
            vs = critic(client, p, [{"title": vid.get("title", ""),
                                     "description": " ".join(x["text"] for x in vid["scenes"])}]).get(0)
            if vs is not None and vs.get("pushy", 10) <= 4 and not vs.get("unsupported_claim"):
                queue.append({"type": "video", "product_id": p["id"], "product_name": p["name"],
                              "title": vid.get("title", p["name"])[:100],
                              "description": vid.get("caption", "")[:800], "scenes": vid["scenes"],
                              "link": tracked_link(p["url"], p["id"], "video"), "image": None,
                              "angle": "video", "status": "pending"})
            else:
                print("  Critic blocked video")
        print(f"Planned: {p['name']}")
    save(PLANS, plans); save(QUEUE, queue)


# ---------------- REVIEW ----------------
def review():
    queue = load(QUEUE, [])
    for q in queue:
        if q["status"] != "pending":
            continue
        print(f"\n[{q['angle']}] {q['title']}\n{q['description']}\n{('VIDEO: ' + ' | '.join(x['text'] for x in q['scenes'])) if q.get('type') == 'video' else 'Image: ' + (q['image'] or 'auto-generated')}")
        a = input("approve / reject / skip (a/r/s)? ").strip().lower()
        q["status"] = {"a": "approved", "r": "rejected"}.get(a, "pending")
    save(QUEUE, queue)


# ---------------- POST ----------------
def dry_run():
    return os.environ.get("DRY_RUN", "").lower() in ("1", "yes", "true")


def write_preview():
    """Phone-friendly summary of everything queued, written to data/preview.md."""
    queue, plans = load(QUEUE, []), load(PLANS, {})
    out = [f"# Agent preview ({dt.date.today()})", "DRY RUN: nothing has been posted.\n"]
    for q in queue:
        if q["status"] in ("pending", "approved"):
            kind = "VIDEO" if q.get("type") == "video" else "PIN"
            out.append(f"## [{kind}/{q.get('angle')}] {q['title']}\n{q['description']}\n")
            if q.get("type") == "video":
                out.append("Scenes: " + " | ".join(x["text"] for x in q["scenes"]) + "\n")
            out.append(f"Link: {q['link']}\n")
    out.append("# Other platforms (post these yourself)")
    for pid, p in plans.items():
        for o in p.get("other_platforms", []):
            out.append(f"- **{pid}** on {o['platform']}: {o['why']}. First post: {o.get('first_post', '')}")
    (DATA / "preview.md").write_text("\n".join(out))
    print("DRY RUN: wrote data/preview.md; nothing posted.")


# ---------------- CHANNELS ----------------
def image_bytes(q):
    if q.get("image"):
        try:
            r = requests.get(q["image"], timeout=30)
            if r.ok:
                return r.content, r.headers.get("Content-Type", "image/jpeg")
        except requests.RequestException:
            pass
    return base64.b64decode(make_image(q["title"], q.get("product_name", ""))), "image/png"


def prepare_media(q):
    if q.get("type") == "video":
        return {"kind": "video", "path": make_video(q)}
    b, ct = image_bytes(q)
    return {"kind": "image", "bytes": b, "ctype": ct}


def link_for(q, channel):
    return q["link"].replace("utm_source=pinterest", f"utm_source={channel}")


def short_text(q, channel, limit):
    """Title + description trimmed so that body + link always fits in `limit`."""
    url = link_for(q, channel)
    body = f"{q['title']}\n\n{q['description']}"
    room = limit - len(url) - 4
    if len(body) > room:
        body = body[:room - 1].rstrip() + "…"
    return f"{body}\n\n{url}", url


def post_pinterest(q, media):
    ms = upload_video(q, media["path"]) if media["kind"] == "video" else media_source(q)
    r = _check(requests.post(f"{PINTEREST}/pins", headers=pin_headers(), timeout=30, json={
        "board_id": need("PINTEREST_BOARD_ID"), "title": q["title"],
        "description": q["description"], "link": q["link"], "media_source": ms}), "Pinterest pin")
    return r.json().get("id")


def post_bluesky(q, media):
    if media["kind"] != "image":
        return None  # Bluesky video needs a separate upload service; images only for now
    base = "https://bsky.social/xrpc"
    s = _check(requests.post(f"{base}/com.atproto.server.createSession", timeout=30, json={
        "identifier": need("BLUESKY_HANDLE"), "password": need("BLUESKY_APP_PASSWORD")}),
        "Bluesky login").json()
    h = {"Authorization": f"Bearer {s['accessJwt']}"}
    blob = _check(requests.post(f"{base}/com.atproto.repo.uploadBlob", timeout=60, data=media["bytes"],
                                headers={**h, "Content-Type": media["ctype"]}), "Bluesky image").json()["blob"]
    text, url = short_text(q, "bluesky", 300)
    start = len(text[:text.rindex(url)].encode("utf-8"))
    record = {"$type": "app.bsky.feed.post", "text": text,
              "createdAt": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
              "facets": [{"index": {"byteStart": start, "byteEnd": start + len(url.encode("utf-8"))},
                          "features": [{"$type": "app.bsky.richtext.facet#link", "uri": url}]}],
              "embed": {"$type": "app.bsky.embed.images",
                        "images": [{"alt": q["title"], "image": blob}]}}
    r = _check(requests.post(f"{base}/com.atproto.repo.createRecord", headers=h, timeout=30, json={
        "repo": s["did"], "collection": "app.bsky.feed.post", "record": record}), "Bluesky post")
    return r.json().get("uri")


def post_mastodon(q, media):
    base = need("MASTODON_URL").rstrip("/")
    h = {"Authorization": f"Bearer {need('MASTODON_TOKEN')}"}
    if media["kind"] == "video":
        with open(media["path"], "rb") as f:
            r = requests.post(f"{base}/api/v2/media", headers=h, timeout=180, data={"description": q["title"]},
                              files={"file": (media["path"].name, f, "video/mp4")})
    else:
        r = requests.post(f"{base}/api/v2/media", headers=h, timeout=60, data={"description": q["title"]},
                          files={"file": ("pin.png", media["bytes"], media["ctype"])})
    _check(r, "Mastodon media")
    mid = r.json()["id"]
    if r.status_code == 202:  # still processing server-side
        for _ in range(30):
            if requests.get(f"{base}/api/v1/media/{mid}", headers=h, timeout=30).status_code == 200:
                break
            time.sleep(5)
    # Mastodon counts every URL as 23 characters, so there is room for more text
    body = f"{q['title']}\n\n{q['description']}"
    body = body if len(body) <= 450 else body[:449].rstrip() + "…"
    r = _check(requests.post(f"{base}/api/v1/statuses", headers=h, timeout=30, json={
        "status": f"{body}\n\n{link_for(q, 'mastodon')}", "media_ids": [mid], "visibility": "public"}),
        "Mastodon post")
    return r.json().get("id")


def post_telegram(q, media):
    tok, chat = need("TELEGRAM_BOT_TOKEN"), need("TELEGRAM_CHAT_ID")
    body = f"{q['title']}\n\n{q['description']}"
    cap = (body if len(body) <= 850 else body[:849] + "…") + "\n\n" + link_for(q, "telegram")
    api = f"https://api.telegram.org/bot{tok}"
    if media["kind"] == "video":
        with open(media["path"], "rb") as f:
            r = requests.post(f"{api}/sendVideo", data={"chat_id": chat, "caption": cap},
                              files={"video": f}, timeout=180)
    else:
        r = requests.post(f"{api}/sendPhoto", data={"chat_id": chat, "caption": cap},
                          files={"photo": ("pin.png", media["bytes"])}, timeout=60)
    _check(r, "Telegram")
    return str(r.json().get("result", {}).get("message_id"))


CHANNELS = {  # name: (is it configured?, poster)
    "pinterest": (lambda: os.environ.get("PINTEREST_TOKEN") and os.environ.get("PINTEREST_BOARD_ID"), post_pinterest),
    "bluesky": (lambda: os.environ.get("BLUESKY_HANDLE") and os.environ.get("BLUESKY_APP_PASSWORD"), post_bluesky),
    "mastodon": (lambda: os.environ.get("MASTODON_URL") and os.environ.get("MASTODON_TOKEN"), post_mastodon),
    "telegram": (lambda: os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"), post_telegram),
}


def enabled_channels():
    return [n for n, (ok, _) in CHANNELS.items() if ok()]


def post(auto=False):
    if dry_run():
        write_preview(); return
    chans = enabled_channels()
    if not chans:
        sys.exit("No channel configured. Set credentials for at least one (see SETUP.md).")
    print("Channels:", ", ".join(chans))
    queue = load(QUEUE, [])
    if auto:  # hands-off: approve everything the critic let through
        for q in queue:
            if q["status"] == "pending":
                q["status"] = "approved"
    today = dt.date.today().isoformat()
    done_today = sum(1 for q in queue if q.get("posted_at", "").startswith(today))
    stamps = [dt.datetime.fromisoformat(q["posted_at"]) for q in queue if q.get("posted_at")]
    last_post = max(stamps) if stamps else None
    groups = {}  # round-robin across products so one product never takes the whole day
    for q in queue:
        if q["status"] == "approved":
            groups.setdefault(q["product_id"], []).append(q)
    posted_n = {}
    for q in queue:
        if q["status"] == "posted":
            posted_n[q["product_id"]] = posted_n.get(q["product_id"], 0) + 1
    order = []  # always pick the product that has had the fewest posts so far
    while any(groups.values()):
        pid = min((k for k, g in groups.items() if g), key=lambda k: posted_n.get(k, 0))
        order.append(groups[pid].pop(0)); posted_n[pid] = posted_n.get(pid, 0) + 1
    order.sort(key=lambda q: not q.get("channels"))  # finish half-posted items first
    for q in order:
        resuming = bool(q.get("channels"))  # already live somewhere: just finish the other channels
        if not resuming:
            if done_today >= DAILY_PIN_CAP:
                print("Daily cap reached; the rest wait until tomorrow."); break
            if last_post and (dt.datetime.now() - last_post).total_seconds() < MIN_GAP_SECONDS:
                wait = int(MIN_GAP_SECONDS - (dt.datetime.now() - last_post).total_seconds()) // 60
                print(f"Spacing posts out: next one in ~{wait} min (run `post` again then)."); break
        try:
            media = prepare_media(q)
        except Exception as e:
            print("Media failed:", q["title"], str(e)[:120]); q["status"], q["error"] = "failed", str(e)[:200]
            save(QUEUE, queue); continue
        done, bad = q.setdefault("channels", {}), q.setdefault("failed_channels", {})
        stop = False
        for ch in chans:
            if ch in done or ch in bad:
                continue
            try:
                res = CHANNELS[ch][1](q, media)
                if res is None:
                    bad[ch] = "format not supported"; continue
                done[ch] = res; print(f"Posted to {ch}: {q['title']}")
            except Permanent as e:
                bad[ch] = str(e)[:200]; print(f"{ch} rejected this post: {e}")
            except (Transient, requests.RequestException) as e:
                print(f"{ch} paused, will retry next run: {str(e)[:150]}"); stop = True
        if done and not q.get("posted_at"):
            q["posted_at"] = dt.datetime.now().isoformat()
            last_post = dt.datetime.now(); done_today += 1
        if "pinterest" in done:
            q["pin_id"] = done["pinterest"]
        if all(c in done or c in bad for c in chans):
            q["status"] = "posted" if done else "failed"
        save(QUEUE, queue)
        if stop:
            break
    save(QUEUE, queue)


# ---------------- REPORT ----------------
def report():
    ins, plans = load(INSIGHTS, {}), load(PLANS, {})
    print(json.dumps(ins, indent=2))
    for pid, p in plans.items():
        print(f"\n== {pid}\nAudience: {p.get('audience')}")
        for o in p.get("other_platforms", []):
            print(f" - {o['platform']}: {o['why']}")


def run(auto=False):
    sync(); learn(); plan()
    if not auto:
        review()
    post(auto=auto)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    auto = "--auto" in sys.argv
    if cmd == "run": run(auto)
    elif cmd == "post": post(auto)
    elif cmd in ("sync", "learn", "plan", "review", "report"): globals()[cmd]()
    else: print(__doc__)

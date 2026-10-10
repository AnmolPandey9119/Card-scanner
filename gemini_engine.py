"""Gemini card reader: reads FRONT + BACK together in ONE request, fast, and free-tier friendly.

Why it is fast / does not break on the free tier:
  1. ONE request per card: both sides are sent together (each labelled FRONT / BACK) so the model
     merges them into a single row - details printed only on the back also land in the Excel.
  2. Thinking is kept at the lowest level the model allows (it is the main reason for slow answers); the prompt makes
     the model transcribe the card first and then fill the columns, which keeps accuracy high without thinking.
  3. Every model has its own requests-per-minute window. If the main model is busy for a long time the next
     model in GEMINI_MODELS takes the card instead of everybody waiting.
  4. HTTP 429: waits for the retryDelay Google asks for; a finished DAILY quota (or a 404 "model not available")
     parks that model for an hour, so it is not asked again for every card.
  5. Connections are reused (keep-alive), identical cards are cached so "Retry" never wastes a request.
  6. If every model is exhausted it raises QuotaExhausted (the app then shows a clear "Retry" message).

Env / secrets:
  GEMINI_API_KEY   required (https://aistudio.google.com/apikey)
  GEMINI_MODELS    optional, comma separated, tried in order (default below)
  GEMINI_RPM       optional, max requests per minute PER MODEL (default: 9, lite models 14)
"""
import base64
import hashlib
import json
import os
import re
import threading
import time
from collections import deque

import requests
from requests.adapters import HTTPAdapter

# Gemini 2.5 models are closed for NEW projects (since Sept 2026) -> 404. These are the current free-tier models.
# Each model has its own quota, so when one is busy the next one takes the card.
DEFAULT_MODELS = "gemini-3.8-flash,gemini-3.5-flash,gemini-3.5-flash-lite"
API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
FIELDS = ["Name", "Designation", "Company", "Mobile", "Phone / Landline", "Email", "Website", "Address",
          "City", "State", "Pincode", "Country", "LinkedIn / Social", "Services / Products", "Other Notes"]

SPILL_WAIT = 4.0      # main model busy for longer than this (seconds) -> use the next model
MAX_CARD_WAIT = 240   # give up on one card after this many seconds of waiting / retrying
CACHE_MAX = 300

PROMPT = (
    "You are an expert data-entry operator reading Indian and international business cards. "
    "You get the FRONT image and, if present, the BACK image of ONE card (each image is labelled). "
    "Treat both sides as ONE card: read BOTH completely and merge everything into one record - a detail that is "
    "printed only on the back (address, second phone, email, website, products, branches) must be filled in too.\n"
    "Return ONLY a JSON object. The FIRST key is \"Card Text\": transcribe every printed line of the card exactly as "
    "printed, front then back, lines separated by \" / \". Then these string keys, in this order: "
    + ", ".join(f'"{f}"' for f in FIELDS) + ".\n"
    "Fill the columns from your transcription. Every value goes into ONE correct column only:\n"
    "- Name = the person's name only (keep Mr./Dr./CA if printed). Never a company, never a designation. Degrees such as "
    "B.Tech / MBA go to Other Notes.\n"
    "- Designation = ONLY that person's job title (e.g. 'Sales Manager', 'Director - Exports'). It must NOT contain the "
    "company name, city, phone or email. If title and company share one line ('Director, Sharma Traders') split them.\n"
    "- Company = ONLY the organisation's name (e.g. 'Sharma Traders Pvt. Ltd.'). It must NOT contain the person's designation, "
    "city, state, pincode, tagline or 'Since 1998'. A branch/city word after the company ('ABC Enterprises, Noida') goes to "
    "City, not to Company. Keep a city that is truly part of the registered name ('Delhi Public School', "
    "'Indian Institute of Technology Delhi'). Taglines, 'Manufacturers of ...' go to Services / Products.\n"
    "- Address = street / building / area / sector part as printed, WITHOUT city, state, pincode, country. City, State, Pincode, "
    "Country each go in their own column. City is ONLY the city/town name ('Noida', not 'Noida - 201301'). Write the full "
    "state name ('U.P.' -> 'Uttar Pradesh'). If front and back show different addresses (head office / factory) keep the "
    "first in Address and the other in Other Notes.\n"
    "- Mobile = mobile numbers (Indian 10-digit as '+91 XXXXXXXXXX'); Phone / Landline = landline/office numbers with STD "
    "code; a fax is written '<number> (Fax)' in Phone / Landline. Several values are separated by '; '.\n"
    "- Email = email addresses only; Website = websites only; LinkedIn / Social = social profile links only.\n"
    "- Services / Products = what the business makes or offers (front and back). Other Notes = GST no., CIN, a second "
    "person's name and title, and anything that fits nowhere else.\n"
    "Rules: use \"\" for anything NOT printed on the card - never guess or invent, never infer a city, state or country that is "
    "not printed. Do not duplicate a detail that appears on both sides. If the card lists several people, use the most "
    "prominent one and put the others in Other Notes. Ignore Hindi/regional-script duplicates of English text. Fix only "
    "unambiguous print/OCR slips (e.g. 'rajesh(a)xyz.com' -> 'rajesh@xyz.com').\n"
    "Example 1 - FRONT: 'ABC Enterprises Pvt Ltd, Noida / Rajesh Kumar / Sales Manager / M: 98765 43210'; BACK: 'Plot 5, "
    "Sector 62, Noida - 201301 (U.P.) / www.abc.com / Manufacturers of Pumps & Valves' -> Name 'Rajesh Kumar', Designation "
    "'Sales Manager', Company 'ABC Enterprises Pvt Ltd', Mobile '+91 9876543210', Address 'Plot 5, Sector 62', City 'Noida', "
    "State 'Uttar Pradesh', Pincode '201301', Website 'www.abc.com', Services / Products 'Pumps & Valves'.\n"
    "Example 2 - FRONT: 'Dr. Neha Gupta - Director, Green Valley Foods' -> Name 'Dr. Neha Gupta', Designation 'Director', "
    "Company 'Green Valley Foods' (City stays \"\" because no city is printed)."
)


class QuotaExhausted(RuntimeError):
    """Gemini could not read this card right now (limit reached / not available)."""


class _Limiter:
    """Sliding 60 s window for ONE model, shared by all threads."""

    def __init__(self):
        self.lock = threading.Lock()
        self.stamps = deque()

    def try_acquire(self, rpm) -> float:
        """0.0 = a slot was taken, otherwise the seconds until one frees up."""
        with self.lock:
            now = time.time()
            while self.stamps and now - self.stamps[0] > 60:
                self.stamps.popleft()
            if len(self.stamps) < rpm:
                self.stamps.append(now)
                return 0.0
            return 60 - (now - self.stamps[0]) + 0.1


_limiters = {}
_cooldown = {}            # model -> unix time until which it is skipped (daily quota / 429)
_no_think = set()         # models that reject thinkingConfig
_cache = {}               # image hash -> result dict
_state_lock = threading.Lock()
_tls = threading.local()
status = {"last": "", "fallbacks": 0}   # read by the UI


def enabled() -> bool:
    return bool(os.environ.get("GEMINI_API_KEY", "").strip())


def _models():
    return [m.strip() for m in os.environ.get("GEMINI_MODELS", DEFAULT_MODELS).split(",") if m.strip()]


def _rpm(model) -> int:
    try:
        return max(1, int(os.environ["GEMINI_RPM"]))
    except (KeyError, ValueError):
        return 14 if "lite" in model else 9


def _limiter(model) -> _Limiter:
    with _state_lock:
        return _limiters.setdefault(model, _Limiter())


def _session() -> requests.Session:
    s = getattr(_tls, "session", None)
    if s is None:
        s = requests.Session()
        s.mount("https://", HTTPAdapter(pool_connections=2, pool_maxsize=4, max_retries=0))
        _tls.session = s
    return s


def _retry_delay(resp) -> float:
    """Seconds Google asks us to wait (details.retryDelay, message text or Retry-After)."""
    try:
        err = resp.json().get("error", {})
        for d in err.get("details", []):
            rd = d.get("retryDelay")
            if rd:
                return float(str(rd).rstrip("s"))
        m = re.search(r"retry in ([\d.]+)s", err.get("message", ""), re.I)
        if m:
            return float(m.group(1))
    except Exception:
        pass
    h = resp.headers.get("Retry-After")
    return float(h) if h and h.replace(".", "").isdigit() else 15.0


def _is_daily(resp) -> bool:
    try:
        return "perday" in resp.text.replace(" ", "").lower()
    except Exception:
        return False


def _pick_model(models, dead, deadline):
    """Reserve a request slot on the best available model; wait if none is free. None = nothing usable."""
    while True:
        now = time.time()
        waits, first = [], True
        for m in models:
            if m in dead:
                continue
            cool = _cooldown.get(m, 0) - now
            if cool > 60:                 # parked (daily quota): not worth waiting for
                continue
            if cool > 0:
                waits.append(cool)
                continue
            w = _limiter(m).try_acquire(_rpm(m))
            if w == 0:
                return m
            waits.append(w)
            if first and w <= SPILL_WAIT:  # main model frees up in a moment: wait for it, keep accuracy
                break
            first = False
        if not waits or now + min(waits) > deadline:
            return None
        time.sleep(max(min(waits), 0.1))


def _thinking_cfg(model):
    """Lowest-latency thinking setting each model family accepts (None = leave the model default)."""
    if model in _no_think:
        return None
    if re.search(r"gemini-2\.5", model):
        return {"thinkingBudget": 0}                 # 2.5 models: budget 0 = off
    if re.search(r"gemini-3\.[78]-", model):
        return {"thinkingLevel": "low"}              # 3.7 / 3.8 Flash do not allow "minimal"
    return {"thinkingLevel": "minimal"}              # 3.5 Flash, 3.x Flash-Lite


def _call(model, images, key):
    parts = [{"text": PROMPT}]
    for label, b in images:
        parts.append({"text": f"{label} image:"})
        parts.append({"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(b).decode()}})
    gen = {"responseMimeType": "application/json", "maxOutputTokens": 2048}
    if "gemini-2.5" in model:
        gen["temperature"] = 0                            # Gemini 3 models are best left at their default temperature
    think = _thinking_cfg(model)
    if think:
        gen["thinkingConfig"] = think                     # no long hidden "thinking": much faster answers
    return _session().post(API_URL.format(model=model), headers={"x-goog-api-key": key},
                           json={"contents": [{"parts": parts}], "generationConfig": gen}, timeout=(8, 60))


def _parse(resp) -> dict:
    data = resp.json()
    parts = data["candidates"][0]["content"]["parts"]
    text = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    obj = json.loads(text)
    if isinstance(obj, list) and obj:
        obj = obj[0]
    out = {}
    for f in FIELDS:                      # "Card Text" is only the model's scratch pad, not a column
        v = obj.get(f, "")
        out[f] = "; ".join(map(str, v)) if isinstance(v, list) else str(v or "").strip()
    return out


def read_card_gemini(front_jpeg, back_jpeg) -> dict:
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        raise QuotaExhausted("GEMINI_API_KEY set nahi hai")
    images = [(lbl, b) for lbl, b in (("FRONT", front_jpeg), ("BACK", back_jpeg)) if b]
    if not images:
        raise QuotaExhausted("Card ki koi image nahi mili")
    h = hashlib.sha1(b"|".join(lbl.encode() + b for lbl, b in images)).hexdigest()
    with _state_lock:
        if h in _cache:
            return dict(_cache[h])

    models, dead, bad = _models(), set(), {}
    deadline = time.time() + MAX_CARD_WAIT
    errs = {}                                  # model -> last problem, shown together if everything fails
    last_err = "unknown error"
    for attempt in range(10):
        model = _pick_model(models, dead, deadline)
        if model is None:
            break
        try:
            resp = _call(model, images, key)
        except requests.RequestException as e:
            last_err = f"network: {e}"
            time.sleep(min(2 * (attempt + 1), 6))
            continue
        code = resp.status_code
        if code == 200:
            try:
                result = _parse(resp)
            except Exception as e:        # blocked / malformed output -> try again, then the next model
                last_err = errs[model] = f"{model}: bad response ({e})"
                bad[model] = bad.get(model, 0) + 1
                if bad[model] >= 2:
                    dead.add(model)
                continue
            with _state_lock:
                if len(_cache) >= CACHE_MAX:
                    _cache.pop(next(iter(_cache)))
                _cache[h] = result
                status["last"] = f"Gemini OK ({model})"
            return dict(result)
        if code == 429:
            if _is_daily(resp):           # per-day quota finished: park this model for 1 hour
                _cooldown[model] = time.time() + 3600
                last_err = errs[model] = f"{model}: daily quota finished"
            else:
                delay = min(_retry_delay(resp), 60) + 1
                _cooldown[model] = time.time() + delay
                last_err = errs[model] = f"{model}: rate limited ({delay:.0f}s wait)"
            continue
        if code in (500, 502, 503, 504):
            last_err = errs[model] = f"{model}: server busy ({code})"
            time.sleep(min(3 * (attempt + 1), 8))
            continue
        if code == 400 and "thinking" in resp.text.lower() and model not in _no_think:
            _no_think.add(model)          # this model does not accept thinkingConfig -> retry without it
            continue
        dead.add(model)                   # bad key (400/403), retired model (404), anything else
        if code == 404:                   # model closed / renamed: do not ask it again for every card
            _cooldown[model] = time.time() + 3600
        last_err = errs[model] = f"{model}: HTTP {code} {re.sub(r'\\s+', ' ', resp.text)[:110]}"
    last_err = " | ".join(errs.values()) or last_err
    with _state_lock:
        status["last"] = f"Gemini limit/err: {last_err}"
    raise QuotaExhausted(last_err)
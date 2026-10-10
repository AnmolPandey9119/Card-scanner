"""Gemini card reader that survives "Limit reached (requests)" / HTTP 429 errors.

What it does to avoid the limit problem:
  1. ONE request per card (front + back images are sent together) -> half the requests.
  2. A shared rate limiter (GEMINI_RPM, default 8/min) so parallel workers never burst past the free tier.
  3. On HTTP 429 it waits for the retryDelay Google asks for, then retries (max 3 times).
  4. If a model's DAILY quota is gone, it switches to the next model in GEMINI_MODELS.
  5. If every model is exhausted, it raises QuotaExhausted and card_reader falls back to the offline OCR,
     so a card is never lost.
  6. Identical images are cached, so "Retry" never wastes a request.

Env / secrets:
  GEMINI_API_KEY   required to enable Gemini (https://aistudio.google.com/apikey)
  GEMINI_MODELS    optional, comma separated, tried in order (default below)
  GEMINI_RPM       optional, max requests per minute (default 8; free tier flash is ~10)
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

DEFAULT_MODELS = "gemini-2.5-flash,gemini-2.5-flash-lite"
API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
FIELDS = ["Name", "Designation", "Company", "Mobile", "Phone / Landline", "Email", "Website", "Address",
          "City", "State", "Pincode", "Country", "LinkedIn / Social", "Services / Products", "Other Notes"]

PROMPT = (
    "You are an expert data-entry operator reading Indian/International business cards. "
    "Image 1 is the FRONT, image 2 (if present) is the BACK. Return ONLY a JSON object with exactly these string keys: "
    + ", ".join(f'"{f}"' for f in FIELDS) + ".\n"
    "Read the card like a human would and put every value in ONE correct column only:\n"
    "- Name = the person's name only (keep Mr./Dr. if printed). Never a company, never a designation.\n"
    "- Designation = ONLY the job title of that person (e.g. 'Sales Manager', 'Director - Exports'). It must NOT contain the "
    "company name, city, phone or department-of-company text. If the title and company are on one line, split them.\n"
    "- Company = ONLY the organisation's name (e.g. 'Sharma Traders Pvt. Ltd.'). It must NOT contain the person's designation, "
    "city, state, pincode or tagline. Taglines / 'Since 1998' / 'Manufacturers of ...' go to Services / Products or Other Notes.\n"
    "- Address = street/area/building/sector part of the address, as printed. City, State, Pincode, Country each go in their own "
    "column; City is ONLY the city/town name (e.g. 'Noida', not 'Noida - 201301' and not part of Company).\n"
    "- Mobile = mobile numbers (Indian 10-digit as '+91 XXXXXXXXXX'); Phone / Landline = landline/office numbers, with STD code; "
    "a fax number is written as '<number> (Fax)' in Phone / Landline. Several values are separated by '; '.\n"
    "- Email = email addresses only; Website = website only; LinkedIn / Social = social profile links only.\n"
    "- Services / Products = what the business makes or offers. Other Notes = GST number, CIN and anything that fits nowhere else.\n"
    "Rules: use \"\" for anything NOT printed on the card (never guess or invent, never infer a city from the company name). "
    "Use the front and back together: if the back repeats the same details, do not duplicate them. "
    "Fix obvious OCR-style slips only when the text is unambiguous (e.g. 'rajesh(a)xyz.com' -> 'rajesh@xyz.com').\n"
    "Example: card text 'Rajesh Kumar / Sales Manager / ABC Enterprises Pvt Ltd / Plot 5, Sector 62, Noida - 201301 (U.P.)' -> "
    "Name 'Rajesh Kumar', Designation 'Sales Manager', Company 'ABC Enterprises Pvt Ltd', Address 'Plot 5, Sector 62', "
    "City 'Noida', State 'Uttar Pradesh', Pincode '201301'."
)


class QuotaExhausted(RuntimeError):
    """All Gemini models are out of quota right now."""


class _Limiter:
    """Sliding-window limiter shared by all threads."""

    def __init__(self):
        self.lock = threading.Lock()
        self.stamps = deque()

    def wait(self, rpm):
        while True:
            with self.lock:
                now = time.time()
                while self.stamps and now - self.stamps[0] > 60:
                    self.stamps.popleft()
                if len(self.stamps) < rpm:
                    self.stamps.append(now)
                    return
                sleep_for = 60 - (now - self.stamps[0]) + 0.2
            time.sleep(max(sleep_for, 0.2))


_limiter = _Limiter()
_cooldown = {}            # model -> unix time until which we skip it (daily quota hit)
_cache = {}               # image hash -> result dict
_state_lock = threading.Lock()
status = {"last": "", "fallbacks": 0}   # read by the UI


def enabled() -> bool:
    return bool(os.environ.get("GEMINI_API_KEY", "").strip())


def _models():
    return [m.strip() for m in os.environ.get("GEMINI_MODELS", DEFAULT_MODELS).split(",") if m.strip()]


def _rpm():
    try:
        return max(1, int(os.environ.get("GEMINI_RPM", "8")))
    except ValueError:
        return 8


def _retry_delay(resp) -> float:
    """Seconds Google asks us to wait (from details.retryDelay or the message text)."""
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


def _call(model, images, key):
    parts = [{"text": PROMPT}] + [
        {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(b).decode()}} for b in images
    ]
    body = {"contents": [{"parts": parts}],
            "generationConfig": {"temperature": 0, "responseMimeType": "application/json"}}
    return requests.post(API_URL.format(model=model), params={"key": key}, json=body, timeout=90)


def _parse(resp) -> dict:
    data = resp.json()
    text = data["candidates"][0]["content"]["parts"][0]["text"]
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    obj = json.loads(text)
    if isinstance(obj, list) and obj:
        obj = obj[0]
    out = {}
    for f in FIELDS:
        v = obj.get(f, "")
        out[f] = "; ".join(map(str, v)) if isinstance(v, list) else str(v or "").strip()
    return out


def read_card_gemini(front_jpeg, back_jpeg) -> dict:
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        raise QuotaExhausted("GEMINI_API_KEY set nahi hai")
    images = [b for b in (front_jpeg, back_jpeg) if b]
    h = hashlib.sha1(b"".join(images)).hexdigest()
    with _state_lock:
        if h in _cache:
            return dict(_cache[h])

    last_err = "unknown error"
    for model in _models():
        with _state_lock:
            if _cooldown.get(model, 0) > time.time():
                continue
        for attempt in range(4):
            _limiter.wait(_rpm())
            try:
                resp = _call(model, images, key)
            except requests.RequestException as e:
                last_err = f"network: {e}"
                time.sleep(2 * (attempt + 1))
                continue
            if resp.status_code == 200:
                try:
                    result = _parse(resp)
                except Exception as e:      # blocked / malformed output -> retry once, then next model
                    last_err = f"bad response: {e}"
                    continue
                with _state_lock:
                    _cache[h] = result
                    status["last"] = f"Gemini OK ({model})"
                return dict(result)
            if resp.status_code == 429:
                if _is_daily(resp):      # per-day quota: skip this model for 1 hour
                    with _state_lock:
                        _cooldown[model] = time.time() + 3600
                    last_err = f"{model}: daily quota finished"
                    break
                delay = min(_retry_delay(resp), 60) + 1
                last_err = f"{model}: rate limited, waited {delay:.0f}s"
                time.sleep(delay)
                continue
            if resp.status_code in (500, 502, 503, 504):
                last_err = f"{model}: server busy ({resp.status_code})"
                time.sleep(3 * (attempt + 1))
                continue
            if resp.status_code in (400, 403):   # bad key / not allowed -> no point retrying this model
                last_err = f"{model}: {resp.status_code} {resp.text[:150]}"
                break
            if resp.status_code == 404:           # model name retired
                last_err = f"{model}: model not found"
                break
            last_err = f"{model}: HTTP {resp.status_code}"
            break
    with _state_lock:
        status["last"] = f"Gemini limit/err: {last_err}"
    raise QuotaExhausted(last_err)
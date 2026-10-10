"""Visiting Card Scanner
Take a photo (phone camera) or upload images of visiting cards (front + back).
Every card is read in the background the moment you add it and its row goes
straight into the Excel, so "Export Excel" is instant.
Run locally:  streamlit run app.py
"""
import hmac
import html
import inspect
import io
import os
import re
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from itertools import zip_longest

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from PIL import Image, ImageOps

import gemini_engine
from card_reader import max_workers, ocr_fallback_enabled, read_card, warmup

MAX_SIDE = 1200   # photos are shrunk to this before being stored / sent (card text stays sharp, upload stays small)
MAX_PENDING = 60  # max cards being read at the same time
REFRESH_SECS = 2  # live status refresh while cards are being read (each refresh redraws the table)
EXTS = ["jpg", "jpeg", "png", "webp"]
# Browser camera: by default Streamlit takes the photo at the on-screen size (~400px wide on a phone = too blurry to read).
# Newer Streamlit can ask the camera for 1080p; older versions simply skip this option.
CAM_KW = {"resolution": "1080p"} if "resolution" in inspect.signature(st.camera_input).parameters else {}

# Newer Streamlit: the Excel is built only when "Export" is clicked and the click does not re-run the page.
_DL_PARAMS = inspect.signature(st.download_button).parameters
DL_LAZY = "on_click" in _DL_PARAMS and "ignore" in str(_DL_PARAMS["on_click"].annotation)
STORE_TTL = 12 * 3600   # a session's cards stay on the server this long after its last visit (survives page refresh)

HEADERS = [
    "Name",
    "Designation",
    "Company",
    "Mobile",
    "Phone / Landline",
    "Email",
    "Website",
    "Address",
    "City",
    "State",
    "Pincode",
    "Country",
    "LinkedIn / Social",
    "Services / Products",
    "Other Notes",
    "Front File",
    "Back File",
]

# Company's universal Excel header (exact spelling/order of Headers_10_1.xlsx - do not "fix" the
# trailing space in 'Product Category ' or the 'Business TYpe' spelling, they match the master sheet).
UNIVERSAL_HEADERS = [
    "SNO", "Source", "FileName", "Updated By", "Visitors/Exhibitors", "Person Linked URL",
    "Company Linked URL", "CompanyName", "Sector", "Sub Sector", "Product Category ", "Business TYpe",
    "Title", "FirstName", "LastName", "Designation", "Address", "City", "Pincode", "State", "Country",
    "Tele1", "Tele2", "Mobile1", "Mobile2", "Fax", "Email1", "Email2", "Website1", "Website2",
    "Status", "Remark", "Rank", "Date of Update", "Event",
]

# Browser camera: ask for the BACK camera by default (falls back to whatever camera exists,
# so laptops with a single webcam keep working). Streamlit's camera widget always asks
# for the front camera, so we wrap getUserMedia in the main page once, and also switch a
# camera that already started before the wrap was in place.
REAR_CAMERA_JS = """
<script>
(function () {
  try {
    var w = window.parent;
    var md = w.navigator.mediaDevices;
    if (!md || !md.getUserMedia) return;
    if (!w.__rearCamPatched) {
      var orig = md.getUserMedia.bind(md);
      md.getUserMedia = function (c) {
        try {
          if (c && c.video) {
            var v = (c.video === true) ? {} : Object.assign({}, c.video);
            v.facingMode = { ideal: "environment" };
            c = Object.assign({}, c, { video: v });
          }
        } catch (e) {}
        return orig(c);
      };
      w.__rearCamPatched = true;
    }
    var tries = 0;
    var timer = setInterval(function () {
      tries += 1;
      if (tries > 20) { clearInterval(timer); return; }
      w.document.querySelectorAll("video").forEach(function (vid) {
        var s = vid.srcObject;
        if (!s || !s.getVideoTracks || vid.__rearChecked) return;
        var t = s.getVideoTracks()[0];
        if (!t) return;
        vid.__rearChecked = true;
        var f = (t.getSettings && t.getSettings().facingMode) || "";
        if (f === "environment") return;
        md.getUserMedia({ video: true }).then(function (ns) {
          s.getTracks().forEach(function (x) { x.stop(); });
          vid.srcObject = ns;
        }).catch(function () {});
      });
    }, 700);
  } catch (e) {}
})();
</script>
"""

STYLE = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');
:root {
  --brand:#1F4E78; --brand2:#2F80D1; --ink:#0F172A; --muted:#64748B; --line:#E2E8F0; --soft:#EEF4FB;
  --ok:#12B76A; --busy:#2F80D1; --warn:#F79009; --bad:#F04438;
}
.stApp, .stApp button, .stApp input, .stApp textarea, [data-testid="stMarkdownContainer"],
[data-testid="stCaptionContainer"], .hero, .stat, .empty, .note {
  font-family: 'Inter', system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif;
}
.block-container { padding-top: 1.1rem; padding-bottom: 5rem; max-width: 1500px; }
footer, #MainMenu { visibility: hidden; }
button { touch-action: manipulation; }   /* no double-tap-zoom delay -> fast repeated taps */

/* ---------- hero ---------- */
.hero { position: relative; overflow: hidden; display: flex; align-items: center; justify-content: space-between; gap: 1rem;
  padding: 1.1rem 1.3rem; margin-bottom: 1rem; border-radius: 18px; color: #fff;
  background: linear-gradient(120deg, #0E3A66 0%, #1F6FB2 58%, #2F9BD6 100%); box-shadow: 0 10px 28px rgba(31,78,120,.25); }
.hero::after { content: ""; position: absolute; right: -70px; top: -80px; width: 240px; height: 240px; border-radius: 50%; background: rgba(255,255,255,.08); }
.hero-left { display: flex; align-items: center; gap: .9rem; min-width: 0; position: relative; z-index: 1; }
.hero-ic { font-size: 1.8rem; width: 3rem; height: 3rem; flex: none; display: grid; place-items: center; border-radius: 14px; background: rgba(255,255,255,.16); }
.hero-t { font-weight: 800; font-size: 1.45rem; letter-spacing: -.01em; line-height: 1.15; }
.hero-s { opacity: .88; font-size: .92rem; margin-top: .2rem; }
.pill { position: relative; z-index: 1; display: inline-flex; align-items: center; gap: .45rem; padding: .35rem .8rem; border-radius: 999px;
  font-size: .8rem; font-weight: 600; background: rgba(255,255,255,.16); white-space: nowrap; }
.pill i { width: .55rem; height: .55rem; border-radius: 50%; background: #4ADE80; box-shadow: 0 0 0 3px rgba(74,222,128,.3); }
.pill.mid i { background: #FBBF24; box-shadow: 0 0 0 3px rgba(251,191,36,.3); }
.pill.off i { background: #F87171; box-shadow: 0 0 0 3px rgba(248,113,113,.3); }

/* ---------- section titles, stats ---------- */
.sec { display: flex; align-items: center; gap: .55rem; font-weight: 700; font-size: 1.08rem; color: var(--ink); margin: .1rem 0 .6rem; }
.sec .n { background: var(--brand); color: #fff; border-radius: 9px; width: 1.65rem; height: 1.65rem; display: grid; place-items: center; font-size: .85rem; }
.cardno { font-size: .98rem; color: var(--ink); margin-bottom: .1rem; }
.slot { font-size: .78rem; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; color: var(--brand); margin: .2rem 0 .35rem; }
.slot small { color: var(--muted); font-weight: 500; letter-spacing: 0; text-transform: none; }
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(92px, 1fr)); gap: .6rem; margin: .1rem 0 .8rem; }
.stat { background: #fff; border: 1px solid var(--line); border-radius: 14px; padding: .65rem .8rem; box-shadow: 0 1px 2px rgba(15,23,42,.04); }
.stat b { display: block; font-size: 1.5rem; line-height: 1.1; font-weight: 800; color: var(--ink); }
.stat span { font-size: .7rem; color: var(--muted); font-weight: 600; letter-spacing: .06em; text-transform: uppercase; }
.stat.ok b { color: var(--ok); } .stat.busy b { color: var(--busy); } .stat.bad b { color: var(--bad); } .stat.warn b { color: var(--warn); }
.stat.live { animation: pulse 1.4s ease-in-out infinite; }
@keyframes pulse { 0%,100% { box-shadow: 0 0 0 0 rgba(47,128,209,.35); } 50% { box-shadow: 0 0 0 7px rgba(47,128,209,0); } }
.note { border-radius: 12px; padding: .65rem .9rem; font-size: .87rem; border: 1px solid; margin: .2rem 0 .7rem; line-height: 1.45; }
.note.bad { background: #FEF3F2; border-color: #FECDCA; color: #B42318; }
.note.info { background: #EFF8FF; border-color: #B2DDFF; color: #175CD3; }
.empty { text-align: center; padding: 2.2rem 1.2rem; border: 2px dashed #CBD5E1; border-radius: 18px; background: #fff; color: var(--muted); }
.empty .big { font-size: 2.6rem; }
.empty h4 { margin: .3rem 0 .2rem; color: var(--ink); font-size: 1.1rem; }
.empty ol { text-align: left; display: inline-block; margin: .6rem 0 0; padding-left: 1.2rem; line-height: 1.8; }

/* ---------- tabs, buttons, uploader, table ---------- */
.stTabs [data-baseweb="tab-list"] { gap: .35rem; background: #E7EEF7; padding: .3rem; border-radius: 14px; }
.stTabs [data-baseweb="tab"] { flex: 1; justify-content: center; height: 2.7rem; border-radius: 10px; background: transparent; font-weight: 600; padding: 0 .5rem; }
.stTabs [aria-selected="true"] { background: #fff; color: var(--brand); box-shadow: 0 1px 4px rgba(15,23,42,.12); }
.stTabs [data-baseweb="tab-highlight"], .stTabs [data-baseweb="tab-border"] { display: none; }

[data-testid="stElementContainer"]:has(> [data-testid="stButton"]),
[data-testid="stElementContainer"]:has(> [data-testid="stDownloadButton"]),
[data-testid="stButton"], [data-testid="stDownloadButton"] { width: 100% !important; }
[data-testid="stButton"] button, [data-testid="stDownloadButton"] button {
    width: 100%; min-height: 3rem; font-size: 1.02rem; font-weight: 600; border-radius: 12px; transition: transform .08s ease, filter .15s ease; }
[data-testid="stButton"] button:active, [data-testid="stDownloadButton"] button:active { transform: scale(.985); }
button[kind="primary"], [data-testid="stBaseButton-primary"] {
    background: linear-gradient(135deg, #1F4E78, #2F80D1) !important; border: 0 !important; color: #fff !important;
    box-shadow: 0 6px 16px rgba(31,78,120,.28); }
button[kind="primary"]:hover, [data-testid="stBaseButton-primary"]:hover { filter: brightness(1.07); }
button[kind="primary"]:disabled, [data-testid="stBaseButton-primary"]:disabled { background: #CBD5E1 !important; box-shadow: none; }

[data-testid="stFileUploaderDropzone"] { border: 2px dashed #B8C7DA; border-radius: 14px; background: #fff; transition: border-color .15s, background .15s; }
[data-testid="stFileUploaderDropzone"]:hover { border-color: var(--brand2); background: #F8FBFF; }
[data-testid="stImage"] img { max-height: 38vh; object-fit: contain; border-radius: 10px; border: 1px solid var(--line); }
[data-testid="stDataFrame"] { border: 1px solid var(--line); border-radius: 14px; overflow: hidden; }
[data-testid="stSidebar"] { background: #fff; border-right: 1px solid var(--line); }

/* front / back slots stay SIDE BY SIDE, also on a phone */
[class*="st-key-slots_"] [data-testid="stHorizontalBlock"] { flex-wrap: nowrap !important; gap: .6rem !important; }
[class*="st-key-slots_"] [data-testid="stHorizontalBlock"] > div { min-width: 0 !important; flex: 1 1 0 !important; }

/* export bar sticks to the bottom while the table is on screen */
.st-key-exportbar { position: sticky; bottom: .6rem; z-index: 40; padding: .55rem .7rem; border-radius: 16px;
    background: rgba(255,255,255,.93); backdrop-filter: blur(8px); border: 1px solid var(--line); box-shadow: 0 10px 28px rgba(15,23,42,.14); }

/* ---------- Browser camera: FULL SCREEN with a big capture bar ---------- */
.st-key-camfs {
    position: fixed !important; inset: 0 !important; z-index: 1000000 !important;
    background: #000; padding: 0 !important; margin: 0 !important; gap: 0 !important;
    display: flex; flex-direction: column; width: 100vw !important; height: 100vh !important; height: 100dvh !important;
    max-width: none !important; overflow: hidden;
}
.st-key-camfs [data-testid="stCameraInput"] {
    flex: 1 1 auto; display: flex; flex-direction: column; width: 100% !important; min-height: 0; margin: 0;
}
.st-key-camfs [data-testid="stCameraInput"],
.st-key-camfs [data-testid="stCameraInputWebcamComponent"],
.st-key-camfs [data-testid="stCameraInputWebcamStyledBox"] {   /* Streamlit pins these to a 16:9 box */
    height: calc(100vh - 5.5rem) !important; height: calc(100dvh - 5.5rem) !important; aspect-ratio: auto !important;
}
.st-key-camfs video, .st-key-camfs [data-testid="stCameraInput"] img {
    width: 100% !important; height: calc(100vh - 5.5rem) !important; height: calc(100dvh - 5.5rem) !important;
    object-fit: cover; border-radius: 0 !important;
}
.st-key-camfs [data-testid="stCameraInputButton"] {
    position: fixed !important; left: 0; right: 0; bottom: 0; z-index: 1000001;
    min-height: 5.5rem !important; border-radius: 0 !important; border: 0 !important;
    background: #1F4E78 !important; justify-content: center;
}
.st-key-camfs [data-testid="stCameraInputButton"], .st-key-camfs [data-testid="stCameraInputButton"] * {
    font-size: 1.6rem !important; font-weight: 700 !important; color: #fff !important;
}
.st-key-camfs .cam-banner {
    position: fixed; top: 0; left: 0; right: 0; z-index: 1000001; padding: 0.9rem 5.5rem 0.9rem 1rem;
    color: #fff; font-size: 1.15rem; background: linear-gradient(#000c, #0000);
}
.st-key-cam_close { position: fixed !important; top: 0.5rem; right: 0.6rem; z-index: 1000002; width: auto !important; }
div.st-key-cam_close[data-testid="stElementContainer"]:has(> [data-testid="stButton"]) {
    width: auto !important; left: auto !important; right: 0.6rem !important; top: 0.5rem !important;
}
.st-key-cam_close [data-testid="stButton"] { width: auto !important; }
.st-key-cam_close button { min-height: 2.6rem; width: auto !important; padding: 0 1rem; background: #000a !important; color: #fff !important; border: 1px solid #fff8 !important; box-shadow: none; }

@media (max-width: 640px) {
    .block-container { padding: .7rem .7rem 5rem .7rem; }
    .hero { flex-direction: column; align-items: flex-start; padding: .95rem 1rem; }
    .hero-t { font-size: 1.3rem; }
    [data-testid="stButton"] button, [data-testid="stDownloadButton"] button { min-height: 3.4rem; font-size: 1.1rem; }
    /* Phone scan: one huge tap area per side instead of a small "Browse files" button */
    [data-testid="stFileUploaderDropzone"] { min-height: 7rem; justify-content: center; border-radius: 12px; padding: 0.4rem; }
    [data-testid="stFileUploaderDropzoneInstructions"] { display: none; }
    [data-testid="stFileUploaderDropzone"] button {
        width: 100%; min-height: 5.5rem; font-size: 1.1rem; font-weight: 700;
        background: linear-gradient(135deg, #1F4E78, #2F80D1); color: #fff; border-radius: 12px; border: 0;
    }
}
</style>
"""


# ------------------------- config helpers -------------------------
def get_secret(name: str) -> str:
    """Read from Streamlit secrets first, then environment variables."""
    try:
        val = st.secrets.get(name)
    except Exception:
        val = None
    return str(val) if val else os.environ.get(name, "")


# ------------------------- image + extraction -------------------------
def compress_image(file_bytes: bytes) -> bytes:
    """Fix rotation, shrink and re-encode as JPEG (keeps memory low)."""
    img = Image.open(io.BytesIO(file_bytes))
    if img.format == "JPEG":
        img.draft("RGB", (MAX_SIDE, MAX_SIDE))   # decode a 12MP photo at ~1/2-1/4 size straight away
    img = ImageOps.exif_transpose(img).convert("RGB")
    img.thumbnail((MAX_SIDE, MAX_SIDE))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85, optimize=False)
    return buf.getvalue()


@st.cache_data(max_entries=12, show_spinner=False)
def preview(file_bytes: bytes) -> bytes:
    """Small copy of a photo just for showing it on screen (a 12MP phone photo is too heavy to redraw on every click)."""
    img = Image.open(io.BytesIO(file_bytes))
    if img.format == "JPEG":
        img.draft("RGB", (700, 700))
    img = ImageOps.exif_transpose(img).convert("RGB")
    img.thumbnail((640, 640))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=72)
    return buf.getvalue()


def extract_card(front, back) -> dict:
    """front/back are (filename, jpeg_bytes) or None. Free local OCR, no API."""
    data = read_card(front[1] if front else None, back[1] if back else None)
    row = {h: str(data.get(h, "") or "").strip() for h in HEADERS[:-2]}
    row["Front File"] = front[0] if front else ""
    row["Back File"] = back[0] if back else ""
    return row


def failed_row(front, back) -> dict:
    row = {h: "" for h in HEADERS}
    row["Front File"] = front[0] if front else ""
    row["Back File"] = back[0] if back else ""
    row["Other Notes"] = "EXTRACTION FAILED - fill manually"
    return row


def clean(v) -> str:
    if v is None or (isinstance(v, float) and v != v):  # None / NaN
        return ""
    return str(v).strip()


class CardStore:
    """Per-session store of cards. Worker threads only touch this object, never st.*.

    Each card: reading -> done | failed. Rows live here, so the Excel is always
    up to date with whatever has finished reading."""

    def __init__(self):
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=max_workers())   # Gemini: several cards at once; offline OCR: follows the CPU
        self.cards = {}      # id -> {id, status, row, front, back, raw, error}
        self.next_id = 1
        self.version = 0     # bumps on every change (used to cache the Excel bytes)
        self._xlsx = (-1, b"")
        self._df = (-1, None)   # (version, DataFrame): the table is rebuilt only when something changed

    # ---- adding / reading ----
    def submit(self, front, back, raw=False) -> int:
        with self.lock:
            cid = self.next_id
            self.next_id += 1
            self.cards[cid] = {
                "id": cid, "status": "reading", "row": None,
                "front": front, "back": back, "raw": raw, "error": "",
            }
            self.version += 1
        self.pool.submit(self._work, cid)
        return cid

    def _work(self, cid):
        with self.lock:
            card = self.cards.get(cid)
            if card is None:      # removed before it started
                return
            front, back, raw = card["front"], card["back"], card["raw"]
        try:
            if raw:               # bulk upload: shrink here so the UI stays snappy
                front = (front[0], compress_image(front[1])) if front else None
                back = (back[0], compress_image(back[1])) if back else None
            row = extract_card(front, back)
            status, err = "done", ""
        except Exception as e:    # one bad card must not stop the others
            label = (front or back)[0] if (front or back) else f"card {cid}"
            row, status, err = failed_row(front, back), "failed", f"{label}: {e}"
        with self.lock:
            card = self.cards.get(cid)
            if card is None:      # removed while reading
                return
            keep = status == "failed"   # photos are only needed again for "Retry"; free the RAM for finished cards
            card.update(status=status, row=row, error=err, front=front if keep else None,
                        back=back if keep else None, raw=False)
            self.version += 1

    def retry_failed(self) -> int:
        with self.lock:
            ids = [i for i, c in self.cards.items() if c["status"] == "failed"]
            for i in ids:
                self.cards[i].update(status="reading", row=None, error="")
            if ids:
                self.version += 1
        for i in ids:
            self.pool.submit(self._work, i)
        return len(ids)

    # ---- reading state ----
    def counts(self) -> dict:
        with self.lock:
            c = {"reading": 0, "done": 0, "failed": 0}
            for card in self.cards.values():
                c[card["status"]] += 1
            return c

    def pending(self) -> int:
        return self.counts()["reading"]

    def errors(self) -> list:
        with self.lock:
            return [c["error"] for c in self.cards.values() if c["status"] == "failed" and c["error"]]

    def dataframe(self) -> pd.DataFrame:
        """Finished rows in upload order; index = card id (used to map edits back)."""
        with self.lock:
            version = self.version
            if self._df[0] == version:
                return self._df[1]
            ids = sorted(i for i, c in self.cards.items() if c["row"] is not None)
            rows = [dict(self.cards[i]["row"]) for i in ids]
        df = pd.DataFrame(rows, index=pd.Index(ids, name="id"), columns=HEADERS)
        with self.lock:
            self._df = (version, df)   # callers must not modify it in place (they copy)
        return df

    # ---- edits from the table ----
    def apply_edits(self, edited: pd.DataFrame):
        with self.lock:
            for cid, rec in edited.iterrows():
                card = self.cards.get(int(cid))
                if card is None:
                    continue
                if bool(rec.get("Delete")):
                    del self.cards[int(cid)]
                    continue
                new_row = {h: clean(rec.get(h)) for h in HEADERS}
                if new_row != card["row"]:
                    card["row"] = new_row
                    if card["status"] == "failed":   # fixed by hand
                        card.update(status="done", error="")
            self.version += 1

    def remove(self, ids):
        with self.lock:
            for i in ids:
                self.cards.pop(int(i), None)
            self.version += 1

    def clear_all(self):
        with self.lock:
            self.cards.clear()
            self.version += 1

    # ---- Excel, always ready ----
    def excel_bytes(self) -> bytes:
        with self.lock:
            cached_version, cached = self._xlsx
            version = self.version
        if cached_version == version:
            return cached
        data = to_excel(self.dataframe())
        with self.lock:
            self._xlsx = (version, data)
        return data


TITLE_RE = re.compile(r"^(mr|mrs|ms|miss|dr|shri|smt|prof|er|ca|adv)\.?\s+", re.I)


def _parts(value: str) -> list:
    """'a; b ; c' -> ['a', 'b', 'c'] (the reader joins multiple values with '; ')."""
    return [p.strip() for p in clean(value).split(";") if p.strip()]   # clean(): a missing cell (NaN) is "", never 'nan'


def _phone_kind(p: str, default: str) -> str:
    """'mobile' or 'tele' for one number, decided by the number itself (Indian patterns), not by which box the
    reader put it in. Foreign / unclear numbers keep the column the card gave."""
    s = p.strip()
    if s.startswith("+") and not s.startswith("+91"):
        return default
    d = re.sub(r"\D", "", s)
    if d.startswith("91") and len(d) >= 12:
        d = d[2:]
    if len(d) == 11 and d[0] == "0" and d[1] == "9":          # 09876543210
        return "mobile"
    if d.startswith(("0", "1800", "1860")):                    # STD code / toll free
        return "tele"
    if len(d) == 10 and d[0] in "6789":
        return "mobile"
    if len(d) in (10, 11) and d[0] in "12345":                 # STD code written without the leading 0
        return "tele"
    return default


_SOCIAL_RE = re.compile(r"facebook|instagram|twitter|x\.com|youtube|wa\.me|whatsapp|t\.me|telegram", re.I)
_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}")


def _unique(items: list) -> list:
    seen, out = set(), []
    for v in items:
        k = re.sub(r"\W", "", v).lower()
        if k and k not in seen:
            seen.add(k)
            out.append(v)
    return out


def to_universal(df: pd.DataFrame) -> pd.DataFrame:
    """Scanner columns -> company's universal header layout.
    Anything the card does not give stays blank (Source, FileName, Updated By, Sector, ...)."""
    out = []
    for n, (_, r) in enumerate(df.iterrows(), start=1):
        row = {h: "" for h in UNIVERSAL_HEADERS}
        row["SNO"] = n

        # name -> Title / FirstName / LastName
        name = clean(r.get("Name"))
        m = TITLE_RE.match(name)
        if m:
            row["Title"] = {"ca": "CA", "er": "Er.", "adv": "Adv."}.get(m.group(1).lower(), m.group(1).capitalize() + ".")
            name = name[m.end():].strip()
        words = name.split()
        if len(words) == 1:
            row["FirstName"] = words[0]
        elif words:
            row["FirstName"], row["LastName"] = " ".join(words[:-1]), words[-1]

        row["CompanyName"] = clean(r.get("Company"))
        row["Designation"] = clean(r.get("Designation"))
        row["Product Category "] = clean(r.get("Services / Products"))
        row["Address"] = clean(r.get("Address"))
        row["City"] = clean(r.get("City"))
        row["Pincode"] = clean(r.get("Pincode"))
        row["State"] = clean(r.get("State"))
        row["Country"] = clean(r.get("Country"))

        remark = []

        # numbers: every number is placed by its own pattern (mobile / landline / fax), max 2 each, extras -> Remark
        tele, fax, mobiles = [], [], []
        mobile_field = _parts(r.get("Mobile"))
        for p in mobile_field + _parts(r.get("Phone / Landline")):    # numbers the card called "mobile" keep first place
            if re.search(r"\(fax\)|\bfax\b", p, re.I):
                fax.append(re.sub(r"\s*\(fax\)|\bfax\b[:\s]*", "", p, flags=re.I).strip())
                continue
            default = "mobile" if p in mobile_field else "tele"
            (mobiles if _phone_kind(p, default) == "mobile" else tele).append(p)
        tele, fax, mobiles = _unique(tele), _unique(fax), _unique(mobiles)
        for col, vals in (("Tele", tele), ("Mobile", mobiles)):
            for i, v in enumerate(vals[:2], start=1):
                row[f"{col}{i}"] = v
            if len(vals) > 2:
                remark.append(f"Extra {col.lower()}: " + ", ".join(vals[2:]))
        if fax:
            row["Fax"] = fax[0]
            if len(fax) > 1:
                remark.append("Extra fax: " + ", ".join(fax[1:]))

        # emails / websites / social links: sorted by what the value IS, whichever box it was written in
        emails, sites, person_in, company_in = [], [], [], []
        for v in _parts(r.get("Email")) + _parts(r.get("Website")) + _parts(r.get("LinkedIn / Social")):
            low = v.lower()
            if _EMAIL_RE.fullmatch(v):
                emails.append(v)
            elif "linkedin.com/company" in low or "linkedin.com/school" in low:
                company_in.append(v)
            elif "linkedin.com" in low:
                person_in.append(v)
            elif _SOCIAL_RE.search(low) or v.startswith("@"):
                remark.append(v)
            elif re.search(r"\.[a-z]{2,}", low):
                sites.append(v)
            else:
                remark.append(v)
        for col, vals in (("Email", _unique(emails)), ("Website", _unique(sites))):
            for i, v in enumerate(vals[:2], start=1):
                row[f"{col}{i}"] = v
            if len(vals) > 2:
                remark.append(f"Extra {col.lower()}: " + ", ".join(vals[2:]))
        if person_in:
            row["Person Linked URL"] = person_in[0]
        if company_in:
            row["Company Linked URL"] = company_in[0]
        remark += person_in[1:] + company_in[1:]

        if clean(r.get("Other Notes")):
            remark.append(clean(r.get("Other Notes")))
        row["Remark"] = " | ".join(remark)
        out.append(row)
    return pd.DataFrame(out, columns=UNIVERSAL_HEADERS)


def to_excel(df: pd.DataFrame) -> bytes:
    from openpyxl.styles import Alignment, Font, PatternFill

    df = to_universal(df)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Sheet1")
        ws = writer.sheets["Sheet1"]
        for cell in ws[1]:   # same look as the company header file: white bold on blue, Calibri 11
            cell.font = Font(name="Calibri", size=11, bold=True, color="FFFFFFFF")
            cell.fill = PatternFill("solid", fgColor="FF0070C0")
            cell.alignment = Alignment(horizontal="left")
        text = df.astype(str)
        if text.apply(lambda col: col.str.startswith("=")).to_numpy().any():
            for row in ws.iter_rows(min_row=2):
                for c in row:  # text from a card must never run as an Excel formula
                    if isinstance(c.value, str) and c.value.startswith("="):
                        c.data_type = "s"
        for i, name in enumerate(df.columns, start=1):
            longest = max(len(str(name)), int(text[name].str.len().max() or 0) if len(text) else 0)
            ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = min(max(longest + 2, 12), 50)
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
    return buf.getvalue()


def review_flags(df: pd.DataFrame) -> dict:
    """card id -> '' (looks fine) or a short warning: duplicate / manual entry needed / name or contact missing."""
    flags, seen = {}, {}
    for cid, r in df.iterrows():
        keys = set()
        for field in ("Mobile", "Phone / Landline"):
            for p in _parts(r[field]):
                if _phone_kind(p, "mobile" if field == "Mobile" else "tele") == "mobile":
                    d = re.sub(r"\D", "", p)[-10:]
                    if len(d) >= 10:
                        keys.add("m" + d)          # a landline is shared by colleagues, so only mobiles count
        keys.update("e" + e.lower() for e in _parts(r["Email"]))
        nm, co = re.sub(r"\W", "", r["Name"]).lower(), re.sub(r"\W", "", r["Company"]).lower()
        if nm and co:
            keys.add(f"n{nm}|{co}")
        first = next((seen[k] for k in keys if k in seen), None)
        if first is not None:
            flags[cid] = f"⚠ Duplicate (card #{first})"
            continue
        for k in keys:
            seen[k] = cid
        if "EXTRACTION FAILED" in r["Other Notes"]:
            flags[cid] = "⚠ Haath se bharo"
        else:
            miss = [w for w, ok in (("naam", clean(r["Name"])),
                                    ("contact", clean(r["Mobile"]) or clean(r["Phone / Landline"]) or clean(r["Email"]))) if not ok]
            flags[cid] = ("⚠ " + " + ".join(miss) + " nahi") if miss else ""
    return flags


# ------------------------- cards survive a page refresh -------------------------
# Phones kill browser tabs all the time. Each visit gets a short token in the URL (?s=...); the cards live
# on the server under that token, so a refresh / reopening the same link brings them back (and a laptop can open
# the same link to see what the phone is scanning).
@st.cache_resource
def _registry() -> dict:
    """Lives for the whole server process (Streamlit re-runs this file on every click, so a plain module-level dict
    would be emptied each time)."""
    return {"stores": {}, "lock": threading.Lock()}   # token -> [CardStore, last_seen]


def _register(token: str, store: "CardStore"):
    reg = _registry()
    with reg["lock"]:
        reg["stores"][token] = [store, time.time()]


def _touch(token: str, store: "CardStore"):
    reg = _registry()
    with reg["lock"]:
        entry = reg["stores"].get(token)
        if entry:
            entry[1] = time.time()
            return
    _register(token, store)   # was cleaned up while the page stayed open


def _gc_stores():
    reg, now = _registry(), time.time()
    with reg["lock"]:
        stale = [t for t, (_, seen) in reg["stores"].items() if now - seen > STORE_TTL]
        victims = [reg["stores"].pop(t)[0] for t in stale]
    for v in victims:
        v.pool.shutdown(wait=False, cancel_futures=True)


def get_store():
    _gc_stores()
    reg = _registry()
    token = st.query_params.get("s", "")
    with reg["lock"]:
        entry = reg["stores"].get(token) if token else None
        if entry:
            entry[1] = time.time()
    if entry:
        return token, entry[0]
    token, store = secrets.token_urlsafe(9), CardStore()
    _register(token, store)
    st.query_params["s"] = token
    return token, store


# ------------------------- session state -------------------------
def init_state():
    ss = st.session_state
    if "store" not in ss:
        ss.token, ss.store = get_store()
    else:   # keep the server-side copy alive while this session is open
        _touch(ss.token, ss.store)
    ss.setdefault("phone_n", 0)      # bumping it resets the phone-scan uploaders after "Save"
    ss.setdefault("cam_front", None)  # raw bytes of the browser-camera shots for the card in progress
    ss.setdefault("cam_back", None)
    ss.setdefault("cam_open", None)   # None | "front" | "back": which side the full-screen camera is for
    ss.setdefault("cam_k", 0)         # bumping it gives the camera widget a fresh start
    ss.setdefault("up_n", 0)          # resets the bulk-upload widgets after "add"
    ss.setdefault("editor_ver", 0)
    ss.setdefault("was_pending", False)


def submit_card(front, back, raw=False) -> int:
    """Send one card (front + optional back) to be read in the background right away."""
    ss = st.session_state
    if ss.store.pending() >= MAX_PENDING:
        st.error(f"Ek saath max {MAX_PENDING} cards read ho sakte hain. Thoda ruko, pehle wale ho jayen.")
        return 0
    return ss.store.submit(front, back, raw=raw)


# ------------------------- UI pieces -------------------------
def _reader_pill():
    if gemini_engine.enabled():
        return "", "Gemini ON"
    if ocr_fallback_enabled():
        return "mid", "Offline OCR"
    return "off", "API key missing"


def hero():
    cls, txt = _reader_pill()
    st.markdown(
        '<div class="hero"><div class="hero-left"><div class="hero-ic">📇</div><div>'
        '<div class="hero-t">Visiting Card Scanner</div>'
        '<div class="hero-s">Photo lo → AI padhe → company ke format ka Excel taiyaar</div></div></div>'
        f'<div class="pill {cls}"><i></i>{txt}</div></div>',
        unsafe_allow_html=True)


def section(num: int, title: str):
    st.markdown(f'<div class="sec"><span class="n">{num}</span>{html.escape(title)}</div>', unsafe_allow_html=True)


def password_gate():
    pw = get_secret("APP_PASSWORD")
    if not pw or st.session_state.get("authed"):
        return
    hero()
    _, mid, _ = st.columns([1, 2, 1])
    with mid, st.form("login"):
        entered = st.text_input("Password", type="password")
        ok = st.form_submit_button("Login", type="primary")
    if ok:
        if hmac.compare_digest(entered.encode(), pw.encode()):
            st.session_state.authed = True
            st.rerun()
        else:
            st.error("Galat password.")
    st.stop()


SIDE_LABEL = {"front": "FRONT", "back": "BACK"}


def camera_overlay():
    """Full-screen camera (CSS .st-key-camfs). Capture -> stored -> opens BACK automatically, or closes."""
    ss = st.session_state
    side = ss.cam_open
    with st.container(key="camfs"):
        st.markdown(f"<div class='cam-banner'>Card #{ss.store.next_id} · <b>{SIDE_LABEL[side]}</b> side</div>",
                    unsafe_allow_html=True)
        if st.button("⏭ Back nahi hai" if side == "back" else "✕ Band karo", key="cam_close"):
            ss.cam_open = None
            ss.cam_k += 1
            st.rerun()
        shot = st.camera_input("camera", key=f"cam_in_{ss.cam_k}", label_visibility="collapsed", **CAM_KW)
    if shot is not None:
        ss[f"cam_{side}"] = shot.getvalue()
        ss.cam_open = "back" if (side == "front" and not ss.cam_back) else None
        ss.cam_k += 1
        st.rerun()


def scan_flow(mode: str):
    """One card = FRONT and BACK slots side by side (back optional) + one Save button.
    mode 'phone': phone's own camera app via the file picker (rear camera, full quality).
    mode 'cam'  : full-screen in-browser camera; back side opens right after the front."""
    ss = st.session_state
    if mode == "cam" and ss.cam_open:
        camera_overlay()
    st.markdown(f"<div class='cardno'>Card <b>#{ss.store.next_id}</b> · front aur back ek saath lo, phir ek baar Save</div>",
                unsafe_allow_html=True)
    if mode == "phone":
        st.caption("Dono side ke box me **Upload** dabao → **Camera / Take Photo** chuno. Back optional hai, order koi bhi chalega.")
    else:
        st.caption("Camera full screen khulega. Front lete hi back ka camera khud khul jayega.")

    shots = {}
    with st.container(key=f"slots_{mode}"):
        for col, side in zip(st.columns(2, gap="small"), ("front", "back")):
            with col:
                st.markdown(f"<div class='slot'>{SIDE_LABEL[side]}" + (" <small>· optional</small>" if side == "back" else "") + "</div>",
                            unsafe_allow_html=True)
                if mode == "phone":
                    f = st.file_uploader(f"{side} photo", type=EXTS, key=f"phone_{ss.phone_n}_{side}",
                                         label_visibility="collapsed")
                    shots[side] = f.getvalue() if f else None
                else:
                    shots[side] = ss[f"cam_{side}"]
                    if st.button((("🔄 Retake" if shots[side] else "📷 Take") + f" {side}"), key=f"cam_btn_{side}"):
                        ss.cam_open = side
                        ss.cam_k += 1
                        st.rerun()
                if shots[side]:
                    st.image(preview(shots[side]))

    if st.button("✅ Save card", type="primary", disabled=not shots["front"], key=f"{mode}_save"):
        base = f"card_{ss.store.next_id:03d}"
        front = (f"{base}_front.jpg", shots["front"])
        back = (f"{base}_back.jpg", shots["back"]) if shots["back"] else None
        cid = submit_card(front, back, raw=True)   # shrinking happens in the background worker
        if cid:
            ss.flash = f"Card #{cid} save ho gaya, reading shuru ✅"
            if mode == "phone":
                ss.phone_n += 1
            else:
                ss.cam_front = ss.cam_back = None
            st.rerun()
    if not shots["front"]:
        st.caption("Save tab hoga jab front side aa jayegi.")


def upload_tab():
    ss = st.session_state
    k = ss.up_n
    col1, col2 = st.columns(2)
    fronts = col1.file_uploader("Front images", type=EXTS, accept_multiple_files=True, key=f"up_f_{k}")
    backs = col2.file_uploader("Back images (optional)", type=EXTS, accept_multiple_files=True, key=f"up_b_{k}")
    st.caption("Ek saath bahut saari images ke liye (laptop par best). Front/back filename order se pair hote hain: 001_front.jpg + 001_back.jpg.")

    fronts = sorted(fronts, key=lambda f: f.name.lower())
    backs = sorted(backs, key=lambda f: f.name.lower())
    if backs and len(backs) != len(fronts):
        st.warning(f"{len(fronts)} fronts aur {len(backs)} backs hain, sorted order me pair honge.")
    pairs = list(zip_longest(fronts, backs))

    if st.button(f"➕ Add {len(pairs)} card(s) - reading turant shuru", disabled=not pairs, type="primary"):
        added = 0
        for f, b in pairs:
            front = (f.name, f.getvalue()) if f else None
            back = (b.name, b.getvalue()) if b else None
            if not submit_card(front, back, raw=True):
                break
            added += 1
        ss.flash = f"{added} card(s) add hue, reading shuru ✅"
        ss.up_n += 1
        st.rerun()


def _norm(df: pd.DataFrame) -> pd.DataFrame:
    return df.fillna("").astype(str)


def _col(kind, label=None, **kw):
    """st.column_config.<kind>; drops options an older Streamlit does not know (e.g. pinned)."""
    factory = getattr(st.column_config, kind)
    try:
        return factory(label, **kw)
    except TypeError:
        kw.pop("pinned", None)
        return factory(label, **kw)


def _table_config() -> dict:
    cfg = {
        "#": _col("NumberColumn", "#", width="small", disabled=True, pinned=True, format="%d"),
        "Name": _col("TextColumn", "Name", width="medium", pinned=True),
        "Delete": _col("CheckboxColumn", "🗑", width="small", help="Tick karo → ye card hat jayega"),
        "Check": _col("TextColumn", "Check", width="medium", disabled=True,
                      help="Duplicate / naam ya contact missing wale cards yahan flag hote hain"),
        "Address": _col("TextColumn", "Address", width="large"),
        "Services / Products": _col("TextColumn", "Services / Products", width="large"),
        "Other Notes": _col("TextColumn", "Other Notes", width="medium"),
        "Front File": _col("TextColumn", "Front File", disabled=True),
        "Back File": _col("TextColumn", "Back File", disabled=True),
    }
    return cfg


def stats_html(total, counts, n_review) -> str:
    live = " live" if counts["reading"] else ""
    tiles = [
        ("", total, "Total"),
        ("ok", counts["done"], "Ready"),
        (f"busy{live}", counts["reading"], "Reading"),
        ("bad", counts["failed"], "Failed"),
        ("warn", n_review, "Check karo"),
    ]
    return '<div class="stats">' + "".join(f'<div class="stat {c}"><b>{n}</b><span>{lbl}</span></div>' for c, n, lbl in tiles) + "</div>"


EMPTY_HTML = (
    '<div class="empty"><div class="big">🗂️</div><h4>Abhi koi card nahi</h4>'
    '<div>Card scan hote hi yahan table banti jayegi.</div>'
    '<ol><li>Front (aur back) ki photo lo</li><li><b>Save card</b> dabao, AI turant padhna shuru kar dega</li>'
    '<li>Table check karo → <b>Export Excel</b></li></ol></div>'
)


def results_panel():
    """Live status + editable table + Export. Auto-refreshes while cards are being read."""
    ss = st.session_state
    store = ss.store
    counts = store.counts()
    total = sum(counts.values())

    # reading just finished -> one full rerun so auto-refresh stops and everything is final
    if ss.was_pending and counts["reading"] == 0:
        ss.was_pending = False
        st.rerun()
    ss.was_pending = counts["reading"] > 0

    if total == 0:
        st.markdown(EMPTY_HTML, unsafe_allow_html=True)
        return

    df = store.dataframe()
    flags = review_flags(df) if not df.empty else {}
    n_review = sum(1 for f in flags.values() if f)
    st.markdown(stats_html(total, counts, n_review), unsafe_allow_html=True)
    if counts["reading"]:
        st.progress((counts["done"] + counts["failed"]) / total,
                    text="Cards background me read ho rahe hain, aap agla card add karte rahiye…")

    errs = store.errors()
    if errs:   # the same reason usually repeats for every card -> show each reason once
        uniq = list(dict.fromkeys(e.split(": ", 1)[-1] for e in errs))[:3]
        st.markdown(f'<div class="note bad"><b>{len(errs)} card read nahi hue.</b><br>'
                    + "<br>".join(html.escape(u) for u in uniq) + "</div>", unsafe_allow_html=True)

    if not df.empty:
        st.caption("Galti ho to table me seedha edit karo. Hatana ho to 🗑 tick karo. Phone par table side me scroll hoti hai.")
        view = df.copy()
        view.insert(0, "#", view.index.astype(int))
        view.insert(2, "Delete", False)
        view.insert(3, "Check", [flags.get(i, "") for i in df.index])
        key = f"editor_{ss.editor_ver}_{hash(tuple(df.index))}"
        edited = st.data_editor(view, key=key, hide_index=True, num_rows="fixed", column_config=_table_config(),
                                disabled=["#", "Check", "Front File", "Back File"],
                                height=min(40 + 36 * len(view), 540))
        if not _norm(edited).equals(_norm(view)):
            store.apply_edits(edited)
            ss.editor_ver += 1
            st.rerun()

    dups = [i for i, f in flags.items() if f.startswith("⚠ Duplicate")]
    actions = []
    if counts["failed"]:
        actions.append(("🔁 Failed cards dobara", "retry"))
    if dups:
        actions.append((f"🧹 {len(dups)} duplicate hatao", "dups"))
    if actions:
        for col, (label, what) in zip(st.columns(len(actions)), actions):
            with col:
                if st.button(label, key=f"act_{what}"):
                    if what == "retry":
                        store.retry_failed()
                    else:
                        store.remove(dups)
                        ss.editor_ver += 1
                    st.rerun()

    n_ready = counts["done"] + counts["failed"]
    with st.container(key="exportbar"):
        st.download_button(
            f"⬇️ Export Excel ({n_ready} cards)",
            data=store.excel_bytes if DL_LAZY else store.excel_bytes(),   # lazy: built only on click
            file_name=time.strftime("Cards_%Y-%m-%d_%H-%M.xlsx"),
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            type="primary",
            disabled=n_ready == 0,
            **({"on_click": "ignore"} if DL_LAZY else {}),   # downloading does not re-run the page
        )
    if counts["reading"]:
        st.caption(f"⏳ {counts['reading']} card(s) abhi read ho rahe hain. Export abhi tak ke ready cards ka hoga; baaki ready hote hi table aur Excel me aa jayenge.")

    with st.expander("Aur options"):
        if st.button("🗑️ Sab cards clear karo"):
            old = ss.store
            ss.store = CardStore()
            _register(ss.token, ss.store)
            old.pool.shutdown(wait=False, cancel_futures=True)
            ss.editor_ver += 1
            st.rerun()


def sidebar():
    with st.sidebar:
        st.markdown("### 📇 Card Scanner")
        st.markdown(
            "**Kaise use karein**\n\n"
            "1. Card ki photo lo (front + back)\n"
            "2. **Save card** dabao, AI turant padhna shuru kar deta hai, aap agla card le sakte ho\n"
            "3. Table check/edit karo (⚠ wale cards dekh lo)\n"
            "4. **Export Excel**\n\n"
            "Saare cards ek hi Excel me, company ke format me."
        )
        st.divider()
        if gemini_engine.enabled():
            fb = gemini_engine.status["fallbacks"]
            st.caption("🤖 Gemini ON · front + back dono side ek saath read hoti hain"
                       + (f" · {fb} card(s) offline OCR se read hue (Gemini limit)" if fb else ""))
            if gemini_engine.status["last"]:
                st.caption(gemini_engine.status["last"])
        elif ocr_fallback_enabled():
            st.caption("💻 Offline OCR mode (Gemini key nahi hai)")
        else:
            st.error("GEMINI_API_KEY set nahi hai. Render → Environment me key daaliye, tabhi cards read honge.")
        st.caption("💾 Page refresh ya phone lock hone par bhi cards bache rehte hain. Isi link ko laptop par kholo to wahi cards dikhenge.")
        if get_secret("APP_PASSWORD") and st.button("Logout"):
            st.query_params.clear()
            st.session_state.clear()
            st.rerun()


def main():
    st.set_page_config(page_title="Visiting Card Scanner", page_icon="📇", layout="wide")
    if get_secret("GEMINI_API_KEY"):   # make secrets visible to gemini_engine
        os.environ["GEMINI_API_KEY"] = get_secret("GEMINI_API_KEY")
    for _k in ("GEMINI_MODELS", "GEMINI_RPM", "OCR_FALLBACK"):
        if get_secret(_k):
            os.environ[_k] = get_secret(_k)
    warmup()   # loads the offline OCR model only when it can really be used
    st.markdown(STYLE, unsafe_allow_html=True)
    password_gate()
    init_state()
    ss = st.session_state
    if ss.get("flash"):
        st.toast(ss.pop("flash"))
    if hasattr(st, "iframe"):   # newer Streamlit; components.html is being removed
        st.iframe(REAR_CAMERA_JS.strip(), height=1)
    else:
        components.html(REAR_CAMERA_JS, height=0)

    hero()
    sidebar()

    left, right = st.columns([5, 6], gap="large")   # desktop: scan on the left, live table on the right; phone: stacked
    with left:
        section(1, "Card scan karo")
        tab_phone, tab_cam, tab_up = st.tabs(["📱 Phone scan", "📷 Browser camera", "📁 Bulk upload"])
        with tab_phone:
            scan_flow("phone")
        with tab_cam:
            scan_flow("cam")
        with tab_up:
            upload_tab()
    with right:
        section(2, "Check karo aur Excel export karo")
        # auto-refresh every few seconds only while something is still being read
        st.fragment(run_every=REFRESH_SECS if ss.store.pending() else None)(results_panel)()


if __name__ == "__main__":
    main()
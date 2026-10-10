"""Visiting Card Scanner
Take a photo (phone camera) or upload images of visiting cards (front + back).
Every card is read in the background the moment you add it and its row goes
straight into the Excel, so "Export Excel" is instant.
Run locally:  streamlit run app.py
"""
import base64
import hmac
import io
import json
import os
import re
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
[data-testid="stElementContainer"]:has(> [data-testid="stButton"]),
[data-testid="stElementContainer"]:has(> [data-testid="stDownloadButton"]),
[data-testid="stButton"], [data-testid="stDownloadButton"] { width: 100% !important; }
[data-testid="stButton"] button, [data-testid="stDownloadButton"] button {
    width: 100%; min-height: 3rem; font-size: 1.05rem; border-radius: 10px;
}
button { touch-action: manipulation; }   /* no double-tap-zoom delay -> fast repeated taps */
footer {visibility: hidden;}

/* Front / back slots stay SIDE BY SIDE, also on a phone */
div[data-testid="stHorizontalBlock"] { flex-wrap: nowrap !important; gap: 0.6rem !important; }
div[data-testid="stHorizontalBlock"] > div { min-width: 0 !important; flex: 1 1 0 !important; }
[data-testid="stImage"] img { max-height: 38vh; object-fit: contain; border-radius: 8px; border: 1px solid #d0d7de; }

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
.st-key-cam_close button { min-height: 2.6rem; width: auto !important; padding: 0 1rem; background: #000a; color: #fff; border: 1px solid #fff8; }

@media (max-width: 640px) {
    .block-container {padding: 1rem 0.8rem 5rem 0.8rem;}
    h1 {font-size: 1.7rem !important;}
    [data-testid="stButton"] button, [data-testid="stDownloadButton"] button {min-height: 3.4rem; font-size: 1.1rem;}
    /* Phone scan: one huge tap area per side instead of a small "Browse files" button */
    [data-testid="stFileUploaderDropzone"] {
        min-height: 7rem; justify-content: center; border-radius: 12px; padding: 0.4rem;
    }
    [data-testid="stFileUploaderDropzoneInstructions"] {display: none;}
    [data-testid="stFileUploaderDropzone"] button {
        width: 100%; min-height: 5.5rem; font-size: 1.1rem; font-weight: 700;
        background: #1F4E78; color: #fff; border-radius: 12px;
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
            card.update(status=status, row=row, error=err, front=front, back=back, raw=False)
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
            ids = sorted(i for i, c in self.cards.items() if c["row"] is not None)
            rows = [dict(self.cards[i]["row"]) for i in ids]
        return pd.DataFrame(rows, index=pd.Index(ids, name="id"), columns=HEADERS)

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
    return [p.strip() for p in str(value or "").split(";") if p.strip()]


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

        # numbers: landline vs fax vs mobile (max 2 each, extra ones go to Remark)
        tele, fax = [], []
        for p in _parts(r.get("Phone / Landline")):
            (fax if re.search(r"\(fax\)", p, re.I) else tele).append(re.sub(r"\s*\(fax\)", "", p, flags=re.I))
        mobiles = _parts(r.get("Mobile"))
        for col, vals in (("Tele", tele), ("Mobile", mobiles)):
            for i, v in enumerate(vals[:2], start=1):
                row[f"{col}{i}"] = v
            if len(vals) > 2:
                remark.append(f"Extra {col.lower()}: " + ", ".join(vals[2:]))
        if fax:
            row["Fax"] = fax[0]
            if len(fax) > 1:
                remark.append("Extra fax: " + ", ".join(fax[1:]))

        for col, key in (("Email", "Email"), ("Website", "Website")):
            vals = _parts(r.get(key))
            for i, v in enumerate(vals[:2], start=1):
                row[f"{col}{i}"] = v
            if len(vals) > 2:
                remark.append(f"Extra {col.lower()}: " + ", ".join(vals[2:]))

        # social links: LinkedIn person / company get their own column, the rest go to Remark
        for link in _parts(r.get("LinkedIn / Social")):
            low = link.lower()
            if "linkedin.com/company" in low and not row["Company Linked URL"]:
                row["Company Linked URL"] = link
            elif "linkedin.com" in low and not row["Person Linked URL"]:
                row["Person Linked URL"] = link
            else:
                remark.append(link)

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
            cell.font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="0070C0")
            cell.alignment = Alignment(vertical="center")
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


# ------------------------- session state -------------------------
def init_state():
    ss = st.session_state
    ss.setdefault("store", CardStore())
    ss.setdefault("phone_n", 0)      # bumping it resets the phone-scan uploaders after "Save"
    ss.setdefault("cam_front", None)  # raw bytes of the browser-camera shots for the card in progress
    ss.setdefault("cam_back", None)
    ss.setdefault("cam_open", None)   # None | "front" | "back": which side the full-screen camera is for
    ss.setdefault("cam_k", 0)         # bumping it gives the camera widget a fresh start
    ss.setdefault("up_n", 0)          # resets the bulk-upload widgets after "add"
    ss.setdefault("editor_ver", 0)
    ss.setdefault("was_pending", False)


def submit_card(front, back, raw=False) -> bool:
    """Send one card (front + optional back) to be read in the background right away."""
    ss = st.session_state
    if ss.store.pending() >= MAX_PENDING:
        st.error(f"Ek saath max {MAX_PENDING} cards read ho sakte hain. Thoda ruko, pehle wale ho jayen.")
        return False
    ss.store.submit(front, back, raw=raw)
    return True


# ------------------------- UI pieces -------------------------
def password_gate():
    pw = get_secret("APP_PASSWORD")
    if not pw or st.session_state.get("authed"):
        return
    st.title("📇 Visiting Card Scanner")
    with st.form("login"):
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
        shot = st.camera_input("camera", key=f"cam_in_{ss.cam_k}", label_visibility="collapsed")
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
    st.markdown(f"**Card #{ss.store.next_id}** · front aur back ek saath lo, phir ek baar Save")
    if mode == "phone":
        st.caption("Dono side ke box me **Upload** dabao → **Camera / Take Photo** chuno. Back optional hai, order koi bhi chalega.")
    else:
        st.caption("Camera full screen khulega. Front lete hi back ka camera khud khul jayega.")

    shots = {}
    for col, side in zip(st.columns(2, gap="small"), ("front", "back")):
        with col:
            st.markdown(f"**{SIDE_LABEL[side]}**" + (" · optional" if side == "back" else ""))
            if mode == "phone":
                f = st.file_uploader(f"{side} photo", type=EXTS, key=f"phone_{ss.phone_n}_{side}",
                                     label_visibility="collapsed")
                shots[side] = f.getvalue() if f else None
            else:
                shots[side] = ss[f"cam_{side}"]
                if st.button(("🔄 Retake" if shots[side] else "📷 Take") + f" {side}", key=f"cam_btn_{side}"):
                    ss.cam_open = side
                    ss.cam_k += 1
                    st.rerun()
            if shots[side]:
                st.image(preview(shots[side]))

    if st.button("✅ Save card", type="primary", disabled=not shots["front"], key=f"{mode}_save"):
        base = f"card_{ss.store.next_id:03d}"
        front = (f"{base}_front.jpg", shots["front"])
        back = (f"{base}_back.jpg", shots["back"]) if shots["back"] else None
        if submit_card(front, back, raw=True):   # shrinking happens in the background worker
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
        for f, b in pairs:
            front = (f.name, f.getvalue()) if f else None
            back = (b.name, b.getvalue()) if b else None
            if not submit_card(front, back, raw=True):
                break
        ss.up_n += 1
        st.rerun()


def _norm(df: pd.DataFrame) -> pd.DataFrame:
    return df.fillna("").astype(str)


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

    st.divider()
    if total == 0:
        st.subheader("Cards")
        st.caption("Abhi koi card nahi. Upar se photo lo ya upload karo, card turant read hona shuru ho jayega.")
        return

    st.subheader(f"Cards: {total}")
    st.markdown(f"✅ **{counts['done']}** ready · ⏳ **{counts['reading']}** reading · ⚠️ **{counts['failed']}** failed")
    if counts["reading"]:
        st.progress((counts["done"] + counts["failed"]) / total, text="Cards background me read ho rahe hain, aap agla card add karte rahiye…")

    for err in store.errors()[:5]:
        st.error(err)

    df = store.dataframe()
    if not df.empty:
        st.caption("Galti ho to table me seedha edit karo. Kisi row ko hatana ho to Delete tick karo. Phone par table side me scroll hoti hai.")
        view = df.copy()
        view.insert(0, "Delete", False)
        key = f"editor_{ss.editor_ver}_{hash(tuple(df.index))}"
        edited = st.data_editor(view, key=key, hide_index=True, num_rows="fixed")
        if not _norm(edited).equals(_norm(view)):
            store.apply_edits(edited)
            ss.editor_ver += 1
            st.rerun()

    n_ready = counts["done"] + counts["failed"]
    st.download_button(
        f"⬇️ Export Excel ({n_ready} cards)",
        data=store.excel_bytes(),
        file_name="visiting_cards.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        type="primary",
        disabled=n_ready == 0,
    )
    if counts["reading"]:
        st.caption(f"⏳ {counts['reading']} card(s) abhi read ho rahe hain. Export abhi tak ke ready cards ka hoga; baaki ready hote hi table aur Excel me aa jayenge.")

    if counts["failed"] and st.button("🔁 Retry failed cards"):
        store.retry_failed()
        st.rerun()
    with st.expander("Sab cards clear karo"):
        if st.button("🗑️ Clear all cards"):
            ss.store = CardStore()
            ss.editor_ver += 1
            st.rerun()


def main():
    if get_secret("GEMINI_API_KEY"):   # make secrets visible to gemini_engine
        os.environ["GEMINI_API_KEY"] = get_secret("GEMINI_API_KEY")
    for _k in ("GEMINI_MODELS", "GEMINI_RPM"):
        if get_secret(_k):
            os.environ[_k] = get_secret(_k)
    warmup()   # load OCR model in background while the page renders
    st.set_page_config(page_title="Visiting Card Scanner", page_icon="📇", layout="wide")
    st.markdown(STYLE, unsafe_allow_html=True)
    password_gate()
    init_state()
    ss = st.session_state
    if hasattr(st, "iframe"):   # newer Streamlit; components.html is being removed
        st.iframe(REAR_CAMERA_JS.strip(), height=1)
    else:
        components.html(REAR_CAMERA_JS, height=0)

    st.title("📇 Visiting Card Scanner")
    st.caption("Card ki photo lo ya upload karo (front + back) → turant read hoke Excel me judta jayega → jab chahein Export.")

    with st.sidebar:
        st.markdown(
            "**Kaise use karein**\n\n"
            "1. Phone scan se card (front + back) save karo\n"
            "2. Card turant background me read hota hai, aap agla le sakte ho\n"
            "3. Table check/edit karo\n"
            "4. *Export Excel*\n\n"
            "Saare cards ek hi Excel me jud jate hain."
        )
        if gemini_engine.enabled():
            fb = gemini_engine.status["fallbacks"]
            st.caption("🤖 Gemini ON (front + back dono side ek saath read hoti hain)" + (f" · {fb} card(s) offline OCR se read hue (Gemini limit)" if fb else ""))
            if gemini_engine.status["last"]:
                st.caption(gemini_engine.status["last"])
        elif ocr_fallback_enabled():
            st.caption("💻 Offline OCR mode (Gemini key nahi hai)")
        else:
            st.error("GEMINI_API_KEY set nahi hai. Render → Environment me key daaliye, tabhi cards read honge.")
        if get_secret("APP_PASSWORD") and st.button("Logout"):
            st.session_state.clear()
            st.rerun()

    tab_phone, tab_cam, tab_up = st.tabs(["📱 Phone scan", "📷 Browser camera", "📁 Bulk upload"])
    with tab_phone:
        scan_flow("phone")
    with tab_cam:
        scan_flow("cam")
    with tab_up:
        upload_tab()

    # auto-refresh every few seconds only while something is still being read
    st.fragment(run_every=REFRESH_SECS if ss.store.pending() else None)(results_panel)()


if __name__ == "__main__":
    main()
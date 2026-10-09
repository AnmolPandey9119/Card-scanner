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

from ocr_engine import read_card, warmup

MAX_SIDE = 1280   # photos are shrunk to this before being stored / sent
MAX_PENDING = 60  # max cards being read at the same time
WORKERS = 2       # cards read in parallel (OCR is CPU heavy; keep low on small servers)
REFRESH_SECS = 1  # live status refresh while cards are being read
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
div.stButton > button, div.stDownloadButton > button {
    width: 100%; min-height: 3rem; font-size: 1.05rem; border-radius: 10px;
}
button { touch-action: manipulation; }   /* no double-tap-zoom delay -> fast repeated taps */

/* Browser camera: big "Take Photo" bar so cards can be shot quickly */
[data-testid="stCameraInputButton"] {
    min-height: 5rem !important;
    background: #1F4E78 !important;
    border-radius: 0 0 10px 10px;
}
[data-testid="stCameraInputButton"], [data-testid="stCameraInputButton"] * {
    font-size: 1.5rem !important; font-weight: 700 !important; color: #fff !important;
}

footer {visibility: hidden;}
@media (max-width: 640px) {
    .block-container {padding: 1rem 0.8rem 5rem 0.8rem;}
    h1 {font-size: 1.7rem !important;}
    /* bigger buttons for Next / Save / Skip on phones */
    div.stButton > button, div.stDownloadButton > button {min-height: 3.6rem; font-size: 1.2rem;}
    [data-testid="stCameraInputButton"] {min-height: 7rem !important;}
    [data-testid="stCameraInputButton"], [data-testid="stCameraInputButton"] * {font-size: 1.8rem !important;}
    /* Phone scan / upload: one huge tap area instead of a small "Browse files" button */
    [data-testid="stFileUploaderDropzone"] {
        min-height: 9rem; justify-content: center; border-radius: 12px;
        padding: 0.5rem;
    }
    [data-testid="stFileUploaderDropzoneInstructions"] {display: none;}
    [data-testid="stFileUploaderDropzone"] button {
        width: 100%; min-height: 6rem; font-size: 1.5rem; font-weight: 700;
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
    img = ImageOps.exif_transpose(img).convert("RGB")
    img.thumbnail((MAX_SIDE, MAX_SIDE))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
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
        self.pool = ThreadPoolExecutor(max_workers=WORKERS)
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


def to_excel(df: pd.DataFrame) -> bytes:
    from openpyxl.styles import Alignment, Font, PatternFill

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Cards")
        ws = writer.sheets["Cards"]
        for cell in ws[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="1F4E78")
            cell.alignment = Alignment(vertical="center")
        for row in ws.iter_rows(min_row=2):
            for c in row:  # text from a card must never run as an Excel formula
                if isinstance(c.value, str) and c.value.startswith("="):
                    c.data_type = "s"
        for col in ws.columns:
            longest = max(len(str(c.value or "")) for c in col)
            ws.column_dimensions[col[0].column_letter].width = min(max(longest + 2, 12), 50)
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
    return buf.getvalue()


# ------------------------- session state -------------------------
def init_state():
    ss = st.session_state
    ss.setdefault("store", CardStore())
    for mode in ("phone", "cam"):   # one card-by-card flow per tab
        ss.setdefault(f"{mode}_n", 0)            # bumping it resets that tab's widgets
        ss.setdefault(f"{mode}_step", "front")
        ss.setdefault(f"{mode}_front", None)
    ss.setdefault("up_n", 0)        # resets the bulk-upload widgets after "add"
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


def scan_flow(mode: str):
    """Card-by-card flow: front -> back -> save (reading starts as soon as you save).
    mode 'phone': phone's own camera app via the file picker (rear camera, full quality).
    mode 'cam'  : in-browser camera widget."""
    ss = st.session_state
    n, step = ss[f"{mode}_n"], ss[f"{mode}_step"]
    side = "FRONT" if step == "front" else "BACK"
    st.markdown(f"**Card #{ss.store.next_id} · {side} side**")

    if mode == "phone":
        st.caption("Neeche tap karo → **Take Photo** chuno → card ki photo lo.")
        shot = st.file_uploader(f"{step} photo", type=EXTS, key=f"phone_{n}_{step}", label_visibility="collapsed")
    else:
        st.caption("Card ko seedha, achhi roshni me, poora frame me rakho. Camera allow karna.")
        shot = st.camera_input(f"{step} photo", key=f"cam_{n}_{step}", label_visibility="collapsed")

    def save_card(back_bytes):
        base = f"card_{ss.store.next_id:03d}"
        back = (f"{base}_back.jpg", back_bytes) if back_bytes else None
        front = (f"{base}_front.jpg", ss[f"{mode}_front"])
        if submit_card(front, back):
            ss[f"{mode}_n"] += 1
            ss[f"{mode}_step"], ss[f"{mode}_front"] = "front", None
            st.rerun()

    if step == "front":
        if shot:
            st.image(shot.getvalue(), width=200)
            if st.button("➡️ Next: back side", type="primary", key=f"{mode}_next_{n}"):
                ss[f"{mode}_front"] = compress_image(shot.getvalue())
                ss[f"{mode}_step"] = "back"
                st.rerun()
            if st.button("✅ Save (no back side)", key=f"{mode}_nob_{n}"):
                ss[f"{mode}_front"] = compress_image(shot.getvalue())
                save_card(None)
    else:
        st.image(ss[f"{mode}_front"], caption="Front saved", width=140)
        if shot:
            st.image(shot.getvalue(), width=200)
            if st.button("✅ Save card", type="primary", key=f"{mode}_save_{n}"):
                save_card(compress_image(shot.getvalue()))
        if st.button("⏭️ Skip back & save", key=f"{mode}_skip_{n}"):
            save_card(None)
        if st.button("↩️ Retake front", key=f"{mode}_retake_{n}"):
            ss[f"{mode}_step"], ss[f"{mode}_front"] = "front", None
            st.rerun()


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
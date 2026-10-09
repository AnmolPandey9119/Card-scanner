"""Free, offline card reader: RapidOCR (ONNX) for text + rule-based parser for fields.
No API key, no internet needed after install (models ship inside the pip package)."""
import io
import re
import threading

import numpy as np
from PIL import Image

_engine = None
_engine_lock = threading.Lock()
_run_lock = threading.Lock()   # onnxruntime session is shared; run one image at a time


def _get_engine():
    global _engine
    with _engine_lock:
        if _engine is None:
            from rapidocr_onnxruntime import RapidOCR
            _engine = RapidOCR()
        return _engine


def ocr_lines(jpeg_bytes: bytes) -> list:
    """Return text lines top-to-bottom: [{"text", "h", "y"}] (h = text height, used for font size)."""
    img = Image.open(io.BytesIO(jpeg_bytes)).convert("RGB")
    arr = np.array(img)[:, :, ::-1]   # RGB -> BGR
    with _run_lock:
        result, _ = _get_engine()(arr)
    items = []
    for box, text, conf in (result or []):
        text = str(text).strip()
        if not text or float(conf) < 0.4:
            continue
        ys = [p[1] for p in box]
        xs = [p[0] for p in box]
        items.append({"text": text, "x": min(xs), "y": min(ys), "h": max(ys) - min(ys), "yc": sum(ys) / 4})
    # group boxes that sit on the same visual line (left-to-right), then order top-to-bottom
    items.sort(key=lambda d: d["yc"])
    lines, cur = [], []
    for it in items:
        if cur and abs(it["yc"] - cur[-1]["yc"]) > max(cur[-1]["h"], it["h"]) * 0.6:
            lines.append(cur)
            cur = []
        cur.append(it)
    if cur:
        lines.append(cur)
    out = []
    for grp in lines:
        grp.sort(key=lambda d: d["x"])
        out.append({
            "text": "  ".join(g["text"] for g in grp),
            "h": max(g["h"] for g in grp),
            "y": grp[0]["y"],
        })
    return out


# ------------------------- parsing -------------------------
STATES = [
    "Andhra Pradesh", "Arunachal Pradesh", "Assam", "Bihar", "Chhattisgarh", "Goa", "Gujarat", "Haryana",
    "Himachal Pradesh", "Jharkhand", "Karnataka", "Kerala", "Madhya Pradesh", "Maharashtra", "Manipur",
    "Meghalaya", "Mizoram", "Nagaland", "Odisha", "Orissa", "Punjab", "Rajasthan", "Sikkim", "Tamil Nadu",
    "Telangana", "Tripura", "Uttar Pradesh", "Uttarakhand", "West Bengal", "Delhi", "New Delhi",
    "Jammu & Kashmir", "Jammu and Kashmir", "Ladakh", "Chandigarh", "Puducherry",
]
COUNTRIES = ["India", "UAE", "United Arab Emirates", "USA", "United States", "UK", "United Kingdom",
             "Singapore", "Nepal", "Bangladesh", "Sri Lanka", "Australia", "Canada", "Germany"]
DESIG_WORDS = r"""director|manager|founder|co-?founder|ceo|cto|cfo|coo|chairman|president|vice president|\bvp\b|
partner|proprietor|owner|engineer|developer|consultant|executive|officer|head|lead|sales|marketing|
associate|analyst|architect|designer|doctor|dr\.|advocate|ca\b|accountant|supervisor|incharge|in-charge|
representative|general manager|\bgm\b|\bagm\b|\bmd\b|managing|proprieter|specialist|coordinator|
administrator|business development|relationship"""
COMPANY_WORDS = r"""pvt|private|ltd|limited|llp|llc|inc\b|corp|company|co\.|enterprises?|solutions?|industries|
industrial|traders?|trading|technologies|technology|tech\b|systems|services|associates|group|agency|agencies|
exports?|imports?|international|global|infra|constructions?|builders|engineering|labs?|laboratories|
pharma|motors|foods|studio|consultancy|consultants|marketing|works|stores?|mart|distributors?|
suppliers?|electricals?|electronics|textiles?|fabrics?|printers|packaging|logistics|hospital|clinic|
institute|academy|school|college|bank|finance|insurance|realty|developers|hotels?|resort"""
ADDR_WORDS = r"""road|\brd\b|street|\bst\b|nagar|colony|sector|plot|floor|building|bldg|tower|complex|
near|opp\.?|opposite|behind|lane|marg|chowk|market|industrial area|estate|phase|block|\bno\.|\bpo\b|
village|vill|dist|district|taluka|tehsil|cross|main|layout|park|avenue|\bave\b|palace|apartment|
flat|shop|office no|survey|gali|bazaar|bazar|society|vihar|puram|enclave|extension|\bext\b|
cinema|mall|plaza|house|bhavan|bhawan|mandir|cantonment"""

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+\s?@\s?[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
URL_RE = re.compile(r"(?:https?://)?(?:www\.)[A-Za-z0-9\-._~/?#=&%]+|(?:https?://)[A-Za-z0-9\-._~/?#=&%]+"
                    r"|\b[A-Za-z0-9\-]+\.(?:com|in|co\.in|net|org|co|io|biz|info|online|store|shop|tech)\b(?:/[^\s]*)?",
                    re.I)
SOCIAL_RE = re.compile(r"linkedin|facebook|instagram|twitter|youtube|t\.me|wa\.me|whatsapp", re.I)
PHONE_RE = re.compile(r"(?<![\w@])(?:\+?\d[\d\s\-().]{7,}\d)(?![\w@])")
PIN_RE = re.compile(r"(?<!\d)[1-9]\d{2}\s?\d{3}(?!\d)")
GST_RE = re.compile(r"\b\d{2}[A-Z]{5}\d{4}[A-Z][A-Z\d]Z[A-Z\d]\b")


def _w(words):
    return re.compile(words.replace("\n", ""), re.I)


DESIG_RE, COMPANY_RE, ADDR_RE = _w(DESIG_WORDS), _w(COMPANY_WORDS), _w(ADDR_WORDS)
LABEL_RE = re.compile(r"^\s*(?:e-?mail|email|mail|web(?:site)?|url|tel(?:ephone)?|ph(?:one)?|mob(?:ile)?|cell|"
                      r"fax|whats\s?app|office|res|contact|call|m|t|p|e|w|f)\s*[:.\-|/]+\s*", re.I)


def _digits(s):
    return re.sub(r"\D", "", s)


def _split_phone(raw: str):
    """Return (kind, formatted) where kind is mobile | landline | None."""
    d = _digits(raw)
    if len(d) < 8 or len(d) > 15:
        return None, ""
    if len(d) == 12 and d.startswith("91") and d[2] in "6789":
        return "mobile", "+91 " + d[2:]
    if len(d) == 11 and d.startswith("0") and d[1] in "6789":
        return "mobile", d[1:]
    if len(d) == 10 and d[0] in "6789":
        return "mobile", d
    if len(d) == 10 and d[0] == "0":
        return "landline", raw.strip()
    if raw.strip().startswith("+") and len(d) >= 11:   # foreign number
        return "mobile", raw.strip()
    return "landline", raw.strip()


def _clean_line(t):
    return re.sub(r"\s{2,}", " ", t).strip(" |,;:-")


def parse_card(front_lines: list, back_lines: list) -> dict:
    """front_lines/back_lines: output of ocr_lines (or [])."""
    out = {k: [] for k in ["Name", "Designation", "Company", "Mobile", "Phone / Landline", "Email", "Website",
                           "Address", "City", "State", "Pincode", "Country", "LinkedIn / Social",
                           "Services / Products", "Other Notes"]}

    def add(k, v):
        v = v.strip()
        if v and v.lower() not in [x.lower() for x in out[k]]:
            out[k].append(v)

    sides = [("front", front_lines or []), ("back", back_lines or [])]
    leftovers = {"front": [], "back": []}
    gst_found = set()

    for side, lines in sides:
        for ln in lines:
            text = ln["text"]
            rest = text
            hit = False

            for g in GST_RE.findall(text.replace(" ", "").upper()):
                gst_found.add(g)
                hit = True
            m_gst = re.search(r"gst\w*\s*(?:no\.?|in)?\s*[:\-]?", text, re.I)
            if m_gst and GST_RE.search(text.replace(" ", "").upper()):
                rest = ""

            for m in EMAIL_RE.findall(rest):
                add("Email", m.replace(" ", "").lower().strip(".,;"))
                rest = rest.replace(m, " ")
                hit = True

            for m in URL_RE.findall(rest):
                u = m.strip(".,;")
                if "@" in u:
                    continue
                if SOCIAL_RE.search(u):
                    add("LinkedIn / Social", u)
                else:
                    add("Website", u)
                rest = rest.replace(m, " ")
                hit = True

            if SOCIAL_RE.search(rest) and not hit:
                add("LinkedIn / Social", _clean_line(rest))
                rest = ""
                hit = True

            # phones (skip pincode-like 6-digit groups; those are handled in the address step)
            for m in PHONE_RE.findall(rest):
                kind, val = _split_phone(m)
                if not kind:
                    continue
                label = rest[: rest.find(m)].lower()
                if re.search(r"fax", label[-8:]):
                    kind = "landline"
                    val = val + " (Fax)"
                elif re.search(r"(mob|cell|m\s*[:.\-]|whats)", label[-10:]) and kind == "landline":
                    kind = "mobile"
                add("Mobile" if kind == "mobile" else "Phone / Landline", val)
                rest = rest.replace(m, " ")
                hit = True

            rest = LABEL_RE.sub("", rest) if hit else rest
            rest = _clean_line(re.sub(r"\b(?:e-?mail|email|web(?:site)?|tel|ph|phone|mob(?:ile)?|fax|cell)\b\s*[:.\-]?", " ", rest, flags=re.I)) if hit else _clean_line(rest)
            if rest and (not hit or len(re.sub(r"[^A-Za-z]", "", rest)) >= 4):
                leftovers[side].append({**ln, "text": rest})

    # ---- classify leftovers ----
    front_left = leftovers["front"]
    back_left = leftovers["back"]
    all_left = [(s, l) for s in ("front", "back") for l in leftovers[s]]

    used = set()

    def take(idx):
        used.add(idx)

    # address: lines with address keywords or a pincode, plus neighbours that look like continuation
    addr_idx = []
    for i, (s, l) in enumerate(all_left):
        t = l["text"]
        if PIN_RE.search(t) or ADDR_RE.search(t) or any(re.search(rf"\b{re.escape(st)}\b", t, re.I) for st in STATES):
            if not (COMPANY_RE.search(t) and not ADDR_RE.search(t) and not PIN_RE.search(t)):
                addr_idx.append(i)
    # fill a single-line gap between two address lines on the same side
    for a, b in zip(addr_idx, addr_idx[1:]):
        if b - a == 2 and all_left[a][0] == all_left[b][0] and (a + 1) not in addr_idx:
            mid = all_left[a + 1][1]["text"]
            if not DESIG_RE.search(mid) and not COMPANY_RE.search(mid):
                addr_idx.append(a + 1)
    addr_idx = sorted(set(addr_idx))
    addr_text = ", ".join(all_left[i][1]["text"] for i in addr_idx)
    for i in addr_idx:
        take(i)

    if addr_text:
        pins = PIN_RE.findall(addr_text)
        if pins:
            add("Pincode", pins[-1].replace(" ", ""))
        for st in sorted(STATES, key=len, reverse=True):
            if re.search(rf"\b{re.escape(st)}\b", addr_text, re.I):
                add("State", "Delhi" if st == "New Delhi" else st)
                break
        for c in COUNTRIES:
            if re.search(rf"\b{re.escape(c)}\b", addr_text, re.I):
                add("Country", c)
                break
        # city = segment just before state / pincode
        parts = [p.strip() for p in re.split(r"[,\n]| - ", addr_text) if p.strip()]
        st_val = out["State"][0] if out["State"] else ""
        city = ""
        for j, p in enumerate(parts):
            pn = PIN_RE.sub("", p).strip(" -.")
            if st_val and re.fullmatch(re.escape(st_val), pn, re.I) and j > 0:
                city = PIN_RE.sub("", parts[j - 1]).strip(" -.")
                break
        if not city:
            for p in parts:
                m = re.match(r"^([A-Za-z .]{3,})\s*[-–]?\s*[1-9]\d{2}\s?\d{3}$", p)
                if m:
                    city = m.group(1).strip(" -.")
                    break
        if not city and out["Pincode"]:
            for j, p in enumerate(parts):
                if out["Pincode"][0] in p.replace(" ", "") and j > 0 and not PIN_RE.sub("", p).strip(" -."):
                    city = parts[j - 1]
        if city and not ADDR_RE.search(city) and len(city) < 30:
            add("City", city.title() if city.isupper() else city)
        out["Address"].append(addr_text)

    # designation
    for i, (s, l) in enumerate(all_left):
        if i not in used and DESIG_RE.search(l["text"]) and not COMPANY_RE.search(l["text"]) and len(l["text"]) < 60:
            add("Designation", l["text"])
            take(i)
            if len(out["Designation"]) >= 2:
                break

    # company: keyword match, otherwise the biggest-font line on the front
    comp = [i for i, (s, l) in enumerate(all_left) if i not in used and COMPANY_RE.search(l["text"]) and len(l["text"]) < 70]
    if comp:
        i = comp[0]
        add("Company", all_left[i][1]["text"])
        take(i)

    # name: front line, 2-4 alphabetic words, no digits; prefer the one closest to the designation
    def looks_like_name(t):
        t = re.sub(r"^(mr|mrs|ms|dr|shri|smt|er|ca|adv)\.?\s+", "", t, flags=re.I)
        words = t.split()
        if not 2 <= len(words) <= 4 and not (len(words) == 1 and len(t) > 3):
            return False
        if re.search(r"\d|@|www|\.com", t, re.I):
            return False
        return all(re.fullmatch(r"[A-Za-z.'\-]+", w) for w in words)

    name_cands = [i for i, (s, l) in enumerate(all_left)
                  if i not in used and s == "front" and looks_like_name(l["text"])]
    if name_cands:
        di = next((i for i, (s, l) in enumerate(all_left) if l["text"] in out["Designation"]), None)
        if di is not None:
            before = [i for i in name_cands if i < di]
            pick = (before or name_cands)[-1] if before else name_cands[0]
        else:
            pick = max(name_cands, key=lambda i: all_left[i][1]["h"])
            if len(name_cands) > 1 and not out["Company"]:
                pick = name_cands[0]
        add("Name", all_left[pick][1]["text"])
        take(pick)

    if not out["Company"]:   # fall back to the largest remaining front line
        rem = [i for i, (s, l) in enumerate(all_left) if i not in used and s == "front"
               and len(re.sub(r"[^A-Za-z]", "", l["text"])) >= 3]
        if rem:
            i = max(rem, key=lambda k: all_left[k][1]["h"])
            add("Company", all_left[i][1]["text"])
            take(i)

    # remaining: back side -> services, front side -> notes
    for i, (s, l) in enumerate(all_left):
        if i in used:
            continue
        add("Services / Products" if s == "back" else "Other Notes", l["text"])
    for g in sorted(gst_found):
        add("Other Notes", f"GST: {g}")

    row = {k: "; ".join(v) for k, v in out.items()}
    row["Address"] = out["Address"][0] if out["Address"] else ""
    return row


def read_card(front_jpeg, back_jpeg) -> dict:
    """front_jpeg / back_jpeg: bytes or None -> dict of fields."""
    f = ocr_lines(front_jpeg) if front_jpeg else []
    b = ocr_lines(back_jpeg) if back_jpeg else []
    if not f and not b:
        raise RuntimeError("Image me koi text nahi mila (photo blur / bahut dark ho sakti hai).")
    return parse_card(f, b)

"""Free, offline card reader: RapidOCR (ONNX) for text + rule-based parser for fields.
No API key, no internet needed after install (models ship inside the pip package)."""
import io
import re
import threading

import numpy as np
from PIL import Image

_engine = None
_engine_lock = threading.Lock()
DET_MAX_SIDE = 960   # smaller = faster detection; cards have big enough text for this


def _get_engine():
    global _engine
    with _engine_lock:
        if _engine is None:
            from rapidocr_onnxruntime import RapidOCR
            _engine = RapidOCR(det_model_path=None, det_limit_type="max", det_limit_side_len=DET_MAX_SIDE)
            _engine(np.zeros((320, 320, 3), dtype=np.uint8))   # warm-up so the first real card is fast
        return _engine


def warmup():
    """Load the OCR model in the background (call once at app start)."""
    threading.Thread(target=_get_engine, daemon=True).start()


def _run(arr):
    result, _ = _get_engine()(arr)   # onnxruntime sessions are thread-safe
    items = []
    for box, text, conf in (result or []):
        text = str(text).strip()
        if not text or float(conf) < 0.4:
            continue
        ys = [p[1] for p in box]
        xs = [p[0] for p in box]
        items.append({"text": text, "x": min(xs), "y": min(ys), "h": max(ys) - min(ys),
                      "w": max(xs) - min(xs), "yc": sum(ys) / 4})
    return items


_DOMAIN_RE = re.compile(r"[A-Za-z0-9\-]+\.(?:com|in|net|org|co|biz|info|co\.in)\b", re.I)


def _recheck_at(arr, items):
    """A box that looks like an email but has no '@' is read again from an enlarged crop
    (small '@' signs are often misread as 'a' / dropped at the normal size)."""
    H, W = arr.shape[:2]
    for it in items:
        t = it["text"]
        if "@" in t or not _DOMAIN_RE.search(t) or re.match(r"(?i)\s*(https?://|www\.)", t):
            continue
        pad = max(6, int(it["h"] * 0.4))
        x0, x1 = max(0, int(it["x"]) - pad), min(W, int(it["x"] + it["w"]) + pad)
        y0, y1 = max(0, int(it["y"]) - pad), min(H, int(it["y"] + it["h"]) + pad)
        crop = arr[y0:y1, x0:x1]
        if crop.size == 0:
            continue
        scale = max(1.5, min(4.0, 80.0 / max(it["h"], 1)))
        big = np.array(Image.fromarray(crop[:, :, ::-1]).resize(
            (int(crop.shape[1] * scale), int(crop.shape[0] * scale)), Image.LANCZOS))[:, :, ::-1]
        sub = sorted(_run(np.ascontiguousarray(big)), key=lambda d: d["x"])
        new = "".join(d["text"] for d in sub)
        if "@" in new:
            it["text"] = new
    return items


def _score(items):
    """Rough quality: number of characters read (used to pick the best rotation)."""
    return sum(len(i["text"]) for i in items)


def _flatness(items):
    if not items:
        return 0
    r = sorted(i["w"] / max(i["h"], 1) for i in items)
    return r[len(r) // 2]


def ocr_lines(jpeg_bytes: bytes) -> list:
    """Return text lines top-to-bottom: [{"text", "h", "y"}] (h = text height, used for font size)."""
    img = Image.open(io.BytesIO(jpeg_bytes)).convert("RGB")
    arr = np.array(img)[:, :, ::-1]   # RGB -> BGR
    items = _run(arr)
    items = _recheck_at(arr, items)
    portrait = arr.shape[0] > arr.shape[1] * 1.15   # cards are usually landscape -> may be sideways
    if portrait or _score(items) < 25:   # try 90 / 270 degrees as well
        cands = [items] + [_run(np.ascontiguousarray(np.rot90(arr, k))) for k in (1, 3)]
        top = max(_score(c) for c in cands)
        good = [c for c in cands if _score(c) >= 0.8 * top]
        # upright text has wide, flat boxes: pick the candidate with the flattest boxes
        items = max(good, key=_flatness)
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
DESIG_WORDS = r"""director|manager|founder|co-?founder|\bceo\b|\bcto\b|\bcfo\b|\bcoo\b|chairman|president|vice president|\bvp\b|
partner|proprietor|owner|engineer|developer|consultant|executive|officer|head|lead|sales|marketing|
associate|analyst|architect|designer|doctor|dr\.|advocate|\bca\b|accountant|supervisor|incharge|in-charge|
representative|general manager|\bgm\b|\bagm\b|\bmd\b|managing|proprieter|specialist|coordinator|
administrator|business development|relationship|chief|senior|junior|\bsr\.?|\bjr\.?|trainee|intern|assistant|
secretary|regional|zonal|dealer|distributor|franchise|agent|advisor|adviser|surgeon|professor|principal|
trustee|treasurer|technician|operator|controller|auditor|instructor|trainer|dentist|physician|lawyer|attorney|
counsel|programmer|scientist|strategist|planner|surveyor|contractor|broker|editor|producer|photographer|
supervisor|inspector|incharge|proprietress|chairperson|chairwoman|promoter|expert|
clerk|cashier|receptionist|\bhr\b|human resources?|admin\b|procurement|service engineer"""
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
STRONG_COMPANY_RE = re.compile(r"\b(?:pvt|private|ltd|limited|llp|llc|inc|corp|corporation|company|co|enterprises?|"
                               r"industries|traders?|trading|associates|group|exports?|imports?)\b", re.I)
STRONG_ADDR_RE = re.compile(r"\b(?:road|rd|street|nagar|colony|sector|plot|floor|building|bldg|tower|complex|lane|marg|"
                            r"chowk|near|opp|opposite|behind|phase|block|village|district|po)\b", re.I)
LEGAL_RE = re.compile(r"\b(?:pvt|private|ltd|limited|llp|llc|inc|corp|corporation|co|enterprises?|industries|"
                      r"traders?|associates|sons|brothers|bros)\b", re.I)
ROLE_RE = re.compile(r"\b(?:manager|director|head|executive|officer|engineer|secretary|incharge|in-charge|analyst|"
                     r"consultant|partner|chairman|chairperson|president|founder|co-?founder|owner|proprietor|"
                     r"proprietress|ceo|cfo|cto|coo|md|gm|agm|vp|supervisor|coordinator|specialist|administrator|"
                     r"representative|advisor|adviser|architect|designer|developer|trainee|intern|assistant)\b", re.I)


def _company_like(t: str) -> bool:
    """True for 'Sharma Traders Pvt Ltd'; False for 'Export Manager' / 'Company Secretary'."""
    if LEGAL_RE.search(t) and not (ROLE_RE.search(t) and not re.search(r"\b(?:pvt|private|ltd|limited|llp|llc)\b", t, re.I)):
        return True
    return bool(STRONG_COMPANY_RE.search(t)) and not ROLE_RE.search(t)


SPLIT_RE = re.compile(r"\s{2,}|\s[|/\u2022\u2013\u2014]\s|\s-\s")
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
    return t.strip(" |,;:-")   # keep double spaces: they mark gaps between separate text boxes


AT_FIXES = [
    (re.compile(r"\(\s*a\s*\)|\[\s*at\s*\]|\(\s*at\s*\)|[\u00a9\u00ae\uff20]", re.I), "@"),   # (a) [at] (c) (R) -> @
    (re.compile(r"(\.(?:com|net|org|biz|info|in))[|/\\IJ](?=[A-Za-z0-9._-]+@)"), r"\1 "),   # two emails glued by | / \ I J
]
EMAIL_LABEL_RE = re.compile(r"e-?\s?mail|\bmail\b|^\s*e\s*[:.\-|]", re.I)


def fix_at(text: str) -> str:
    for rx, rep in AT_FIXES:
        text = rx.sub(rep, text)
    return text


def repair_email(token: str, hosts: list) -> str:
    """OCR often turns '@' into 'a' (or drops it): 'rajeshasharmatraders.com' -> 'rajesh@sharmatraders.com'.
    The email domain is usually the same as the website, so use the website host to find the split."""
    t = token.strip().lower()
    for h in sorted(hosts, key=len, reverse=True):
        if t.endswith(h) and len(t) > len(h):
            local = t[: -len(h)]
            if len(local) >= 3 and local[-1] in "a@":
                local = local[:-1]
            local = local.strip(".-_ ")
            if local:
                return f"{local}@{h}"
    return t


def parse_card(front_lines: list, back_lines: list) -> dict:
    """front_lines/back_lines: output of ocr_lines (or [])."""
    out = {k: [] for k in ["Name", "Designation", "Company", "Mobile", "Phone / Landline", "Email", "Website",
                           "Address", "City", "State", "Pincode", "Country", "LinkedIn / Social",
                           "Services / Products", "Other Notes"]}

    def add(k, v):
        v = re.sub(r"\s+", " ", v).strip()
        if v and v.lower() not in [x.lower() for x in out[k]]:
            out[k].append(v)

    email_cands = []
    sides = [("front", front_lines or []), ("back", back_lines or [])]
    leftovers = {"front": [], "back": []}
    gst_found = set()

    for side, lines in sides:
        for ln in lines:
            text = fix_at(ln["text"])
            rest = text
            hit = False
            email_label = bool(EMAIL_LABEL_RE.search(text))

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
                elif email_label and not re.match(r"(?i)(https?://|www\.)", u):
                    email_cands.append(u)          # looks like an email whose '@' was misread
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

    # emails whose '@' was lost: rebuild using the website host
    hosts = [re.sub(r"(?i)^(https?://)?(www\.)?", "", w).split("/")[0].lower() for w in out["Website"]]
    for c in email_cands:
        add("Email", repair_email(c, hosts))

    # ---- classify leftovers ----
    front_left = leftovers["front"]
    back_left = leftovers["back"]
    # "Rajesh Kumar | Director" on one line -> two lines (only when exactly one part is a designation)
    for side in ("front", "back"):
        new = []
        for l in leftovers[side]:
            parts = [p.strip() for p in SPLIT_RE.split(l["text"]) if p.strip()]
            flags = [bool(DESIG_RE.search(p)) and not _company_like(p) for p in parts]
            if len(parts) >= 2 and 0 < sum(flags) < len(parts) and not PIN_RE.search(l["text"]):
                new += [{**l, "text": p} for p in parts]
            else:
                new.append(l)
        leftovers[side] = new
    all_left = [(s, l) for s in ("front", "back") for l in leftovers[s]]

    used = set()

    def take(idx):
        used.add(idx)

    # designation first (so words like "Marketing" / "Main" do not push it into company / address)
    for i, (s, l) in enumerate(all_left):
        t = l["text"]
        if (DESIG_RE.search(t) and not re.search(r"\d", t) and not _company_like(t)
                and not STRONG_ADDR_RE.search(t) and len(t) < 60 and len(out["Designation"]) < 2):
            add("Designation", t)
            take(i)

    # address: lines with address keywords or a pincode, plus neighbours that look like continuation
    addr_idx = []
    for i, (s, l) in enumerate(all_left):
        t = l["text"]
        if i in used:
            continue
        weak_ok = bool(ADDR_RE.search(t)) and (STRONG_ADDR_RE.search(t) or re.search(r"\d|,", t))
        if PIN_RE.search(t) or weak_ok or any(re.search(rf"\b{re.escape(st)}\b", t, re.I) for st in STATES):
            if not (COMPANY_RE.search(t) and not ADDR_RE.search(t) and not PIN_RE.search(t)):
                addr_idx.append(i)
    # fill a single-line gap between two address lines on the same side
    for a, b in zip(addr_idx, addr_idx[1:]):
        if b - a == 2 and all_left[a][0] == all_left[b][0] and (a + 1) not in addr_idx:
            mid = all_left[a + 1][1]["text"]
            if not DESIG_RE.search(mid) and not COMPANY_RE.search(mid):
                addr_idx.append(a + 1)
    addr_idx = sorted(set(addr_idx))
    addr_text = re.sub(r"\s+", " ", ", ".join(all_left[i][1]["text"] for i in addr_idx))
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

    # designation (second chance: lines that mention company-like words but are short)
    for i, (s, l) in enumerate(all_left):
        if i not in used and DESIG_RE.search(l["text"]) and not _company_like(l["text"]) and len(l["text"]) < 60:
            add("Designation", l["text"])
            take(i)
            if len(out["Designation"]) >= 2:
                break

    # company: keyword match, otherwise the biggest-font line on the front
    comp = [i for i, (s, l) in enumerate(all_left) if i not in used and COMPANY_RE.search(l["text"]) and len(l["text"]) < 70]
    comp.sort(key=lambda i: (not _company_like(all_left[i][1]["text"]), i))   # real companies first, keep card order
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

    if not out["Designation"] and out["Name"]:   # line right below (or above) the name, if it looks like a title
        ni = next((i for i, (s, l) in enumerate(all_left) if l["text"] == out["Name"][0]), None)
        for j in ([ni + 1, ni - 1] if ni is not None else []):
            if 0 <= j < len(all_left) and j not in used and all_left[j][0] == "front":
                t = all_left[j][1]["text"]
                if (re.fullmatch(r"[A-Za-z&.,/\- ]{3,40}", t) and len(t.split()) <= 5
                        and not _company_like(t) and not STRONG_ADDR_RE.search(t)
                        and all_left[j][1]["h"] <= all_left[ni][1]["h"] * 1.15):
                    add("Designation", t)
                    take(j)
                    break

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


# ------------------------- cross-field clean-up (used for OCR *and* Gemini output) -------------------------
CITIES = """Delhi|New Delhi|Noida|Greater Noida|Gurgaon|Gurugram|Ghaziabad|Faridabad|Mumbai|Navi Mumbai|Thane|Pune|
Bengaluru|Bangalore|Chennai|Hyderabad|Secunderabad|Kolkata|Ahmedabad|Surat|Vadodara|Rajkot|Jaipur|Jodhpur|Udaipur|
Kota|Lucknow|Kanpur|Agra|Varanasi|Allahabad|Prayagraj|Meerut|Bareilly|Moradabad|Aligarh|Mathura|Gorakhpur|Jhansi|
Saharanpur|Muzaffarnagar|Bulandshahr|Hapur|Sahibabad|Dehradun|Haridwar|Roorkee|Chandigarh|Mohali|Panchkula|
Ludhiana|Amritsar|Jalandhar|Patiala|Bhopal|Indore|Gwalior|Jabalpur|Nagpur|Nashik|Aurangabad|Kolhapur|Solapur|
Patna|Ranchi|Jamshedpur|Dhanbad|Bhubaneswar|Cuttack|Guwahati|Raipur|Bhilai|Visakhapatnam|Vijayawada|Guntur|
Tirupati|Coimbatore|Madurai|Tiruppur|Salem|Trichy|Tiruchirappalli|Kochi|Cochin|Thiruvananthapuram|Trivandrum|
Kozhikode|Mysore|Mysuru|Mangalore|Hubli|Belgaum|Panipat|Sonipat|Rohtak|Ambala|Karnal|Hisar|Bhiwadi|Jammu|
Srinagar|Shimla|Panaji|Vapi|Silvassa|Siliguri|Howrah|Durgapur|Dubai|Abu Dhabi|Sharjah|Singapore|Kathmandu|Dhaka""".replace("\n", "").split("|")
_CITY_ALT = "|".join(re.escape(c.strip()) for c in sorted(set(CITIES), key=len, reverse=True) if c.strip())
CITY_TAIL_RE = re.compile(rf"[\s,|\-\u2013/]*\b({_CITY_ALT})\b(?:[\s,\-\u2013]*[1-9]\d{{2}}\s?\d{{3}})?\s*$", re.I)
CITY_ONLY_RE = re.compile(rf"^\s*({_CITY_ALT})\b[\s,\-\u2013]*(?:[1-9]\d{{2}}\s?\d{{3}})?\s*$", re.I)
_SEP_RE = re.compile(r"\s*[|\u2022\u2013\u2014/,]\s*|\s+-\s+|\s{2,}")
_STOP_END = {"of", "and", "&", "the", "in", "for", "at", "to"}


def _parts(t):
    return [p.strip(" .-") for p in _SEP_RE.split(t) if p.strip(" .-")]


def _cut_role(text, min_head):
    """'Sales Manager ABC Enterprises' -> ('Sales Manager', 'ABC Enterprises'); no cut -> (text, '')."""
    toks = text.split()
    last = max((i for i, tk in enumerate(toks) if ROLE_RE.fullmatch(tk.strip(".,;:()"))), default=-1)
    if last < 0 or last + 1 >= len(toks) or last + 1 < min_head:
        return text, ""
    head, tail = " ".join(toks[: last + 1]), " ".join(toks[last + 1:]).lstrip("-|,:& ").strip()
    if tail and _company_like(tail):
        return head, tail
    return text, ""


def _is_geo(p):
    q = PIN_RE.sub("", p).strip(" -,.")
    if not q:
        return "pin"
    m = CITY_ONLY_RE.match(q)
    if m:
        return "city:" + m.group(1)
    if any(re.fullmatch(re.escape(s), q, re.I) for s in STATES):
        return "state"
    return None


def reconcile_row(row: dict) -> dict:
    """Fix the classic mix-ups so each value ends up in its own column:
       - company name that carries the city / state / pincode      -> city moved to City
       - designation that carries the company ('Sales Manager ABC Pvt Ltd') -> company moved to Company
       - company that starts with a designation                    -> designation moved to Designation
       - City empty but the address clearly names one              -> City filled; City cleaned of state/pin"""
    r = {k: (str(v).strip() if v is not None else "") for k, v in row.items()}
    comp, desig, city = r.get("Company", ""), r.get("Designation", ""), r.get("City", "")

    # ---- designation: remove a company that got glued to it ----
    # a "company" that is really just a city (the card's last line) must not block the real company
    g0 = _is_geo(comp) if comp else None
    if g0 and g0.startswith("city:"):
        city, comp = city or g0[5:], ""

    moved_comp, fixed = "", []
    for part in [p.strip() for p in desig.split(";") if p.strip()]:
        kept, changed = [], False
        for sg in _parts(part):
            h, t = _cut_role(sg, 1)
            if t:                                  # 'Sales Manager ABC Pvt Ltd'
                moved_comp, changed = moved_comp or t, True
                kept.append(h)
            elif _company_like(sg) and not ROLE_RE.search(sg):   # 'Director | Sharma Traders'
                moved_comp, changed = moved_comp or sg, True
            else:
                kept.append(sg)
        fixed.append(", ".join(kept) if changed else part)
    desig = "; ".join(x for x in fixed if x)
    if moved_comp and not comp:
        comp = moved_comp

    # ---- company: remove designation head, city, state, pincode ----
    if comp:
        h, t = _cut_role(comp, 2)
        if t:
            comp = t
            desig = desig or h
        keep = []
        parts = _parts(comp)
        for p in parts:
            g = _is_geo(p)
            if g and len(parts) > 1:
                if g.startswith("city:") and not city:
                    city = g[5:]
                continue
            keep.append(p)
        joined = " ".join(keep) if len(keep) != len(parts) else comp
        m = CITY_TAIL_RE.search(joined)
        if m:
            rem = joined[: m.start()].strip(" ,|-\u2013/")
            words = rem.split()
            if len(words) >= 2 and words[-1].lower() not in _STOP_END:
                city = city or m.group(1)
                joined = rem
        comp = joined.strip()

    # ---- city: clean + fill from the address if empty ----
    if city:
        city = PIN_RE.sub("", city)
        for s in sorted(STATES, key=len, reverse=True):
            city = re.sub(rf"(?i)[\s,\-]*\b{re.escape(s)}\b", "", city)
        city = city.strip(" ,-.")
        city = city.title() if city.isupper() else city
    if not city and r.get("Address"):
        for p in reversed(re.split(r"[,\n]", r["Address"])):
            g = _is_geo(p.strip())
            if g and g.startswith("city:"):
                city = g[5:].title() if g[5:].isupper() else g[5:]
                break

    r.update(Company=comp, Designation=desig, City=city)
    return r


def read_card(front_jpeg, back_jpeg) -> dict:
    """front_jpeg / back_jpeg: bytes or None -> dict of fields."""
    f = ocr_lines(front_jpeg) if front_jpeg else []
    b = ocr_lines(back_jpeg) if back_jpeg else []
    if not f and not b:
        raise RuntimeError("Image me koi text nahi mila (photo blur / bahut dark ho sakti hai).")
    return parse_card(f, b)
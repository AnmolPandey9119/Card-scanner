"""Single entry point used by app.py.

Main reader  : Gemini (front + back go in ONE request, both sides end up in the same row).
Offline OCR  : OFF by default. Turn it on only if you want a backup when Gemini's free limit is over:
               set OCR_FALLBACK=1 (env variable / secret). Keeping it off saves RAM + startup time on Render.
Both results go through reconcile_row so city / designation / company never end up in each other's column.
"""
import os
import threading

import gemini_engine
import ocr_engine

OCR_PARALLEL = 1 if ocr_engine.cpu_budget() < 1.5 else 2   # offline OCR is CPU heavy
GEMINI_PARALLEL = 6                                          # Gemini work is waiting on the network, so more threads are fine
_ocr_gate = threading.Semaphore(OCR_PARALLEL)


def cpu_budget() -> float:
    return ocr_engine.cpu_budget()


def ocr_fallback_enabled() -> bool:
    return os.environ.get("OCR_FALLBACK", "").strip().lower() in ("1", "true", "yes", "on")


def max_workers() -> int:
    """How many cards are read at the same time."""
    return GEMINI_PARALLEL if gemini_engine.enabled() else OCR_PARALLEL


def warmup():
    """Load the offline model in the background ONLY when it will really be used."""
    if ocr_fallback_enabled() or not gemini_engine.enabled():
        ocr_engine.warmup()


def read_card(front_jpeg, back_jpeg) -> dict:
    if gemini_engine.enabled():
        try:
            return ocr_engine.reconcile_row(gemini_engine.read_card_gemini(front_jpeg, back_jpeg))
        except gemini_engine.QuotaExhausted as e:
            if not ocr_fallback_enabled():   # no silent low-quality reading: the card can be retried later
                raise RuntimeError(f"Gemini abhi card read nahi kar paya ({e}). Thodi der baad 'Retry failed cards' dabaiye.")
            gemini_engine.status["fallbacks"] += 1   # backup is on -> do NOT lose the card
    elif not ocr_fallback_enabled():
        raise RuntimeError("GEMINI_API_KEY set nahi hai. Render > Environment me key daaliye (ya OCR_FALLBACK=1 rakhiye).")
    with _ocr_gate:
        return ocr_engine.reconcile_row(ocr_engine.read_card(front_jpeg, back_jpeg))
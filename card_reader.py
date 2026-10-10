"""Single entry point used by app.py: Gemini first (if a key is set), offline OCR as the safety net.
Both results go through reconcile_row so city / designation / company never end up in each other's column."""
import gemini_engine
import ocr_engine


def cpu_budget() -> float:
    return ocr_engine.cpu_budget()


def warmup():
    ocr_engine.warmup()   # offline model is the fallback, keep it ready


def read_card(front_jpeg, back_jpeg) -> dict:
    if gemini_engine.enabled():
        try:
            return ocr_engine.reconcile_row(gemini_engine.read_card_gemini(front_jpeg, back_jpeg))
        except gemini_engine.QuotaExhausted:
            gemini_engine.status["fallbacks"] += 1   # limit reached -> do NOT fail the card
    return ocr_engine.reconcile_row(ocr_engine.read_card(front_jpeg, back_jpeg))
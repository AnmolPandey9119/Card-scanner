"""Single entry point used by app.py: Gemini first (if a key is set), offline OCR as the safety net."""
import gemini_engine
import ocr_engine


def warmup():
    ocr_engine.warmup()   # offline model is the fallback, keep it ready


def read_card(front_jpeg, back_jpeg) -> dict:
    if gemini_engine.enabled():
        try:
            return gemini_engine.read_card_gemini(front_jpeg, back_jpeg)
        except gemini_engine.QuotaExhausted:
            gemini_engine.status["fallbacks"] += 1   # limit reached -> do NOT fail the card
    return ocr_engine.read_card(front_jpeg, back_jpeg)
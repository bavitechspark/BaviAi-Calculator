import tkinter as tk
from tkinter import messagebox
import speech_recognition as sr
import sounddevice as sd
import pyttsx3
import wave
import io
import re
import threading
import time
import math

# ============================================================
# BAVITECHSPARK AI CALCULATOR
# Tamil + English | Background wake word | Natural calculations
# ============================================================

APP_NAME = "BAVITECHSPARK AI CALCULATOR"
BG = "#160B2E"
DISPLAY_BG = "#241447"
BUTTON = "#34205C"
BUTTON_ACTIVE = "#4D2D7D"
PURPLE = "#8B5CF6"
WHITE = "#FFFFFF"
GREEN = "#22C55E"
RED = "#EF4444"
GRAY = "#B8AFC9"

root = None
status_label = None
ai_input = None
ai_output = None
voice_button = None

app_running = True
voice_busy = threading.Event()
tts_lock = threading.Lock()
calculator_value = ""
display_is_ai_result = False

# -------------------- Language helpers --------------------

def contains_tamil(text):
    return bool(re.search(r"[\u0B80-\u0BFF]", text or ""))


def tamil_reply(question, language=None):
    return language == "ta" or contains_tamil(question)


def format_number(value):
    try:
        value = float(value)
        if math.isfinite(value) and value.is_integer():
            return str(int(value))
        return f"{value:.8f}".rstrip("0").rstrip(".")
    except (ValueError, TypeError, OverflowError):
        return str(value)


def format_readable_number(value):
    """Add thousands separators for answers shown to the user."""
    try:
        number = float(value)
        if not math.isfinite(number):
            return str(value)
        if number.is_integer():
            return f"{int(number):,}"
        return f"{number:,.8f}".rstrip("0").rstrip(".")
    except (ValueError, TypeError, OverflowError):
        return str(value)


def expand_k_suffix(text):
    """Convert amounts such as 226K and 12.5k into full numbers."""
    def repl(match):
        try:
            return str(float(match.group(1)) * 1000).removesuffix(".0")
        except ValueError:
            return match.group(0)
    text = re.sub(r"(?<![\w.])(\d+(?:\.\d+)?)\s*k\b", repl, text, flags=re.I)
    # Remove only thousands separators (10,000 -> 10000), but preserve a
    # spoken pause such as "salary 10000, shopping 5000" for role parsing.
    text = re.sub(r"(?<=\d),(?=\d)", "", text)
    # Convert Tamil numerals to Arabic numerals.
    tamil_digits = str.maketrans("௦௧௨௩௪௫௬௭௮௯", "0123456789")
    return text.translate(tamil_digits)


# Convert adjacent English number words to a single number without treating
# the operator in "fifty and fifty" as part of one number.
_EN_ONES = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4,
    "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
    "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
    "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30,
    "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
    "eighty": 80, "ninety": 90, "hundred": 100, "thousand": 1000,
}
_EN_NUM_WORDS = "|".join(sorted(_EN_ONES, key=len, reverse=True))
_EN_NUM_PHRASE = re.compile(r"\b(?:" + _EN_NUM_WORDS + r")(?:[ -]+(?:" + _EN_NUM_WORDS + r"))*\b", re.I)


def _english_words_value(phrase):
    words = re.findall(r"[a-z]+", phrase.lower())
    total = 0
    current = 0
    for word in words:
        n = _EN_ONES[word]
        if n == 100:
            current = max(current, 1) * 100
        elif n == 1000:
            total += max(current, 1) * 1000
            current = 0
        else:
            current += n
    return total + current


def convert_english_number_words(text):
    return _EN_NUM_PHRASE.sub(lambda m: str(_english_words_value(m.group(0))), text)


_TAMIL_NUMBERS = {
    "பூஜ்ஜியம்": "0", "பூஜ்யம்": "0", "ஒன்று": "1", "ஒரு": "1",
    "இரண்டு": "2", "மூன்று": "3", "நான்கு": "4", "ஐந்து": "5",
    "ஆறு": "6", "ஏழு": "7", "எட்டு": "8", "ஒன்பது": "9",
    "பத்து": "10", "பதினொன்று": "11", "பன்னிரண்டு": "12",
    "பதின்மூன்று": "13", "பதினான்கு": "14", "பதினைந்து": "15",
    "பதினாறு": "16", "பதினேழு": "17", "பதினெட்டு": "18",
    "பத்தொன்பது": "19", "இருபது": "20", "முப்பது": "30",
    "நாற்பது": "40", "ஐம்பது": "50", "அறுபது": "60",
    "எழுபது": "70", "எண்பது": "80", "தொண்ணூறு": "90",
    "நூறு": "100", "ஆயிரம்": "1000",
}


def normalize_numbers(text):
    text = expand_k_suffix(text.lower())
    text = convert_english_number_words(text)
    for word, number in sorted(_TAMIL_NUMBERS.items(), key=lambda x: len(x[0]), reverse=True):
        text = text.replace(word, number)
    return text


# -------------------- Money / savings helpers --------------------
_AMOUNT_RE = re.compile(r"(?<![\w])\d+(?:\.\d+)?")


def amount_tokens(text):
    return [(m.start(), m.end(), float(m.group(0))) for m in _AMOUNT_RE.finditer(text)]


def nearest_amount_to_keyword(text, keywords, all_role_keywords=None):
    """Find the amount belonging to a label, preferring amounts AFTER the label.

    Real speech commonly sounds like ``salary ... 10000, shopping ... 5000``.
    Pure distance can wrongly attach 10000 to the word shopping because it sits
    immediately before that label. This version searches forward first and
    stops at the next income/expense label, then falls back to a value before
    the label (for phrasing such as ``10000 salary``).
    """
    tokens = amount_tokens(text)
    if not tokens:
        return None

    all_role_keywords = all_role_keywords or keywords
    role_positions = []
    for role_key in sorted(set(all_role_keywords), key=len, reverse=True):
        start_pos = 0
        while True:
            pos = text.find(role_key, start_pos)
            if pos < 0:
                break
            role_positions.append((pos, pos + len(role_key), role_key))
            start_pos = pos + max(1, len(role_key))
    role_positions.sort()

    candidates = []
    for key in sorted(set(keywords), key=len, reverse=True):
        start_pos = 0
        while True:
            pos = text.find(key, start_pos)
            if pos < 0:
                break
            key_end = pos + len(key)
            # Find the next distinct role-label boundary after this label.
            later_labels = [r[0] for r in role_positions if r[0] >= key_end and r[0] > pos]
            next_boundary = min(later_labels) if later_labels else len(text) + 1

            # First prefer the closest amount after this label, before another
            # income/expense label. Allow ordinary filler words between them.
            after = [t for t in tokens if key_end <= t[0] < next_boundary and t[0] - key_end <= 55]
            if after:
                token = min(after, key=lambda t: t[0] - key_end)
                candidates.append((0, token[0] - key_end, token[0], token[1], token[2]))
            else:
                # Fallback for reversed phrasing: "10000 salary".
                previous_boundary = max((r[1] for r in role_positions if r[1] <= pos), default=0)
                before = [t for t in tokens if previous_boundary <= t[1] <= pos and pos - t[1] <= 28]
                if before:
                    token = min(before, key=lambda t: pos - t[1])
                    candidates.append((1, pos - token[1], token[0], token[1], token[2]))
            start_pos = pos + max(1, len(key))

    if not candidates:
        return None
    _, _, token_start, token_end, value = min(candidates, key=lambda x: (x[0], x[1]))
    return token_start, token_end, value


def get_income_expense(text):
    income_words = [
        "income", "salary", "earned", "earnings", "earn", "made",
        "வருமானம்", "சம்பளம்", "சம்பாதித்தேன்", "சம்பாதிச்சேன்", "சம்பாதிச்ச",
        "சம்பாதி", "சம்பாரிச்சேன்", "சம்பாரிச்ச", "சம்பாரி"
    ]
    expense_words = [
        "shopping expenses", "shopping expense", "shopping", "shop", "purchase",
        "purchases", "bought", "buy", "spent on", "spending", "expense", "expenses",
        "spent", "spend", "செலவு செய்தேன்", "செலவு பண்ணிட்டேன்", "செலவு பண்ணினேன்",
        "செலவு பண்ணேன்", "செலவு பண்ண", "செலவுபண்ண", "செலவழிச்சேன்", "செலவழித்தேன்",
        "செலவிட்டேன்", "செலவானது", "செலவாச்சு", "ஷாப்பிங் செலவு", "ஷாப்பிங்",
        "வாங்கினேன்", "வாங்கியது", "வாங்குனேன்", "கடைச் செலவு", "கடை செலவு",
        "செலவு"
    ]
    all_role_words = income_words + expense_words

    # First split common speech into clauses. This prevents the salary amount
    # from being mistaken for a shopping amount when the two are close together.
    clauses = re.split(r",\s*|;\s*|\b(?:and|then)\b|மற்றும்", text, flags=re.I)
    income_value = None
    expense_value = None

    for clause in clauses:
        amounts = amount_tokens(clause)
        if not amounts:
            continue
        has_income = any(word in clause for word in income_words)
        has_expense = any(word in clause for word in expense_words)

        # If this clause says exactly one role, its amount belongs to that role.
        if has_income and not has_expense:
            income_value = amounts[-1][2] if len(amounts) == 1 else nearest_amount_to_keyword(
                clause, income_words
            )[2]
        elif has_expense and not has_income:
            expense_value = amounts[-1][2] if len(amounts) == 1 else nearest_amount_to_keyword(
                clause, expense_words
            )[2]

    if income_value is not None and expense_value is not None:
        return income_value, expense_value

    # Fallback for compact phrases such as "salary 10000 shopping 5000" with
    # no pauses or conjunctions.
    income = nearest_amount_to_keyword(text, income_words, all_role_words)
    expense = nearest_amount_to_keyword(text, expense_words, all_role_words)
    if income and expense and (income[0], income[1]) != (expense[0], expense[1]):
        return income[2], expense[2]
    return None


# -------------------- Natural-language calculator --------------------

def solve_ai_question(question, language=None):
    original = (question or "").strip()
    if not original:
        return "ஒரு கணக்கைச் சொல்லுங்கள்." if tamil_reply(original, language) else "Please tell me a calculation."

    is_ta = tamil_reply(original, language)
    text = normalize_numbers(original)
    text = text.replace("₹", " ")
    low = text.lower()

    # Income - expenses -> savings. Parse while speech pauses (commas) are
    # still available, before the normal math parser treats "and" as plus.
    financial = get_income_expense(low)
    if financial:
        income, expense = financial
        savings = income - expense
        if is_ta:
            if savings < 0:
                return (f"உங்கள் வருமானம் ₹{format_readable_number(income)}. செலவு ₹{format_readable_number(expense)}.\n"
                        f"செலவு வருமானத்தைவிட ₹{format_readable_number(abs(savings))} அதிகம்.")
            return (f"உங்கள் வருமானம் ₹{format_readable_number(income)}.\n"
                    f"உங்கள் செலவு ₹{format_readable_number(expense)}.\n"
                    f"உங்கள் சேமிப்பு ₹{format_readable_number(savings)}.")
        if savings < 0:
            return (f"Your income is ₹{format_readable_number(income)} and expenses are ₹{format_readable_number(expense)}.\n"
                    f"Expenses exceed income by ₹{format_readable_number(abs(savings))}.")
        return (f"Your income is ₹{format_readable_number(income)}.\n"
                f"Your expenses are ₹{format_readable_number(expense)}.\n"
                f"Your savings are ₹{format_readable_number(savings)}.")

    # If this is clearly a savings question but one role/value was not detected,
    # never fall through to generic math and accidentally add the two amounts.
    savings_intent = bool(re.search(r"savings?|சேமிப்பு|மீதம்", low, re.I))
    has_income_label = any(word in low for word in (
        "income", "salary", "earned", "earnings", "சம்பளம்", "வருமானம்", "சம்பாதி", "சம்பாரி"
    ))
    has_expense_label = any(word in low for word in (
        "shopping", "purchase", "bought", "spent", "expense", "செலவு", "ஷாப்பிங்", "வாங்கின"
    ))
    if savings_intent and (has_income_label or has_expense_label):
        return ("சம்பளம்/வருமானம் மற்றும் செலவு ஆகிய இரண்டு தொகைகளையும் தெளிவாகச் சொல்லுங்கள்."
                if is_ta else "Please say both amounts clearly: your salary/income and your expense.")

    # From this point on, commas are thousands separators or punctuation in a
    # normal math expression; they should not influence expression parsing.
    low = low.replace(",", "")

    percent_words = r"(?:%|percent(?:age)?|per\s*cent|சதவீதம்|சதவிகிதம்|பர்சன்டேஜ்|பர்சன்ட்|பர்சன்டேஜ்)"

    # Ratio expressed as a percentage: "226 by 60 what percentage" -> 376.66%.
    ratio_match = re.search(
        r"(\d+(?:\.\d+)?)\s*(?:by|divided\s+by|divide\s+by|/|÷)\s*(\d+(?:\.\d+)?)",
        low,
        re.I,
    )
    has_percent_question = bool(re.search(percent_words + r"|percentage|எவ்வளவு|எத்தனை", low, re.I))
    if ratio_match and has_percent_question:
        numerator = float(ratio_match.group(1))
        denominator = float(ratio_match.group(2))
        if denominator == 0:
            return "பூஜ்ஜியத்தால் வகுக்க முடியாது." if is_ta else "I cannot divide by zero."
        result = numerator / denominator * 100
        if is_ta:
            return (f"{format_readable_number(numerator)} ÷ {format_readable_number(denominator)} = "
                    f"{format_readable_number(numerator / denominator)}.\n"
                    f"சதவீதமாக: {format_readable_number(result)}%.")
        return (f"{format_readable_number(numerator)} ÷ {format_readable_number(denominator)} = "
                f"{format_readable_number(numerator / denominator)}. As a percentage: {format_readable_number(result)}%.")

    # X% of Y, including spoken variants and Tamil percentage words.
    percent_match = re.search(
        r"(\d+(?:\.\d+)?)\s*" + percent_words + r"\s*(?:of|இன்|இல்|க்கு)?\s*(\d+(?:\.\d+)?)",
        low,
        re.I,
    )
    if percent_match:
        percentage = float(percent_match.group(1))
        total = float(percent_match.group(2))
        result = percentage / 100 * total
        if is_ta:
            return (f"{format_readable_number(total)}-இல் {format_readable_number(percentage)}% = "
                    f"{format_readable_number(result)}.")
        return f"{format_readable_number(percentage)}% of {format_readable_number(total)} is {format_readable_number(result)}."

    # "40 out of 60 percentage" / "40 out of 60 is what percent?"
    out_of_match = re.search(r"(\d+(?:\.\d+)?)\s+out\s+of\s+(\d+(?:\.\d+)?)", low, re.I)
    if out_of_match and has_percent_question:
        part = float(out_of_match.group(1))
        total = float(out_of_match.group(2))
        if total == 0:
            return "பூஜ்ஜியத்தால் வகுக்க முடியாது." if is_ta else "The total cannot be zero."
        result = part / total * 100
        return (f"சதவீதம்: {format_readable_number(result)}%." if is_ta
                else f"That is {format_readable_number(result)}%.")

    # If they say a percentage but provide only one value, explain what is missing.
    if re.search(percent_words, low, re.I) and not percent_match and not ratio_match:
        if is_ta:
            return ("சதவீதத்தைக் கணக்கிட இன்னொரு மதிப்பும் தேவை.\n"
                    "உதாரணம்: 36 percent of 326500 அல்லது 226 by 60 எவ்வளவு percentage என்று சொல்லுங்கள்.")
        return ("I need a base/total to calculate a percentage.\n"
                "Try: '36 percent of 326500' or '226 by 60 as a percentage'.")

    # Map operators to symbols, longest phrases first.
    replacements = [
        (r"\b(?:divided\s+by|divide\s+by|divided|divide)\b", "/"),
        (r"\b(?:multiplied\s+by|multiply\s+by|times|multiply|multiplied)\b", "*"),
        (r"\b(?:plus|add|added\s+to|and)\b", "+"),
        (r"\b(?:minus|subtract|subtracting)\b", "-"),
        (r"\b(?:by)\b", "/"),
        (r"\b(?:over)\b", "/"),
        (r"\b(?:into)\b", "*"),
        (r"\b(?:modulo|mod)\b", "%"),
        (r"\b(?:raised\s+to|power\s+of)\b", "**"),
    ]
    for pattern, replacement in replacements:
        low = re.sub(pattern, f" {replacement} ", low, flags=re.I)

    tamil_operators = [
        ("கூட்டல்", "+"), ("கூட்டி", "+"), ("கூட்டினால்", "+"), ("கூட்டவும்", "+"),
        ("சேர்த்து", "+"), ("மற்றும்", "+"),
        ("கழித்தல்", "-"), ("கழித்து", "-"), ("கழிக்கவும்", "-"), ("கழி", "-"),
        ("பெருக்கல்", "*"), ("பெருக்கி", "*"), ("பெருக்கவும்", "*"),
        ("வகுத்தல்", "/"), ("வகுத்து", "/"), ("வகுக்கவும்", "/"),
    ]
    for word, symbol in tamil_operators:
        low = low.replace(word, f" {symbol} ")

    # Remove conversational filler without removing numbers or math signs.
    fillers = [
        "what is", "what's", "calculate", "calculator", "please", "can you",
        "tell me", "give me", "find", "answer", "how much is", "how much",
        "is equal to", "equal to", "equals", "the answer", "எவ்வளவு", "எத்தனை",
        "கணக்கிடு", "கணக்கு", "பதில்", "சொல்லு", "சொல்லுங்கள்", "கொடு", "கொடுங்கள்",
        "தயவுசெய்து", "எனக்கு", "இதன்", "மதிப்பு", "ஆகும்", "எவ்வளவு என்று",
    ]
    for phrase in sorted(fillers, key=len, reverse=True):
        low = low.replace(phrase, " ")

    # 
    expression = re.sub(r"[^0-9+\-*/().%\s]", " ", low)
    expression = re.sub(r"\s+", " ", expression).strip()
    # "add 50 50" becomes "50+50".
    expression = re.sub(r"(?<=\d)\s+(?=\d)", "+", expression)
    expression = re.sub(r"\s*([+\-*/%])\s*", r"\1", expression)

    if not expression or not re.search(r"\d", expression):
        return "அந்தக் கணக்கைப் புரிந்துகொள்ள முடியவில்லை. மீண்டும் சொல்லுங்கள்." if is_ta else "I could not understand that calculation. Please try again."
    if not re.fullmatch(r"[0-9+\-*/().%\s]+", expression):
        return "கணக்கைப் புரிந்துகொள்ள முடியவில்லை." if is_ta else "I could not understand the calculation."

    try:
        result = eval(expression, {"__builtins__": None}, {})
        if isinstance(result, (int, float)) and math.isfinite(result):
            answer = format_readable_number(result)
            return f"பதில்: {answer}." if is_ta else f"The answer is {answer}."
    except ZeroDivisionError:
        return "பூஜ்ஜியத்தால் வகுக்க முடியாது." if is_ta else "I cannot divide by zero."
    except Exception:
        pass

    if re.search(r"percent|percentage|சதவீதம்|சதவிகிதம்|பர்சன்ட்", original, re.I):
        return ("சதவீதம் கணக்கிட அடிப்படை மதிப்பு தேவை. உதாரணம்: 226K out of 300K." if is_ta
                else "A percentage needs a base value. For example: 226K out of 300K.")
    return "அந்தக் கணக்கைப் புரிந்துகொள்ள முடியவில்லை. மீண்டும் சொல்லுங்கள்." if is_ta else "Sorry, I could not understand that calculation."


# -------------------- Audio recording and recognition --------------------
def record_audio(duration=5, sample_rate=16000):
    recording = sd.rec(int(duration * sample_rate), samplerate=sample_rate,
                       channels=1, dtype="int16")
    sd.wait()
    wav_data = io.BytesIO()
    with wave.open(wav_data, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(recording.tobytes())
    wav_data.seek(0)
    return wav_data


def speech_to_text(duration=6):
    """Try Tamil and Indian English recognition using the same recording."""
    recognizer = sr.Recognizer()
    try:
        audio_file = record_audio(duration=duration)
        with sr.AudioFile(audio_file) as source:
            audio = recognizer.record(source)
    except Exception as exc:
        print("Microphone recording error:", exc)
        return "", "en", []

    candidates = []
    for language_code, short_language in (("ta-IN", "ta"), ("en-IN", "en")):
        try:
            recognized = recognizer.recognize_google(audio, language=language_code)
            if recognized:
                candidates.append((recognized.strip(), short_language))
                print(f"Recognized ({language_code}):", recognized)
        except sr.UnknownValueError:
            pass
        except sr.RequestError as exc:
            print("Speech service/network error:", exc)
        except Exception as exc:
            print("Speech recognition error:", exc)

    for text, lang in candidates:
        if contains_tamil(text):
            return text, "ta", candidates
    for text, lang in candidates:
        if lang == "en":
            return text, "en", candidates
    if candidates:
        text, lang = candidates[0]
        return text, lang, candidates
    return "", "en", []


# -------------------- Text-to-speech --------------------
def _find_voice(engine, language):
    try:
        voices = engine.getProperty("voices")
        wanted = ("tamil", "ta-in", "ta_in", "tamil india") if language == "ta" else ("english", "en-in", "en_in", "india")
        for voice in voices:
            details = (str(getattr(voice, "name", "")) + " " +
                       str(getattr(voice, "id", "")) + " " +
                       str(getattr(voice, "languages", ""))).lower()
            if any(item in details for item in wanted):
                return voice.id
    except Exception:
        pass
    return None


def speak_answer(text, language="en", wait=False):
    def worker():
        try:
            with tts_lock:
                engine = pyttsx3.init()
                voice_id = _find_voice(engine, language)
                if voice_id:
                    engine.setProperty("voice", voice_id)
                engine.setProperty("rate", 165)
                engine.setProperty("volume", 1.0)
                engine.say(str(text))
                engine.runAndWait()
                engine.stop()
        except Exception as exc:
            print("Text-to-speech error:", exc)
    if wait:
        worker()
    else:
        threading.Thread(target=worker, daemon=True).start()


# -------------------- Safe UI updates from voice threads --------------------
def ui_after(function):
    try:
        if root is not None and app_running:
            root.after(0, function)
    except Exception:
        pass


def set_status(text, color=GREEN):
    def update():
        if status_label is not None:
            status_label.config(text=text, fg=color)
    ui_after(update)


def pretty_display_number(value):
    """Format a result for the large calculator display, without changing math values."""
    try:
        number = float(str(value).replace(",", ""))
        if number.is_integer():
            return f"{int(number):,}"
        return f"{number:,.6f}".rstrip("0").rstrip(".")
    except (ValueError, TypeError, OverflowError):
        return str(value)


def show_result_in_main_display(answer):
    """Put the final numeric answer in the calculator's main display area."""
    global calculator_value, display_is_ai_result
    if display_var is None:
        return

    # The last number in a complete answer is normally the result. This also
    # handles multi-line salary/expense/savings replies by displaying savings.
    number_matches = list(re.finditer(r"(?<![A-Za-z])[-+]?\d[\d,]*(?:\.\d+)?", str(answer)))
    if not number_matches:
        return

    match = number_matches[-1]
    raw_number = match.group(0).replace(",", "")
    # Do not replace a useful display with a number embedded in an apology.
    lower_answer = str(answer).lower()
    if any(message in lower_answer for message in (
        "could not understand", "cannot understand", "முடியவில்லை", "புரிந்துகொள்ள முடியவில்லை"
    )):
        return

    try:
        numeric = float(raw_number)
        calculator_value = format_number(numeric)  # Keep the internal value comma-free.
        display_text = pretty_display_number(numeric)

        # Currency questions should show a currency sign on the main display.
        if "₹" in str(answer) or "சேமிப்பு" in str(answer) or "savings" in lower_answer:
            display_text = "₹" + display_text
        # If the final result itself is a percentage, display the percent sign too.
        tail = str(answer)[match.end():]
        if re.match(r"\s*%", tail):
            display_text += "%"
        display_var.set(display_text)
        display_is_ai_result = True
    except (ValueError, TypeError, OverflowError):
        pass


def set_answer(question, answer):
    def update():
        if ai_input is not None and question:
            ai_input.delete(0, tk.END)
            ai_input.insert(0, question)
        if ai_output is not None:
            ai_output.config(text=answer)
        show_result_in_main_display(answer)
    ui_after(update)


def show_calculator():
    def show():
        try:
            root.deiconify()
            root.lift()
            root.attributes("-topmost", True)
            root.after(1200, lambda: root.attributes("-topmost", False))
        except Exception:
            pass
    ui_after(show)


# -------------------- Wake word / voice assistant --------------------
def detect_wake_word(candidates):
    english_wake_words = ("hey calculator", "hi calculator", "hello calculator",
                          "hey calculate", "calculator", "calcolator", "calcu later")
    tamil_wake_words = (
        "ஹேய் கால்குலேட்டர்", "ஹே கால்குலேட்டர்", "கால்குலேட்டர்",
        "கல்குலேட்டர்", "கால்குலேட்டரே", "கணிப்பான்", "ஹே கால்குலேட்டர்"
    )
    for text, lang in candidates:
        normalized = re.sub(r"[^a-z0-9\u0B80-\u0BFF ]", " ", text.lower())
        normalized = re.sub(r"\s+", " ", normalized).strip()
        if any(word in normalized for word in tamil_wake_words):
            return True, "ta"
        if any(word in normalized for word in english_wake_words):
            return True, "ta" if contains_tamil(text) else "en"
    return False, "en"


def launch_assistant(language="en"):
    if voice_busy.is_set() or not app_running:
        return
    voice_busy.set()
    threading.Thread(target=ask_question_after_wake, args=(language,), daemon=True).start()


def ask_question_after_wake(prompt_language="en"):
    try:
        show_calculator()
        set_status("● கேட்கிறேன்..." if prompt_language == "ta" else "● Listening...", GREEN)
        def mark_main_display_listening():
            global calculator_value, display_is_ai_result
            if display_var is not None:
                calculator_value = ""
                display_is_ai_result = False
                display_var.set("கேட்கிறேன்..." if prompt_language == "ta" else "Listening...")
        ui_after(mark_main_display_listening)
        prompt = "என்ன கணக்கு வேண்டும்? சொல்லுங்கள்." if prompt_language == "ta" else "What calculation can I help you with?"
        speak_answer(prompt, prompt_language, wait=True)
        question, language, candidates = speech_to_text(duration=8)
        if not question:
            apology = ("உங்கள் குரலைப் புரிந்துகொள்ள முடியவில்லை. மீண்டும் முயற்சி செய்யுங்கள்."
                       if prompt_language == "ta" else "I could not understand your voice. Please try again.")
            set_answer("", apology)
            speak_answer("மன்னிக்கவும், மீண்டும் சொல்லுங்கள்." if prompt_language == "ta"
                         else "Sorry, please say that again.", prompt_language, wait=True)
        else:
            answer = solve_ai_question(question, language)
            set_answer(question, answer)
            speak_answer(answer, language, wait=True)
        set_status("● Background listening ON | 'Hey Calculator' / 'கால்குலேட்டர்' சொல்லுங்கள்", GREEN)
    except Exception as exc:
        print("Assistant error:", exc)
        set_status("Voice error - microphone / internet check செய்யவும்", RED)
        try:
            speak_answer("மைக்ரோஃபோன் அல்லது இணைய இணைப்பைச் சரிபார்க்கவும்." if prompt_language == "ta"
                         else "Please check the microphone and internet connection.", prompt_language, wait=True)
        except Exception:
            pass
    finally:
        voice_busy.clear()


def manual_voice():
    launch_assistant("ta")


def wake_word_listener():
    print("BAVITECHSPARK AI CALCULATOR background listener started.")
    print("Say 'Hey Calculator' or 'கால்குலேட்டர்'. Internet is required for Google speech recognition.")
    while app_running:
        try:
            if voice_busy.is_set():
                time.sleep(0.2)
                continue
            _, _, candidates = speech_to_text(duration=2.6)
            if candidates:
                print("Background heard:", [text for text, _ in candidates])
            detected, language = detect_wake_word(candidates)
            if detected:
                print("Wake word detected.")
                launch_assistant(language)
                time.sleep(1.0)
            else:
                time.sleep(0.1)
        except Exception as exc:
            print("Background listener error:", exc)
            set_status("Background voice error; mic / internet check செய்யவும்", RED)
            time.sleep(2)


# -------------------- Calculator --------------------
def update_display(value):
    global calculator_value, display_is_ai_result
    value = str(value)
    if display_is_ai_result:
        # Continue from an answer when an operator is pressed; start a fresh
        # expression when a digit or opening bracket is pressed.
        if value not in ("+", "-", "×", "÷", "%", ")"):
            calculator_value = ""
        display_is_ai_result = False
    calculator_value += value
    display_var.set(calculator_value)


def clear_display():
    global calculator_value, display_is_ai_result
    calculator_value = ""
    display_is_ai_result = False
    display_var.set("")


def delete_last():
    global calculator_value, display_is_ai_result
    display_is_ai_result = False
    calculator_value = calculator_value[:-1]
    display_var.set(calculator_value)


def calculate_result():
    global calculator_value, display_is_ai_result
    expression = calculator_value.replace("×", "*").replace("÷", "/")
    expression = re.sub(r"(\d+(?:\.\d+)?)%", r"(\1/100)", expression)
    try:
        if not re.fullmatch(r"[0-9+\-*/().\s]+", expression):
            raise ValueError("Invalid expression")
        result = eval(expression, {"__builtins__": None}, {})
        calculator_value = format_number(result)
        display_var.set(calculator_value)
        display_is_ai_result = True
    except Exception:
        display_var.set("Error")
        calculator_value = ""
        display_is_ai_result = False


def solve_ai_text():
    question = ai_input.get().strip()
    if not question:
        ai_output.config(text="ஒரு கேள்வியை type செய்யுங்கள் / Please type a question.")
        return
    language = "ta" if contains_tamil(question) else "en"
    answer = solve_ai_question(question, language)
    ai_output.config(text=answer)
    show_result_in_main_display(answer)
    speak_answer(answer, language)


def press_in(button):
    button.config(bg=BUTTON_ACTIVE)


def release_out(button):
    button.config(bg=BUTTON)


def create_button(parent, text, row, column, action, colspan=1):
    button = tk.Button(parent, text=text, command=action, bg=BUTTON, fg=WHITE,
                       activebackground=BUTTON_ACTIVE, activeforeground=WHITE,
                       font=("Arial", 15, "bold"), bd=0, relief="flat", cursor="hand2")
    button.grid(row=row, column=column, columnspan=colspan, padx=4, pady=4, sticky="nsew")
    button.bind("<ButtonPress-1>", lambda event, b=button: press_in(b))
    button.bind("<ButtonRelease-1>", lambda event, b=button: release_out(b))
    return button


def close_app():
    global app_running
    app_running = False
    try:
        root.destroy()
    except Exception:
        pass


# -------------------- Build window --------------------
def main():
    global root, status_label, ai_input, ai_output, voice_button
    global display_var, calculator_value

    root = tk.Tk()
    root.title(APP_NAME)
    root.geometry("430x850")
    root.configure(bg=BG)
    root.resizable(False, False)
    root.protocol("WM_DELETE_WINDOW", close_app)

    tk.Label(root, text="BAVITECHSPARK", bg=BG, fg=PURPLE,
             font=("Arial", 20, "bold")).pack(pady=(12, 0))
    tk.Label(root, text="AI CALCULATOR", bg=BG, fg=WHITE,
             font=("Arial", 12, "bold")).pack(pady=(0, 5))

    status_label = tk.Label(root,
        text="● Background listening ON | 'Hey Calculator' / 'கால்குலேட்டர்'",
        bg=BG, fg=GREEN, font=("Arial", 9, "bold"), wraplength=400)
    status_label.pack(pady=(0, 8))

    display_var = tk.StringVar()
    display = tk.Entry(root, textvariable=display_var, font=("Arial", 25, "bold"),
                       bg=DISPLAY_BG, fg=WHITE, insertbackground=WHITE,
                       justify="right", bd=0)
    display.pack(padx=15, pady=8, ipady=12, fill="x")

    calc_frame = tk.Frame(root, bg=BG)
    calc_frame.pack(padx=12, pady=5, fill="x")
    for col in range(4):
        calc_frame.grid_columnconfigure(col, weight=1)

    keypad = [
        ("C", 0, 0, clear_display), ("⌫", 0, 1, delete_last),
        ("(", 0, 2, lambda: update_display("(")), (")", 0, 3, lambda: update_display(")")),
        ("7", 1, 0, lambda: update_display("7")), ("8", 1, 1, lambda: update_display("8")),
        ("9", 1, 2, lambda: update_display("9")), ("÷", 1, 3, lambda: update_display("÷")),
        ("4", 2, 0, lambda: update_display("4")), ("5", 2, 1, lambda: update_display("5")),
        ("6", 2, 2, lambda: update_display("6")), ("×", 2, 3, lambda: update_display("×")),
        ("1", 3, 0, lambda: update_display("1")), ("2", 3, 1, lambda: update_display("2")),
        ("3", 3, 2, lambda: update_display("3")), ("-", 3, 3, lambda: update_display("-")),
        ("0", 4, 0, lambda: update_display("0")), (".", 4, 1, lambda: update_display(".")),
        ("%", 4, 2, lambda: update_display("%")), ("+", 4, 3, lambda: update_display("+")),
    ]
    for label, row, col, action in keypad:
        create_button(calc_frame, label, row, col, action)
    create_button(calc_frame, "=", 5, 0, calculate_result, colspan=4)

    tk.Label(root, text="🤖 BAVI AI", bg=BG, fg=PURPLE,
             font=("Arial", 16, "bold")).pack(pady=(10, 3))
    tk.Label(root, text="தமிழிலும் English-லும் இயல்பாகப் பேசுங்கள்",
             bg=BG, fg=GRAY, font=("Arial", 9)).pack(pady=(0, 5))

    ai_input = tk.Entry(root, font=("Arial", 13), bg=DISPLAY_BG, fg=WHITE,
                        insertbackground=WHITE, bd=0)
    ai_input.pack(padx=15, pady=5, ipady=8, fill="x")

    ai_button_frame = tk.Frame(root, bg=BG)
    ai_button_frame.pack(pady=5)
    tk.Button(ai_button_frame, text="SOLVE", command=solve_ai_text,
              bg=PURPLE, fg=WHITE, activebackground=BUTTON_ACTIVE,
              activeforeground=WHITE, font=("Arial", 11, "bold"), bd=0,
              padx=20, pady=8, cursor="hand2").grid(row=0, column=0, padx=5)
    voice_button = tk.Button(ai_button_frame, text="🎤 VOICE", command=manual_voice,
              bg=GREEN, fg=WHITE, activebackground="#16A34A", activeforeground=WHITE,
              font=("Arial", 11, "bold"), bd=0, padx=20, pady=8, cursor="hand2")
    voice_button.grid(row=0, column=1, padx=5)

    ai_output = tk.Label(root,
        text="உங்கள் பதில் இங்கே வரும் / Your answer will appear here.",
        bg=DISPLAY_BG, fg=WHITE, font=("Arial", 12, "bold"),
        wraplength=380, justify="center", padx=10, pady=13)
    ai_output.pack(padx=15, pady=8, fill="x")

    tk.Label(root, text='Say "Hey Calculator" or "கால்குலேட்டர்"',
             bg=BG, fg=GRAY, font=("Arial", 9, "italic")).pack(pady=(2, 8))
    root.bind("<Return>", lambda event: solve_ai_text())

    threading.Thread(target=wake_word_listener, daemon=True).start()
    root.mainloop()


if __name__ == "__main__":
    main()

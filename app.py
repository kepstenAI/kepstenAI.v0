import os
import re
import json
import sqlite3
from datetime import date, timedelta
from typing import List, Tuple, Optional, Dict

from flask import Flask, request, jsonify, render_template
from twilio.rest import Client
import urllib.parse
import requests
from bs4 import BeautifulSoup

# Try to import your AI/TTS clients; fallback to simple stubs if not available
try:
    from ai.mistral_client import get_mistral_response
except Exception:
    def get_mistral_response(prompt: str) -> str:
        # Simple fallback: echo with friendly wrapper
        return f"Ava: {prompt[:300]}"

try:
    from ai.elevenlabs_client import generate_voice
except Exception:
    def generate_voice(text: str) -> Optional[str]:
        # fallback: return None to indicate use <Say> rather than <Play>
        # or return a pseudo-URL if you prefer <Play>.
        return None


# ----------------------------- Config --------------------------------
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "your_sid")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "your_token")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER", "your_twilio_number")

# Public base for Twilio webhook callbacks (set to your ngrok/render domain)
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://kepstenai-v0.onrender.com").rstrip("/")
NGROK_DOMAIN = PUBLIC_BASE_URL  # kept for compatibility with older variable names

DB_PATH = os.getenv("DB_PATH", "bookings.db")

# Pages to scrape for services & FAQs (can be adjusted)
HOUSE_CLEANING_LANDING = "https://kepsten.com/house-cleaning/"
DEEP_CLEANING_CATEGORY = "https://kepsten.com/product-category/cleaning/house-cleaning/deep-cleaning/"
FAQ_URLS = ["https://kepsten.com/faqs/", "https://kepsten.com/faq/"]

HEADERS = {"User-Agent": "AvaBot/1.0 (+https://kepsten.com)"}


# ----------------------------- DB Schema ------------------------------
SCHEMA = {
    "confirmed_requests": (
        """
        CREATE TABLE IF NOT EXISTS confirmed_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            email TEXT,
            phone TEXT,
            city TEXT,
            address TEXT,
            service TEXT,
            bedrooms INTEGER,
            message TEXT,
            confirmation TEXT,
            booking_time TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    ),
    "services": (
        """
        CREATE TABLE IF NOT EXISTS services (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE,
            description TEXT,
            price TEXT,
            category TEXT,
            meta JSON,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    ),
    "faqs": (
        """
        CREATE TABLE IF NOT EXISTS faqs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            question TEXT UNIQUE,
            answer TEXT,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    ),
    "availability_slots": (
        """
        CREATE TABLE IF NOT EXISTS availability_slots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            day TEXT,
            slot TEXT,
            is_available INTEGER DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(day, slot)
        )
        """
    ),
    "interactions": (
        """
        CREATE TABLE IF NOT EXISTS interactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT,
            intent TEXT,
            transcript TEXT,
            ai_response TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
}


def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    for ddl in SCHEMA.values():
        cur.execute(ddl)
    conn.commit()
    conn.close()


init_db()


# ----------------------------- Scraper --------------------------------
def safe_get(url: str) -> Optional[str]:
    try:
        r = requests.get(url, headers=HEADERS, timeout=12)
        if r.status_code == 200:
            return r.text
    except Exception:
        pass
    return None


def parse_and_store_services():
    """Lightweight scraper for kepsten house-cleaning section & deep-cleaning category.
       Stores results in services table. This is best-effort and uses simple selectors."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # We'll upsert by name
    def upsert(name: str, desc: str, price: str, category: str = None, meta: dict = None):
        meta_json = json.dumps(meta or {})
        cur.execute("""
            INSERT INTO services(name, description, price, category, meta, updated_at)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(name) DO UPDATE SET
              description=excluded.description,
              price=excluded.price,
              category=COALESCE(excluded.category, services.category),
              meta=COALESCE(excluded.meta, services.meta),
              updated_at=CURRENT_TIMESTAMP
        """, (name.strip(), desc.strip(), price.strip(), category, meta_json))

    # Scrape landing for categories / links
    html = safe_get(HOUSE_CLEANING_LANDING)
    links = set()
    if html:
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.select("a[href]"):
            href = a["href"].strip()
            if "/product-category/cleaning/house-cleaning/" in href:
                links.add(href if href.startswith("http") else urllib.parse.urljoin(HOUSE_CLEANING_LANDING, href))

    # Visit each category link and extract products
    for link in links:
        page_html = safe_get(link)
        if not page_html:
            continue
        psoup = BeautifulSoup(page_html, "html.parser")
        # Find product elements (WooCommerce-style)
        products = psoup.select("li.product, .product")
        for prod in products:
            # title
            title_el = prod.select_one(".woocommerce-loop-product__title, h2, .product_title")
            title = title_el.get_text(strip=True) if title_el else None
            # price
            price_el = prod.select_one("ins .amount, .price .amount, .price, .amount")
            price = price_el.get_text(" ", strip=True) if price_el else ""
            # description fallback
            desc = prod.get_text(" ", strip=True)
            if title:
                upsert(title, desc, price, category="House Cleaning", meta={"source": link})

    # Deep cleaning category (explicit link provided by user)
    deep_html = safe_get(DEEP_CLEANING_CATEGORY)
    if deep_html:
        dsoup = BeautifulSoup(deep_html, "html.parser")
        for prod in dsoup.select("li.product, .product"):
            title_el = prod.select_one(".woocommerce-loop-product__title, h2, a")
            title = title_el.get_text(strip=True) if title_el else None
            price_el = prod.select_one("ins .amount, .price .amount, .price, .amount")
            price = price_el.get_text(" ", strip=True) if price_el else ""
            desc = prod.get_text(" ", strip=True)
            if title:
                upsert(title, desc, price, category="Deep Cleaning", meta={"source": DEEP_CLEANING_CATEGORY})

    # FAQs: store as "service" rows or separate table
    for faq_url in FAQ_URLS:
        fhtml = safe_get(faq_url)
        if not fhtml:
            continue
        fsoup = BeautifulSoup(fhtml, "html.parser")
        # look for common toggles / accordions
        # elementor / et_pb etc.
        toggles = fsoup.select(".et_pb_toggle, .faq, .faq-item, details, .elementor-accordion-item")
        for t in toggles:
            # try common structures
            q_el = t.select_one(".et_pb_toggle_title, summary, h3, h4, .question, .faq-question")
            a_el = t.select_one(".et_pb_toggle_content, .answer, p, .elementor-tab-content, .faq-answer")
            q = q_el.get_text(" ", strip=True) if q_el else None
            a = a_el.get_text(" ", strip=True) if a_el else None
            if q and a:
                cur.execute("""
                    INSERT INTO faqs(question, answer, updated_at)
                    VALUES (?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(question) DO UPDATE SET
                      answer=excluded.answer,
                      updated_at=CURRENT_TIMESTAMP
                """, (q, a))

    conn.commit()
    conn.close()


# Run scraper at startup (best-effort). Comment if you want manual reindex only.
try:
    parse_and_store_services()
except Exception:
    pass


# ----------------------------- Helpers ---------------------------------
def save_request_to_db(data: dict, confirmation: Optional[str] = None, booking_time: Optional[str] = None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT INTO confirmed_requests (name, email, phone, city, address, service, bedrooms, message, confirmation, booking_time)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        data.get("name"),
        data.get("email"),
        data.get("phone"),
        data.get("city"),
        data.get("address"),
        data.get("service"),
        data.get("bedrooms"),
        data.get("message"),
        confirmation,
        booking_time
    ))
    conn.commit()
    conn.close()


def update_booking_time(phone: str, booking_time: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE confirmed_requests SET booking_time = ? WHERE phone = ?", (booking_time, phone))
    conn.commit()
    conn.close()


def record_interaction(phone: str, intent: str, transcript: str, ai_response: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("INSERT INTO interactions(phone, intent, transcript, ai_response) VALUES (?,?,?,?)",
                (phone, intent, transcript, ai_response))
    conn.commit()
    conn.close()


def search_knowledge_base(query: str, limit: int = 6) -> List[Tuple[str, str, str]]:
    q = f"%{query}%"
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT name, description, price FROM services WHERE name LIKE ? OR description LIKE ? LIMIT ?", (q, q, limit))
    rows = cur.fetchall()
    conn.close()
    return rows


# ------------------------- Flask + Twilio Setup -------------------------
app = Flask(__name__)
app.secret_key = os.getenv("APP_SECRET_KEY", "app_secret_will_be_here")
twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# ephemeral call state (in-memory)
call_state: Dict[str, Dict] = {}


# ---------------------- TwiML helpers -----------------------
def twiml_play_or_say(text_or_url: Optional[str], gather_action: str):
    """If generator returned an http URL, use <Play>, otherwise use <Say> with text."""
    if text_or_url and isinstance(text_or_url, str) and text_or_url.lower().startswith("http"):
        return f"""
        <Response>
            <Play>{text_or_url}</Play>
            <Gather input="speech" action="{gather_action}" method="POST" timeout="6" speechTimeout="auto"/>
        </Response>
        """, 200, {'Content-Type': 'application/xml'}
    else:
        # If generate_voice returned None, treat the original text (passed in gather_action state) as say text.
        # In our code below we pass the actual text into generate_voice; if it returns None we will use the text.
        say_text = text_or_url if text_or_url and not text_or_url.startswith("http") else ""
        return f"""
        <Response>
            <Say>{say_text}</Say>
            <Gather input="speech" action="{gather_action}" method="POST" timeout="6" speechTimeout="auto"/>
        </Response>
        """, 200, {'Content-Type': 'application/xml'}


def respond_play_or_say(text: str, next_action: str):
    """Wrap: try to produce audio_url via generate_voice(); fallback to Say."""
    audio_url = None
    try:
        audio_url = generate_voice(text)
    except Exception:
        audio_url = None
    if audio_url:
        return f"""
        <Response>
            <Play>{audio_url}</Play>
            <Gather bargeIn="true" input="speech" action="{next_action}" method="POST" timeout="6" speechTimeout="auto"/>
        </Response>
        """, 200, {'Content-Type': 'application/xml'}
    else:
        # Use Say with the text
        return f"""
        <Response>
            <Say>{text}</Say>
            <Gather bargeIn="true" input="speech" action="{next_action}" method="POST" timeout="6" speechTimeout="auto"/>
        </Response>
        """, 200, {'Content-Type': 'application/xml'}


# ------------------------- Routes: Web UI -------------------------
@app.route('/')
def index():
    return "Ava (Kepsten) Frontdesk is running."


@app.route('/view-bookings')
def view_bookings():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT id, name, email, phone, city, address, service, bedrooms, message, confirmation, booking_time, created_at FROM confirmed_requests ORDER BY created_at DESC")
    rows = cursor.fetchall()
    conn.close()
    headers = ["ID", "Name", "Email", "Phone", "City", "Address", "Service", "Bedrooms", "Message", "Confirmation", "Booking Time", "Created At"]
    return render_template("view_bookings.html", data=rows, headers=headers)


# ------------------------- Admin endpoints -------------------------
@app.route('/admin/reindex', methods=['POST'])
def admin_reindex():
    try:
        parse_and_store_services()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route('/admin/slots', methods=['GET', 'POST', 'DELETE'])
def admin_slots():
    if request.method == 'GET':
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("SELECT day, slot, is_available FROM availability_slots ORDER BY day")
        rows = cur.fetchall()
        conn.close()
        return jsonify({"slots": rows})
    payload = request.get_json(force=True)
    day = payload.get('day')
    slot = payload.get('slot')
    if not day or not slot:
        return jsonify({"error": "day and slot required"}), 400
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    if request.method == 'POST':
        cur.execute("INSERT INTO availability_slots(day, slot, is_available) VALUES (?,?,1) ON CONFLICT(day, slot) DO UPDATE SET is_available=1", (day, slot))
    else:
        cur.execute("UPDATE availability_slots SET is_available=0 WHERE day=? AND slot=?", (day, slot))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# ------------------------- Twilio: Outbound trigger -------------------------
@app.route('/trigger_call', methods=['POST'])
def trigger_call():
    payload = request.get_json(force=True)
    name = payload.get("name")
    phone = payload.get("phone")
    service = payload.get("service")
    message = payload.get("message")
    email = payload.get("email")
    city = payload.get("city")
    address = payload.get("address")

    if not all([name, phone, service, message]):
        return jsonify({"error": "Missing fields"}), 400

    # store initial context
    call_state[phone] = {
        "name": name,
        "email": email,
        "phone": phone,
        "service": service,
        "message": message,
        "city": city,
        "address": address,
        "phase": "intro"
    }

    try:
        call = twilio_client.calls.create(
            to=phone,
            from_=TWILIO_PHONE_NUMBER,
            url=f"{PUBLIC_BASE_URL}/voice?phone={urllib.parse.quote_plus(phone)}"
        )
        return jsonify({"ok": True, "sid": call.sid})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ------------------------- Twilio: Outbound call initial TwiML -------------------------
@app.route('/voice', methods=['GET', 'POST'])
def voice():
    phone = request.args.get("phone")
    state = call_state.get(phone, {}) or {}
    name = state.get("name") or "there"
    service = state.get("service") or "cleaning"

    prompt = f"Hi {name}, this is Ava from Kepsten. We received your request for {service}. Is now a good time to confirm details?"
    audio_url = None
    try:
        audio_url = generate_voice(prompt)
    except Exception:
        audio_url = None

    # Use Play if audio_url, else Say
    if audio_url:
        return f"""
        <Response>
            <Play>{audio_url}</Play>
            <Gather input="speech" action="{PUBLIC_BASE_URL}/gather?phone={urllib.parse.quote_plus(phone)}" method="POST" timeout="6" speechTimeout="auto"/>
        </Response>
        """, 200, {'Content-Type': 'application/xml'}
    else:
        return f"""
        <Response>
            <Say>{prompt}</Say>
            <Gather input="speech" action="{PUBLIC_BASE_URL}/gather?phone={urllib.parse.quote_plus(phone)}" method="POST" timeout="6" speechTimeout="auto"/>
        </Response>
        """, 200, {'Content-Type': 'application/xml'}


# ------------------------- Twilio: Incoming call -------------------------
@app.route('/incoming_call', methods=['POST'])
def incoming_call():
    caller = request.values.get('From') or "unknown"
    # initialize state
    call_state.setdefault(caller, {"phase": "intro"})
    prompt = "Hi, I'm Ava from Kepsten. How can I help you today?"
    audio_url = None
    try:
        audio_url = generate_voice(prompt)
    except Exception:
        audio_url = None

    if audio_url:
        return f"""
        <Response>
            <Play>{audio_url}</Play>
            <Gather input="speech" action="{PUBLIC_BASE_URL}/gather?phone={urllib.parse.quote_plus(caller)}" method="POST" timeout="6" speechTimeout="auto"/>
        </Response>
        """, 200, {'Content-Type': 'application/xml'}
    else:
        return f"""
        <Response>
            <Say>{prompt}</Say>
            <Gather input="speech" action="{PUBLIC_BASE_URL}/gather?phone={urllib.parse.quote_plus(caller)}" method="POST" timeout="6" speechTimeout="auto"/>
        </Response>
        """, 200, {'Content-Type': 'application/xml'}


# ------------------------- Conversation: gather + routing -------------------------
EMAIL_REGEX = r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"


def detect_simple_intent(text: str) -> str:
    t = (text or "").lower()
    if any(x in t for x in ["book", "schedule", "i want", "i'd like", "please book", "yes", "confirm", "sounds good"]):
        return "book"
    if re.search(EMAIL_REGEX, t) or "email" in t:
        return "email"
    if any(k in t for k in ["address", "street", "avenue", "road", "apt", "suite", "city", "postal", "code"]):
        return "location"
    if any(k in t for k in ["today", "tomorrow", "am", "pm", "morning", "evening"]):
        return "availability"
    if any(k in t for k in ["deep cleaning", "standard cleaning", "move", "post construction", "hourly"]):
        return "service_choice"
    return "question"


@app.route('/gather', methods=['POST'])
def gather():
    user_input = (request.form.get('SpeechResult') or "").strip()
    phone = request.args.get("phone") or request.values.get('From') or request.values.get('Caller') or "unknown"
    state = call_state.setdefault(phone, {})

    if not user_input:
        fallback = "Sorry, I didn't catch that. Could you please repeat?"
        audio_url = generate_voice(fallback) if callable(generate_voice) else None
        if audio_url:
            return f"<Response><Play>{audio_url}</Play><Gather input='speech' action='{PUBLIC_BASE_URL}/gather?phone={urllib.parse.quote_plus(phone)}' method='POST' timeout='6' speechTimeout='auto'/></Response>", 200, {'Content-Type': 'application/xml'}
        else:
            return f"<Response><Say>{fallback}</Say><Gather input='speech' action='{PUBLIC_BASE_URL}/gather?phone={urllib.parse.quote_plus(phone)}' method='POST' timeout='6' speechTimeout='auto'/></Response>", 200, {'Content-Type': 'application/xml'}

    # record interaction
    record_interaction(phone, "user_speech", user_input, "")

    # Quick KB lookup
    kb = search_knowledge_base(user_input)
    if kb:
        # Summarize first few results (short)
        parts = []
        for name, desc, price in kb[:3]:
            p = name
            if price:
                p += f" — {price}"
            parts.append(p)
        reply = "I found: " + "; ".join(parts) + ". Would you like to book one of these?"
        audio_url = generate_voice(reply) if callable(generate_voice) else None
        if audio_url:
            return f"<Response><Play>{audio_url}</Play><Gather input='speech' action='{PUBLIC_BASE_URL}/gather?phone={urllib.parse.quote_plus(phone)}' method='POST' timeout='6' speechTimeout='auto'/></Response>", 200, {'Content-Type': 'application/xml'}
        else:
            return f"<Response><Say>{reply}</Say><Gather input='speech' action='{PUBLIC_BASE_URL}/gather?phone={urllib.parse.quote_plus(phone)}' method='POST' timeout='6' speechTimeout='auto'/></Response>", 200, {'Content-Type': 'application/xml'}

    # No KB result — route via simple intent/dialog flow
    intent = detect_simple_intent(user_input)

    # If we are mid-booking, follow the booking sub-flow
    phase = state.get("phase")

    # If not started booking and user asked to book, begin
    if intent == "book" and phase not in ("collecting", "confirming"):
        state["phase"] = "collecting"
        # ask for service choice if not present
        if not state.get("service"):
            prompt = "Sure — which service would you like? We offer Standard Cleaning, Deep Cleaning, Move In/Move Out, Post Construction, and Hourly Packages."
        else:
            prompt = "Great. Can I get the full name for the booking?"
        audio_url = generate_voice(prompt) if callable(generate_voice) else None
        if audio_url:
            return f"<Response><Play>{audio_url}</Play><Gather input='speech' action='{PUBLIC_BASE_URL}/gather?phone={urllib.parse.quote_plus(phone)}' method='POST' timeout='6' speechTimeout='auto'/></Response>", 200, {'Content-Type': 'application/xml'}
        else:
            return f"<Response><Say>{prompt}</Say><Gather input='speech' action='{PUBLIC_BASE_URL}/gather?phone={urllib.parse.quote_plus(phone)}' method='POST' timeout='6' speechTimeout='auto'/></Response>", 200, {'Content-Type': 'application/xml'}

    # If collecting details: capture name, email, city, address, then availability
    if phase == "collecting":
        # Capture name if not present
        if not state.get("name"):
            state["name"] = user_input
            reply = "Thanks. What's the best email address for confirmation?"
            state["phase"] = "collecting"
            audio_url = generate_voice(reply) if callable(generate_voice) else None
            if audio_url:
                return f"<Response><Play>{audio_url}</Play><Gather input='speech' action='{PUBLIC_BASE_URL}/gather?phone={urllib.parse.quote_plus(phone)}' method='POST' timeout='6' speechTimeout='auto'/></Response>", 200, {'Content-Type': 'application/xml'}
            else:
                return f"<Response><Say>{reply}</Say><Gather input='speech' action='{PUBLIC_BASE_URL}/gather?phone={urllib.parse.quote_plus(phone)}' method='POST' timeout='6' speechTimeout='auto'/></Response>", 200, {'Content-Type': 'application/xml'}

        # Capture email
        if not state.get("email"):
            m = re.search(EMAIL_REGEX, user_input)
            if m:
                state["email"] = m.group(0)
            else:
                # user might have spelled it — accept raw and confirm later
                state["email"] = user_input
            reply = "Thanks. Which city are you in?"
            audio_url = generate_voice(reply) if callable(generate_voice) else None
            return (f"<Response><Play>{audio_url}</Play><Gather input='speech' action='{PUBLIC_BASE_URL}/gather?phone={urllib.parse.quote_plus(phone)}' method='POST' timeout='6' speechTimeout='auto'/></Response>", 200, {'Content-Type': 'application/xml'}) if audio_url else (f"<Response><Say>{reply}</Say><Gather input='speech' action='{PUBLIC_BASE_URL}/gather?phone={urllib.parse.quote_plus(phone)}' method='POST' timeout='6' speechTimeout='auto'/></Response>", 200, {'Content-Type': 'application/xml'})

        # Capture city
        if not state.get("city"):
            state["city"] = user_input
            reply = "Great — can you provide the street address (or nearest intersection)?"
            audio_url = generate_voice(reply) if callable(generate_voice) else None
            return (f"<Response><Play>{audio_url}</Play><Gather input='speech' action='{PUBLIC_BASE_URL}/gather?phone={urllib.parse.quote_plus(phone)}' method='POST' timeout='6' speechTimeout='auto'/></Response>", 200, {'Content-Type': 'application/xml'}) if audio_url else (f"<Response><Say>{reply}</Say><Gather input='speech' action='{PUBLIC_BASE_URL}/gather?phone={urllib.parse.quote_plus(phone)}' method='POST' timeout='6' speechTimeout='auto'/></Response>", 200, {'Content-Type': 'application/xml'})

        # Capture address
        if not state.get("address"):
            state["address"] = user_input
            # ask bedrooms if service is deep cleaning or not specified
            svc = (state.get("service") or "").lower()
            if "deep" in svc or "deep" in (user_input.lower() or ""):
                state["phase"] = "collecting"
                reply = "How many bedrooms should we plan for? 1, 2, 3, 4 or 5?"
            else:
                reply = "Thanks — would you like a slot for today or tomorrow, AM or PM?"
                state["phase"] = "availability"
            audio_url = generate_voice(reply) if callable(generate_voice) else None
            return (f"<Response><Play>{audio_url}</Play><Gather input='speech' action='{PUBLIC_BASE_URL}/gather?phone={urllib.parse.quote_plus(phone)}' method='POST' timeout='6' speechTimeout='auto'/></Response>", 200, {'Content-Type': 'application/xml'}) if audio_url else (f"<Response><Say>{reply}</Say><Gather input='speech' action='{PUBLIC_BASE_URL}/gather?phone={urllib.parse.quote_plus(phone)}' method='POST' timeout='6' speechTimeout='auto'/></Response>", 200, {'Content-Type': 'application/xml'})

        # Capture bedrooms (if needed)
        if "bedrooms" not in state and ("deep" in (state.get("service") or "").lower() or re.search(r"\b(1|2|3|4|5)\b", user_input)):
            m = re.search(r"\b(1|2|3|4|5)\b", user_input)
            if m:
                state["bedrooms"] = int(m.group(1))
                state["phase"] = "availability"
                reply = "Thanks — would you like a slot for today or tomorrow, AM or PM?"
                audio_url = generate_voice(reply) if callable(generate_voice) else None
                return (f"<Response><Play>{audio_url}</Play><Gather input='speech' action='{PUBLIC_BASE_URL}/gather?phone={urllib.parse.quote_plus(phone)}' method='POST' timeout='6' speechTimeout='auto'/></Response>", 200, {'Content-Type': 'application/xml'}) if audio_url else (f"<Response><Say>{reply}</Say><Gather input='speech' action='{PUBLIC_BASE_URL}/gather?phone={urllib.parse.quote_plus(phone)}' method='POST' timeout='6' speechTimeout='auto'/></Response>", 200, {'Content-Type': 'application/xml'})

    # Availability phase: parse today/tomorrow AM/PM or explicit date slot
    if state.get("phase") in ("availability",):
        t = user_input.lower()
        chosen_day = None
        if "today" in t:
            chosen_day = date.today().isoformat()
        elif "tomorrow" in t:
            chosen_day = (date.today() + timedelta(days=1)).isoformat()
        else:
            m = re.search(r"(20\d{2}-\d{2}-\d{2})", t)
            if m:
                chosen_day = m.group(1)
        slot = None
        if "am" in t or "morning" in t:
            slot = "AM"
        elif "pm" in t or "evening" in t or "afternoon" in t:
            slot = "PM"
        if not chosen_day or not slot:
            # ask clarifying
            reply = "Would you prefer AM or PM, and is that for today or tomorrow?"
            audio_url = generate_voice(reply) if callable(generate_voice) else None
            return (f"<Response><Play>{audio_url}</Play><Gather input='speech' action='{PUBLIC_BASE_URL}/gather?phone={urllib.parse.quote_plus(phone)}' method='POST' timeout='6' speechTimeout='auto'/></Response>", 200, {'Content-Type': 'application/xml'}) if audio_url else (f"<Response><Say>{reply}</Say><Gather input='speech' action='{PUBLIC_BASE_URL}/gather?phone={urllib.parse.quote_plus(phone)}' method='POST' timeout='6' speechTimeout='auto'/></Response>", 200, {'Content-Type': 'application/xml'})

        # All details gathered: save booking
        booking_data = {
            "name": state.get("name"),
            "email": state.get("email"),
            "phone": phone,
            "city": state.get("city"),
            "address": state.get("address"),
            "service": state.get("service"),
            "bedrooms": state.get("bedrooms"),
            "message": state.get("message")
        }
        save_request_to_db(booking_data, confirmation="yes", booking_time=f"{chosen_day} {slot}")
        # mark slot taken
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("INSERT INTO availability_slots(day, slot, is_available) VALUES (?, ?, 0) ON CONFLICT(day, slot) DO UPDATE SET is_available=0", (chosen_day, slot))
        conn.commit()
        conn.close()

        state["phase"] = "done"
        resp_text = f"Booked {state.get('service', 'your service')} for {chosen_day} {slot}. We'll email confirmation to {state.get('email')}. Anything else I can help with?"
        audio_url = generate_voice(resp_text) if callable(generate_voice) else None
        if audio_url:
            return f"<Response><Play>{audio_url}</Play><Gather input='speech' action='{PUBLIC_BASE_URL}/gather?phone={urllib.parse.quote_plus(phone)}' method='POST' timeout='6' speechTimeout='auto'/></Response>", 200, {'Content-Type': 'application/xml'}
        else:
            return f"<Response><Say>{resp_text}</Say><Gather input='speech' action='{PUBLIC_BASE_URL}/gather?phone={urllib.parse.quote_plus(phone)}' method='POST' timeout='6' speechTimeout='auto'/></Response>", 200, {'Content-Type': 'application/xml'}

    # Default: use LLM to answer freeform question
    prompt = f"You are Ava from Kepsten. The user said: '{user_input}'. Answer briefly and naturally; include helpful service info if known."
    ai_reply = get_mistral_response(prompt)
    audio_url = generate_voice(ai_reply) if callable(generate_voice) else None
    record_interaction(phone, "ai_reply", user_input, ai_reply)
    if audio_url:
        return f"<Response><Play>{audio_url}</Play><Gather input='speech' action='{PUBLIC_BASE_URL}/gather?phone={urllib.parse.quote_plus(phone)}' method='POST' timeout='6' speechTimeout='auto'/></Response>", 200, {'Content-Type': 'application/xml'}
    else:
        return f"<Response><Say>{ai_reply}</Say><Gather input='speech' action='{PUBLIC_BASE_URL}/gather?phone={urllib.parse.quote_plus(phone)}' method='POST' timeout='6' speechTimeout='auto'/></Response>", 200, {'Content-Type': 'application/xml'}


# ------------------------- Simple confirm-time endpoint -------------------------
@app.route('/confirm-time', methods=['POST'])
def confirm_time():
    user_time = (request.form.get('SpeechResult') or "").strip()
    phone = request.args.get("phone") or request.form.get("From") or "unknown"
    if user_time:
        update_booking_time(phone, user_time)
        txt = f"Thank you. We've scheduled your service for {user_time}. Goodbye!"
    else:
        txt = "We didn't catch a time. Goodbye!"
    audio_url = generate_voice(txt) if callable(generate_voice) else None
    if audio_url:
        return f"<Response><Play>{audio_url}</Play><Hangup/></Response>", 200, {'Content-Type': 'application/xml'}
    else:
        return f"<Response><Say>{txt}</Say><Hangup/></Response>", 200, {'Content-Type': 'application/xml'}


# ------------------------- Run app -------------------------
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)

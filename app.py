import os
import re
import json
import sqlite3
from datetime import date, timedelta
from typing import List, Tuple, Optional, Dict

from config import TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER
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
        return None


# ----------------------------- Config --------------------------------

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
       Stores results in services and faqs tables. This is best-effort and uses simple selectors."""
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
            title_el = prod.select_one(".woocommerce-loop-product__title, h2, .product_title, a")
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
            title_el = prod.select_one(".woocommerce-loop-product__title, h2, a, .product_title")
            title = title_el.get_text(strip=True) if title_el else None
            price_el = prod.select_one("ins .amount, .price .amount, .price, .amount")
            price = price_el.get_text(" ", strip=True) if price_el else ""
            desc = prod.get_text(" ", strip=True)
            if title:
                upsert(title, desc, price, category="Deep Cleaning", meta={"source": DEEP_CLEANING_CATEGORY})

    # FAQs: store in faqs table
    for faq_url in FAQ_URLS:
        fhtml = safe_get(faq_url)
        if not fhtml:
            continue
        fsoup = BeautifulSoup(fhtml, "html.parser")
        # look for common toggles / accordions
        toggles = fsoup.select(".et_pb_toggle, .faq, .faq-item, details, .elementor-accordion-item, .accordion-item")
        for t in toggles:
            q_el = t.select_one(".et_pb_toggle_title, summary, h3, h4, .question, .faq-question, .accordion-title")
            a_el = t.select_one(".et_pb_toggle_content, .answer, p, .elementor-tab-content, .faq-answer, .accordion-content")
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


# Run scraper at startup (best-effort). Comment this line if you prefer manual reindex.
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
    # Also search faqs for exact question matches / helpful snippets
    cur.execute("SELECT question, answer FROM faqs WHERE question LIKE ? OR answer LIKE ? LIMIT ?", (q, q, limit))
    faqs = cur.fetchall()
    conn.close()
    # Return services first, then faqs as (question, answer, '')
    results: List[Tuple[str, str, str]] = []
    for r in rows:
        results.append((r[0], r[1], r[2]))
    for f in faqs:
        results.append((f[0], f[1], ""))
    return results


# ------------------------- Flask + Twilio Setup -------------------------
app = Flask(__name__)
app.secret_key = os.getenv("APP_SECRET_KEY", "app_secret_will_be_here")
twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# ephemeral call / conversation state (in-memory)
call_state: Dict[str, Dict] = {}


# ---------------------- TwiML helpers -----------------------
def respond_with_text_or_audio(text: str, next_action: str):
    """
    Try to generate audio via generate_voice(text). If audio_url is returned (http...), use <Play>.
    Otherwise fallback to <Say> with the text.
    """
    audio_url = None
    try:
        audio_url = generate_voice(text)
    except Exception:
        audio_url = None

    if audio_url and isinstance(audio_url, str) and audio_url.lower().startswith("http"):
        return f"""
        <Response>
            <Play>{audio_url}</Play>
            <Gather input="speech" action="{next_action}" method="POST" timeout="6" speechTimeout="auto"/>
        </Response>
        """, 200, {'Content-Type': 'application/xml'}
    else:
        # Twilio <Say> expects plaintext; keep it short
        safe_text = text.replace("&", "and")
        return f"""
        <Response>
            <Say>{safe_text}</Say>
            <Gather input="speech" action="{next_action}" method="POST" timeout="6" speechTimeout="auto"/>
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
        "phase": "intro",
        "stage": "greeting",
        "data": {}
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

    prompt = f"Hi {name}, this is Ava from Kepsten. We received your request for {service}. How can I help you today?"
    return respond_with_text_or_audio(prompt, f"{PUBLIC_BASE_URL}/gather?phone={urllib.parse.quote_plus(phone)}")


# ------------------------- Twilio: Incoming call -------------------------
@app.route('/incoming_call', methods=['POST'])
def incoming_call():
    caller = request.values.get('From') or "unknown"
    # initialize state
    call_state.setdefault(caller, {"phase": "intro", "stage": "greeting", "data": {}})
    prompt = "Hi, I'm Ava from Kepsten. How can I help you today?"
    return respond_with_text_or_audio(prompt, f"{PUBLIC_BASE_URL}/gather?phone={urllib.parse.quote_plus(caller)}")


# ------------------------- Conversation: gather + routing -------------------------
EMAIL_REGEX = r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"


def detect_booking_intent(user_input: str) -> bool:
    kws = ["book", "schedule", "appointment", "clean my", "need cleaning", "i need", "i want cleaning", "can you clean"]
    t = (user_input or "").lower()
    return any(kw in t for kw in kws)


@app.route('/gather', methods=['POST'])
def gather():
    phone = request.args.get("phone") or request.values.get('From') or request.values.get('Caller') or "unknown"
    user_input = (request.form.get('SpeechResult') or request.values.get('SpeechResult') or request.form.get('SpeechResult') or "").strip()
    state = call_state.setdefault(phone, {"phase": "intro", "stage": "greeting", "data": {}})

    # If there's no user input (empty), reprompt
    if not user_input:
        reprompt = "Sorry, I didn't catch that. How can I help you?"
        return respond_with_text_or_audio(reprompt, f"{PUBLIC_BASE_URL}/gather?phone={urllib.parse.quote_plus(phone)}")

    # record raw user interaction
    record_interaction(phone, "user", user_input, "")

    # 1) Check KB (services + faqs) for direct answer/helpful info first
    kb_results = search_knowledge_base(user_input, limit=4)
    if kb_results:
        # prefer an exact service match or short summary
        first = kb_results[0]
        name, desc, price = first
        summary = f"{name}."
        if price:
            summary += f" Price: {price}."
        else:
            # use a short snippet of description
            snippet = (desc[:200] + "...") if desc and len(desc) > 200 else desc
            if snippet:
                summary += f" {snippet}"
        # Offer continuation: booking or more info
        follow = " Would you like to book this or hear more options?"
        reply = summary + follow
        # Save AI response record
        record_interaction(phone, "kb_reply", user_input, reply)
        return respond_with_text_or_audio(reply, f"{PUBLIC_BASE_URL}/gather?phone={urllib.parse.quote_plus(phone)}")

    # 2) If KB didn't match, detect booking intent
    if detect_booking_intent(user_input) or state.get("stage") in ("ask_service", "ask_bedrooms", "confirm_booking", "ask_name", "ask_city", "ask_address", "ask_slot"):
        # booking flow
        stage = state.get("stage", "greeting")
        data = state.setdefault("data", {})

        # If stage not started and user asked to book, ask for service
        if stage in ("greeting", None):
            state["stage"] = "ask_service"
            call_state[phone] = state
            prompt = "Sure — which service would you like? We offer Standard Cleaning, Deep Cleaning, Move In/Move Out, Post Construction, and Hourly Packages."
            return respond_with_text_or_audio(prompt, f"{PUBLIC_BASE_URL}/gather?phone={urllib.parse.quote_plus(phone)}")

        # user answering service
        if stage == "ask_service":
            data["service"] = user_input
            state["stage"] = "ask_bedrooms"
            call_state[phone] = state
            prompt = "How many bedrooms should we plan for? (1, 2, 3, 4, 5)"
            return respond_with_text_or_audio(prompt, f"{PUBLIC_BASE_URL}/gather?phone={urllib.parse.quote_plus(phone)}")

        # capture bedrooms
        if stage == "ask_bedrooms":
            m = re.search(r"\b([1-5])\b", user_input)
            if m:
                data["bedrooms"] = int(m.group(1))
            else:
                # accept words like 'three'
                words_to_nums = {"one":1, "two":2, "three":3, "four":4, "five":5}
                for w,n in words_to_nums.items():
                    if w in user_input.lower():
                        data["bedrooms"] = n
                        break
            state["stage"] = "confirm_booking"
            call_state[phone] = state

            # try to find price for exact bedroom package in services
            price_text = ""
            kb = search_knowledge_base(data.get("service", ""), limit=10)
            for name, desc, price in kb:
                # match by bedroom number in product title like "3 Bedroom Package"
                if data.get("bedrooms") and str(data["bedrooms"]) in name:
                    price_text = f"The {name} costs {price}."
                    break

            if not price_text:
                # best-effort: give a short generic line
                price_text = "I can get you a quote based on bedrooms — would you like me to book a slot and then confirm price by email?"
            prompt = price_text + " Would you like to proceed and book?"
            return respond_with_text_or_audio(prompt, f"{PUBLIC_BASE_URL}/gather?phone={urllib.parse.quote_plus(phone)}")

        # confirmation to book
        if stage == "confirm_booking":
            if "yes" in user_input.lower() or "sure" in user_input.lower() or "please" in user_input.lower():
                state["stage"] = "ask_name"
                call_state[phone] = state
                return respond_with_text_or_audio("Great — may I have your full name, please?", f"{PUBLIC_BASE_URL}/gather?phone={urllib.parse.quote_plus(phone)}")
            else:
                state["stage"] = "greeting"
                call_state[phone] = state
                return respond_with_text_or_audio("No problem — anything else I can help with?", f"{PUBLIC_BASE_URL}/gather?phone={urllib.parse.quote_plus(phone)}")

        # capture name
        if stage == "ask_name":
            data["name"] = user_input
            state["stage"] = "ask_city"
            call_state[phone] = state
            return respond_with_text_or_audio("Thanks. Which city are you in?", f"{PUBLIC_BASE_URL}/gather?phone={urllib.parse.quote_plus(phone)}")

        # capture city
        if stage == "ask_city":
            data["city"] = user_input
            state["stage"] = "ask_address"
            call_state[phone] = state
            return respond_with_text_or_audio("And your full address (or nearest intersection)?", f"{PUBLIC_BASE_URL}/gather?phone={urllib.parse.quote_plus(phone)}")

        # capture address
        if stage == "ask_address":
            data["address"] = user_input
            state["stage"] = "ask_slot"
            call_state[phone] = state
            return respond_with_text_or_audio("When would you like the service — today or tomorrow, AM or PM?", f"{PUBLIC_BASE_URL}/gather?phone={urllib.parse.quote_plus(phone)}")

        # capture slot & finalize booking
        if stage == "ask_slot":
            # parse day and am/pm
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
            if not (chosen_day and slot):
                # ask again
                return respond_with_text_or_audio("Could you say if you want AM or PM, and is it for today or tomorrow?", f"{PUBLIC_BASE_URL}/gather?phone={urllib.parse.quote_plus(phone)}")

            booking_time = f"{chosen_day} {slot}"
            data["booking_time"] = booking_time
            data["phone"] = phone
            # persist booking
            try:
                save_request_to_db(data, confirmation="yes", booking_time=booking_time)
                # mark slot taken
                conn = sqlite3.connect(DB_PATH)
                cur = conn.cursor()
                cur.execute("INSERT INTO availability_slots(day, slot, is_available) VALUES (?, ?, 0) ON CONFLICT(day, slot) DO UPDATE SET is_available=0", (chosen_day, slot))
                conn.commit()
                conn.close()
            except Exception:
                pass

            state["stage"] = "done"
            call_state[phone] = state
            reply = f"Booked {data.get('service','service')} for {booking_time}. We'll email confirmation to {data.get('email','the email you provided')}. Anything else I can help with?"
            return respond_with_text_or_audio(reply, f"{PUBLIC_BASE_URL}/gather?phone={urllib.parse.quote_plus(phone)}")

    # 3) Default: fallback to LLM (Mistral) for freeform Q/A
    prompt = f"You are Ava from Kepsten. The user said: '{user_input}'. Answer briefly, warmly, and helpfully. If useful, mention we can book a cleaning."
    ai_reply = get_mistral_response(prompt)
    record_interaction(phone, "ai_reply", user_input, ai_reply)
    return respond_with_text_or_audio(ai_reply, f"{PUBLIC_BASE_URL}/gather?phone={urllib.parse.quote_plus(phone)}")


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
    audio_url = None
    try:
        audio_url = generate_voice(txt)
    except Exception:
        audio_url = None
    if audio_url and isinstance(audio_url, str) and audio_url.lower().startswith("http"):
        return f"<Response><Play>{audio_url}</Play><Hangup/></Response>", 200, {'Content-Type': 'application/xml'}
    else:
        return f"<Response><Say>{txt}</Say><Hangup/></Response>", 200, {'Content-Type': 'application/xml'}


# ------------------------- Run app -------------------------
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)

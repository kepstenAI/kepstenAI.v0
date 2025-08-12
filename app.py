import sqlite3
from flask import Flask, request, jsonify, render_template
from twilio.rest import Client
from ai.mistral_client import get_mistral_response
from ai.elevenlabs_client import generate_voice
import urllib.parse
import pandas as pd
import re
from config import TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER


# Connect to your database (or create it if it doesn't exist)
conn = sqlite3.connect('bookings.db')  # Replace with your DB name if different
cursor = conn.cursor()

# Create table
cursor.execute("""
CREATE TABLE IF NOT EXISTS confirmed_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    email TEXT,
    phone TEXT,
    service TEXT,
    message TEXT,
    confirmation TEXT,
    booking_time TEXT
)
""")

# Commit changes and close
conn.commit()
conn.close()





app = Flask(__name__)
client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

app.secret_key = "app_secret_will_be_here"
call_data_store = {}
NGROK_DOMAIN = "https://kepstenai-v0.onrender.com/"


def save_request_to_db(data, confirmation=None, booking_time=None):
    conn = sqlite3.connect("bookings.db")
    c = conn.cursor()
    c.execute("""
        INSERT INTO confirmed_requests (name, email, phone, service, message, confirmation, booking_time)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        data.get("name"),
        data.get("email"),
        data.get("phone"),
        data.get("service"),
        data.get("message"),
        confirmation,
        booking_time
    ))
    conn.commit()
    conn.close()


def update_booking_time(phone, booking_time):
    conn = sqlite3.connect("bookings.db")
    c = conn.cursor()
    c.execute("UPDATE confirmed_requests SET booking_time = ? WHERE phone = ?", (booking_time, phone))
    conn.commit()
    conn.close()


@app.route('/')
def index():
    return render_template('index.html')

@app.route('/form')
def form():
  return render_template("form.html")

@app.route('/form_submit', methods=['POST'])
def form_submit():
    data = request.form.to_dict()

    if not all([data.get("name"), data.get("email"), data.get("phone"), data.get("service"), data.get("message")]):
        return jsonify({"error": "Missing fields"}), 400

    # Save to DB (no confirmation or time yet)
    save_request_to_db(data)

    # Store in memory for call follow-up
    call_data_store[data["phone"]] = {
        "name": data["name"],
        "email": data["email"],
        "service": data["service"],
        "message": data["message"]
    }

    return jsonify({"success": True}), 200


@app.route('/trigger_call', methods=['POST'])
def trigger_call():
    data = request.get_json(force=True)

    name = data.get("name")
    phone = data.get("phone")
    service = data.get("service")
    message = data.get("message")
    email = data.get("email")

    if not all([name, phone, service, message]):
        return jsonify({"error": "Missing fields"}), 400

    # Store in memory
    call_data_store[phone] = {
        "name": name,
        "email": email,
        "service": service,
        "message": message
    }

    encoded_phone = urllib.parse.quote_plus(phone)

    try:
        call = client.calls.create(
            to=phone,
            from_=TWILIO_PHONE_NUMBER,
            url=f"{NGROK_DOMAIN}/voice?phone={encoded_phone}"
        )
        return jsonify({"success": True, "sid": call.sid}), 200
    except Exception as e:
        print("Twilio Error:", e)
        return jsonify({"error": "Call failed"}), 500


@app.route('/voice', methods=['POST', 'GET'])
def voice():
    phone = request.args.get("phone")
    data = call_data_store.get(phone, {})
    name = data.get("name", "there")
    service = data.get("service", "cleaning")
    message = data.get("message", "")

    prompt = f"You are Ava, the Kepsten assistant. You're calling {name} about their request for {service}. Their message: {message}. Ask if they made this request. Keep it short and warm."

    ai_response = get_mistral_response(prompt)
    audio_url = generate_voice(ai_response)
    encoded_phone = urllib.parse.quote_plus(phone)

    return f"""
        <Response>
            <Play>{audio_url}</Play>
            <Gather input="speech" action="{NGROK_DOMAIN}/gather?phone={encoded_phone}" method="POST" timeout="5" speechTimeout="auto"/>
        </Response>
    """, 200, {'Content-Type': 'application/xml'}


# Load services from Excel at startup
services_df = pd.read_excel("services.xlsx")  # Must have columns: Service, Price

def find_service_info(query):
    for _, row in services_df.iterrows():
        if re.search(row["Service"], query, re.IGNORECASE):
            return row["Service"], row["Price"]
    return None, None

def is_service_question(user_input):
    # Common ways users might ask about services
    keywords = [
        "service", "services", "what do you do", "what can you do",
        "offer", "provide", "available services", "cleaning options"
    ]
    return any(kw in user_input for kw in keywords)

@app.route('/gather', methods=['POST'])
def gather():
    user_input = request.form.get('SpeechResult', '').strip().lower()
    phone = request.args.get("phone")
    data = call_data_store.get(phone, {})

    # If user says nothing
    if not user_input:
        retry_prompt = "I didn’t catch that. Could you please repeat?"
        audio_url = generate_voice(retry_prompt)
        return f"""
        <Response>
            <Play>{audio_url}</Play>
            <Gather bargeIn="true" input="speech" action="{NGROK_DOMAIN}/gather?phone={urllib.parse.quote_plus(phone)}"
                    method="POST" timeout="5" speechTimeout="auto"/>
        </Response>
        """, 200, {'Content-Type': 'application/xml'}

    # ✅ Handle service-related questions (Excel-driven)
    if is_service_question(user_input):
        services_list = ", ".join(services_df["Service"].tolist())
        ai_response = f"We currently offer the following services: {services_list}. Which one are you interested in?"
    
    elif "where you got my number" in user_input or "from where" in user_input:
        ai_response = f"We received your contact details from your service request for {data.get('service', 'our services')}."
    
    elif "what service" in user_input and "request" in user_input:
        ai_response = f"You requested our {data.get('service', 'service')} service."
    
    elif "price" in user_input or "cost" in user_input:
        service_name, price = find_service_info(user_input)
        if service_name and price:
            ai_response = f"The price for {service_name} is {price}."
        else:
            ai_response = "Could you specify which service you’re asking about?"
    
    elif "yes" in user_input or "i did" in user_input:
        save_request_to_db({
            "name": data.get("name"),
            "email": data.get("email"),
            "phone": phone,
            "service": data.get("service"),
            "message": data.get("message")
        }, confirmation="yes")
        ai_response = f"Great! When would you like our team to come for the {data.get('service')} service?"
        audio_url = generate_voice(ai_response)
        return f"""
        <Response>
            <Play>{audio_url}</Play>
            <Gather bargeIn="true" input="speech" action="{NGROK_DOMAIN}/confirm-time?phone={urllib.parse.quote_plus(phone)}"
                    method="POST" timeout="5" speechTimeout="auto"/>
        </Response>
        """, 200, {'Content-Type': 'application/xml'}
    
    else:
        prompt = f"You are Ava from Kepsten. The user said: '{user_input}'. Respond warmly, keeping the conversation natural."
        ai_response = get_mistral_response(prompt)

    audio_url = generate_voice(ai_response)
    return f"""
    <Response>
        <Play>{audio_url}</Play>
        <Gather bargeIn="true" input="speech" action="{NGROK_DOMAIN}/gather?phone={urllib.parse.quote_plus(phone)}"
                method="POST" timeout="5" speechTimeout="auto"/>
    </Response>
    """, 200, {'Content-Type': 'application/xml'}





@app.route('/confirm-time', methods=['POST'])
def confirm_time():
    user_time = request.form.get('SpeechResult', '').strip()
    phone = request.args.get("phone")

    update_booking_time(phone, user_time)

    return f"""
        <Response>
            <Say>Thank you. We've scheduled your service for {user_time}. Goodbye!</Say>
            <Hangup/>
        </Response>
    """, 200, {'Content-Type': 'application/xml'}


@app.route('/goodbye', methods=['GET', 'POST'])
def goodbye():
    return """
        <Response>
            <Say>We didn't hear anything. Goodbye.</Say>
            <Hangup/>
        </Response>
    """, 200, {'Content-Type': 'application/xml'}


#view booking data 

@app.route("/view-bookings")
def view_bookings():
    conn = sqlite3.connect("bookings.db")
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM confirmed_requests")
    data = cursor.fetchall()
    conn.close()

    # Table headers
    headers = ["ID", "Name", "Email", "Phone", "Service", "Message", "Confirmation", "Booking Time"]

    return render_template("view_bookings.html", data=data, headers=headers)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)

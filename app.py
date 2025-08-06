import sqlite3
from flask import Flask, request, jsonify, render_template
from twilio.rest import Client
from ai.mistral_client import get_mistral_response
from ai.elevenlabs_client import generate_voice
import urllib.parse

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
NGROK_DOMAIN = "https://d4aa43545439.ngrok-free.app"


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


@app.route('/gather', methods=['POST'])
def gather():
    user_input = request.form.get('SpeechResult', '').lower()
    phone = request.args.get("phone")
    data = call_data_store.get(phone, {})

    if "yes" in user_input or "i did" in user_input:
        # Update DB with confirmation
        save_request_to_db({
            "name": data.get("name"),
            "email": data.get("email"),
            "phone": phone,
            "service": data.get("service"),
            "message": data.get("message")
        }, confirmation="yes")

        return f"""
        <Response>
            <Say>Great! When would you like our team to come for the {data.get('service')} service?</Say>
            <Gather input="speech" action="{NGROK_DOMAIN}/confirm-time?phone={urllib.parse.quote_plus(phone)}" method="POST" timeout="5" speechTimeout="auto"/>
        </Response>
        """, 200, {'Content-Type': 'application/xml'}

    else:
        # Continue conversation
        prompt = f"You are Ava from Kepsten. The user replied: '{user_input}'. Respond warmly again, ask them to confirm their service request for {data.get('service')}."
        ai_response = get_mistral_response(prompt)
        audio_url = generate_voice(ai_response)

        return f"""
        <Response>
            <Play>{audio_url}</Play>
            <Gather input="speech" action="{NGROK_DOMAIN}/gather?phone={urllib.parse.quote_plus(phone)}" method="POST" timeout="5" speechTimeout="auto"/>
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

import requests
  # Set these in your config.py

MISTRAL_API_KEY = "iGZ5k6mhz0E45TTw4edsr2HYwP3H8xIQ"
MISTRAL_MODEL = "mistral-small"

def get_mistral_response(user_query):
    headers = {
        "Authorization": f"Bearer iGZ5k6mhz0E45TTw4edsr2HYwP3H8xIQ",
        "Content-Type": "application/json"
    }

    payload = {
        "model": MISTRAL_MODEL,  # e.g., 'mistral-small', 'mistral-medium'
        "messages": [
            {"role": "user", "content": user_query}
        ],
        "temperature": 0.7,
        "max_tokens": 300
    }

    try:
        response = requests.post("https://api.mistral.ai/v1/chat/completions", headers=headers, json=payload)
        data = response.json()

        if response.status_code == 200:
            return data["choices"][0]["message"]["content"].strip()
        else:
            print(f"❌ API Error {response.status_code}: {data}")
            return "Sorry, the AI model failed to generate a response."

    except Exception as e:
        print("❌ Exception:", e)
        return "AI model failed to respond."

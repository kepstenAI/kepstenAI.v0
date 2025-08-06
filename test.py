import pyttsx3

engine = pyttsx3.init()

# List all voices
voices = engine.getProperty('voices')
for index, voice in enumerate(voices):
    print(f"{index}: {voice.name}, Gender: {voice.gender}, ID: {voice.id}")

# Set a female voice (based on what your system supports)
for voice in voices:
    if "female" in voice.name.lower() or "zira" in voice.name.lower():  # "Zira" is default female on Windows
        engine.setProperty('voice', voice.id)
        break

# Speak text
engine.say("Hello, I am your assistant with a female voice.")
engine.runAndWait()

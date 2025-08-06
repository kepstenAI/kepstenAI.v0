from gtts import gTTS

def generate_voice(text, output_path='static/audio/output.mp3'):
    tts = gTTS(text=text, lang='en')
    tts.save(output_path)
    return output_path

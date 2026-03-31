"""Teste rápido da extração/transcrição de áudio."""
import os
import tempfile
import requests

AUDIO_URL = "https://f004.backblazeb2.com/file/temp-file-download/instances/3EFABF270A13919DE95AD6F4B026E35E/3B66AC72F31507BA158B/dMxkXBVB7flAWgR6rBmlGA==.ogg"
MIME_TYPE = "audio/ogg; codecs=opus"

def transcrever_audio(audio_url: str, mime_type: str = "") -> str:
    from openai import OpenAI
    response = requests.get(audio_url, timeout=30)
    response.raise_for_status()

    tipo = mime_type or response.headers.get("Content-Type", "")
    suffix = ".ogg"
    if "mp4" in tipo or "m4a" in tipo:
        suffix = ".mp4"
    elif "mpeg" in tipo or "mp3" in tipo:
        suffix = ".mp3"
    elif "wav" in tipo:
        suffix = ".wav"

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(response.content)
        tmp_path = tmp.name

    try:
        client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
        with open(tmp_path, "rb") as audio_file:
            transcript = client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
                language="pt",
            )
        return transcript.text.strip()
    finally:
        os.unlink(tmp_path)

if __name__ == "__main__":
    print(f"OPENAI_API_KEY: {'OK' if os.environ.get('OPENAI_API_KEY') else 'NAO ENCONTRADA'}")
    print(f"Baixando áudio de: {AUDIO_URL}")
    resultado = transcrever_audio(AUDIO_URL, MIME_TYPE)
    if resultado:
        print(f"\nTranscrição: {resultado}")
    else:
        print("\nRetornou vazio.")

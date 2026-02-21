"""
MusicAbility – MVP Streamlit
Accesibilidad motora: el usuario escribe una instrucción y obtiene un MIDI simple.
"""

import io
import json
import math
import os
import re
import struct
from pathlib import Path
from urllib.parse import urlparse

import requests
import streamlit as st
import streamlit.components.v1 as components
from audio_recorder_streamlit import audio_recorder
from dotenv import load_dotenv

# ─────────────────────────────────────────────────────────────────────────────
# 1. CONFIGURACIÓN
# ─────────────────────────────────────────────────────────────────────────────

# Carga .env siempre relativo al archivo, sin depender del CWD
load_dotenv(dotenv_path=Path(__file__).parent / ".env", override=True)

FOUNDRY_API_KEY  = os.getenv("FOUNDRY_API_KEY", "")
FOUNDRY_ENDPOINT = os.getenv("FOUNDRY_ENDPOINT", "").rstrip("/")
MODEL_DEPLOYMENT = os.getenv("MODEL_DEPLOYMENT_NAME", "")
# MODEL_NAME: nombre base del modelo (ej. gpt-5-nano). Si no se define,
# se usa MODEL_DEPLOYMENT_NAME como fallback.
MODEL_NAME       = os.getenv("MODEL_NAME", "") or MODEL_DEPLOYMENT
# Para endpoints services.ai.azure.com/api/projects/... usar 2024-05-01-preview
# Para endpoints clásicos *.openai.azure.com usar 2024-10-21
FOUNDRY_API_VERSION = os.getenv("FOUNDRY_API_VERSION", "2024-05-01-preview")

# Azure Speech (Speech-to-Text)
SPEECH_KEY    = os.getenv("SPEECH_KEY", "")
SPEECH_REGION = os.getenv("AZURE_SPEECH_REGION", "").strip('"')

# Campos mínimos que el JSON del modelo debe contener
REQUIRED_FIELDS = {"title", "tempo_bpm", "key", "length_bars",
                   "time_signature", "melody"}

# Rango MIDI permitido: C3 (48) – C5 (72)
PITCH_MIN = 48
PITCH_MAX = 72

# ─────────────────────────────────────────────────────────────────────────────
# 2a. HELPERS DE AUDIO
# ─────────────────────────────────────────────────────────────────────────────

def _wav_duration(wav_bytes: bytes) -> float:
    """
    Extrae la duración en segundos de un buffer WAV/PCM.
    """
    try:
        if len(wav_bytes) < 44:
            return 0.0
        channels    = struct.unpack_from("<H", wav_bytes, 22)[0]
        sample_rate = struct.unpack_from("<I", wav_bytes, 24)[0]
        bits        = struct.unpack_from("<H", wav_bytes, 34)[0]
        pcm         = wav_bytes[44:]
        n_samples   = len(pcm) // (bits // 8)
        duration    = n_samples / (sample_rate * channels) if sample_rate else 0
        return round(duration, 1)
    except Exception:
        return 0.0


# Timer JS que observa el botón del audio_recorder y muestra cronómetro en vivo
_RECORDING_TIMER_HTML = """
<div id="rec-timer-root" style="text-align:center;padding:6px 0;font-family:sans-serif;">
  <div style="font-size:13px;color:#888;">⏱ Tiempo de grabación</div>
  <div id="rec-display" style="font-size:28px;font-weight:bold;color:#ccc;margin-top:2px;">0:00.0</div>
</div>
<script>
(function(){
  const display = document.getElementById('rec-display');
  let recording = false, startT = 0, interval = null, finalTime = '0:00.0';

  function fmt(ms){
    const totalSec = ms / 1000;
    const m = Math.floor(totalSec / 60);
    const s = totalSec - m * 60;
    return m + ':' + s.toFixed(1).padStart(4,'0');
  }

  function tick(){
    display.textContent = fmt(Date.now() - startT);
  }

  function startTimer(){
    recording = true;
    startT = Date.now();
    display.style.color = '#e74c3c';
    display.textContent = '0:00.0';
    interval = setInterval(tick, 100);
  }

  function stopTimer(){
    recording = false;
    if(interval){ clearInterval(interval); interval = null; }
    finalTime = fmt(Date.now() - startT);
    display.textContent = finalTime;
    display.style.color = '#2ecc71';
  }

  /* Observa clics en el botón del audio_recorder (iframe padre) */
  function poll(){
    try {
      const btns = window.parent.document.querySelectorAll('button[kind="mic"], .audio-recorder-btn, iframe');
      /* audio_recorder usa un iframe; buscamos su contenedor */
      const iframes = window.parent.document.querySelectorAll('iframe');
      for(const ifr of iframes){
        try {
          const doc = ifr.contentDocument || ifr.contentWindow.document;
          const recBtn = doc.querySelector('button, [role="button"]');
          if(!recBtn || recBtn._timerBound) continue;
          recBtn._timerBound = true;
          recBtn.addEventListener('click', ()=>{
            if(!recording) startTimer(); else stopTimer();
          });
        } catch(e){}
      }
    } catch(e){}
  }

  /* MutationObserver para detectar cuando el iframe del recorder aparece */
  const obs = new MutationObserver(poll);
  obs.observe(window.parent.document.body, {childList:true, subtree:true});
  poll();
})();
</script>
"""


# ─────────────────────────────────────────────────────────────────────────────
# 2b. TRANSCRIPCIÓN DE VOZ (Azure Speech-to-Text REST API)
# ─────────────────────────────────────────────────────────────────────────────

def transcribe_audio(audio_bytes: bytes, language: str = "es-CR") -> str:
    """
    Envía audio WAV a Azure Speech-to-Text REST API y devuelve el texto.
    Lanza RuntimeError si la transcripción falla.
    """
    if not SPEECH_KEY or not SPEECH_REGION:
        raise RuntimeError(
            "Variables SPEECH_KEY o AZURE_SPEECH_REGION no están configuradas."
        )

    url = (
        f"https://{SPEECH_REGION}.stt.speech.microsoft.com"
        f"/speech/recognition/conversation/cognitiveservices/v1"
        f"?language={language}&format=detailed"
    )
    headers = {
        "Ocp-Apim-Subscription-Key": SPEECH_KEY,
        "Content-Type": "audio/wav; codecs=audio/pcm; samplerate=48000",
        "Accept": "application/json",
    }
    try:
        resp = requests.post(url, headers=headers, data=audio_bytes, timeout=30)
        resp.raise_for_status()
    except requests.exceptions.HTTPError:
        raise RuntimeError(
            f"Error HTTP {resp.status_code} de Speech API: {resp.text[:400]}"
        )
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"Error de conexión con Speech API: {e}")

    result = resp.json()
    status = result.get("RecognitionStatus", "")
    if status != "Success":
        raise RuntimeError(
            f"No se pudo transcribir el audio (status={status}). "
            "Intenta hablar más claro o más cerca del micrófono."
        )
    return result.get("DisplayText", "").strip()


# ─────────────────────────────────────────────────────────────────────────────
# 2b. PROMPT DEL SISTEMA
# ─────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """
Eres un compositor asistido por IA especializado en accesibilidad musical.
El usuario describe una idea musical. Debes devolver ÚNICAMENTE un objeto JSON
válido, sin texto adicional, sin bloques de código, sin explicaciones.

Esquema obligatorio:
{
  "title": "string",
  "tempo_bpm": int,
  "key": "string",
  "length_bars": int,
  "time_signature": "string",
  "melody": [
    {
      "pitch": "string",
      "start_beat": float,
      "duration_beats": float,
      "velocity": int
    }
  ],
  "assumptions": ["string"]
}

Restricciones:
- Rango de notas: C3 a C5 exclusivamente (pitch como "C4", "D#4", "Bb3").
- Melodía cantable: evita saltos mayores a una sexta (9 semitonos) consecutivos.
- length_bars máximo 8 si el usuario no indica otro valor.
- tempo_bpm entre 40 y 200. Default 90.
- velocity entre 40 y 110. Default 80.
- time_signature default "4/4".
- SOLO devuelves JSON válido, nada más.
""".strip()

# ─────────────────────────────────────────────────────────────────────────────
# 3. INTEGRACIÓN CON AZURE AI FOUNDRY
# ─────────────────────────────────────────────────────────────────────────────

def _clean_model_response(raw: str) -> str:
    """Elimina bloques ```json ... ``` que el modelo pueda agregar."""
    raw = raw.strip()
    match = re.search(r"```(?:json)?\s*(\{.*\})\s*```", raw, re.DOTALL)
    if match:
        return match.group(1).strip()
    if raw.startswith("{"):
        return raw
    match = re.search(r"(\{.*\})", raw, re.DOTALL)
    if match:
        return match.group(1).strip()
    return raw


def call_foundry_for_music_json(user_text: str) -> dict:
    """
    Llama a Azure AI Foundry (chat completions) con el texto del usuario.
    Devuelve el dict Python del JSON musical o lanza ValueError / RuntimeError.
    """
    if not FOUNDRY_API_KEY or not FOUNDRY_ENDPOINT or not MODEL_DEPLOYMENT:
        raise RuntimeError(
            "Variables FOUNDRY_API_KEY, FOUNDRY_ENDPOINT o "
            "MODEL_DEPLOYMENT_NAME no están configuradas."
        )

    # Construir URL correcta según tipo de endpoint:
    # - services.ai.azure.com  → /models/chat/completions  (AI Foundry Inference)
    # - openai.azure.com       → /openai/deployments/{model}/chat/completions
    parsed = urlparse(FOUNDRY_ENDPOINT)
    base = f"{parsed.scheme}://{parsed.netloc}"

    if "openai.azure.com" in parsed.netloc:
        url = (
            f"{base}"
            f"/openai/deployments/{MODEL_DEPLOYMENT}"
            f"/chat/completions?api-version={FOUNDRY_API_VERSION}"
        )
    else:
        # Endpoint AI Foundry (services.ai.azure.com)
        url = f"{base}/models/chat/completions?api-version={FOUNDRY_API_VERSION}"

    headers = {
        "Content-Type": "application/json",
        "api-key": FOUNDRY_API_KEY,
    }
    payload = {
        "model": MODEL_NAME,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": user_text},
        ],
        # gpt-5-nano es un modelo de razonamiento: usa max_completion_tokens
        # (incluye reasoning tokens internos + tokens de respuesta visibles).
        # Se reservan ~14 000 tokens para razonamiento y ~2 000 para el JSON.
        "max_completion_tokens": 16000,
    }

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=120)
        resp.raise_for_status()
    except requests.exceptions.HTTPError as e:
        raise RuntimeError(
            f"Error HTTP {resp.status_code}: {resp.text[:400]}\n\n"
            f"URL llamada: `{url}`"
        ) from e
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"Error de conexión: {e}") from e

    choice = resp.json()["choices"][0]
    raw_content   = choice["message"]["content"] or ""
    finish_reason = choice.get("finish_reason", "")

    # El modelo de razonamiento puede agotar el presupuesto de tokens *antes*
    # de escribir la respuesta, dejando content vacío con finish_reason="length".
    if not raw_content.strip():
        raise RuntimeError(
            "El modelo no generó contenido (finish_reason="
            f"'{finish_reason}'). Intenta con una instrucción "
            "más corta o simplifica la melodía solicitada."
        )

    clean = _clean_model_response(raw_content)

    try:
        data = json.loads(clean)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"El modelo no devolvió JSON válido.\n"
            f"Respuesta recibida:\n{raw_content[:500]}\n\nError: {e}"
        )

    missing = REQUIRED_FIELDS - set(data.keys())
    if missing:
        raise ValueError(f"Faltan campos en el JSON: {', '.join(sorted(missing))}")

    if not isinstance(data["melody"], list) or len(data["melody"]) == 0:
        raise ValueError("El campo 'melody' debe ser una lista no vacía.")

    return data


# ─────────────────────────────────────────────────────────────────────────────
# 4. GENERACIÓN MIDI (solo stdlib + mido)
# ─────────────────────────────────────────────────────────────────────────────

_NOTE_MAP  = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
_ACCID_MAP = {"#": 1, "b": -1}


def pitch_to_midi(pitch_str: str) -> int:
    """
    Convierte 'C4', 'D#4', 'Bb3' a número MIDI.
    Si está fuera del rango C3-C5 lo transpone por octavas hasta encajar.
    """
    match = re.fullmatch(r"([A-G])([#b]?)(-?\d+)", pitch_str.strip())
    if not match:
        raise ValueError(f"Pitch inválido: '{pitch_str}'")
    note, acc, octave = match.group(1), match.group(2), int(match.group(3))
    midi = (octave + 1) * 12 + _NOTE_MAP[note] + _ACCID_MAP.get(acc, 0)
    while midi < PITCH_MIN:
        midi += 12
    while midi > PITCH_MAX:
        midi -= 12
    return midi


def _encode_varint(value: int) -> bytes:
    """Codifica entero como MIDI variable-length quantity."""
    buf = [value & 0x7F]
    value >>= 7
    while value:
        buf.append((value & 0x7F) | 0x80)
        value >>= 7
    return bytes(reversed(buf))


def _meta_tempo(bpm: int) -> bytes:
    """Mensaje meta FF 51 03 <3 bytes μs/beat>."""
    us = int(60_000_000 / bpm)
    return b"\xFF\x51\x03" + struct.pack(">I", us)[1:]


def build_midi(music_json: dict) -> bytes:
    """
    Construye un archivo MIDI tipo 0 desde el dict musical.
    ticks_per_beat = 480, canal 0, program 0 (piano).
    """
    tpb  = 480
    bpm  = max(40, min(200, int(music_json.get("tempo_bpm", 90))))
    notes = sorted(music_json["melody"], key=lambda n: float(n["start_beat"]))

    # Eventos: (tick_absoluto, prioridad, status, p1, p2)
    # prioridad 0 = note_off primero cuando coinciden ticks
    events: list[tuple[int, int, int, int, int]] = []
    for note in notes:
        try:
            midi_note = pitch_to_midi(str(note["pitch"]))
        except ValueError:
            continue
        vel   = max(0, min(127, int(note.get("velocity", 80))))
        start = int(float(note["start_beat"])      * tpb)
        dur   = max(1, int(float(note["duration_beats"]) * tpb))
        events.append((start,       1, 0x90, midi_note, vel))   # note_on
        events.append((start + dur, 0, 0x80, midi_note, 0))     # note_off

    events.sort(key=lambda e: (e[0], e[1]))

    track = bytearray()
    # set_tempo
    track += _encode_varint(0) + _meta_tempo(bpm)
    # program_change → piano
    track += _encode_varint(0) + bytes([0xC0, 0x00])

    current_tick = 0
    for tick, _, status, p1, p2 in events:
        delta = tick - current_tick
        current_tick = tick
        track += _encode_varint(delta) + bytes([status, p1, p2])

    # End of track
    track += b"\x00\xFF\x2F\x00"

    track_bytes = bytes(track)
    header  = b"MThd" + struct.pack(">IHHH", 6, 0, 1, tpb)
    chunk   = b"MTrk" + struct.pack(">I", len(track_bytes)) + track_bytes
    return header + chunk


def synthesize_wav(music_json: dict, sample_rate: int = 44100) -> bytes:
    """
    Sintetiza el JSON musical a un buffer WAV (16-bit PCM mono)
    usando ondas sinusoidales con envolvente ADSR simple.
    """
    bpm   = max(40, min(200, int(music_json.get("tempo_bpm", 90))))
    spb   = 60.0 / bpm  # segundos por beat
    notes = music_json.get("melody", [])

    # Calcular duración total en muestras
    max_end = 0.0
    for n in notes:
        end = (float(n["start_beat"]) + float(n["duration_beats"])) * spb
        if end > max_end:
            max_end = end
    total_samples = int((max_end + 0.3) * sample_rate)  # +0.3s de cola
    buf = [0.0] * total_samples

    for n in notes:
        try:
            midi_note = pitch_to_midi(str(n["pitch"]))
        except ValueError:
            continue
        freq = 440.0 * (2.0 ** ((midi_note - 69) / 12.0))
        vel  = max(0, min(127, int(n.get("velocity", 80)))) / 127.0
        t0   = float(n["start_beat"]) * spb
        dur  = float(n["duration_beats"]) * spb
        s0   = int(t0 * sample_rate)
        ns   = int(dur * sample_rate)
        if s0 + ns > total_samples:
            ns = total_samples - s0

        # Envolvente ADSR simple (attack 20ms, decay 40ms, sustain 0.6, release 60ms)
        att = int(0.02 * sample_rate)
        dec = int(0.04 * sample_rate)
        rel = int(0.06 * sample_rate)
        sus_level = 0.6

        for i in range(ns):
            # Envolvente
            if i < att:
                env = i / att if att > 0 else 1.0
            elif i < att + dec:
                env = 1.0 - (1.0 - sus_level) * ((i - att) / dec) if dec > 0 else sus_level
            elif i < ns - rel:
                env = sus_level
            else:
                remaining = ns - i
                env = sus_level * (remaining / rel) if rel > 0 else 0.0

            t = i / sample_rate
            # Onda con un leve armónico para dar cuerpo
            sample = (math.sin(2 * math.pi * freq * t)
                      + 0.3 * math.sin(4 * math.pi * freq * t)
                      + 0.1 * math.sin(6 * math.pi * freq * t))
            idx = s0 + i
            if 0 <= idx < total_samples:
                buf[idx] += sample * vel * env * 0.35

    # Normalizar y convertir a 16-bit PCM
    peak = max(abs(s) for s in buf) if buf else 1.0
    if peak < 1e-6:
        peak = 1.0
    scale = 32000.0 / peak
    pcm = struct.pack(f"<{len(buf)}h",
                      *[max(-32768, min(32767, int(s * scale))) for s in buf])

    # Construir WAV
    data_size  = len(pcm)
    byte_rate  = sample_rate * 2  # 16-bit mono
    wav = io.BytesIO()
    wav.write(b"RIFF")
    wav.write(struct.pack("<I", 36 + data_size))
    wav.write(b"WAVE")
    wav.write(b"fmt ")
    wav.write(struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, byte_rate, 2, 16))
    wav.write(b"data")
    wav.write(struct.pack("<I", data_size))
    wav.write(pcm)
    return wav.getvalue()


# ─────────────────────────────────────────────────────────────────────────────
# 5. INTERFAZ STREAMLIT
# ─────────────────────────────────────────────────────────────────────────────

st.set_page_config(page_title="MusicAbility", page_icon="🎵", layout="centered")

st.title("🎵 MusicAbility")
st.caption("Accesibilidad musical — describe tu idea y descarga un MIDI.")

# Verificar variables de entorno (nunca mostrar los valores)
_missing_env = [v for v in ("FOUNDRY_API_KEY", "FOUNDRY_ENDPOINT", "MODEL_DEPLOYMENT_NAME")
                if not os.getenv(v)]
if _missing_env:
    st.error(
        f"❌ Faltan variables en `.env`: `{'`, `'.join(_missing_env)}`\n\n"
        "Copia `.env.example` a `.env` y completa los valores."
    )
    st.stop()

# ── Entrada ───────────────────────────────────────────────────────────────────
st.subheader("💬 Describe tu melodía")

tab_text, tab_mic = st.tabs(["✏️ Escribir texto", "🎙️ Grabar con micrófono"])

user_input = ""

with tab_text:
    user_input_text = st.text_area(
        label="Instrucción musical:",
        placeholder=(
            "Ej: Una melodía tranquila en Do mayor, tempo lento, 8 compases, "
            "que suene esperanzadora y fácil de tararear."
        ),
        height=120,
    )
    generate_text = st.button(
        "🎼 Generar melodía", type="primary", use_container_width=True,
        key="btn_text",
    )

with tab_mic:
    st.markdown(
        "Presiona el botón del micrófono para grabar tu instrucción musical "
        "con la voz. Al terminar de hablar, presiona de nuevo para detener."
    )
    audio_bytes = audio_recorder(
        text="Pulsa para grabar",
        recording_color="#e74c3c",
        neutral_color="#3498db",
        icon_size="2x",
        pause_threshold=2.0,
        sample_rate=48_000,
    )
    components.html(_RECORDING_TIMER_HTML, height=70)

    transcribed_text = ""
    if audio_bytes:
        # Mostrar duración final del audio grabado
        dur = _wav_duration(audio_bytes)
        mins = int(dur // 60)
        secs = dur - mins * 60
        st.metric("⏱ Duración de la grabación", f"{mins}:{secs:04.1f}")

        with st.spinner("🗣️ Transcribiendo audio con Azure Speech…"):
            try:
                transcribed_text = transcribe_audio(audio_bytes)
            except RuntimeError as e:
                st.error(f"❌ Error al transcribir: {e}")

        if transcribed_text:
            st.info(f"📝 Texto reconocido: **{transcribed_text}**")

    generate_mic = st.button(
        "🎼 Generar melodía desde voz", type="primary",
        use_container_width=True, key="btn_mic",
        disabled=not transcribed_text,
    )

# Determinar cuál flujo se activó
generate = False
if generate_text:
    user_input = user_input_text.strip()
    generate = True
elif generate_mic and transcribed_text:
    user_input = transcribed_text
    generate = True

# ── Procesamiento ─────────────────────────────────────────────────────────────
if generate:
    if not user_input:
        st.warning("⚠️ Por favor escribe o dicta una instrucción antes de generar.")
        st.stop()

    with st.spinner("🧠 Analizando con Azure AI Foundry…"):
        try:
            music_data = call_foundry_for_music_json(user_input.strip())
        except ValueError as e:
            st.error(f"❌ JSON inválido del modelo:\n\n{e}")
            st.stop()
        except RuntimeError as e:
            st.error(f"❌ Error de conexión con Foundry:\n\n{e}")
            st.stop()

    st.success(f"✅ Melodía generada: **{music_data.get('title', 'Sin título')}**")

    # Métricas resumen
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Tonalidad",  music_data.get("key", "–"))
    col2.metric("Tempo",      f"{music_data.get('tempo_bpm', '–')} BPM")
    col3.metric("Compases",   music_data.get("length_bars", "–"))
    col4.metric("Compás",     music_data.get("time_signature", "–"))

    # Supuestos del compositor
    if music_data.get("assumptions"):
        with st.expander("💡 Supuestos del compositor"):
            for a in music_data["assumptions"]:
                st.write(f"• {a}")

    # JSON completo
    with st.expander("📄 Ver JSON musical completo"):
        st.json(music_data)

    # Tabla de notas
    st.subheader("🎼 Partitura (notas)")
    st.dataframe(
        [
            {
                "Nota":     n.get("pitch", "?"),
                "Inicio":   n.get("start_beat", 0),
                "Duración": n.get("duration_beats", 1),
                "Velocidad":n.get("velocity", 80),
            }
            for n in music_data["melody"]
        ],
        use_container_width=True,
        hide_index=True,
    )

    # Generar MIDI
    with st.spinner("🎹 Construyendo archivo MIDI…"):
        try:
            midi_bytes = build_midi(music_data)
        except Exception as e:
            st.error(f"❌ Error al generar MIDI: {e}")
            st.stop()

    # Sintetizar audio para reproducción en navegador
    with st.spinner("🔊 Sintetizando audio…"):
        try:
            wav_bytes = synthesize_wav(music_data)
        except Exception as e:
            wav_bytes = None
            st.warning(f"⚠️ No se pudo sintetizar audio: {e}")

    st.subheader("▶️ Reproducir melodía")
    if wav_bytes:
        st.audio(wav_bytes, format="audio/wav")
    else:
        st.info("La reproducción no está disponible. Descarga el MIDI.")

    st.subheader("⬇️ Descarga tu MIDI")
    st.download_button(
        label="💾 Descargar musicability.mid",
        data=midi_bytes,
        file_name="musicability.mid",
        mime="audio/midi",
        use_container_width=True,
    )
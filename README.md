# 🎵 MusicAbility — Foundry + Speech

**UCENFOTEC · Aplicaciones de IA · Cuatrimestre 4**

---

## Descripción

MusicAbility es una aplicación web de **accesibilidad musical** construida con Streamlit. Permite a cualquier usuario describir una idea musical —escribiendo texto o hablando por micrófono— y obtener automáticamente un archivo MIDI listo para descargar.

La app utiliza **Azure AI Foundry** (modelo `gpt-5-nano`) para interpretar la descripción y generar la estructura musical, y **Azure Speech-to-Text** para transcribir instrucciones de voz.

---

## Arquitectura

```
┌──────────────────────────────────────────────────────────┐
│                    Navegador (Streamlit)                  │
│                                                          │
│   ┌──────────────┐          ┌──────────────────────┐     │
│   │ ✏️ Texto      │          │ 🎙️ Micrófono         │     │
│   │ (text_area)  │          │ (audio_recorder)     │     │
│   └──────┬───────┘          └──────────┬───────────┘     │
│          │                             │                 │
│          │                    ┌────────▼────────┐        │
│          │                    │ Azure Speech    │        │
│          │                    │ (STT REST API)  │        │
│          │                    └────────┬────────┘        │
│          │                             │ texto           │
│          └──────────┬──────────────────┘                 │
│                     ▼                                    │
│          ┌─────────────────────┐                         │
│          │ Azure AI Foundry    │                         │
│          │ (gpt-5-nano)        │                         │
│          │ prompt → JSON       │                         │
│          └────────┬────────────┘                         │
│                   ▼                                      │
│          ┌─────────────────────┐                         │
│          │ Generador MIDI      │                         │
│          │ (Python puro)       │                         │
│          └────────┬────────────┘                         │
│                   ▼                                      │
│          ┌─────────────────────┐                         │
│          │ 💾 Descarga .mid    │                         │
│          └─────────────────────┘                         │
└──────────────────────────────────────────────────────────┘
```

### Flujo paso a paso

1. **Entrada** — El usuario elige una de dos pestañas:
   - **Escribir texto**: escribe la instrucción musical en un campo de texto.
   - **Grabar con micrófono**: graba audio desde el navegador; la grabación se envía a **Azure Speech-to-Text** (REST API, región `southcentralus`, idioma `es-CR`) y se muestra el texto reconocido.
2. **Generación del JSON musical** — El texto (escrito o transcrito) se envía a **Azure AI Foundry** (chat completions, modelo `gpt-5-nano`). Un *system prompt* detallado obliga al modelo a devolver un JSON con: `title`, `tempo_bpm`, `key`, `length_bars`, `time_signature`, `melody[]` y `assumptions[]`.
3. **Construcción del MIDI** — El JSON se procesa con un generador MIDI escrito en Python puro (sin dependencias externas). Se construye un archivo MIDI tipo 0, 480 ticks/beat, canal 0, programa 0 (piano). Las notas se limitan al rango C3–C5.
4. **Visualización y descarga** — La app muestra métricas (tonalidad, tempo, compases), una tabla de notas, el JSON completo, y un botón de descarga del archivo `.mid`.

---

## Estructura del proyecto

```
musicability-foundry-speech/
├── app.py              # Aplicación principal (Streamlit)
├── .env                # Variables de entorno (no se sube al repo)
├── requirements.txt    # Dependencias de Python
├── instrucciones/      # Documentación interna del proyecto
└── README.md           # Este archivo
```

---

## Requisitos previos

- **Python 3.11+**
- Una suscripción a Azure con:
  - **Azure AI Foundry** (endpoint + API key + deployment de `gpt-5-nano`)
  - **Azure Speech Service** (clave + región)

---

## Instalación

```bash
# 1. Clonar el repositorio
git clone <url-del-repo>
cd musicability-foundry-speech

# 2. Crear y activar entorno virtual
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

# 3. Instalar dependencias
pip install -r requirements.txt
```

---

## Configuración

Crear un archivo `.env` en la raíz del proyecto con las siguientes variables:

```env
# Azure Speech (Speech-to-Text)
SPEECH_KEY=<tu-clave-de-speech>
SPEECH_ENDPOINT=https://<region>.stt.speech.microsoft.com
AZURE_SPEECH_REGION="<region>"

# Azure AI Foundry (Chat Completions)
FOUNDRY_API_KEY=<tu-api-key>
FOUNDRY_ENDPOINT=https://<recurso>.services.ai.azure.com/api/projects/<proyecto>
MODEL_DEPLOYMENT_NAME=<nombre-del-deployment>
MODEL_NAME=gpt-5-nano
```

---

## Ejecución

```bash
streamlit run app.py
```

La app se abrirá en el navegador (por defecto `http://localhost:8501`).

---

## Uso

### Opción 1 — Escribir texto

1. Abre la pestaña **"✏️ Escribir texto"**.
2. Escribe una instrucción, por ejemplo: *"Una melodía alegre en Sol mayor, 4 compases, tempo rápido"*.
3. Presiona **"🎼 Generar melodía"**.
4. Espera unos segundos mientras el modelo genera el JSON y se construye el MIDI.
5. Descarga el archivo `.mid`.

### Opción 2 — Grabar con micrófono

1. Abre la pestaña **"🎙️ Grabar con micrófono"**.
2. Presiona el botón del micrófono y dicta tu instrucción musical.
3. Presiona de nuevo para detener la grabación.
4. La app transcribe el audio automáticamente y muestra el texto reconocido.
5. Presiona **"🎼 Generar melodía desde voz"**.
6. Descarga el archivo `.mid`.

---

## Dependencias

| Paquete                    | Propósito                                      |
| -------------------------- | ---------------------------------------------- |
| `streamlit`                | Framework de la interfaz web                   |
| `python-dotenv`            | Carga de variables de entorno desde `.env`     |
| `requests`                 | Llamadas HTTP a Azure AI Foundry y Speech API  |
| `audio-recorder-streamlit` | Componente de grabación de audio en el browser |

---

## Servicios de Azure utilizados

| Servicio               | Uso en la app                                                |
| ---------------------- | ------------------------------------------------------------ |
| **Azure AI Foundry**   | Generación del JSON musical a partir de texto (gpt-5-nano)  |
| **Azure Speech (STT)** | Transcripción de voz a texto para la entrada por micrófono   |

---

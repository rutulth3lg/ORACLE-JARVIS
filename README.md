# Oracle

Local voice assistant for macOS. Say "Oracle" and it wakes up.

Threaded architecture, Groq LLM backend, persistent memory, media playback, and system automation — all running locally except the LLM calls.

Open source — PRs welcome if you want to add stuff.

---

## What It Does

- Plays music by searching YouTube (yt-dlp, no browser needed)
- Opens apps, websites, YouTube videos
- Answers questions with conversation memory that persists across sessions
- Sets timers and reminders with voice + macOS notification callbacks
- Controls volume, takes screenshots, locks screen
- Remembers things you tell it ("remember that my car is a Tesla")
- Floating HUD in the corner showing status (STANDBY / LISTENING / SPEAKING)
- Auto-sleeps after inactivity

## Setup

```bash
pip install groq edge-tts SpeechRecognition pyaudio yt-dlp
brew install portaudio ffmpeg
```

Grab a free Groq key from [console.groq.com](https://console.groq.com).

```bash
git clone https://github.com/rutulth3lg/ORACLE-JARVIS.git
cd ORACLE-JARVIS
cp .env.example .env
# Fill in your GROQ_API_KEY, ORACLE_OWNER_NAME, ORACLE_OWNER_FIRST
python oracle.py
```

### Auto-start at login
```bash
python oracle.py --install
```

## Usage

Say **"Oracle"** or **"Jarvis"**, wait for **"Sir?"**, then talk.

| Say this | It does this |
|---|---|
| "Play Blinding Lights" | Finds + plays audio via yt-dlp |
| "Play Sidemen on YouTube" | Opens YouTube search in browser |
| "Open VS Code" | Launches the app |
| "What's the weather" | Answers using conversation context |
| "Set a timer for 5 minutes" | Timer with voice + notification callback |
| "Remember that my birthday is March 15" | Stores it, recalls later |
| "Take a screenshot" | Screenshots your screen |
| "Go to sleep" | Manual sleep mode |

## How It Works

- **Wake word** — Continuously listens for "Oracle"/"Jarvis" using Google Speech Recognition
- **LLM** — Groq API running LLaMA-3.3-70B for fast responses
- **TTS** — Edge-TTS for natural voice output
- **Music** — yt-dlp searches YouTube, streams audio directly
- **Memory** — JSON-based persistent memory for facts + conversation history
- **HUD** — Tkinter overlay window, always on top, updates state in real time

## Tech

Python · Groq API · Edge-TTS · SpeechRecognition · yt-dlp · Tkinter

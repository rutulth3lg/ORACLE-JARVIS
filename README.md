# Oracle

local voice assistant for macOS. say "Oracle" and it wakes up.

open source — PRs welcome if you want to add stuff.

---

## what it does

- plays music by searching youtube (yt-dlp, no browser needed)
- opens apps, websites, youtube videos
- answers questions with conversation memory that persists across sessions
- sets timers and reminders with voice + macOS notification callbacks
- controls volume, takes screenshots, locks screen
- remembers things you tell it ("remember that my car is a Tesla")
- floating HUD in the corner showing status (STANDBY / LISTENING / SPEAKING)
- auto-sleeps after inactivity

## setup

```bash
pip install groq edge-tts SpeechRecognition pyaudio yt-dlp
brew install portaudio ffmpeg
```

grab a free groq key from [console.groq.com](https://console.groq.com).

```bash
git clone https://github.com/rutulth3lg/ORACLE-JARVIS.git
cd ORACLE-JARVIS
cp .env.example .env
# fill in your GROQ_API_KEY, ORACLE_OWNER_NAME, ORACLE_OWNER_FIRST
python oracle.py
```

### auto-start at login
```bash
python oracle.py --install
```

## usage

say **"Oracle"** or **"Jarvis"**, wait for **"Sir?"**, then talk.

| say this | it does this |
|---|---|
| "play blinding lights" | finds + plays audio via yt-dlp |
| "play sidemen on youtube" | opens youtube search in browser |
| "open vs code" | launches the app |
| "what's the weather" | answers using conversation context |
| "set a timer for 5 minutes" | timer with voice + notification callback |
| "remember that my birthday is march 15" | stores it, recalls later |
| "take a screenshot" | screenshots your screen |
| "go to sleep" | manual sleep mode |

## how it works

- **wake word** — continuously listens for "oracle"/"jarvis" using google speech recognition
- **LLM** — groq API running llama-3.3-70b for fast responses
- **TTS** — edge-tts for natural voice output
- **music** — yt-dlp searches youtube, streams audio directly
- **memory** — JSON-based persistent memory for facts + conversation history
- **HUD** — tkinter overlay window, always on top, updates state in real time

## tech

python · groq API · edge-tts · speechrecognition · yt-dlp · tkinter

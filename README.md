# Oracle

A local voice assistant for macOS, built by Rutul Gajjar. Say "Oracle" or "Jarvis" to activate it. No cloud subscriptions, no always-on servers — it runs entirely on your Mac.

---

## What it does

- Plays music and audio by searching YouTube (via yt-dlp, no browser needed)
- Opens apps, websites, and YouTube videos on command
- Answers questions with a rolling conversation memory that persists across sessions
- Sets timers and reminders with voice and notification callbacks
- Controls system volume, takes screenshots, locks the screen
- Remembers personal facts you tell it ("remember that my car is a Tesla")
- Has a floating status HUD in the corner of your screen (STANDBY / LISTENING / SPEAKING)
- Auto-sleeps after a configurable period of inactivity

---

## Requirements

### Python packages
```bash
pip install groq edge-tts SpeechRecognition pyaudio yt-dlp
```

### Homebrew packages
```bash
brew install portaudio ffmpeg
```

`portaudio` is needed by PyAudio for microphone access. `ffmpeg` is used by yt-dlp when processing audio.

---

## Setup

1. Clone or download this repo.
2. Open `oracle.py` and set your Groq API key at the top:
   ```python
   GROQ_API_KEY = "your_key_here"
   ```
   Get a free key at [console.groq.com](https://console.groq.com).
3. Run it:
   ```bash
   python oracle.py
   ```

### To start Oracle automatically at login
```bash
python oracle.py --install
```
This installs a LaunchAgent plist. Oracle will launch silently every time you log in with no terminal window needed.

To uninstall:
```bash
launchctl unload ~/Library/LaunchAgents/com.oracle.assistant.plist
rm ~/Library/LaunchAgents/com.oracle.assistant.plist
```

---

## How to use it

Say **"Oracle"** or **"Jarvis"** and wait for the "Sir?" acknowledgement, then speak your command.

| What you say | What happens |
|---|---|
| "Play Blinding Lights" | Finds and plays the audio via yt-dlp |
| "Play a song by Drake" | Same — plays best match |
| "Play Sidemen on YouTube" | Opens YouTube search in browser |
| "Open VS Code" | Launches the app |
| "Open GitHub" | Opens github.com in browser |
| "Set a 10 minute timer" | Timer fires with voice + notification |
| "Remind me to call mum in 30 minutes" | Reminder fires in 30 min |
| "What time is it" | Tells you the time |
| "Battery status" | Reports battery level and charging state |
| "Volume to 60" | Sets system volume to 60 |
| "Take a screenshot" | Saves to Desktop |
| "Lock the screen" | Locks macOS |
| "Start my workspace" | Opens QuantConnect + VS Code + plays Iron Man |
| "Shut down Oracle" | Exits cleanly |

Oracle also auto-sleeps after 10 minutes of inactivity (configurable in `oracle.py`).

---

## Configuration

All user-facing settings are at the top of `oracle.py`:

```python
GROQ_API_KEY       = "..."       # your Groq API key
OWNER_NAME         = "Rutul Gajjar"
OWNER_FIRST        = "Rutul"
VOICE              = "en-GB-RyanNeural"   # edge-tts voice
AUTO_SLEEP_MINUTES = 10                   # 0 to disable auto-sleep
TTS_RATE           = "+6%"               # voice speed
MAX_HISTORY_TURNS  = 20                   # conversation memory depth
```

---

## Uploading to GitHub

1. **Create a new repository** at [github.com/new](https://github.com/new). Name it something like `oracle-assistant`. Keep it private if you want.

2. **Initialise git in this folder:**
   ```bash
   cd /path/to/oracle/folder
   git init
   git add oracle.py README.md
   git commit -m "Initial commit"
   ```

3. **Add your remote and push:**
   ```bash
   git remote add origin https://github.com/YOUR_USERNAME/oracle-assistant.git
   git branch -M main
   git push -u origin main
   ```

4. **Before pushing, remove your API key from the code.** Replace it with an environment variable so it's never in the repo:

   In `oracle.py`, change:
   ```python
   GROQ_API_KEY = "gsk_..."
   ```
   to:
   ```python
   GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
   ```
   Then set it in your shell:
   ```bash
   export GROQ_API_KEY="gsk_..."
   ```
   Add that line to your `~/.zshrc` or `~/.bash_profile` so it persists.

5. **Add a `.gitignore`** to avoid committing memory files and temp audio:
   ```
   oracle_memory.json
   oracle_tmp/
   *.mp3
   *.wav
   __pycache__/
   *.pyc
   ```

---

## Architecture

| Thread | Role |
|---|---|
| Main thread | tkinter HUD — never blocked |
| `oracle-worker` | All mic I/O, Whisper transcription of commands, LLM calls |
| `wake-capture` | Keeps microphone open continuously, pushes audio clips to queue |
| `transcriber` | Runs Whisper on wake clips, signals oracle-worker on detection |
| `tts-worker` | Asyncio event loop — generates edge-tts audio and plays it |
| `media-player` | yt-dlp download + afplay per play request |
| Timer/reminder threads | One per active timer, fire independently |

---

## Troubleshooting

**Music searches but nothing plays**
Run `yt-dlp --version` and `ffmpeg -version` in terminal. If either is missing, install them with pip/brew as shown above.

**Microphone not picking up wake word**
Increase `energy_threshold` in `wake_capture_thread()`. The default is 600 — try 400 if your environment is quiet.

**"Sir?" fires but Oracle doesn't respond to the command**
The command listener has a 7-second timeout. Speak within 7 seconds of hearing "Sir?".

**TTS generates but no audio plays**
Check that macOS has granted microphone and audio output permissions to Terminal (System Preferences → Privacy & Security → Microphone).# ORACLE-JARVIS

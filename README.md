# Oracle

A local voice assistant for macOS. Say **"Oracle"** or **"Jarvis"** to activate it.

Built by [Rutul Gajjar](https://github.com/rutulgajjar). Open source and open to contributions — if you want to add a feature, fix a bug, or extend it in any direction, pull requests are welcome.

---

## What it does

- Plays music by searching YouTube (yt-dlp, no browser tab needed)
- Opens apps, websites, and YouTube videos on command
- Answers questions with a rolling conversation memory that persists across sessions
- Sets timers and reminders with voice and macOS notification callbacks
- Controls system volume, takes screenshots, locks the screen
- Remembers personal facts you tell it ("remember that my car is a Tesla")
- Floating status HUD in the corner of your screen (STANDBY / LISTENING / SPEAKING)
- Auto-sleeps after a configurable period of inactivity

---

## Requirements

```bash
pip install groq edge-tts SpeechRecognition pyaudio yt-dlp
brew install portaudio ffmpeg
```

Get a free Groq API key at [console.groq.com](https://console.groq.com).

---

## Setup

1. Clone the repo:
   ```bash
   git clone https://github.com/yourusername/oracle-assistant.git
   cd oracle-assistant
   ```

2. Install dependencies (see above).

3. Create your `.env` file:
   ```bash
   cp .env.example .env
   ```
   Open `.env` and fill in your values:
   ```
   GROQ_API_KEY=your_groq_api_key_here
   ORACLE_OWNER_NAME=Your Full Name
   ORACLE_OWNER_FIRST=YourFirstName
   ```

4. Run it:
   ```bash
   python oracle.py
   ```

### Auto-start at login
```bash
python oracle.py --install
```
To uninstall:
```bash
launchctl unload ~/Library/LaunchAgents/com.oracle.assistant.plist
rm ~/Library/LaunchAgents/com.oracle.assistant.plist
```

---

## Usage

Say **"Oracle"** or **"Jarvis"**, wait for **"Sir?"**, then speak your command.

| Command | What happens |
|---|---|
| "Play Blinding Lights" | Finds and plays audio via yt-dlp |
| "Play a song by Drake" | Same |
| "Play Sidemen on YouTube" | Opens YouTube search in browser |
| "Open VS Code" | Launches the app |
| "Open GitHub" | Opens github.com |
| "Search for SpaceX news" | Google search in browser |
| "Set a 10 minute timer" | Voice alert + macOS notification |
| "Remind me to call mum in 30 minutes" | Same |
| "What time is it" | Tells you the time |
| "Battery status" | Reports level and charging state |
| "Volume to 60" | Sets system volume |
| "Take a screenshot" | Saves to Desktop |
| "Lock the screen" | Locks macOS |
| "Start my workspace" | Opens VS Code + Claude, plays Paranoid |
| "Who are you" | Oracle introduces itself |
| "Shut down Oracle" | Exits cleanly |

---

## Configuration

All settings live at the top of `oracle.py`. Secrets go in `.env`.

```python
VOICE              = "en-GB-RyanNeural"   # any edge-tts voice
AUTO_SLEEP_MINUTES = 10                   # 0 to disable
TTS_RATE           = "+6%"               # voice speed
MAX_HISTORY_TURNS  = 20                   # conversation memory depth
```

---

## Architecture

| Thread | Role |
|---|---|
| Main thread | tkinter HUD only — never blocked |
| `oracle-worker` | Mic I/O, Whisper, LLM calls — serialised |
| `wake-capture` | Keeps microphone open, pushes clips to queue |
| `transcriber` | Whisper on wake clips, signals worker on detection |
| `tts-worker` | Asyncio loop — edge-tts generation + afplay |
| `media-player` | yt-dlp download + afplay per play request |
| Timer threads | One per active timer/reminder |

---

## Contributing

Contributions are welcome. A few ideas if you want to pick something up:

- Spotify playback control via AppleScript
- Calendar integration (read upcoming events)
- Weather lookups
- Custom wake words
- Windows / Linux support

To contribute:

1. Fork the repo
2. Create a branch: `git checkout -b feature/your-feature-name`
3. Make your changes
4. Open a pull request with a clear description of what you added

Please keep the code style consistent — plain comments, no AI-banner decorations, functions that do one thing.

---

## License

MIT — do whatever you want with it, just keep the credit.

---

## Troubleshooting

**Music finds the track but doesn't play**
Run `yt-dlp --version` and `ffmpeg -version`. If either is missing, install them. yt-dlp downloads to a temp file before playing — first start takes 5–15 seconds.

**Wake word not triggering**
Raise `energy_threshold` in `wake_capture_thread()` if there are false triggers, or lower it if it's not picking up your voice.

**TTS generates but no audio**
Check Terminal has microphone and audio permissions: System Settings → Privacy & Security → Microphone.
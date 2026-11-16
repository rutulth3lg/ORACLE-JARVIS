# Oracle

Local voice assistant for macOS. Say "Oracle" and it wakes up.

Threaded architecture, Groq LLM backend, persistent memory, media playback, and system automation — all running locally except the LLM calls.

Open source — PRs welcome if you want to add stuff.

---

## What It Does

- Does things on your Mac, not just talks. It can run commands, control apps with AppleScript, and read or write files, chaining steps until the job is done ("find the biggest files in my Downloads")
- Looks at your screen when you ask ("what's this error?")
- Searches the web and reads pages, then answers out loud
- Takes a command in one breath ("Oracle, open GitHub") and keeps listening after it answers, so you can follow up without the wake word
- Plays music by searching YouTube (yt-dlp, no browser needed)
- Opens apps, websites, YouTube videos
- Answers questions with conversation memory that persists across sessions
- Sets timers and reminders with voice + macOS notification callbacks
- Controls volume, takes screenshots, locks screen
- Remembers things you tell it ("remember that my car is a Tesla")
- Glowing orb in the corner that changes colour while it listens, thinks, works, and speaks (drag it anywhere)
- Warns you when the battery gets low
- Auto-sleeps after inactivity

## Setup

```bash
pip install groq edge-tts SpeechRecognition pyaudio yt-dlp
pip install pynput   # optional, for the Cmd+Shift+Space hotkey
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

The first run, macOS asks for permissions. Give Terminal access to the Microphone, and to Screen Recording if you want it to see your screen (System Settings > Privacy & Security). It will also ask the first time Oracle controls another app.

### Auto-start at login
```bash
python oracle.py --install
```

## Usage

Say **"Oracle"** or **"Jarvis"**, wait for **"Sir?"**, then talk. Or say it all at once: "Oracle, what's the weather". After it answers you get a few seconds to keep talking without the wake word. Say "thanks" or stay quiet to end it.

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
| "What's on my screen?" | Takes a screenshot and describes it |
| "Find the biggest files in my Downloads" | Runs the commands and tells you |
| "What's the latest on the SpaceX launch?" | Searches the web, reads, answers |
| "Add milk to my reminders" | Adds it in the Reminders app |
| "Delete the zip files on my Desktop" | Asks you to confirm, then does it |
| "Write a script that renames my screenshots" | Writes the file where you ask |
| "Git status" / "Git push" | Runs git in your project |

## Safety

Oracle can run real commands, so it has rules about when to ask first.

- Looking is free: listing files, reading, checking system state.
- Anything that deletes, overwrites, sends, or runs something unfamiliar gets read back to you first, and only a clear "yes" goes ahead. Silence counts as no.
- Some things it will not do by voice at all: `sudo`, wiping a disk, deleting your home folder.
- Once it has read something from the web in a request, every action after that asks first. Web pages can try to trick an AI into doing things.
- The exact command is printed in the terminal, so you can see what you're agreeing to.

Screenshots and anything it reads to answer you are sent to Groq, same as your voice.

## How It Works

- **Wake word** — Continuously listens for "Oracle"/"Jarvis" using Google Speech Recognition
- **LLM** — Groq API. Oracle picks the best model your account has (set `GROQ_MODEL` in `.env` to choose)
- **Tools** — The model calls real functions (shell, AppleScript, files, web, screen) in a loop until it has an answer
- **Vision** — A Groq vision model reads screenshots
- **TTS** — Edge-TTS for natural voice output
- **Music** — yt-dlp searches YouTube, streams audio directly
- **Memory** — JSON-based persistent memory for facts + conversation history
- **HUD** — Tkinter orb, always on top, updates state in real time

## Tech

Python · Groq API · Edge-TTS · SpeechRecognition · yt-dlp · Tkinter

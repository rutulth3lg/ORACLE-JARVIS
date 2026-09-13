# Oracle

Oracle is a voice assistant that runs on your Mac. You say "Oracle" (or "Jarvis"), it answers, and you talk to it like you'd talk to a person. It plays music, opens apps and sites, sets timers, remembers things you tell it, controls your Mac, and answers questions out loud. Everything runs locally except the AI brain, which uses a free Groq API key.

There's a small glowing orb that sits in the corner of your screen. It breathes slowly when it's idle and lights up while it's listening, thinking, and talking.

## What you need first

You need four things before anything works. Don't skip these, most "it doesn't run" problems come from one of them being missing.

1. A Mac. Oracle uses Mac-only tools (`afplay`, `osascript`, `screencapture`), so it won't run on Windows or Linux as-is.
2. Python 3. Check by running `python3 --version` in Terminal. If you get a version number, you're fine. If not, install it from [python.org](https://www.python.org/downloads/).
3. Homebrew. This is the tool that installs the audio bits. If `brew --version` prints nothing, install it from [brew.sh](https://brew.sh) and follow its instructions.
4. A free Groq API key. Go to [console.groq.com](https://console.groq.com), sign in, open "API Keys", and create one. Copy it somewhere safe, you'll paste it in a minute.

## Setup

Open Terminal and do these one at a time.

### 1. Get the code

```bash
git clone https://github.com/rutulth3lg/oracle-jarvis.git
cd oracle-jarvis
```

### 2. Install the audio tools

```bash
brew install portaudio ffmpeg
```

`portaudio` is what lets Python hear your microphone. `ffmpeg` is what lets music play. If either is missing, the matching feature just won't work.

### 3. Install the Python packages

```bash
pip3 install groq edge-tts SpeechRecognition pyaudio yt-dlp pynput
```

`pynput` is optional. It's only there for the keyboard shortcut that wakes Oracle without speaking. Everything else is required. If `pyaudio` fails to install, it almost always means step 2 didn't finish, so run `brew install portaudio` again and retry.

### 4. Add your key and name

```bash
cp .env.example .env
open -e .env
```

That copies the example settings file and opens it in TextEdit. Fill in your Groq key and your name:

```
GROQ_API_KEY=paste_your_key_here
ORACLE_OWNER_NAME=Your Full Name
ORACLE_OWNER_FIRST=YourFirstName
```

`ORACLE_OWNER_FIRST` is what Oracle calls you out loud. Save the file and close it. Your real key stays in `.env`, which git ignores, so it never gets uploaded anywhere.

### 5. Run it

```bash
python3 oracle.py
```

It'll say a greeting and show the orb. Say "Oracle" or "Jarvis", wait for "Sir?", then give a command. To stop it, press Control and C in Terminal, or just say "shut down Oracle".

## The first time you run it

macOS will pop up permission requests the first time Oracle needs your mic and keyboard. This is normal. Go to System Settings, then Privacy and Security, and turn on Terminal (or whatever app you launched from) under Microphone. If you want the keyboard shortcut, turn it on under Accessibility too. Then quit Oracle and start it again.

On startup you should see a line like `[Model] Using ...`. That's Oracle picking an AI model your account can use. If you ever see a model error instead, tell it which model to use by setting `GROQ_MODEL` in your `.env`, or just leave that line blank and let it choose.

## Things you can say

Say "Oracle" or "Jarvis", wait for the reply, then speak.

| Command | What happens |
|---|---|
| "Play Blinding Lights" | Finds and plays the audio, no browser needed |
| "Play a song by Drake" | Same |
| "Play Sidemen on YouTube" | Opens a YouTube search in the browser |
| "Open VS Code" | Launches the app |
| "Open GitHub" | Opens github.com |
| "Search for SpaceX news" | Google search in the browser |
| "Set a 10 minute timer" | Voice alert plus a Mac notification |
| "Remind me to call mum in 30 minutes" | Same |
| "What time is it" | Tells you the time |
| "Battery status" | Reports the level and whether it's charging |
| "Volume to 60" | Sets the system volume |
| "Take a screenshot" | Saves it to your Desktop |
| "Lock the screen" | Locks the Mac |
| "Start my workspace" | Opens VS Code and Claude, plays a track |
| "Who are you" | Oracle introduces itself |
| "Shut down Oracle" | Exits cleanly |

There are more. These run right on your Mac with no AI call, so they're instant:

| Command | What happens |
|---|---|
| "Git status", "Git push", "Git log" | Runs git in your current project, hands-free |
| "Git commit fixed the parser" | Stages everything and commits with that message |
| "Set git repo to ~/code/myapp" | Points Oracle at a specific project folder |
| "Read this to me" | Reads whatever you copied out loud |
| "Stop reading" | Cuts off a read-aloud straight away |
| "Export session" | Saves the conversation to a markdown file on your Desktop |
| "Start focus mode" | Blocks distracting sites for a work block |
| "Start a pomodoro" | Runs a 25 and 5 pomodoro cycle with voice cues |
| "Every morning at 8 remind me to check email" | Sets a repeating daily reminder |
| "Do this then that then the other" | Chains several commands in one go |
| "When I say launch, open VS Code" | Makes your own custom voice shortcut |

Anything that isn't one of these gets sent to the AI, and Oracle answers you out loud.

## Start it automatically at login

If you want Oracle running every time you log in:

```bash
python3 oracle.py --install
```

To undo that:

```bash
launchctl unload ~/Library/LaunchAgents/com.oracle.assistant.plist
rm ~/Library/LaunchAgents/com.oracle.assistant.plist
```

## Settings

The knobs live at the top of `oracle.py`, and your secrets live in `.env`.

```python
VOICE              = "en-GB-RyanNeural"   # any edge-tts voice
AUTO_SLEEP_MINUTES = 10                   # set to 0 to never sleep
TTS_RATE           = "+6%"                # how fast it talks
MAX_HISTORY_TURNS  = 20                   # how much conversation it remembers
```

In `.env` you can also set `GROQ_MODEL` to force a specific model, or `ORACLE_MORNING_BRIEFING=true` if you want a short spoken rundown the first time you wake it each morning. Both are off or automatic by default.

## When something goes wrong

Music finds the song but nothing plays. Run `yt-dlp --version` and `ffmpeg -version`. If either one errors, install it. The first play takes a few seconds because it downloads the audio before playing.

It doesn't hear you. Make sure Terminal has microphone access in System Settings, Privacy and Security, Microphone. If it keeps triggering on background noise, or never triggers, the sensitivity is set in `wake_capture_thread()` inside `oracle.py`. Raise the number if it's too jumpy, lower it if it's too deaf.

It answers in text but says nothing. Check that Terminal has audio permission and your volume is up. If the log shows a model error, set `GROQ_MODEL` in `.env` as described above.

The orb isn't showing. Look at the bottom-right corner, it's small on purpose. Some macOS versions are stubborn about borderless windows, so if it still won't appear, let me know and I'll switch it to a mode that always shows.

## How it's built

Oracle is one Python file. It runs a few background threads so the microphone, the AI, the voice, and the orb never block each other.

| Thread | Job |
|---|---|
| Main thread | Draws the orb, nothing else |
| `oracle-worker` | Handles one command at a time: mic, transcription, AI |
| `wake-capture` | Keeps the mic open and listens for the wake word |
| `transcriber` | Turns wake clips into text and signals the worker |
| `tts-worker` | Generates speech and plays it |
| `media-player` | Downloads and plays music per request |

## Contributing

Pull requests are welcome. A few ideas if you want to build something:

- Spotify control through AppleScript
- Reading your calendar
- Weather lookups
- Custom wake words
- Windows or Linux support

The flow is the usual one: fork it, make a branch, do your thing, open a pull request that says what you changed. Keep the style like the rest of the file, plain comments, small functions that each do one job.

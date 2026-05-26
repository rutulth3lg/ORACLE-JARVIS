# oracle.py
#
# Oracle — personal voice assistant for macOS
# Built by Rutul Gajjar
#
# Say "Oracle" or "Jarvis" to wake it up.
# Stays alive until you say "shutdown" or it auto-sleeps after inactivity.
#
# Requirements:
#   pip install groq edge-tts SpeechRecognition pyaudio yt-dlp
#   brew install portaudio ffmpeg
#
# To run:
#   python oracle.py
#
# To install as a login service (starts automatically at boot):
#   python oracle.py --install

import os
import sys
import time
import re
import json
import subprocess
import threading
import random
import queue
import asyncio
import datetime
import tkinter as tk
from tkinter import font as tkfont

import speech_recognition as sr
from groq import Groq
import edge_tts

# Load .env file so secrets never have to be hardcoded in this file.
# Create a .env in the same folder as oracle.py with:
#   GROQ_API_KEY=your_key_here
#   ORACLE_OWNER_NAME=Your Full Name
#   ORACLE_OWNER_FIRST=YourFirstName
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_env_path):
    with open(_env_path) as _ef:
        for _line in _ef:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))


# ---------------------------------------------------------------------------
# Configuration — set secrets in .env, preferences here
# ---------------------------------------------------------------------------

GROQ_API_KEY   = os.environ.get("GROQ_API_KEY", "")
OWNER_NAME     = os.environ.get("ORACLE_OWNER_NAME", "Your Name")
OWNER_FIRST    = os.environ.get("ORACLE_OWNER_FIRST", "Sir")

VOICE              = "en-GB-RyanNeural"
DOCS_DIR           = os.path.expanduser("~/Documents")
MEMORY_FILE        = os.path.join(DOCS_DIR, "oracle_memory.json")
TEMP_AUDIO_DIR     = os.path.join(DOCS_DIR, "oracle_tmp")

TTS_RATE           = "+6%"      # edge-tts speed — tweak if voice feels off
TTS_AFPLAY_SPEED   = "1.0"      # afplay -r multiplier
MAX_HISTORY_TURNS  = 20         # how many back-and-forth turns to keep in LLM context

# Auto-sleep: Oracle shuts down after this many minutes of silence.
# Set to 0 to disable.
AUTO_SLEEP_MINUTES = 10

# Groq client (single shared instance)
groq_client = Groq(api_key=GROQ_API_KEY)

# Make sure our temp audio folder exists
os.makedirs(TEMP_AUDIO_DIR, exist_ok=True)


# ---------------------------------------------------------------------------
# HUD — the small floating status window in the corner of the screen
# ---------------------------------------------------------------------------

# Any thread can call set_hud(state). The main thread picks it up via after().
_hud_queue: queue.Queue = queue.Queue()

# Each state maps to: (display text, window bg, frame bg, text color)
HUD_CONFIG = {
    "standby":    ("● STANDBY",    "#0d0d0d", "#0d0d0d", "#3a3a7a"),
    "listening":  ("◉ LISTENING",  "#0d0d0d", "#001500", "#00ff41"),
    "processing": ("⟳ PROCESSING", "#0d0d0d", "#1a0d00", "#ff9500"),
    "speaking":   ("▶ SPEAKING",   "#0d0d0d", "#00101a", "#0099ff"),
    "waking":     ("◎ WAKE",       "#0d0d0d", "#1a0010", "#ff0055"),
    "sleeping":   ("◌ SLEEPING",   "#050505", "#050505", "#222244"),
}


def set_hud(state: str):
    """Thread-safe HUD state change. Fine to call from any thread."""
    _hud_queue.put(state)


class OracleHUD:
    """
    Frameless always-on-top overlay in the bottom-right corner.
    All drawing happens on the main (tkinter) thread via after() polling.
    Never call tkinter methods from any other thread.
    """

    def __init__(self, root: tk.Tk):
        self.root        = root
        self._state      = "standby"
        self._pulse_job  = None

        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.attributes("-alpha", 0.90)
        root.configure(bg="#0d0d0d")

        screen_w = root.winfo_screenwidth()
        screen_h = root.winfo_screenheight()
        win_w, win_h = 230, 46
        root.geometry(f"{win_w}x{win_h}+{screen_w - win_w - 18}+{screen_h - win_h - 58}")

        try:
            label_font = tkfont.Font(family="SF Pro Display", size=11, weight="bold")
        except Exception:
            label_font = tkfont.Font(family="Helvetica Neue", size=11, weight="bold")

        self.frame = tk.Frame(
            root, bg="#0d0d0d",
            highlightbackground="#333366", highlightthickness=1
        )
        self.frame.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)

        self.label = tk.Label(
            self.frame, text="● STANDBY",
            font=label_font, fg="#3a3a7a", bg="#0d0d0d",
            padx=14, pady=11
        )
        self.label.pack(side=tk.LEFT)

        # Start the 60 ms polling loop
        self.root.after(60, self._poll)

    def _poll(self):
        changed = False
        while not _hud_queue.empty():
            try:
                new_state = _hud_queue.get_nowait()
                if new_state != self._state:
                    self._state = new_state
                    changed = True
            except queue.Empty:
                break
        if changed:
            self._apply()
        self.root.after(60, self._poll)

    def _apply(self):
        cfg = HUD_CONFIG.get(self._state, HUD_CONFIG["standby"])
        label_text, win_bg, frame_bg, fg = cfg

        self.root.configure(bg=win_bg)
        self.frame.configure(bg=frame_bg, highlightbackground=fg + "44")
        self.label.configure(text=label_text, fg=fg, bg=frame_bg)

        if self._pulse_job:
            self.root.after_cancel(self._pulse_job)
            self._pulse_job = None

        if self._state in ("listening", "processing", "waking"):
            self._start_pulse(fg)

    def _start_pulse(self, fg: str):
        if self._state not in ("listening", "processing", "waking"):
            return
        current = self.label.cget("fg")
        dimmed  = fg + "55"
        self.label.configure(fg=(fg if current != fg else dimmed))
        self._pulse_job = self.root.after(380, lambda: self._start_pulse(fg))


# ---------------------------------------------------------------------------
# Persistent memory — conversation history + named facts survive restarts
# ---------------------------------------------------------------------------

_conversation_history: list[dict] = []
_named_facts:          dict       = {}
_memory_lock                      = threading.Lock()


def load_memory():
    global _conversation_history, _named_facts
    if not os.path.exists(MEMORY_FILE):
        return
    try:
        with open(MEMORY_FILE) as f:
            data = json.load(f)
        with _memory_lock:
            _conversation_history = data.get("history", [])[-MAX_HISTORY_TURNS * 2:]
            _named_facts          = data.get("facts",   {})
        print(f"[Memory] Loaded {len(_conversation_history)//2} turns, "
              f"{len(_named_facts)} stored facts.")
    except Exception as e:
        print(f"[Memory] Could not load: {e}")


def save_memory():
    """Write memory to disk on a background thread so we never block the worker."""
    def _write():
        with _memory_lock:
            payload = {
                "history":   list(_conversation_history),
                "facts":     dict(_named_facts),
                "saved_at":  datetime.datetime.now().isoformat(),
            }
        try:
            with open(MEMORY_FILE, "w") as f:
                json.dump(payload, f, indent=2)
        except Exception as e:
            print(f"[Memory] Could not save: {e}")
    threading.Thread(target=_write, daemon=True).start()


def add_to_history(role: str, content: str):
    with _memory_lock:
        _conversation_history.append({"role": role, "content": content})
        if len(_conversation_history) > MAX_HISTORY_TURNS * 2:
            _conversation_history[:] = _conversation_history[-MAX_HISTORY_TURNS * 2:]
    save_memory()


def store_fact(key: str, value: str):
    with _memory_lock:
        _named_facts[key] = value
    save_memory()


def facts_context_block() -> str:
    with _memory_lock:
        if not _named_facts:
            return ""
        lines = "\n".join(f"  {k}: {v}" for k, v in _named_facts.items())
        return f"\n\nThings Oracle knows about {OWNER_FIRST}:\n{lines}"


def build_llm_messages(user_text: str) -> list[dict]:
    with _memory_lock:
        history_copy = list(_conversation_history)
    system = SYSTEM_PROMPT + facts_context_block()
    return [{"role": "system", "content": system}, *history_copy,
            {"role": "user",   "content": user_text}]


# ---------------------------------------------------------------------------
# Global audio state
# ---------------------------------------------------------------------------

# TTS pipeline controls
stop_tts_flag   = threading.Event()
_tts_afplay     = None
_tts_afplay_lk  = threading.Lock()

# Music/media pipeline controls
stop_media_flag = threading.Event()
_media_proc     = None
_media_lk       = threading.Lock()
_media_ffmpeg   = None   # keep a handle so we can kill ffmpeg too

# The TTS asyncio event loop lives on its own thread
_tts_event_loop: asyncio.AbstractEventLoop = asyncio.new_event_loop()
_tts_queue:      queue.Queue               = queue.Queue()

# Wake-word pipeline
_raw_audio_queue:  queue.Queue = queue.Queue()   # wake-capture → transcriber
_wake_event_queue: queue.Queue = queue.Queue()   # transcriber  → oracle-worker

# Auto-sleep
_last_activity_time = time.time()


# ---------------------------------------------------------------------------
# Text sanitisation — strip tags and markdown before speaking
# ---------------------------------------------------------------------------

_ACTION_TAG_RE = re.compile(r'\[ACTION:[^\]]*\]', re.IGNORECASE)


def sanitize_for_speech(text: str) -> str:
    text = _ACTION_TAG_RE.sub("", text)
    for old, new in [("**",""),("*",""),('`',""),("#",""),("—",", "),("–",", ")]:
        text = text.replace(old, new)
    text = re.sub(r'\be\.g\.\b', "for example", text, flags=re.IGNORECASE)
    text = re.sub(r'\bi\.e\.\b', "that is",     text, flags=re.IGNORECASE)
    text = re.sub(r'\bvs\.?\b',  "versus",      text, flags=re.IGNORECASE)
    text = re.sub(r'\betc\.?\b', "et cetera",   text, flags=re.IGNORECASE)
    text = re.sub(r'https?://\S+', "", text)
    text = re.sub(r'\s{2,}', " ", text)
    return text.strip()


def has_unclosed_bracket(text: str) -> bool:
    """True if there's an opening [ without a matching ] — means an ACTION tag is mid-stream."""
    return text.count("[") > text.count("]")


# ---------------------------------------------------------------------------
# TTS pipeline
#
# Architecture: a single persistent asyncio loop on its own thread.
# speak() enqueues a (text, filepath) pair; the loop generates the mp3 with
# edge-tts, then plays it with afplay.
#
# The double-buffer pattern: while sentence N is playing, sentence N+1 is
# already being generated. This gives zero audible gap between sentences.
#
# task_done() accounting: every item put() into _tts_queue gets exactly
# one task_done() call regardless of which code path runs.
# ---------------------------------------------------------------------------

def _kill_tts_afplay():
    global _tts_afplay
    with _tts_afplay_lk:
        if _tts_afplay and _tts_afplay.poll() is None:
            _tts_afplay.terminate()
            try:
                _tts_afplay.wait(0.3)
            except Exception:
                pass
        _tts_afplay = None


def force_stop_tts():
    """Kill current TTS playback and discard everything queued."""
    stop_tts_flag.set()
    _kill_tts_afplay()
    drained = 0
    while not _tts_queue.empty():
        try:
            _tts_queue.get_nowait()
            _tts_queue.task_done()
            drained += 1
        except queue.Empty:
            break


def _afplay_tts_file(filepath: str):
    """Play one TTS mp3 via afplay. Blocks the TTS loop thread until done."""
    global _tts_afplay
    if stop_tts_flag.is_set():
        try:
            os.remove(filepath)
        except OSError:
            pass
        return
    try:
        with _tts_afplay_lk:
            if stop_tts_flag.is_set():
                try:
                    os.remove(filepath)
                except OSError:
                    pass
                return
            proc = subprocess.Popen(
                ["afplay", "-r", TTS_AFPLAY_SPEED, filepath],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            _tts_afplay = proc
        proc.wait()
        with _tts_afplay_lk:
            _tts_afplay = None
    finally:
        try:
            os.remove(filepath)
        except OSError:
            pass


def _run_tts_event_loop():
    asyncio.set_event_loop(_tts_event_loop)
    _tts_event_loop.run_until_complete(_tts_pipeline())


async def _generate_tts(text: str, filepath: str) -> bool:
    """Generate one TTS file. Returns True on success."""
    try:
        await edge_tts.Communicate(text, VOICE, rate=TTS_RATE, volume="+8%").save(filepath)
        return True
    except Exception as e:
        print(f"[TTS generate] {e}")
        return False


async def _tts_pipeline():
    """
    Double-buffer pipeline.
    'ready' holds a pre-generated file that is waiting to be played.
    While the ready file plays, we generate the next one from the queue.
    Every item that goes through has task_done() called exactly once.
    """
    ready: tuple | None = None   # (text, filepath) generated, not yet played

    while True:
        # --- fetch the next item to generate ---
        try:
            text, filepath = _tts_queue.get_nowait()
        except queue.Empty:
            if ready is None:
                await asyncio.sleep(0.015)
                continue
            # Queue drained — just play whatever is ready
            text, filepath = None, None

        if text is not None:
            # Skip generation if we were interrupted
            if stop_tts_flag.is_set():
                _tts_queue.task_done()
                if ready is not None:
                    try:
                        os.remove(ready[1])
                    except OSError:
                        pass
                    ready = None
                continue

            ok = await _generate_tts(text, filepath)
            if not ok:
                _tts_queue.task_done()
                continue

            # Play the previously-ready item while this new one sits on deck
            if ready is not None:
                if not stop_tts_flag.is_set():
                    await _tts_event_loop.run_in_executor(
                        None, _afplay_tts_file, ready[1]
                    )
                else:
                    try:
                        os.remove(ready[1])
                    except OSError:
                        pass
                # task_done for the item that just played (it came from the queue earlier)
                _tts_queue.task_done()

            # This new item becomes the ready slot
            ready = (text, filepath)

        else:
            # No more items — play the last ready item
            if ready is not None:
                if not stop_tts_flag.is_set():
                    await _tts_event_loop.run_in_executor(
                        None, _afplay_tts_file, ready[1]
                    )
                else:
                    try:
                        os.remove(ready[1])
                    except OSError:
                        pass
                _tts_queue.task_done()
                ready = None


def speak(text: str):
    """Enqueue text for TTS. Non-blocking. Prints to terminal too."""
    clean = sanitize_for_speech(text)
    if not clean:
        return
    print(f"Oracle: {clean}")
    set_hud("speaking")
    filepath = os.path.join(
        TEMP_AUDIO_DIR,
        f"tts_{int(time.time() * 1000)}_{random.randint(100, 999)}.mp3"
    )
    _tts_queue.put((clean, filepath))


def speak_blocking(text: str):
    """Enqueue text and block until it has finished playing."""
    speak(text)
    _tts_queue.join()


# ---------------------------------------------------------------------------
# Media player — yt-dlp + afplay
#
# The right approach for reliable playback on macOS:
#   1. yt-dlp downloads the best audio to a temp file (-x --audio-format mp3)
#   2. afplay plays the file
#
# Streaming via ffmpeg pipes was unreliable because YouTube CDN URLs require
# specific cookies/headers that ffmpeg doesn't handle out of the box.
# Downloading to a temp file first is slower to start (~5-15s) but 100% reliable.
# ---------------------------------------------------------------------------

def stop_media():
    global _media_proc, _media_ffmpeg
    stop_media_flag.set()
    with _media_lk:
        for proc in (_media_proc, _media_ffmpeg):
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(0.5)
                except Exception:
                    pass
        _media_proc   = None
        _media_ffmpeg = None


def _play_audio_worker(query: str):
    global _media_proc
    stop_media_flag.clear()

    print(f"[Media] Fetching: {query}")
    set_hud("processing")

    # Verify yt-dlp is installed
    try:
        subprocess.run(["yt-dlp", "--version"], capture_output=True, timeout=4, check=True)
    except Exception:
        speak("yt-dlp is not installed. Run: pip install yt-dlp")
        set_hud("standby")
        return

    temp_file = os.path.join(TEMP_AUDIO_DIR, f"media_{int(time.time())}.%(ext)s")
    final_file = temp_file.replace(".%(ext)s", ".mp3")

    try:
        # Download to temp file — much more reliable than streaming
        dl = subprocess.run(
            [
                "yt-dlp",
                f"ytsearch1:{query}",
                "-x",
                "--audio-format", "mp3",
                "--audio-quality", "0",
                "--no-playlist",
                "--quiet",
                "--no-warnings",
                "-o", temp_file,
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )

        if dl.returncode != 0 or not os.path.exists(final_file):
            # yt-dlp sometimes uses a different extension — find what it made
            candidates = [
                f for f in os.listdir(TEMP_AUDIO_DIR)
                if f.startswith(f"media_{int(time.time())}"[:-3])
                and not f.endswith(".%(ext)s")
            ]
            if candidates:
                final_file = os.path.join(TEMP_AUDIO_DIR, candidates[0])
            else:
                speak(f"I couldn't find that track, {OWNER_FIRST}. Try a different search.")
                set_hud("standby")
                return

        if not os.path.exists(final_file):
            speak("The download completed but the file is missing. Try again, Sir.")
            set_hud("standby")
            return

        print(f"[Media] Playing: {final_file}")
        set_hud("speaking")

        proc = subprocess.Popen(
            ["afplay", final_file],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        with _media_lk:
            _media_proc = proc

        proc.wait()

        with _media_lk:
            _media_proc = None

        # Clean up the temp file after playback
        try:
            os.remove(final_file)
        except OSError:
            pass

        set_hud("standby")

    except subprocess.TimeoutExpired:
        speak("That search is taking too long. Try again, Sir.")
        set_hud("standby")
    except Exception as e:
        print(f"[Media error] {e}")
        speak("Something went wrong with playback, Sir.")
        set_hud("standby")


def play_audio(query: str):
    """Start audio playback on a background thread. Stops any current playback first."""
    stop_media()
    threading.Thread(
        target=_play_audio_worker,
        args=(query,),
        name="media-player",
        daemon=True
    ).start()


def open_in_browser(url: str):
    subprocess.Popen(["open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def open_youtube(query: str):
    """Open YouTube search results in the browser (correct for video requests)."""
    url = "https://www.youtube.com/results?search_query=" + query.replace(" ", "+")
    open_in_browser(url)


# ---------------------------------------------------------------------------
# macOS system helpers
# ---------------------------------------------------------------------------

def run_applescript(script: str):
    subprocess.Popen(
        ["osascript", "-e", script],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )


def launch_app(name: str):
    app = NATIVE_APPS.get(name.lower().strip(), name.title())
    subprocess.Popen(
        ["open", "-a", app],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )


def get_volume() -> int:
    try:
        result = subprocess.run(
            ["osascript", "-e", "output volume of (get volume settings)"],
            capture_output=True, text=True, timeout=3
        )
        return int(result.stdout.strip())
    except Exception:
        return 50


def set_volume(level: int):
    run_applescript(f"set volume output volume {max(0, min(100, level))}")


def get_wifi_name() -> str:
    # system_profiler is the most reliable across macOS versions
    try:
        result = subprocess.run(
            ["system_profiler", "SPAirPortDataType"],
            capture_output=True, text=True, timeout=6
        )
        match = re.search(r"Current Network Information:\s+(.+?):", result.stdout)
        if match:
            return match.group(1).strip()
    except Exception:
        pass
    # Fallback for older macOS
    for interface in ("en0", "en1"):
        try:
            result = subprocess.run(
                ["networksetup", "-getairportnetwork", interface],
                capture_output=True, text=True, timeout=4
            )
            if "not associated" not in result.stdout.lower():
                match = re.search(r"Network:\s*(.+)", result.stdout)
                if match:
                    return match.group(1).strip()
        except Exception:
            pass
    return "unknown network"


def get_battery_status() -> str:
    try:
        result = subprocess.run(
            ["pmset", "-g", "batt"],
            capture_output=True, text=True, timeout=4
        )
        match    = re.search(r"(\d+)%", result.stdout)
        charging = "ac power" in result.stdout.lower() or "charging" in result.stdout.lower()
        if match:
            pct    = match.group(1)
            status = "charging" if charging else "on battery"
            return f"{pct}%, {status}"
    except Exception:
        pass
    return "unavailable"


def fire_notification(title: str, body: str):
    run_applescript(f'display notification "{body}" with title "{title}"')


def set_timer(seconds: int, label: str):
    def _fire():
        time.sleep(seconds)
        fire_notification("Oracle", f"Timer: {label}")
        speak(f"Sir, your {label} timer is up.")
    threading.Thread(target=_fire, daemon=True).start()


def set_reminder(task: str, seconds: int):
    def _fire():
        time.sleep(seconds)
        fire_notification("Oracle", f"Reminder: {task}")
        speak(f"Sir, just a reminder to {task}.")
    threading.Thread(target=_fire, daemon=True).start()


# ---------------------------------------------------------------------------
# Website and app lookup tables
# Multi-word keys must come before their single-word prefixes so the
# longest-match-first sort resolves "youtube music" before "youtube".
# ---------------------------------------------------------------------------

WEBSITES: dict[str, str] = {
    "youtube music":  "https://music.youtube.com",
    "yt music":       "https://music.youtube.com",
    "google meet":    "https://meet.google.com",
    "google docs":    "https://docs.google.com",
    "google sheets":  "https://sheets.google.com",
    "google drive":   "https://drive.google.com",
    "google maps":    "https://maps.google.com",
    "hacker news":    "https://news.ycombinator.com",
    "apple music":    "https://music.apple.com",
    "product hunt":   "https://www.producthunt.com",
    "yahoo finance":  "https://finance.yahoo.com",
    "youtube":        "https://www.youtube.com",
    "spotify":        "https://open.spotify.com",
    "soundcloud":     "https://soundcloud.com",
    "github":         "https://github.com",
    "google":         "https://www.google.com",
    "reddit":         "https://www.reddit.com",
    "twitter":        "https://twitter.com",
    "x":              "https://x.com",
    "netflix":        "https://www.netflix.com",
    "gmail":          "https://mail.google.com",
    "instagram":      "https://www.instagram.com",
    "linkedin":       "https://www.linkedin.com",
    "twitch":         "https://www.twitch.tv",
    "quantconnect":   "https://www.quantconnect.com",
    "amazon":         "https://www.amazon.com",
    "chatgpt":        "https://chat.openai.com",
    "claude":         "https://claude.ai",
    "perplexity":     "https://perplexity.ai",
    "wikipedia":      "https://www.wikipedia.org",
    "tradingview":    "https://www.tradingview.com",
    "bloomberg":      "https://www.bloomberg.com",
    "notion":         "https://www.notion.so",
    "figma":          "https://www.figma.com",
    "vercel":         "https://vercel.com",
    "supabase":       "https://supabase.com",
    "coinbase":       "https://www.coinbase.com",
    "binance":        "https://www.binance.com",
    "arxiv":          "https://arxiv.org",
    "stackoverflow":  "https://stackoverflow.com",
    "medium":         "https://medium.com",
    "whatsapp":       "https://web.whatsapp.com",
    "discord":        "https://discord.com/app",
    "slack":          "https://app.slack.com",
    "linear":         "https://linear.app",
    "anthropic":      "https://anthropic.com",
    "openai":         "https://openai.com",
    "groq":           "https://groq.com",
}

NATIVE_APPS: dict[str, str] = {
    "google chrome":       "Google Chrome",
    "visual studio code":  "Visual Studio Code",
    "microsoft word":      "Microsoft Word",
    "microsoft excel":     "Microsoft Excel",
    "microsoft powerpoint":"Microsoft PowerPoint",
    "apple music":         "Music",
    "system preferences":  "System Preferences",
    "system settings":     "System Preferences",
    "activity monitor":    "Activity Monitor",
    "quicktime":           "QuickTime Player",
    "chrome":              "Google Chrome",
    "safari":              "Safari",
    "firefox":             "Firefox",
    "brave":               "Brave Browser",
    "arc":                 "Arc",
    "terminal":            "Terminal",
    "iterm":               "iTerm",
    "iterm2":              "iTerm",
    "finder":              "Finder",
    "notes":               "Notes",
    "calendar":            "Calendar",
    "mail":                "Mail",
    "messages":            "Messages",
    "facetime":            "FaceTime",
    "photos":              "Photos",
    "music":               "Music",
    "podcasts":            "Podcasts",
    "xcode":               "Xcode",
    "vscode":              "Visual Studio Code",
    "cursor":              "Cursor",
    "word":                "Microsoft Word",
    "excel":               "Microsoft Excel",
    "powerpoint":          "Microsoft PowerPoint",
    "slack":               "Slack",
    "discord":             "Discord",
    "zoom":                "zoom.us",
    "teams":               "Microsoft Teams",
    "notion":              "Notion",
    "obsidian":            "Obsidian",
    "whatsapp":            "WhatsApp",
    "telegram":            "Telegram",
    "signal":              "Signal",
    "settings":            "System Preferences",
    "calculator":          "Calculator",
    "preview":             "Preview",
    "vlc":                 "VLC",
    "spotify":             "Spotify",
    "figma":               "Figma",
    "sketch":              "Sketch",
    "postman":             "Postman",
    "tableplus":           "TablePlus",
    "docker":              "Docker",
    "1password":           "1Password",
    "warp":                "Warp",
    "steam":               "Steam",
}


# ---------------------------------------------------------------------------
# Quick command handler — resolves obvious requests locally, no LLM call needed
# Returns True if handled, False to fall through to the LLM.
# ---------------------------------------------------------------------------

def handle_quick_command(raw_input: str) -> bool:
    text = raw_input.lower().strip().rstrip(".")

    # Shutdown
    if re.search(r"\b(shut down oracle|quit oracle|exit oracle|goodbye oracle|go offline)\b", text):
        speak_blocking("Shutting down. Goodbye, Sir.")
        sys.exit(0)

    # Introduction / who are you
    if re.search(r"\b(who are you|introduce yourself|what are you|your name)\b", text):
        # Split across multiple speak() calls so every sentence is queued
        # individually — the TTS pipeline is guaranteed to play all of them.
        speak(f"I'm Oracle, {OWNER_FIRST}'s personal AI assistant.")
        speak(
            "I run entirely on this Mac and handle everything from opening apps "
            "and playing music, to answering questions and setting reminders."
        )
        speak(
            f"I remember our conversations across sessions, and I keep track of "
            f"whatever {OWNER_FIRST} needs me to know."
        )
        speak("Think of me as a quieter, more capable version of JARVIS, Sir.")
        return True

    # Workspace ritual — only fires on explicit request, never on generic wake
    if re.search(r"\b(start my workspace|workspace mode|setup workspace)\b", text):
        def _ritual():
            # Open VS Code
            subprocess.Popen(
                ["open", "-a", "Visual Studio Code"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            time.sleep(0.5)
            # Open the Claude desktop app (falls back to claude.ai if not installed)
            claude_result = subprocess.run(
                ["open", "-a", "Claude"],
                capture_output=True
            )
            if claude_result.returncode != 0:
                subprocess.Popen(
                    ["open", "https://claude.ai"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
            time.sleep(0.3)
            speak("VS Code and Claude are open, Sir. Putting on your soundtrack.")
            play_audio("Iron Man Black Sabbath")
        threading.Thread(target=_ritual, daemon=True).start()
        return True

    # Stop music / media
    if re.search(
        r"\b(stop|pause)\b.*(music|song|audio|playing|video|media)\b"
        r"|\bstop playing\b|\bstop the music\b|\bstop media\b",
        text
    ):
        stop_media()
        speak("Stopped, Sir.")
        return True

    # Play audio — "play X", "play a song by X", "put on X"
    play_m = re.search(
        r"^(?:play|put on|start playing)\s+"
        r"(?:(?:a\s+)?(?:song|track|music)\s+(?:by|from|called|named)\s+)?"
        r"(?:something\s+by\s+)?"
        r"(.+?)(?:\s+on\s+spotify)?$",
        text
    )
    if play_m and "video" not in text and "youtube" not in text:
        query = play_m.group(1).strip()
        query = re.sub(r"^(a\s+)?(song|track|music)\s*$", "music", query)
        if "spotify" in text:
            open_in_browser("https://open.spotify.com/search/" + query.replace(" ", "%20"))
            speak(f"Searching Spotify for {query}, Sir.")
            return True
        speak(f"On it, Sir. Finding {query} now.")
        play_audio(query)
        return True

    # Play YouTube video — opens browser (video streaming via afplay is not reliable)
    yt_video_m = re.search(
        r"(?:play|show me|watch|find)\s+(.+?)\s+(?:on\s+)?(?:youtube|yt)\b"
        r"|(?:play|watch)\s+(?:a\s+)?(?:youtube\s+video|video\s+on\s+youtube)"
        r"(?:\s+(?:of|about)\s+)?(.+)?",
        text
    )
    if yt_video_m:
        query = (yt_video_m.group(1) or yt_video_m.group(2) or "trending").strip()
        open_youtube(query)
        speak(f"Opening {query} on YouTube, Sir.")
        return True

    # Open a website or app
    open_m = re.search(
        r"(?:open|go to|pull up|launch|take me to|navigate to)\s+(.+)", text
    )
    if open_m:
        target = open_m.group(1).strip().rstrip(".")
        for key in sorted(WEBSITES, key=len, reverse=True):
            if key in target:
                open_in_browser(WEBSITES[key])
                speak(f"Opening {key}, Sir.")
                return True
        for key in sorted(NATIVE_APPS, key=len, reverse=True):
            if key in target:
                launch_app(key)
                speak(f"Opening {key}, Sir.")
                return True
        # Last resort: treat as a bare domain
        bare = target.replace(" ", "")
        if re.match(r"^[a-zA-Z0-9.\-]+$", bare):
            if "." not in bare:
                bare += ".com"
            open_in_browser(f"https://{bare}")
            speak(f"Opening {target}, Sir.")
            return True

    # YouTube search (browse mode, not play)
    yt_search_m = re.search(
        r"(?:search|find|look up|show me)\s+(.+?)\s+(?:on\s+)?(?:youtube|yt)\b", text
    )
    if yt_search_m:
        query = yt_search_m.group(1).strip()
        open_youtube(query)
        speak(f"Searching {query} on YouTube, Sir.")
        return True

    # Web search
    search_m = re.search(r"(?:search|google|look up|find)\s+(?:for\s+)?(.+)", text)
    if search_m:
        query = search_m.group(1).strip().rstrip(".")
        open_in_browser("https://www.google.com/search?q=" + query.replace(" ", "+"))
        speak(f"Searching for {query}, Sir.")
        return True

    # Volume controls
    vol_num_m = re.search(r"(?:set\s+)?(?:the\s+)?volume\s+(?:to\s+)?(\d{1,3})", text)
    if vol_num_m:
        level = int(vol_num_m.group(1))
        set_volume(level)
        speak(f"Volume set to {level}, Sir.")
        return True
    if re.search(r"\bvolume\s+up\b|\bturn\s+(?:it\s+)?up\b|\braise\s+(?:the\s+)?volume\b", text):
        new = min(100, get_volume() + 15)
        set_volume(new)
        speak(f"Volume up to {new}, Sir.")
        return True
    if re.search(r"\bvolume\s+down\b|\bturn\s+(?:it\s+)?down\b|\blower\s+(?:the\s+)?volume\b", text):
        new = max(0, get_volume() - 15)
        set_volume(new)
        speak(f"Volume down to {new}, Sir.")
        return True
    if re.search(r"\b(mute|silence)\b", text) and "unmute" not in text:
        run_applescript("set volume output muted true")
        speak("Muted, Sir.")
        return True
    if re.search(r"\bunmute\b", text):
        run_applescript("set volume output muted false")
        speak("Unmuted, Sir.")
        return True

    # System status
    if re.search(r"\b(battery|charge level|power level)\b", text):
        speak(f"Battery is at {get_battery_status()}, Sir.")
        return True

    if re.search(r"\bwifi\b|\bnetwork name\b|\bwhat.*(?:network|wifi|connected)\b", text):
        speak(f"You're connected to {get_wifi_name()}, Sir.")
        return True

    if re.search(r"\b(what.*time|current time|time is it|the time)\b", text):
        t = datetime.datetime.now().strftime("%-I:%M %p")
        speak(f"It's {t}, Sir.")
        return True

    if re.search(r"\b(what.*date|today.*date|what day|the date)\b", text):
        d = datetime.datetime.now().strftime("%A, %B %-d, %Y")
        speak(f"Today is {d}, Sir.")
        return True

    # Timer
    timer_m = re.search(
        r"(?:set|start|create)?\s*(?:a\s+)?timer\s+(?:for\s+)?(\d+)\s*"
        r"(second|minute|hour|sec|min|hr)",
        text
    )
    if timer_m:
        n     = int(timer_m.group(1))
        unit  = timer_m.group(2)
        secs  = n * (3600 if "hour" in unit or unit == "hr"
                     else 60 if "min" in unit else 1)
        label = f"{n} {unit}{'s' if n > 1 else ''}"
        set_timer(secs, label)
        speak(f"Timer set for {label}, Sir.")
        return True

    # Reminder
    remind_m = re.search(
        r"remind\s+(?:me\s+)?(?:to\s+)?(.+?)\s+in\s+(\d+)\s*"
        r"(second|minute|hour|sec|min|hr)",
        text
    )
    if remind_m:
        task  = remind_m.group(1).strip()
        n     = int(remind_m.group(2))
        unit  = remind_m.group(3)
        secs  = n * (3600 if "hour" in unit or unit == "hr"
                     else 60 if "min" in unit else 1)
        label = f"{n} {unit}{'s' if n > 1 else ''}"
        set_reminder(task, secs)
        speak(f"I'll remind you to {task} in {label}, Sir.")
        return True

    # Screenshot
    if re.search(r"\b(screenshot|capture screen|screen shot|take a screenshot)\b", text):
        ts   = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        path = os.path.expanduser(f"~/Desktop/oracle_{ts}.png")
        subprocess.Popen(["screencapture", "-x", path])
        speak("Screenshot saved to your Desktop, Sir.")
        return True

    # Lock screen
    if re.search(r"\b(lock screen|lock the screen|lock my screen)\b", text):
        run_applescript(
            'tell application "System Events" to keystroke "q" '
            'using {command down, control down}'
        )
        speak("Screen locked, Sir.")
        return True

    # Remember a fact
    rem_m = re.search(r"remember\s+(?:that\s+)?(?:my\s+)?(.+?)\s+is\s+(.+)", text)
    if rem_m:
        key   = rem_m.group(1).strip().replace(" ", "_")
        value = rem_m.group(2).strip()
        store_fact(key, value)
        speak(f"Noted, Sir. Your {rem_m.group(1)} is {value}.")
        return True

    return False


# ---------------------------------------------------------------------------
# System prompt — what Oracle knows about itself and how to behave
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = f"""You are Oracle, the personal AI assistant of {OWNER_NAME}.

You were built to run locally on {OWNER_FIRST}'s Mac and you have a deep, loyal relationship with him. You know his name, you remember your conversations, and you treat every interaction as if it matters — because to him, it does.

Personality and voice:
Speak in smooth, confident, natural prose. Never use bullet points, numbered lists, markdown headers, or asterisks. Sound like a highly intelligent person who happens to know everything — warm, precise, never corporate or stiff. Keep responses appropriately concise: one sentence for simple lookups, two to four for anything that needs real explanation, more when depth is genuinely needed. Address {OWNER_FIRST} as "Sir" once per response, woven in naturally — not robotically tacked on to every line.

Hard rules:
- For simple factual questions (time, date, quick facts): give one clean answer and stop. No follow-up offers. No "shall I...".
- Never say "I cannot" or "I'm unable to." Oracle always finds a way.
- Never expose ACTION tag syntax in spoken text. The tags are silent system calls.
- You remember this conversation. Reference prior context naturally.

Real system capabilities:
When {OWNER_FIRST} asks you to DO something, embed the correct ACTION tag inline. It will be executed silently before the response is spoken.

Available actions:
  [ACTION:open_url:<full_url>]          open any URL in the browser
  [ACTION:open_app:<app_name>]          open a macOS application by name
  [ACTION:search_web:<query>]           Google search in browser
  [ACTION:search_youtube:<query>]       YouTube search results in browser
  [ACTION:play_audio:<query>]           play audio via yt-dlp (songs, podcasts)
  [ACTION:open_spotify]                 open the Spotify app
  [ACTION:volume_set:<0-100>]           set system volume level
  [ACTION:volume_up]                    raise system volume
  [ACTION:volume_down]                  lower system volume
  [ACTION:screenshot]                   take a screenshot
  [ACTION:lock_screen]                  lock the Mac
  [ACTION:stop_music]                   stop whatever is playing

Routing rules (follow these precisely):
  "play X" or "play a song" or "play something by X"  →  [ACTION:play_audio:<query>]
  "open YouTube" or "open [website]"                  →  [ACTION:open_url:<url>]
  "search X on YouTube" or "show me X on YouTube"     →  [ACTION:search_youtube:<query>]
  "search for X" / "google X"                         →  [ACTION:search_web:<query>]
  "open [app name]"                                   →  [ACTION:open_app:<name>]
  Never describe doing something without actually doing it.
  Speak the confirmation first, embed the tag after the spoken sentence.

Examples of correct responses:
  User: "Play Blinding Lights"
  Oracle: "Playing that now, Sir.[ACTION:play_audio:Blinding Lights The Weeknd]"

  User: "Open GitHub"
  Oracle: "Opening GitHub.[ACTION:open_url:https://github.com]"

  User: "Search SpaceX on YouTube"
  Oracle: "Here you go.[ACTION:search_youtube:SpaceX]"

  User: "What is the speed of light?"
  Oracle: "Approximately 299,792 kilometres per second in a vacuum, Sir."

  User: "Who are you?"
  Oracle: "I'm Oracle, {OWNER_FIRST}'s personal assistant. I run on this Mac and handle everything from answering questions to opening apps, playing music, setting reminders, and keeping track of what matters to you. Built to be useful, Sir — and I take that seriously."
"""


# ---------------------------------------------------------------------------
# Action execution — parse and run ACTION tags embedded in LLM responses
# ---------------------------------------------------------------------------

_ACTION_EXEC_RE = re.compile(r"\[ACTION:([a-zA-Z_]+):?([^\]]*)\]")


def execute_action_tags(text: str) -> str:
    """
    Find all ACTION tags in text, execute them, and return the clean spoken text.
    Called during LLM streaming so actions fire as soon as the tag is complete.
    """
    for match in _ACTION_EXEC_RE.finditer(text):
        tag     = match.group(1).lower()
        payload = match.group(2).strip()
        try:
            if tag == "open_url":
                open_in_browser(payload)
            elif tag == "open_app":
                launch_app(payload)
            elif tag == "search_web":
                open_in_browser("https://www.google.com/search?q=" + payload.replace(" ", "+"))
            elif tag == "search_youtube":
                open_youtube(payload)
            elif tag == "play_audio":
                play_audio(payload)
            elif tag == "open_spotify":
                launch_app("Spotify")
            elif tag == "volume_set":
                set_volume(int(payload))
            elif tag == "volume_up":
                set_volume(min(100, get_volume() + 10))
            elif tag == "volume_down":
                set_volume(max(0,   get_volume() - 10))
            elif tag == "screenshot":
                ts = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
                subprocess.Popen(
                    ["screencapture", "-x", os.path.expanduser(f"~/Desktop/oracle_{ts}.png")]
                )
            elif tag == "lock_screen":
                run_applescript(
                    'tell application "System Events" to keystroke "q" '
                    'using {command down, control down}'
                )
            elif tag == "stop_music":
                stop_media()
        except Exception as e:
            print(f"[Action error] {tag}({payload}): {e}")

    return sanitize_for_speech(text)


# ---------------------------------------------------------------------------
# LLM streaming response
#
# Sentence-level streaming with double-buffer TTS:
#   - First complete sentence is spoken immediately (minimises perceived latency)
#   - Subsequent sentences are batched in pairs (better prosody from edge-tts)
#   - ACTION tags are buffered until closed before flushing to avoid partial execution
# ---------------------------------------------------------------------------

def get_llm_response(user_text: str):
    stop_tts_flag.clear()
    set_hud("processing")

    response_parts:  list[str] = []
    sentence_buffer: list[str] = []
    token_buffer                = ""
    first_sentence_spoken       = False

    try:
        stream = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=build_llm_messages(user_text),
            temperature=0.55,
            max_tokens=700,
            stream=True,
        )

        for chunk in stream:
            if stop_tts_flag.is_set():
                break

            delta = chunk.choices[0].delta.content or ""
            token_buffer  += delta
            response_parts.append(delta)

            # Hold off flushing while an ACTION tag is still open
            if has_unclosed_bracket(token_buffer):
                continue

            # Extract all complete sentences from the buffer
            while True:
                match = re.search(r"(?<=[.!?])\s+", token_buffer)
                if not match:
                    break
                sentence     = token_buffer[:match.start() + 1].strip()
                token_buffer = token_buffer[match.end():]

                if not sentence:
                    continue

                # Execute any actions embedded in this sentence, get clean text
                clean = execute_action_tags(sentence)
                if not clean:
                    continue

                sentence_buffer.append(clean)

                # Flush the very first sentence immediately for low latency
                if not first_sentence_spoken:
                    speak(sentence_buffer[0])
                    sentence_buffer       = sentence_buffer[1:]
                    first_sentence_spoken = True
                elif len(sentence_buffer) >= 2:
                    # Batch pairs — gives edge-tts better prosody context
                    speak(" ".join(sentence_buffer))
                    sentence_buffer = []

        # Flush whatever is left
        if token_buffer.strip():
            clean = execute_action_tags(token_buffer.strip())
            if clean:
                sentence_buffer.append(clean)

        if sentence_buffer:
            speak(" ".join(sentence_buffer))

    except Exception as e:
        print(f"[LLM error] {e}")
        speak("I hit a processing error, Sir. Please try again.")
        return

    full_response = "".join(response_parts).strip()
    if full_response:
        add_to_history("user",      user_text)
        add_to_history("assistant", full_response)

    # Reset HUD once the TTS queue finishes draining
    def _reset_hud():
        _tts_queue.join()
        set_hud("standby")
    threading.Thread(target=_reset_hud, daemon=True).start()


# ---------------------------------------------------------------------------
# Wake-word capture thread
#
# Keeps a single sr.Microphone open continuously. Each time speech is
# detected, the raw WAV bytes are pushed to _raw_audio_queue.
# Keeping the mic open in one persistent with-block avoids the CoreAudio
# segfault that occurs when re-opening the device too rapidly on macOS.
# ---------------------------------------------------------------------------

def wake_capture_thread():
    recognizer = sr.Recognizer()
    recognizer.energy_threshold        = 600
    recognizer.dynamic_energy_threshold = False

    while True:
        try:
            with sr.Microphone() as source:
                recognizer.adjust_for_ambient_noise(source, duration=0.4)
                while True:
                    try:
                        audio = recognizer.listen(
                            source, timeout=1.2, phrase_time_limit=2.5
                        )
                        _raw_audio_queue.put(audio.get_wav_data())
                    except sr.WaitTimeoutError:
                        continue
                    except Exception:
                        break  # mic dropped — reopen in outer loop
        except Exception:
            time.sleep(0.2)


# ---------------------------------------------------------------------------
# Transcription thread — Whisper, completely off the main thread
# ---------------------------------------------------------------------------

def transcription_thread():
    """
    Pulls WAV bytes from _raw_audio_queue, sends each to Whisper, and posts
    to _wake_event_queue whenever "oracle" or "jarvis" is detected.
    """
    thread_id = threading.get_ident()
    temp_wav  = os.path.join(TEMP_AUDIO_DIR, f"wake_{thread_id}.wav")

    while True:
        try:
            wav_bytes = _raw_audio_queue.get(timeout=0.5)
        except queue.Empty:
            continue

        try:
            with open(temp_wav, "wb") as f:
                f.write(wav_bytes)

            with open(temp_wav, "rb") as af:
                transcript = groq_client.audio.transcriptions.create(
                    file=af,
                    model="whisper-large-v3-turbo",
                    response_format="text",
                ).lower()

            try:
                os.remove(temp_wav)
            except OSError:
                pass

            if "oracle" in transcript or "jarvis" in transcript:
                # Drain stale clips before signalling — prevents double-trigger
                while not _raw_audio_queue.empty():
                    try:
                        _raw_audio_queue.get_nowait()
                    except queue.Empty:
                        break
                _wake_event_queue.put(True)

        except Exception as e:
            print(f"[Transcription] {e}")


# ---------------------------------------------------------------------------
# Command listener — called from the oracle worker thread
# ---------------------------------------------------------------------------

def listen_for_command() -> str | None:
    recognizer = sr.Recognizer()
    recognizer.energy_threshold        = 480
    recognizer.dynamic_energy_threshold = False

    set_hud("listening")
    print("Listening...")

    try:
        with sr.Microphone() as source:
            recognizer.adjust_for_ambient_noise(source, duration=0.15)
            audio = recognizer.listen(source, timeout=7, phrase_time_limit=16)

        set_hud("processing")

        uid      = int(time.time() * 1000)
        wav_path = os.path.join(TEMP_AUDIO_DIR, f"cmd_{uid}.wav")

        with open(wav_path, "wb") as f:
            f.write(audio.get_wav_data())

        with open(wav_path, "rb") as af:
            result = groq_client.audio.transcriptions.create(
                file=af,
                model="whisper-large-v3-turbo",
                response_format="text",
            )

        try:
            os.remove(wav_path)
        except OSError:
            pass

        return result.strip()

    except sr.WaitTimeoutError:
        return None
    except Exception as e:
        print(f"[Command listener] {e}")
        return None


# ---------------------------------------------------------------------------
# Auto-sleep — shuts down after N minutes of inactivity
# ---------------------------------------------------------------------------

def auto_sleep_thread():
    if AUTO_SLEEP_MINUTES <= 0:
        return
    while True:
        time.sleep(30)
        idle_minutes = (time.time() - _last_activity_time) / 60
        if idle_minutes >= AUTO_SLEEP_MINUTES:
            print(f"[Auto-sleep] {AUTO_SLEEP_MINUTES} minutes idle. Shutting down.")
            speak_blocking(
                f"Going offline after {AUTO_SLEEP_MINUTES} minutes of inactivity, Sir. "
                f"Run the script again to bring me back."
            )
            os._exit(0)


# ---------------------------------------------------------------------------
# Oracle worker — the main command-response loop
#
# This thread handles everything that touches the microphone or the LLM.
# The main thread is left completely free to run tkinter and repaint the HUD.
# ---------------------------------------------------------------------------

def oracle_worker():
    global _last_activity_time

    while True:
        # Block until a confirmed wake event arrives from the transcription thread
        try:
            _wake_event_queue.get(timeout=0.5)
        except queue.Empty:
            continue

        # Drain any duplicate wake events that may have stacked up
        while not _wake_event_queue.empty():
            try:
                _wake_event_queue.get_nowait()
            except queue.Empty:
                break

        _last_activity_time = time.time()

        # Interrupt anything currently playing
        force_stop_tts()
        set_hud("waking")
        speak_blocking("Sir?")

        user_input = listen_for_command()

        if not user_input:
            speak("I didn't catch that, Sir.")
            set_hud("standby")
            continue

        print(f"\nYou: {user_input}\n")

        # Try local fast-path first; fall through to LLM if unrecognised
        if not handle_quick_command(user_input):
            stop_tts_flag.clear()
            get_llm_response(user_input)

        set_hud("standby")


# ---------------------------------------------------------------------------
# LaunchAgent installer — run `python oracle.py --install` to set up
# ---------------------------------------------------------------------------

def install_as_login_service():
    python_bin  = sys.executable
    script_path = os.path.abspath(__file__)
    agents_dir  = os.path.expanduser("~/Library/LaunchAgents")
    plist_path  = os.path.join(agents_dir, "com.oracle.assistant.plist")
    log_path    = os.path.join(DOCS_DIR, "oracle.log")

    os.makedirs(agents_dir, exist_ok=True)

    plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.oracle.assistant</string>

    <key>ProgramArguments</key>
    <array>
        <string>{python_bin}</string>
        <string>{script_path}</string>
    </array>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <true/>

    <key>StandardOutPath</key>
    <string>{log_path}</string>

    <key>StandardErrorPath</key>
    <string>{log_path}</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin:/opt/homebrew/sbin</string>
    </dict>
</dict>
</plist>"""

    with open(plist_path, "w") as f:
        f.write(plist_content)

    subprocess.run(["launchctl", "load", plist_path], check=False)

    print(f"\nOracle installed as a login service.")
    print(f"  Plist : {plist_path}")
    print(f"  Log   : {log_path}")
    print(f"\nOracle will now start automatically every time you log in.")
    print(f"\nTo uninstall:")
    print(f"  launchctl unload {plist_path}")
    print(f"  rm {plist_path}\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

BOOT_LINES = [
    f"For you, Sir, always. Oracle is online and fully operational.",
    f"Good to have you back, {OWNER_FIRST}. All systems nominal.",
    f"Oracle online. Standing by for your instructions, Sir.",
    f"All protocols live. Ready when you are, {OWNER_FIRST}.",
    f"Systems clear, Sir. Oracle is at your service.",
]

if __name__ == "__main__":

    if "--install" in sys.argv:
        install_as_login_service()
        sys.exit(0)

    load_memory()

    # Start all background threads
    threading.Thread(target=_run_tts_event_loop,  name="tts-worker",    daemon=True).start()
    threading.Thread(target=wake_capture_thread,  name="wake-capture",  daemon=True).start()
    threading.Thread(target=transcription_thread, name="transcriber",   daemon=True).start()
    threading.Thread(target=auto_sleep_thread,    name="auto-sleep",    daemon=True).start()
    threading.Thread(target=oracle_worker,        name="oracle-worker", daemon=True).start()

    # Build the HUD window (must happen on the main thread)
    _root = tk.Tk()
    _root.title("Oracle")
    _hud  = OracleHUD(_root)

    # Boot greeting on a thread so tkinter can finish painting first
    def _boot():
        time.sleep(0.4)
        greeting = random.choice(BOOT_LINES)
        # Use time-aware prefix
        hour = datetime.datetime.now().hour
        if hour < 12:
            greeting = greeting.replace("Good to have you back", "Good morning")
        elif hour < 17:
            greeting = greeting.replace("Good to have you back", "Good afternoon")
        speak_blocking(greeting)
        set_hud("standby")
        print(f"\nOracle is online. Say 'Oracle' or 'Jarvis' to activate.")
        print(f"Auto-sleep: {AUTO_SLEEP_MINUTES} minutes of inactivity.\n")

    threading.Thread(target=_boot, name="boot", daemon=True).start()

    # Hand control to tkinter — oracle_worker handles everything else
    _root.mainloop()
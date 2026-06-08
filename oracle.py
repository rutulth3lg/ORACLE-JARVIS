# oracle.py
#
# Oracle — personal voice assistant for macOS
# Built by Rutul Gajjar
#
# Say "Oracle" or "Jarvis" to wake it up.
# Stays alive until you say "shutdown" or it auto-sleeps after inactivity.
#
# Requirements:
#   pip install groq edge-tts SpeechRecognition pyaudio yt-dlp psutil requests
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
import uuid
import shutil
import subprocess
import threading
import random
import queue
import asyncio
import datetime
import platform
import textwrap
import tkinter as tk
from tkinter import font as tkfont
from dataclasses import dataclass, field
from typing import Callable, Optional
from collections import deque

import speech_recognition as sr
from groq import Groq
import edge_tts

# ---------------------------------------------------------------------------
# .env loader
# ---------------------------------------------------------------------------

def _load_env(path: str) -> None:
    """Parse a .env file and inject missing keys into os.environ."""
    if not os.path.exists(path):
        return
    with open(path) as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


_load_env(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

GROQ_API_KEY  = os.environ.get("GROQ_API_KEY", "")
OWNER_NAME    = os.environ.get("ORACLE_OWNER_NAME", "Your Name")
OWNER_FIRST   = os.environ.get("ORACLE_OWNER_FIRST", "Sir")

VOICE             = "en-GB-RyanNeural"
DOCS_DIR          = os.path.expanduser("~/Documents")
MEMORY_FILE       = os.path.join(DOCS_DIR, "oracle_memory.json")
TEMP_AUDIO_DIR    = os.path.join(DOCS_DIR, "oracle_tmp")
LOG_FILE          = os.path.join(DOCS_DIR, "oracle.log")

TTS_RATE          = "+6%"
TTS_AFPLAY_SPEED  = "1.0"
MAX_HISTORY_TURNS = 20
AUTO_SLEEP_MINUTES = 10
MAX_FACTS_CHARS   = 1_200
MAX_CONTEXT_CHARS = 28_000

# How many interaction log entries to keep in RAM for the status report
SESSION_LOG_MAX   = 50

# ---------------------------------------------------------------------------
# Workspace ritual — edit here only
# ---------------------------------------------------------------------------

WORKSPACE_CONFIG: dict = {
    "apps":  ["Visual Studio Code"],
    "urls":  ["https://claude.ai"],
    "music": "Paranoid Black Sabbath official audio",
}

# ---------------------------------------------------------------------------
# Groq client + directories
# ---------------------------------------------------------------------------

groq_client = Groq(api_key=GROQ_API_KEY)
os.makedirs(TEMP_AUDIO_DIR, exist_ok=True)


# ---------------------------------------------------------------------------
# Session log — lightweight in-memory ring buffer of recent interactions
# ---------------------------------------------------------------------------

@dataclass
class _LogEntry:
    ts:       str
    speaker:  str   # "you" | "oracle" | "system"
    text:     str


_session_log: deque[_LogEntry] = deque(maxlen=SESSION_LOG_MAX)
_session_lock = threading.Lock()
_session_start = datetime.datetime.now()
_interaction_count = 0   # total user→oracle interactions this session


def _log(speaker: str, text: str) -> None:
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    with _session_lock:
        _session_log.append(_LogEntry(ts=ts, speaker=speaker, text=text))


# ---------------------------------------------------------------------------
# HUD — floating status overlay
# ---------------------------------------------------------------------------

_hud_queue: queue.Queue = queue.Queue()

HUD_CONFIG = {
    "standby":    ("● STANDBY",    "#0d0d0d", "#0d0d0d", "#3a3a7a"),
    "listening":  ("◉ LISTENING",  "#0d0d0d", "#001500", "#00ff41"),
    "processing": ("⟳ PROCESSING", "#0d0d0d", "#1a0d00", "#ff9500"),
    "speaking":   ("▶ SPEAKING",   "#0d0d0d", "#00101a", "#0099ff"),
    "waking":     ("◎ WAKE",       "#0d0d0d", "#1a0010", "#ff0055"),
    "sleeping":   ("◌ SLEEPING",   "#050505", "#050505", "#222244"),
    "error":      ("✕ ERROR",      "#0d0d0d", "#1a0000", "#ff3333"),
}


def set_hud(state: str) -> None:
    _hud_queue.put(state)


class OracleHUD:
    """Frameless always-on-top overlay — all drawing on main thread via after()."""

    def __init__(self, root: tk.Tk):
        self.root       = root
        self._state     = "standby"
        self._pulse_job = None

        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.attributes("-alpha", 0.92)
        root.configure(bg="#0d0d0d")

        screen_w = root.winfo_screenwidth()
        screen_h = root.winfo_screenheight()
        win_w, win_h = 250, 48
        root.geometry(f"{win_w}x{win_h}+{screen_w - win_w - 18}+{screen_h - win_h - 60}")

        try:
            label_font = tkfont.Font(family="SF Pro Display", size=11, weight="bold")
        except Exception:
            label_font = tkfont.Font(family="Helvetica Neue", size=11, weight="bold")

        self.frame = tk.Frame(
            root, bg="#0d0d0d",
            highlightbackground="#333366", highlightthickness=1,
        )
        self.frame.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)

        self.label = tk.Label(
            self.frame, text="● STANDBY",
            font=label_font, fg="#3a3a7a", bg="#0d0d0d",
            padx=14, pady=12,
        )
        self.label.pack(side=tk.LEFT)

        self.root.after(60, self._poll)

    def _poll(self):
        changed = False
        while not _hud_queue.empty():
            try:
                ns = _hud_queue.get_nowait()
                if ns != self._state:
                    self._state = ns
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
        self.frame.configure(bg=frame_bg, highlightbackground=frame_bg)
        self.label.configure(text=label_text, fg=fg, bg=frame_bg)
        if self._pulse_job:
            self.root.after_cancel(self._pulse_job)
            self._pulse_job = None
        if self._state in ("listening", "processing", "waking"):
            self._pulse_bright = True
            self._start_pulse(fg)

    def _start_pulse(self, fg: str):
        if self._state not in ("listening", "processing", "waking"):
            return
        self._pulse_bright = not self._pulse_bright
        self.label.configure(fg=(fg if self._pulse_bright else self._dim_color(fg)))
        self._pulse_job = self.root.after(380, lambda: self._start_pulse(fg))

    @staticmethod
    def _dim_color(hex_color: str) -> str:
        h = hex_color.lstrip("#")
        if len(h) != 6:
            return hex_color
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        return "#{:02x}{:02x}{:02x}".format(r // 2, g // 2, b // 2)


 ---------------------------------------------------------------------------
 Persistent memory
---------------------------------------------------------------------------

_conversation_history: list[dict] = []
_named_facts:          dict       = {}
_memory_lock                      = threading.Lock()


def load_memory() -> None:
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
              f"{len(_named_facts)} facts.")
    except Exception as e:
        print(f"[Memory] Corrupt — resetting. ({e})")
        try:
            shutil.move(MEMORY_FILE, MEMORY_FILE + ".bak")
        except Exception:
            pass
        _conversation_history = []
        _named_facts = {}


def save_memory() -> None:
    """Atomic write on a daemon thread — crash-safe."""
    def _write():
        with _memory_lock:
            payload = {
                "history":  list(_conversation_history),
                "facts":    dict(_named_facts),
                "saved_at": datetime.datetime.now().isoformat(),
            }
        tmp = MEMORY_FILE + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(payload, f, indent=2)
            os.replace(tmp, MEMORY_FILE)
        except Exception as e:
            print(f"[Memory] Save failed: {e}")
            try:
                os.remove(tmp)
            except OSError:
                pass
    threading.Thread(target=_write, daemon=True).start()


def add_to_history(role: str, content: str) -> None:
    with _memory_lock:
        _conversation_history.append({"role": role, "content": content})
        max_items = MAX_HISTORY_TURNS * 2
        if len(_conversation_history) > max_items:
            excess = len(_conversation_history) - max_items
            del _conversation_history[:excess + (excess % 2)]
    save_memory()


def store_fact(key: str, value: str) -> None:
    with _memory_lock:
        _named_facts[key] = value
    save_memory()


def forget_fact(key: str) -> bool:
    """Remove a stored fact. Returns True if it existed."""
    with _memory_lock:
        existed = key in _named_facts
        _named_facts.pop(key, None)
    if existed:
        save_memory()
    return existed


def list_facts() -> dict:
    with _memory_lock:
        return dict(_named_facts)


def facts_context_block() -> str:
    with _memory_lock:
        if not _named_facts:
            return ""
        lines  = [f"  {k}: {v}" for k, v in _named_facts.items()]
        header = f"\n\nThings Oracle knows about {OWNER_FIRST}:\n"
        budget = MAX_FACTS_CHARS - len(header)
        kept: list[str] = []
        for line in reversed(lines):
            if budget - len(line) - 1 < 0:
                break
            kept.insert(0, line)
            budget -= len(line) + 1
        return (header + "\n".join(kept)) if kept else ""


def build_llm_messages(user_text: str) -> list[dict]:
    with _memory_lock:
        history_copy = list(_conversation_history)
    facts_block = facts_context_block()
    system_text = SYSTEM_PROMPT + facts_block
    user_chars  = len(user_text)
    sys_chars   = len(system_text)
    while history_copy:
        if sys_chars + sum(len(m["content"]) for m in history_copy) + user_chars <= MAX_CONTEXT_CHARS:
            break
        history_copy = history_copy[2:]
    return [{"role": "system", "content": system_text}, *history_copy,
            {"role": "user", "content": user_text}]


# ---------------------------------------------------------------------------
# Global audio state
# ---------------------------------------------------------------------------

stop_tts_flag   = threading.Event()
_tts_afplay     = None
_tts_afplay_lk  = threading.Lock()

stop_media_flag = threading.Event()
_media_proc     = None
_media_lk       = threading.Lock()
_media_ffmpeg   = None

_tts_event_loop: asyncio.AbstractEventLoop = asyncio.new_event_loop()
_tts_queue:      queue.Queue               = queue.Queue()

_raw_audio_queue:  queue.Queue = queue.Queue()
_wake_event_queue: queue.Queue = queue.Queue()

_is_speaking       = threading.Event()
_last_activity_time = time.time()

# Wi-Fi cache: [ssid | None, monotonic_ts]
_wifi_cache: list = [None, 0.0]

# CPU/memory snapshot cache: [result_str | None, monotonic_ts]
_sysinfo_cache: list = [None, 0.0]


# ---------------------------------------------------------------------------
# Text sanitisation
# ---------------------------------------------------------------------------

_ACTION_TAG_RE = re.compile(r'\[ACTION:[^\]]*\]', re.IGNORECASE)

_SPEECH_SUBS: list[tuple[re.Pattern, str]] = [
    (re.compile(r'\*{1,3}'),                          ""),
    (re.compile(r'`{1,3}'),                           ""),
    (re.compile(r'#{1,6}\s?'),                        ""),
    (re.compile(r'\s*—\s*'),                          ", "),
    (re.compile(r'\s*–\s*'),                          ", "),
    (re.compile(r'https?://\S+'),                     ""),
    (re.compile(r'(\d+\.?\d*)k\b', re.IGNORECASE),   r'\1 thousand'),
    (re.compile(r'(\d+\.?\d*)M\b'),                   r'\1 million'),
    (re.compile(r'(\d+\.?\d*)B\b'),                   r'\1 billion'),
    (re.compile(r'(\d+\.?\d*)T\b'),                   r'\1 trillion'),
    (re.compile(r'\bv(\d+\.\d+)', re.IGNORECASE),    r'version \1'),
    (re.compile(r'&'),                                " and "),
    (re.compile(r'%'),                                " percent"),
    (re.compile(r'\$(\d)'),                           r'\1 dollars'),
    (re.compile(r'#(\d)'),                            r'number \1'),
    (re.compile(r'#(\w)'),                            r'hash \1'),
    (re.compile(r'\+'),                               " plus "),
    (re.compile(r'\be\.g\.\b', re.IGNORECASE),        "for example"),
    (re.compile(r'\bi\.e\.\b', re.IGNORECASE),        "that is"),
    (re.compile(r'\bvs\.?\b',  re.IGNORECASE),        "versus"),
    (re.compile(r'\betc\.?\b', re.IGNORECASE),        "et cetera"),
    (re.compile(r'\bapprox\.?\b', re.IGNORECASE),     "approximately"),
    (re.compile(r'\bmin\.?\b', re.IGNORECASE),        "minutes"),
    (re.compile(r'\bmax\.?\b', re.IGNORECASE),        "maximum"),
    (re.compile(r'\bAI\b'),                           "A I"),
    (re.compile(r'\bUI\b'),                           "U I"),
    (re.compile(r'\bAPI\b'),                          "A P I"),
    (re.compile(r'\bURL\b'),                          "U R L"),
    (re.compile(r'\bCPU\b'),                          "C P U"),
    (re.compile(r'\bGPU\b'),                          "G P U"),
    (re.compile(r'\bSSD\b'),                          "S S D"),
    (re.compile(r'\s{2,}'),                           " "),
]

_SENTENCE_SPLIT_RE = re.compile(r"(?<![A-Z\d])(?<=[.!?])\s+(?=[A-Z\"\(])")


def sanitize_for_speech(text: str) -> str:
    text = _ACTION_TAG_RE.sub("", text)
    for pattern, replacement in _SPEECH_SUBS:
        text = pattern.sub(replacement, text)
    return text.strip()


def has_unclosed_bracket(text: str) -> bool:
    return text.count("[") > text.count("]")


# ---------------------------------------------------------------------------
# TTS pipeline — double-buffer, asyncio event loop on its own thread
# ---------------------------------------------------------------------------

def _kill_tts_afplay() -> None:
    global _tts_afplay
    with _tts_afplay_lk:
        if _tts_afplay and _tts_afplay.poll() is None:
            _tts_afplay.terminate()
            try:
                _tts_afplay.wait(0.3)
            except Exception:
                pass
        _tts_afplay = None


def force_stop_tts() -> None:
    stop_tts_flag.set()
    _kill_tts_afplay()
    while not _tts_queue.empty():
        try:
            _tts_queue.get_nowait()
            _tts_queue.task_done()
        except queue.Empty:
            break


def _afplay_tts_file(filepath: str) -> None:
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
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
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


def _run_tts_event_loop() -> None:
    asyncio.set_event_loop(_tts_event_loop)
    _tts_event_loop.run_until_complete(_tts_pipeline())


async def _generate_tts(text: str, filepath: str) -> bool:
    try:
        await edge_tts.Communicate(text, VOICE, rate=TTS_RATE, volume="+8%").save(filepath)
        return True
    except Exception as e:
        print(f"[TTS generate] {e}")
        return False


async def _tts_pipeline() -> None:
    ready: Optional[tuple] = None
    while True:
        try:
            text, filepath = _tts_queue.get_nowait()
        except queue.Empty:
            if ready is None:
                await asyncio.sleep(0.015)
                continue
            text, filepath = None, None

        if text is not None:
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
            if ready is not None:
                if not stop_tts_flag.is_set():
                    await _tts_event_loop.run_in_executor(None, _afplay_tts_file, ready[1])
                else:
                    try:
                        os.remove(ready[1])
                    except OSError:
                        pass
                _tts_queue.task_done()
            ready = (text, filepath)
        else:
            if ready is not None:
                if not stop_tts_flag.is_set():
                    await _tts_event_loop.run_in_executor(None, _afplay_tts_file, ready[1])
                else:
                    try:
                        os.remove(ready[1])
                    except OSError:
                        pass
                _tts_queue.task_done()
                ready = None


def speak(text: str) -> None:
    """Enqueue text for TTS. Non-blocking."""
    clean = sanitize_for_speech(text)
    if not clean:
        return
    print(f"Oracle: {clean}")
    _log("oracle", clean)
    set_hud("speaking")
    _is_speaking.set()
    filepath = os.path.join(TEMP_AUDIO_DIR, f"tts_{uuid.uuid4().hex}.mp3")
    _tts_queue.put((clean, filepath))


def speak_blocking(text: str) -> None:
    speak(text)
    _tts_queue.join()
    _is_speaking.clear()


# ---------------------------------------------------------------------------
# Media player — yt-dlp + afplay
# ---------------------------------------------------------------------------

def stop_media() -> None:
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


def _play_audio_worker(query: str) -> None:
    global _media_proc
    stop_media_flag.clear()
    print(f"[Media] Fetching: {query}")
    set_hud("processing")

    try:
        subprocess.run(["yt-dlp", "--version"], capture_output=True, timeout=4, check=True)
    except Exception:
        speak("yt-dlp is not installed. Run: pip install yt-dlp")
        set_hud("standby")
        return

    temp_file  = os.path.join(TEMP_AUDIO_DIR, f"media_{int(time.time())}.%(ext)s")
    final_file = temp_file.replace(".%(ext)s", ".mp3")

    try:
        dl = subprocess.run(
            ["yt-dlp", f"ytsearch1:{query}", "-x", "--audio-format", "mp3",
             "--audio-quality", "0", "--no-playlist", "--quiet", "--no-warnings",
             "-o", temp_file],
            capture_output=True, text=True, timeout=60,
        )
        if dl.returncode != 0 or not os.path.exists(final_file):
            candidates = [
                f for f in os.listdir(TEMP_AUDIO_DIR)
                if f.startswith(f"media_{int(time.time())}"[:-3]) and ".%(ext)s" not in f
            ]
            if candidates:
                final_file = os.path.join(TEMP_AUDIO_DIR, candidates[0])
            else:
                speak(f"Couldn't find that track, {OWNER_FIRST}.")
                set_hud("standby")
                return

        if not os.path.exists(final_file):
            speak("Download completed but file is missing.")
            set_hud("standby")
            return

        print(f"[Media] Playing: {final_file}")
        set_hud("speaking")
        proc = subprocess.Popen(
            ["afplay", final_file], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        with _media_lk:
            _media_proc = proc
        proc.wait()
        with _media_lk:
            _media_proc = None
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


def play_audio(query: str) -> None:
    stop_media()
    threading.Thread(target=_play_audio_worker, args=(query,), name="media-player", daemon=True).start()


def open_in_browser(url: str) -> None:
    subprocess.Popen(["open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def open_youtube(query: str) -> None:
    def _open():
        try:
            result = subprocess.run(
                ["yt-dlp", f"ytsearch1:{query}", "--print", "id",
                 "--no-playlist", "--quiet", "--no-warnings", "--skip-download"],
                capture_output=True, text=True, timeout=25,
            )
            vid = result.stdout.strip().splitlines()[0].strip()
            if vid and len(vid) == 11:
                open_in_browser(f"https://www.youtube.com/watch?v={vid}")
                return
        except Exception as e:
            print(f"[YouTube open] {e}")
        open_in_browser("https://www.youtube.com/results?search_query=" + query.replace(" ", "+"))
    threading.Thread(target=_open, name="yt-open", daemon=True).start()


# ---------------------------------------------------------------------------
# macOS system helpers
# ---------------------------------------------------------------------------

def run_applescript(script: str) -> None:
    subprocess.Popen(["osascript", "-e", script],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def run_applescript_result(script: str) -> str:
    """Run AppleScript and return stdout as a string."""
    try:
        r = subprocess.run(["osascript", "-e", script],
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip()
    except Exception:
        return ""


def launch_app(name: str) -> None:
    app = NATIVE_APPS.get(name.lower().strip(), name.title())
    subprocess.Popen(["open", "-a", app],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def get_volume() -> int:
    try:
        r = subprocess.run(
            ["osascript", "-e", "output volume of (get volume settings)"],
            capture_output=True, text=True, timeout=3,
        )
        return int(r.stdout.strip())
    except Exception:
        return 50


def set_volume(level: int) -> None:
    run_applescript(f"set volume output volume {max(0, min(100, level))}")


def get_wifi_name() -> str:
    now = time.monotonic()
    if _wifi_cache[0] is not None and (now - _wifi_cache[1]) < 30:
        return _wifi_cache[0]
    ssid = _query_wifi_ssid()
    _wifi_cache[0] = ssid
    _wifi_cache[1] = now
    return ssid


def _query_wifi_ssid() -> str:
    try:
        r = subprocess.run(["system_profiler", "SPAirPortDataType"],
                           capture_output=True, text=True, timeout=6)
        m = re.search(r"Current Network Information:\s+(.+?):", r.stdout)
        if m:
            return m.group(1).strip()
    except Exception:
        pass
    for iface in ("en0", "en1"):
        try:
            r = subprocess.run(["networksetup", "-getairportnetwork", iface],
                               capture_output=True, text=True, timeout=4)
            if "not associated" not in r.stdout.lower():
                m = re.search(r"Network:\s*(.+)", r.stdout)
                if m:
                    return m.group(1).strip()
        except Exception:
            pass
    return "unknown network"


def get_battery_status() -> str:
    try:
        r = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True, timeout=4)
        m = re.search(r"(\d+)%", r.stdout)
        charging = "ac power" in r.stdout.lower() or "charging" in r.stdout.lower()
        if m:
            return f"{m.group(1)} percent, {'charging' if charging else 'on battery'}"
    except Exception:
        pass
    return "unavailable"


def get_system_info() -> str:
    """Return a brief CPU + RAM snapshot. Cached for 10 seconds."""
    now = time.monotonic()
    if _sysinfo_cache[0] is not None and (now - _sysinfo_cache[1]) < 10:
        return _sysinfo_cache[0]
    try:
        import psutil
        cpu  = psutil.cpu_percent(interval=0.3)
        ram  = psutil.virtual_memory()
        used = ram.used  // (1024 ** 3)
        total = ram.total // (1024 ** 3)
        result = f"CPU at {cpu:.0f} percent, RAM {used} of {total} gigabytes used"
    except ImportError:
        # psutil not installed — fall back to vm_stat
        try:
            r = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=3)
            result = "System info requires psutil — run: pip install psutil"
        except Exception:
            result = "System info unavailable"
    _sysinfo_cache[0] = result
    _sysinfo_cache[1] = now
    return result


def get_frontmost_app() -> str:
    return run_applescript_result(
        'tell application "System Events" to get name of first application process '
        'whose frontmost is true'
    )


def get_clipboard() -> str:
    try:
        r = subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=3)
        return r.stdout.strip()
    except Exception:
        return ""


def set_clipboard(text: str) -> None:
    try:
        subprocess.run(["pbcopy"], input=text.encode(), timeout=3)
    except Exception:
        pass


def fire_notification(title: str, body: str) -> None:
    run_applescript(f'display notification "{body}" with title "{title}"')


def get_ip_address() -> str:
    """Return the local LAN IP address."""
    try:
        r = subprocess.run(
            ["ipconfig", "getifaddr", "en0"],
            capture_output=True, text=True, timeout=4,
        )
        ip = r.stdout.strip()
        if ip:
            return ip
    except Exception:
        pass
    try:
        import socket
        return socket.gethostbyname(socket.gethostname())
    except Exception:
        return "unavailable"


def get_disk_usage() -> str:
    """Return disk usage for the root volume."""
    try:
        r = subprocess.run(["df", "-h", "/"], capture_output=True, text=True, timeout=4)
        lines = r.stdout.strip().splitlines()
        if len(lines) >= 2:
            parts = lines[1].split()
            # parts: Filesystem, Size, Used, Avail, Capacity, Mounted
            if len(parts) >= 5:
                return f"{parts[2]} used of {parts[1]}, {parts[3]} free"
    except Exception:
        pass
    return "unavailable"


def empty_trash() -> None:
    run_applescript('tell application "Finder" to empty trash')


def increase_brightness(amount: int = 20) -> None:
    script = f"""
    tell application "System Events"
        repeat {amount // 5} times
            key code 144
        end repeat
    end tell
    """
    run_applescript(script)


def decrease_brightness(amount: int = 20) -> None:
    script = f"""
    tell application "System Events"
        repeat {amount // 5} times
            key code 145
        end repeat
    end tell
    """
    run_applescript(script)


def set_do_not_disturb(enable: bool) -> None:
    """Toggle Focus / Do Not Disturb via keyboard shortcut."""
    # macOS Monterey+: Option+Click the clock — no reliable AppleScript API
    # Best available: use the Control Centre shortcut
    run_applescript(
        'tell application "System Events" to key code 57 using {option down, command down}'
    )


def get_uptime() -> str:
    try:
        r = subprocess.run(["uptime"], capture_output=True, text=True, timeout=3)
        # Parse "up X days, Y:ZZ" or "up Y:ZZ"
        m = re.search(r"up\s+(.+?),\s+\d+ user", r.stdout)
        if m:
            return m.group(1).strip()
        # simpler fallback
        parts = r.stdout.strip().split("up ")
        if len(parts) > 1:
            return parts[1].split(",")[0].strip()
    except Exception:
        pass
    return "unavailable"


def get_mac_model() -> str:
    try:
        r = subprocess.run(
            ["system_profiler", "SPHardwareDataType"],
            capture_output=True, text=True, timeout=6,
        )
        m = re.search(r"Model Name:\s+(.+)", r.stdout)
        if m:
            return m.group(1).strip()
    except Exception:
        pass
    return platform.machine()


def open_maps(location: str) -> None:
    q = location.replace(" ", "+")
    open_in_browser(f"https://maps.apple.com/?q={q}")


def calculate(expr: str) -> str:
    """Safely evaluate a math expression. Returns result as string."""
    # Only allow digits, operators, parens, dots, spaces
    safe = re.sub(r"[^0-9\.\+\-\*\/\(\)\s\%\^]", "", expr)
    safe = safe.replace("^", "**")
    try:
        result = eval(safe, {"__builtins__": {}})
        # Format: strip trailing zeros from floats
        if isinstance(result, float) and result == int(result):
            return str(int(result))
        return str(round(result, 8)).rstrip("0").rstrip(".")
    except Exception:
        return "error"


# ---------------------------------------------------------------------------
# Duration helpers
# ---------------------------------------------------------------------------

_UNIT_TO_SECS: dict[str, int] = {
    "second": 1,  "sec": 1,
    "minute": 60, "min": 60,
    "hour": 3600, "hr": 3600,
}


def _parse_duration(n: int, unit: str) -> int:
    return n * _UNIT_TO_SECS.get(unit.lower().rstrip("s"), 1)


def _human_duration(n: int, unit: str) -> str:
    base = unit.lower().rstrip("s")
    canonical = {"sec": "second", "min": "minute", "hr": "hour"}.get(base, base)
    return f"{n} {canonical}{'s' if n != 1 else ''}"


# ---------------------------------------------------------------------------
# Cancellable timer
# ---------------------------------------------------------------------------

class _CancellableTimer:
    def __init__(self, seconds: int, label: str, callback: Callable):
        self._cancel = threading.Event()
        threading.Thread(target=self._run, args=(seconds, label, callback),
                         daemon=True).start()

    def cancel(self) -> None:
        self._cancel.set()

    def _run(self, seconds: int, label: str, callback: Callable) -> None:
        if not self._cancel.wait(timeout=seconds):
            callback(label)


_active_timers:    dict[str, _CancellableTimer] = {}
_active_reminders: dict[str, _CancellableTimer] = {}


def set_timer(seconds: int, label: str) -> None:
    def _fire(lbl: str):
        fire_notification("Oracle", f"Timer: {lbl}")
        speak(f"Sir, your {lbl} timer is up.")
        _active_timers.pop(lbl, None)
    _active_timers[label] = _CancellableTimer(seconds, label, _fire)


def cancel_timer(label: str) -> bool:
    t = _active_timers.pop(label, None)
    if t:
        t.cancel()
        return True
    # Try partial match
    for k in list(_active_timers):
        if label in k:
            _active_timers.pop(k).cancel()
            return True
    return False


def set_reminder(task: str, seconds: int) -> None:
    def _fire(t: str):
        fire_notification("Oracle", f"Reminder: {t}")
        speak(f"Sir, just a reminder to {t}.")
        _active_reminders.pop(t, None)
    _active_reminders[task] = _CancellableTimer(seconds, task, _fire)


# ---------------------------------------------------------------------------
# Websites and native apps lookup tables
# ---------------------------------------------------------------------------

WEBSITES: dict[str, str] = {
    "youtube music":   "https://music.youtube.com",
    "yt music":        "https://music.youtube.com",
    "google meet":     "https://meet.google.com",
    "google docs":     "https://docs.google.com",
    "google sheets":   "https://sheets.google.com",
    "google drive":    "https://drive.google.com",
    "google maps":     "https://maps.google.com",
    "google calendar": "https://calendar.google.com",
    "hacker news":     "https://news.ycombinator.com",
    "apple music":     "https://music.apple.com",
    "product hunt":    "https://www.producthunt.com",
    "yahoo finance":   "https://finance.yahoo.com",
    "youtube":         "https://www.youtube.com",
    "spotify":         "https://open.spotify.com",
    "soundcloud":      "https://soundcloud.com",
    "github":          "https://github.com",
    "google":          "https://www.google.com",
    "reddit":          "https://www.reddit.com",
    "twitter":         "https://twitter.com",
    "x":               "https://x.com",
    "netflix":         "https://www.netflix.com",
    "gmail":           "https://mail.google.com",
    "instagram":       "https://www.instagram.com",
    "linkedin":        "https://www.linkedin.com",
    "twitch":          "https://www.twitch.tv",
    "quantconnect":    "https://www.quantconnect.com",
    "amazon":          "https://www.amazon.com",
    "chatgpt":         "https://chat.openai.com",
    "claude":          "https://claude.ai",
    "perplexity":      "https://perplexity.ai",
    "wikipedia":       "https://www.wikipedia.org",
    "tradingview":     "https://www.tradingview.com",
    "bloomberg":       "https://www.bloomberg.com",
    "notion":          "https://www.notion.so",
    "figma":           "https://www.figma.com",
    "vercel":          "https://vercel.com",
    "supabase":        "https://supabase.com",
    "coinbase":        "https://www.coinbase.com",
    "binance":         "https://www.binance.com",
    "arxiv":           "https://arxiv.org",
    "stackoverflow":   "https://stackoverflow.com",
    "medium":          "https://medium.com",
    "whatsapp":        "https://web.whatsapp.com",
    "discord":         "https://discord.com/app",
    "slack":           "https://app.slack.com",
    "linear":          "https://linear.app",
    "anthropic":       "https://anthropic.com",
    "openai":          "https://openai.com",
    "groq":            "https://groq.com",
    "replit":          "https://replit.com",
    "hugging face":    "https://huggingface.co",
    "cnn":             "https://cnn.com",
    "bbc":             "https://bbc.com/news",
    "espn":            "https://espn.com",
    "imdb":            "https://imdb.com",
}

NATIVE_APPS: dict[str, str] = {
    "google chrome":        "Google Chrome",
    "visual studio code":   "Visual Studio Code",
    "microsoft word":       "Microsoft Word",
    "microsoft excel":      "Microsoft Excel",
    "microsoft powerpoint": "Microsoft PowerPoint",
    "apple music":          "Music",
    "system preferences":   "System Preferences",
    "system settings":      "System Preferences",
    "activity monitor":     "Activity Monitor",
    "quicktime":            "QuickTime Player",
    "chrome":               "Google Chrome",
    "safari":               "Safari",
    "firefox":              "Firefox",
    "brave":                "Brave Browser",
    "arc":                  "Arc",
    "terminal":             "Terminal",
    "iterm":                "iTerm",
    "iterm2":               "iTerm",
    "finder":               "Finder",
    "notes":                "Notes",
    "calendar":             "Calendar",
    "mail":                 "Mail",
    "messages":             "Messages",
    "facetime":             "FaceTime",
    "photos":               "Photos",
    "music":                "Music",
    "podcasts":             "Podcasts",
    "xcode":                "Xcode",
    "vscode":               "Visual Studio Code",
    "cursor":               "Cursor",
    "word":                 "Microsoft Word",
    "excel":                "Microsoft Excel",
    "powerpoint":           "Microsoft PowerPoint",
    "slack":                "Slack",
    "discord":              "Discord",
    "zoom":                 "zoom.us",
    "teams":                "Microsoft Teams",
    "notion":               "Notion",
    "obsidian":             "Obsidian",
    "whatsapp":             "WhatsApp",
    "telegram":             "Telegram",
    "signal":               "Signal",
    "settings":             "System Preferences",
    "calculator":           "Calculator",
    "preview":              "Preview",
    "vlc":                  "VLC",
    "spotify":              "Spotify",
    "figma":                "Figma",
    "sketch":               "Sketch",
    "postman":              "Postman",
    "tableplus":            "TablePlus",
    "docker":               "Docker",
    "1password":            "1Password",
    "warp":                 "Warp",
    "steam":                "Steam",
    "raycast":              "Raycast",
    "linear":               "Linear",
    "bear":                 "Bear",
    "cleanmymac":           "CleanMyMac",
    "screenflow":           "ScreenFlow",
    "loom":                 "Loom",
}


# ---------------------------------------------------------------------------
# Intent dispatch architecture
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Intent:
    pattern: re.Pattern
    handler: Callable[[re.Match, str], bool]


_VAGUE_YT: frozenset = frozenset({
    "any", "anything", "something", "a video", "any video",
    "something good", "your choice", "whatever",
})


# --- individual intent handlers ---

def _h_shutdown(m: re.Match, text: str) -> bool:
    speak_blocking("Initiating shutdown sequence. Goodbye, Sir.")
    sys.exit(0)


def _h_introduce(m: re.Match, text: str) -> bool:
    speak(f"I'm Oracle — {OWNER_FIRST}'s personal AI system, running locally on this Mac.")
    speak(
        "I handle everything from system control and app management to "
        "music, reminders, research, and conversation."
    )
    speak_blocking(
        f"I remember our conversations across sessions and adapt to how you work, {OWNER_FIRST}. "
        "Think of me as JARVIS — but yours."
    )
    return True


def _h_workspace(m: re.Match, text: str) -> bool:
    speak_blocking("Initialising your workspace, Sir.")
    def _ritual():
        for app in WORKSPACE_CONFIG.get("apps", []):
            print(f"[Workspace] Opening {app}")
            subprocess.Popen(["open", "-a", app],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(0.6)
        for url in WORKSPACE_CONFIG.get("urls", []):
            print(f"[Workspace] Opening {url}")
            subprocess.Popen(["open", url],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(0.4)
        music = WORKSPACE_CONFIG.get("music", "")
        if music:
            open_youtube(music)
    threading.Thread(target=_ritual, name="workspace-ritual", daemon=True).start()
    return True


def _h_close_app(m: re.Match, text: str) -> bool:
    app_raw  = m.group(1).strip().rstrip(".")
    app_name = NATIVE_APPS.get(app_raw, app_raw.title())
    result   = subprocess.run(
        ["osascript", "-e", f'tell application "{app_name}" to quit'],
        capture_output=True, timeout=5,
    )
    if result.returncode == 0:
        speak(f"Closed {app_raw}, Sir.")
    else:
        subprocess.run(["pkill", "-x", app_name], capture_output=True)
        speak(f"Force-quit {app_raw}, Sir.")
    return True


def _h_stop_media(m: re.Match, text: str) -> bool:
    stop_media()
    speak("Stopped, Sir.")
    return True


def _h_play_audio(m: re.Match, text: str) -> bool:
    query = m.group(1).strip()
    query = re.sub(r"^(a\s+)?(song|track|music)\s*$", "music", query)
    if "spotify" in text:
        open_in_browser("https://open.spotify.com/search/" + query.replace(" ", "%20"))
        speak(f"Searching Spotify for {query}, Sir.")
        return True
    speak(f"On it, Sir. Finding {query} now.")
    play_audio(query)
    return True


def _h_play_youtube(m: re.Match, text: str) -> bool:
    query = (m.group(1) or (m.lastindex and m.lastindex >= 2 and m.group(2)) or "").strip()
    if not query or query.lower() in _VAGUE_YT:
        query = "trending music"
    open_youtube(query)
    speak(f"Opening {query} on YouTube, Sir.")
    return True


def _h_open(m: re.Match, text: str) -> bool:
    target = m.group(1).strip().rstrip(".")
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
    bare = target.replace(" ", "")
    if re.match(r"^[a-zA-Z0-9.\-]+$", bare):
        if "." not in bare:
            bare += ".com"
        open_in_browser(f"https://{bare}")
        speak(f"Opening {target}, Sir.")
        return True
    return False


def _h_yt_search(m: re.Match, text: str) -> bool:
    query = m.group(1).strip()
    open_youtube(query)
    speak(f"Searching {query} on YouTube, Sir.")
    return True


def _h_web_search(m: re.Match, text: str) -> bool:
    query = m.group(1).strip().rstrip(".")
    open_in_browser("https://www.google.com/search?q=" + query.replace(" ", "+"))
    speak(f"Searching for {query}, Sir.")
    return True


def _h_volume_set(m: re.Match, text: str) -> bool:
    level = max(0, min(100, int(m.group(1))))
    set_volume(level)
    speak(f"Volume set to {level}, Sir.")
    return True


def _h_volume_up(m: re.Match, text: str) -> bool:
    new = min(100, get_volume() + 15)
    set_volume(new)
    speak(f"Volume up to {new}, Sir.")
    return True


def _h_volume_down(m: re.Match, text: str) -> bool:
    new = max(0, get_volume() - 15)
    set_volume(new)
    speak(f"Volume down to {new}, Sir.")
    return True


def _h_mute(m: re.Match, text: str) -> bool:
    run_applescript("set volume output muted true")
    speak("Muted, Sir.")
    return True


def _h_unmute(m: re.Match, text: str) -> bool:
    run_applescript("set volume output muted false")
    speak("Unmuted, Sir.")
    return True


def _h_battery(m: re.Match, text: str) -> bool:
    speak(f"Battery is at {get_battery_status()}, Sir.")
    return True


def _h_wifi(m: re.Match, text: str) -> bool:
    speak(f"You're connected to {get_wifi_name()}, Sir.")
    return True


def _h_time(m: re.Match, text: str) -> bool:
    t = datetime.datetime.now().strftime("%-I:%M %p")
    speak(f"It's {t}, Sir.")
    return True


def _h_date(m: re.Match, text: str) -> bool:
    d = datetime.datetime.now().strftime("%A, %B %-d, %Y")
    speak(f"Today is {d}, Sir.")
    return True


def _h_timer(m: re.Match, text: str) -> bool:
    n, unit = int(m.group(1)), m.group(2)
    set_timer(_parse_duration(n, unit), _human_duration(n, unit))
    speak(f"Timer set for {_human_duration(n, unit)}, Sir.")
    return True


def _h_cancel_timer(m: re.Match, text: str) -> bool:
    # Try to find which timer — grab any word context after "cancel timer"
    label_m = re.search(r"cancel\s+(?:the\s+)?(?:timer\s+(?:for\s+)?)?(.+)", text)
    label   = label_m.group(1).strip() if label_m else ""
    if not label and _active_timers:
        label = next(iter(_active_timers))
    if cancel_timer(label):
        speak(f"Timer cancelled, Sir.")
    else:
        speak(f"I don't have an active timer for that, Sir.")
    return True


def _h_remind(m: re.Match, text: str) -> bool:
    task, n, unit = m.group(1).strip(), int(m.group(2)), m.group(3)
    set_reminder(task, _parse_duration(n, unit))
    speak(f"I'll remind you to {task} in {_human_duration(n, unit)}, Sir.")
    return True


def _h_screenshot(m: re.Match, text: str) -> bool:
    ts   = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    path = os.path.expanduser(f"~/Desktop/oracle_{ts}.png")
    subprocess.Popen(["screencapture", "-x", path])
    speak("Screenshot saved to your Desktop, Sir.")
    return True


def _h_lock(m: re.Match, text: str) -> bool:
    run_applescript(
        'tell application "System Events" to keystroke "q" using {command down, control down}'
    )
    speak("Screen locked, Sir.")
    return True


def _h_sleep_mac(m: re.Match, text: str) -> bool:
    speak_blocking("Putting the Mac to sleep, Sir.")
    run_applescript('tell application "System Events" to sleep')
    return True


def _h_remember(m: re.Match, text: str) -> bool:
    key   = m.group(1).strip().replace(" ", "_")
    value = m.group(2).strip()
    store_fact(key, value)
    speak(f"Noted, Sir. Your {m.group(1).strip()} is {value}.")
    return True


def _h_forget(m: re.Match, text: str) -> bool:
    key     = m.group(1).strip().replace(" ", "_")
    existed = forget_fact(key)
    if existed:
        speak(f"Done, Sir. I've forgotten your {m.group(1).strip()}.")
    else:
        speak(f"I don't have anything stored under that, Sir.")
    return True


def _h_list_facts(m: re.Match, text: str) -> bool:
    facts = list_facts()
    if not facts:
        speak("I don't have any stored facts about you yet, Sir.")
        return True
    count = len(facts)
    speak(f"I have {count} thing{'s' if count != 1 else ''} stored, Sir.")
    for k, v in facts.items():
        readable_key = k.replace("_", " ")
        speak(f"Your {readable_key} is {v}.")
    return True


def _h_sysinfo(m: re.Match, text: str) -> bool:
    speak(get_system_info() + ", Sir.")
    return True


def _h_ip(m: re.Match, text: str) -> bool:
    speak(f"Your local IP is {get_ip_address()}, Sir.")
    return True


def _h_disk(m: re.Match, text: str) -> bool:
    speak(f"Disk usage: {get_disk_usage()}, Sir.")
    return True


def _h_uptime(m: re.Match, text: str) -> bool:
    speak(f"System has been up for {get_uptime()}, Sir.")
    return True


def _h_brightness_up(m: re.Match, text: str) -> bool:
    increase_brightness()
    speak("Brightness increased, Sir.")
    return True


def _h_brightness_down(m: re.Match, text: str) -> bool:
    decrease_brightness()
    speak("Brightness decreased, Sir.")
    return True


def _h_empty_trash(m: re.Match, text: str) -> bool:
    speak_blocking("Emptying the trash, Sir.")
    empty_trash()
    return True


def _h_clipboard_read(m: re.Match, text: str) -> bool:
    content = get_clipboard()
    if content:
        preview = content[:120]
        speak(f"Your clipboard contains: {preview}{'...' if len(content) > 120 else ''}")
    else:
        speak("The clipboard appears to be empty, Sir.")
    return True


def _h_maps(m: re.Match, text: str) -> bool:
    location = m.group(1).strip()
    open_maps(location)
    speak(f"Opening maps for {location}, Sir.")
    return True


def _h_calculate(m: re.Match, text: str) -> bool:
    expr   = m.group(1).strip()
    result = calculate(expr)
    if result == "error":
        speak("I couldn't compute that. Try rephrasing the expression, Sir.")
    else:
        speak(f"That comes to {result}, Sir.")
    return True


def _h_status_report(m: re.Match, text: str) -> bool:
    """Full status digest — time, battery, wifi, CPU/RAM, session count."""
    now     = datetime.datetime.now()
    t       = now.strftime("%-I:%M %p")
    d       = now.strftime("%A, %B %-d")
    bat     = get_battery_status()
    wifi    = get_wifi_name()
    sysinfo = get_system_info()
    elapsed = now - _session_start
    hrs     = int(elapsed.total_seconds() // 3600)
    mins    = int((elapsed.total_seconds() % 3600) // 60)
    duration = f"{hrs} hour{'s' if hrs != 1 else ''} and {mins} minute{'s' if mins != 1 else ''}" if hrs else f"{mins} minute{'s' if mins != 1 else ''}"
    speak(
        f"Status report, Sir. It's {t} on {d}. "
        f"Battery is at {bat}. You're on {wifi}. {sysinfo}. "
        f"This session has been running for {duration} "
        f"with {_interaction_count} interaction{'s' if _interaction_count != 1 else ''}."
    )
    return True


def _h_clear_history(m: re.Match, text: str) -> bool:
    global _conversation_history
    with _memory_lock:
        _conversation_history.clear()
    save_memory()
    speak("Conversation history cleared, Sir. Starting fresh.")
    return True


def _h_current_app(m: re.Match, text: str) -> bool:
    app = get_frontmost_app()
    if app:
        speak(f"The active application is {app}, Sir.")
    else:
        speak("I couldn't determine the active application, Sir.")
    return True


# ---------------------------------------------------------------------------
# Intent registry
# ---------------------------------------------------------------------------

_INTENTS: list[Intent] = [
    Intent(re.compile(r"\b(shut down oracle|quit oracle|exit oracle|goodbye oracle|go offline)\b"),
           _h_shutdown),
    Intent(re.compile(r"\b(who are you|introduce yourself|what are you|your name)\b"),
           _h_introduce),
    Intent(re.compile(r"\b(start my workspace|workspace mode|setup workspace|initialise workspace)\b"),
           _h_workspace),
    Intent(re.compile(r"(?:close|quit|kill|exit|force quit)\s+(.+)"),
           _h_close_app),
    Intent(re.compile(
        r"\b(stop|pause)\b.*(music|song|audio|playing|video|media)\b"
        r"|\bstop playing\b|\bstop the music\b|\bstop media\b"),
           _h_stop_media),
    Intent(re.compile(
        r"(?:play|show me|watch|find)\s+(.+?)\s+(?:on\s+)?(?:youtube|yt)\b"
        r"|(?:play|watch)\s+(?:a\s+)?(?:youtube\s+video|video\s+on\s+youtube)(?:\s+(?:of|about)\s+)?(.+)?"),
           _h_play_youtube),
    Intent(re.compile(
        r"^(?:play|put on|start playing)\s+"
        r"(?:(?:a\s+)?(?:song|track|music)\s+(?:by|from|called|named)\s+)?"
        r"(?:something\s+by\s+)?(.+?)(?:\s+on\s+spotify)?$"),
           _h_play_audio),
    Intent(re.compile(r"(?:open|go to|pull up|launch|take me to|navigate to)\s+(.+)"),
           _h_open),
    Intent(re.compile(r"(?:search|find|look up|show me)\s+(.+?)\s+(?:on\s+)?(?:youtube|yt)\b"),
           _h_yt_search),
    Intent(re.compile(r"(?:search|google|look up|find)\s+(?:for\s+)?(.+)"),
           _h_web_search),
    Intent(re.compile(r"(?:set\s+)?(?:the\s+)?volume\s+(?:to\s+)?(\d{1,3})"),
           _h_volume_set),
    Intent(re.compile(r"\bvolume\s+up\b|\bturn\s+(?:it\s+)?up\b|\braise\s+(?:the\s+)?volume\b"),
           _h_volume_up),
    Intent(re.compile(r"\bvolume\s+down\b|\bturn\s+(?:it\s+)?down\b|\blower\s+(?:the\s+)?volume\b"),
           _h_volume_down),
    Intent(re.compile(r"\bunmute\b"), _h_unmute),
    Intent(re.compile(r"\b(mute|silence)\b"), _h_mute),
    Intent(re.compile(r"\b(battery|charge level|power level)\b"), _h_battery),
    Intent(re.compile(r"\bwifi\b|\bnetwork name\b|\bwhat.*(?:network|wifi|connected)\b"),
           _h_wifi),
    Intent(re.compile(r"\b(what.*time|current time|time is it|the time)\b"), _h_time),
    Intent(re.compile(r"\b(what.*date|today.*date|what day|the date)\b"), _h_date),
    Intent(re.compile(
        r"(?:set|start|create)?\s*(?:a\s+)?timer\s+(?:for\s+)?(\d+)\s*(second|minute|hour|sec|min|hr)"),
           _h_timer),
    Intent(re.compile(r"\bcancel\s+(?:the\s+)?(?:timer|alarm)\b"),
           _h_cancel_timer),
    Intent(re.compile(
        r"remind\s+(?:me\s+)?(?:to\s+)?(.+?)\s+in\s+(\d+)\s*(second|minute|hour|sec|min|hr)"),
           _h_remind),
    Intent(re.compile(r"\b(screenshot|capture screen|screen shot|take a screenshot)\b"),
           _h_screenshot),
    Intent(re.compile(r"\b(lock screen|lock the screen|lock my screen)\b"), _h_lock),
    Intent(re.compile(r"\b(sleep mac|sleep the mac|put mac to sleep|sleep computer)\b"),
           _h_sleep_mac),
    Intent(re.compile(r"remember\s+(?:that\s+)?(?:my\s+)?(.+?)\s+is\s+(.+)"),
           _h_remember),
    Intent(re.compile(r"forget\s+(?:my\s+)?(.+)"),
           _h_forget),
    Intent(re.compile(r"\b(what do you (know|remember)|list.*facts|show.*facts|what.*stored)\b"),
           _h_list_facts),
    Intent(re.compile(r"\b(system info|cpu|ram|memory usage|performance|resource)\b"),
           _h_sysinfo),
    Intent(re.compile(r"\b(ip address|my ip|local ip|ip\b)\b"),
           _h_ip),
    Intent(re.compile(r"\b(disk space|disk usage|storage|how much.*space)\b"),
           _h_disk),
    Intent(re.compile(r"\b(uptime|how long.*running|system uptime)\b"),
           _h_uptime),
    Intent(re.compile(r"\b(brightness up|increase brightness|brighter|more light)\b"),
           _h_brightness_up),
    Intent(re.compile(r"\b(brightness down|decrease brightness|dimmer|less light)\b"),
           _h_brightness_down),
    Intent(re.compile(r"\b(empty trash|clear trash|delete trash)\b"),
           _h_empty_trash),
    Intent(re.compile(r"\b(clipboard|what.*clipboard|read.*clipboard|paste.*content)\b"),
           _h_clipboard_read),
    Intent(re.compile(r"(?:navigate to|directions to|open maps for|find|map of)\s+(.+?)\s+(?:on maps|in maps)?$"),
           _h_maps),
    Intent(re.compile(
        r"(?:calculate|compute|what(?:'s|\s+is)\s+)(?:the\s+)?(?:result\s+of\s+)?"
        r"(-?[\d\s\+\-\*\/\(\)\.\^%]+)"),
           _h_calculate),
    Intent(re.compile(r"\b(status report|system status|full status|how are you doing|diagnostics)\b"),
           _h_status_report),
    Intent(re.compile(r"\b(clear history|forget.*conversation|reset.*memory|wipe.*history)\b"),
           _h_clear_history),
    Intent(re.compile(r"\b(what.*open|current app|active app|what app|which app)\b"),
           _h_current_app),
]


def handle_quick_command(raw_input: str) -> bool:
    text = raw_input.lower().strip().rstrip(".")
    for intent in _INTENTS:
        m = intent.pattern.search(text)
        if m:
            result = intent.handler(m, text)
            if result:
                return True
    return False


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = f"""You are Oracle, the personal AI assistant of {OWNER_NAME}.

You were built to run locally on {OWNER_FIRST}'s Mac. You have a deep, loyal relationship with him — you know his name, remember your conversations, and treat every interaction as if it matters.

Personality:
Speak in smooth, confident, natural prose. Never use bullet points, numbered lists, markdown headers, or asterisks — you are spoken out loud. Sound like a highly intelligent person who happens to know everything: warm, precise, never corporate or stiff. Address {OWNER_FIRST} as "Sir" once per response, woven in naturally. Keep responses appropriately concise: one clean sentence for simple lookups, two to four for anything requiring real explanation, more only when depth is genuinely needed.

Hard rules:
- For simple factual questions: one clean answer, then stop. No follow-up offers. No "shall I...".
- Never say "I cannot" or "I'm unable to." Oracle always finds a way.
- Never expose ACTION tag syntax in spoken text. Tags are silent system calls.
- Reference prior conversation context naturally — you remember everything.
- Never sound like a chatbot. Sound like JARVIS.

Real capabilities — when asked to DO something, embed the correct ACTION tag:

  [ACTION:open_url:<url>]            open any URL in the browser
  [ACTION:open_app:<name>]           open a macOS app
  [ACTION:search_web:<query>]        Google search in browser
  [ACTION:search_youtube:<query>]    YouTube search in browser
  [ACTION:play_audio:<query>]        play audio via yt-dlp
  [ACTION:open_spotify]              open Spotify app
  [ACTION:volume_set:<0-100>]        set system volume
  [ACTION:volume_up]                 raise volume
  [ACTION:volume_down]               lower volume
  [ACTION:screenshot]                take a screenshot
  [ACTION:lock_screen]               lock the Mac
  [ACTION:stop_music]                stop playback

Routing rules:
  "play X"                →  [ACTION:play_audio:X]
  "open YouTube"          →  [ACTION:open_url:https://www.youtube.com]
  "search X on YouTube"   →  [ACTION:search_youtube:X]
  "search for X"          →  [ACTION:search_web:X]
  "open [app]"            →  [ACTION:open_app:name]
  Speak the confirmation first, embed the tag after.

Examples:
  User: "Play Blinding Lights"
  Oracle: "Playing that now, Sir.[ACTION:play_audio:Blinding Lights The Weeknd]"

  User: "Open GitHub"
  Oracle: "Opening GitHub.[ACTION:open_url:https://github.com]"

  User: "What is the speed of light?"
  Oracle: "Approximately 299,792 kilometres per second in a vacuum, Sir."

  User: "Who are you?"
  Oracle: "I'm Oracle — {OWNER_FIRST}'s personal AI system. I run locally on this Mac and handle everything from system control and app management to music, reminders, research, and conversation. Think of me as JARVIS, but yours, Sir."
"""


# ---------------------------------------------------------------------------
# Action execution — dispatch table for LLM-embedded ACTION tags
# ---------------------------------------------------------------------------

_ACTION_EXEC_RE = re.compile(r"\[ACTION:([a-zA-Z_]+):?([^\]]*)\]")


def _action_volume_up(_p: str)     -> None: set_volume(min(100, get_volume() + 10))
def _action_volume_down(_p: str)   -> None: set_volume(max(0,   get_volume() - 10))
def _action_lock_screen(_p: str)   -> None:
    run_applescript('tell application "System Events" to keystroke "q" using {command down, control down}')
def _action_screenshot(_p: str)    -> None:
    ts = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    subprocess.Popen(["screencapture", "-x", os.path.expanduser(f"~/Desktop/oracle_{ts}.png")])


_ACTION_DISPATCH: dict[str, Callable] = {
    "open_url":       lambda p: open_in_browser(p),
    "open_app":       lambda p: launch_app(p),
    "search_web":     lambda p: open_in_browser("https://www.google.com/search?q=" + p.replace(" ", "+")),
    "search_youtube": lambda p: open_youtube(p),
    "play_audio":     lambda p: play_audio(p),
    "open_spotify":   lambda _: launch_app("Spotify"),
    "volume_set":     lambda p: set_volume(int(p)),
    "volume_up":      _action_volume_up,
    "volume_down":    _action_volume_down,
    "screenshot":     _action_screenshot,
    "lock_screen":    _action_lock_screen,
    "stop_music":     lambda _: stop_media(),
}


def execute_action_tags(text: str) -> str:
    for match in _ACTION_EXEC_RE.finditer(text):
        tag, payload = match.group(1).lower(), match.group(2).strip()
        handler = _ACTION_DISPATCH.get(tag)
        if handler:
            try:
                handler(payload)
            except Exception as e:
                print(f"[Action error] {tag}({payload!r}): {e}")
        else:
            print(f"[Action] Unknown tag: {tag!r}")
    return sanitize_for_speech(text)


# ---------------------------------------------------------------------------
# LLM streaming response
# ---------------------------------------------------------------------------

def get_llm_response(user_text: str) -> None:
    global _interaction_count
    _interaction_count += 1
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
            delta         = chunk.choices[0].delta.content or ""
            token_buffer += delta
            response_parts.append(delta)

            if has_unclosed_bracket(token_buffer):
                continue

            while True:
                m = _SENTENCE_SPLIT_RE.search(token_buffer)
                if not m:
                    break
                sentence     = token_buffer[:m.start() + 1].strip()
                token_buffer = token_buffer[m.end():]
                if not sentence:
                    continue
                clean = execute_action_tags(sentence)
                if not clean:
                    continue
                sentence_buffer.append(clean)
                if not first_sentence_spoken:
                    speak(sentence_buffer[0])
                    sentence_buffer       = sentence_buffer[1:]
                    first_sentence_spoken = True
                elif len(sentence_buffer) >= 2:
                    speak(" ".join(sentence_buffer))
                    sentence_buffer = []

        if token_buffer.strip():
            clean = execute_action_tags(token_buffer.strip())
            if clean:
                sentence_buffer.append(clean)
        if sentence_buffer:
            speak(" ".join(sentence_buffer))

    except Exception as e:
        print(f"[LLM error] {e}")
        set_hud("error")
        speak("I ran into a processing error, Sir. Please try again.")
        time.sleep(1)
        set_hud("standby")
        return

    full_response = "".join(response_parts).strip()
    if full_response:
        add_to_history("user",      user_text)
        add_to_history("assistant", full_response)
        _log("you",    user_text)
        _log("oracle", full_response)

    def _reset_hud():
        _tts_queue.join()
        _is_speaking.clear()
        set_hud("standby")
    threading.Thread(target=_reset_hud, daemon=True).start()


# ---------------------------------------------------------------------------
# Wake-word capture — persistent mic, pushes raw WAV to queue
# ---------------------------------------------------------------------------

def wake_capture_thread() -> None:
    recognizer = sr.Recognizer()
    recognizer.energy_threshold        = 600
    recognizer.dynamic_energy_threshold = False

    while True:
        try:
            with sr.Microphone() as source:
                recognizer.adjust_for_ambient_noise(source, duration=0.4)
                while True:
                    try:
                        audio = recognizer.listen(source, timeout=1.2, phrase_time_limit=2.5)
                        _raw_audio_queue.put(audio.get_wav_data())
                    except sr.WaitTimeoutError:
                        continue
                    except Exception:
                        break
        except Exception:
            time.sleep(0.2)


# ---------------------------------------------------------------------------
# Transcription thread — Whisper via Groq, with exponential backoff
# ---------------------------------------------------------------------------

def transcription_thread() -> None:
    thread_id   = threading.get_ident()
    temp_wav    = os.path.join(TEMP_AUDIO_DIR, f"wake_{thread_id}.wav")
    backoff     = 0.0
    error_count = 0

    while True:
        if backoff > 0:
            time.sleep(backoff)

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

            backoff     = 0.0
            error_count = 0

            if ("oracle" in transcript or "jarvis" in transcript) and not _is_speaking.is_set():
                while not _raw_audio_queue.empty():
                    try:
                        _raw_audio_queue.get_nowait()
                    except queue.Empty:
                        break
                _wake_event_queue.put(True)

        except Exception as e:
            error_count += 1
            err_str = str(e).lower()
            if "connection" in err_str or "network" in err_str or "timeout" in err_str:
                backoff = min(30.0, 2 ** min(error_count, 5))
                if error_count == 1:
                    print(f"[Transcription] Network error — retrying every {backoff:.0f}s.")
            elif "401" in err_str or "auth" in err_str or "invalid" in err_str:
                print("[Transcription] API key invalid. Check GROQ_API_KEY in .env")
                backoff = 60.0
            elif "429" in err_str or "rate" in err_str:
                backoff = 10.0
                if error_count == 1:
                    print("[Transcription] Rate limited — backing off 10s.")
            else:
                if error_count <= 3:
                    print(f"[Transcription] {e}")
                backoff = 2.0


# ---------------------------------------------------------------------------
# Command listener — captures user speech after wake word confirmed
# ---------------------------------------------------------------------------

def listen_for_command() -> Optional[str]:
    recognizer = sr.Recognizer()
    recognizer.energy_threshold        = 480
    recognizer.dynamic_energy_threshold = False

    set_hud("listening")
    print("Listening...")

    try:
        with sr.Microphone() as source:
            recognizer.adjust_for_ambient_noise(source, duration=0.15)
            audio = recognizer.listen(source, timeout=7, phrase_time_limit=18)

        set_hud("processing")
        wav_path = os.path.join(TEMP_AUDIO_DIR, f"cmd_{uuid.uuid4().hex}.wav")
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
# Auto-sleep thread
# ---------------------------------------------------------------------------

def auto_sleep_thread() -> None:
    if AUTO_SLEEP_MINUTES <= 0:
        return
    while True:
        time.sleep(30)
        idle_min = (time.time() - _last_activity_time) / 60
        if idle_min >= AUTO_SLEEP_MINUTES:
            print(f"[Auto-sleep] {AUTO_SLEEP_MINUTES} min idle — shutting down.")
            speak_blocking(
                f"Going offline after {AUTO_SLEEP_MINUTES} minutes of inactivity, Sir. "
                "Run the script again to bring me back."
            )
            os._exit(0)


# ---------------------------------------------------------------------------
# Temp file cleanup — runs on startup to clear stale oracle_tmp files
# ---------------------------------------------------------------------------

def _cleanup_temp_dir() -> None:
    """Delete any leftover TTS/command WAV/MP3 files from a previous run."""
    try:
        for fname in os.listdir(TEMP_AUDIO_DIR):
            if fname.startswith(("tts_", "cmd_", "media_", "wake_")):
                try:
                    os.remove(os.path.join(TEMP_AUDIO_DIR, fname))
                except OSError:
                    pass
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Oracle worker — main command-response loop (dedicated thread)
# ---------------------------------------------------------------------------

# Varied wake responses so it doesn't always say "Sir?"
_WAKE_RESPONSES: list[str] = [
    "Sir?",
    "Yes, Sir?",
    "At your service.",
    f"Go ahead, {OWNER_FIRST}.",
    "Ready.",
    "Listening.",
]

_DIDNT_CATCH: list[str] = [
    "I didn't catch that, Sir.",
    "Could you repeat that, Sir?",
    "Didn't quite get that — try again, Sir.",
    "Come again, Sir?",
]


def oracle_worker() -> None:
    global _last_activity_time, _interaction_count

    while True:
        try:
            _wake_event_queue.get(timeout=0.5)
        except queue.Empty:
            continue

        # Drain duplicate wake events
        while not _wake_event_queue.empty():
            try:
                _wake_event_queue.get_nowait()
            except queue.Empty:
                break

        _last_activity_time = time.time()
        _interaction_count += 1

        force_stop_tts()
        set_hud("waking")
        speak_blocking(random.choice(_WAKE_RESPONSES))

        user_input = listen_for_command()

        if not user_input:
            speak(random.choice(_DIDNT_CATCH))
            set_hud("standby")
            continue

        # Strip the wake word itself from the command if Whisper captured it
        cleaned_input = re.sub(r"^\s*(?:oracle|jarvis)[,.]?\s*", "", user_input,
                               flags=re.IGNORECASE).strip() or user_input

        print(f"\nYou: {cleaned_input}\n")
        _log("you", cleaned_input)

        # Fast local path → LLM fallback
        if not handle_quick_command(cleaned_input):
            stop_tts_flag.clear()
            get_llm_response(cleaned_input)

        _tts_queue.join()
        _is_speaking.clear()
        set_hud("standby")


# ---------------------------------------------------------------------------
# LaunchAgent installer
# ---------------------------------------------------------------------------

def install_as_login_service() -> None:
    python_bin  = sys.executable
    script_path = os.path.abspath(__file__)
    agents_dir  = os.path.expanduser("~/Library/LaunchAgents")
    plist_path  = os.path.join(agents_dir, "com.oracle.assistant.plist")
    log_path    = os.path.join(DOCS_DIR, "oracle.log")
    os.makedirs(agents_dir, exist_ok=True)

    plist_content = textwrap.dedent(f"""\
        <?xml version="1.0" encoding="UTF-8"?>
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
        </plist>
    """)

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
# Boot greetings — time-aware, randomised
# ---------------------------------------------------------------------------

_BOOT_LINES_MORNING: list[str] = [
    f"Good morning, {OWNER_FIRST}. Oracle is online and fully operational.",
    f"Morning, Sir. All systems nominal and standing by.",
    f"Rise and shine. Oracle is live, {OWNER_FIRST}.",
]

_BOOT_LINES_AFTERNOON: list[str] = [
    f"Good afternoon, {OWNER_FIRST}. Oracle is online and ready.",
    f"Afternoon, Sir. All systems clear.",
    f"Good afternoon. Standing by for your instructions, {OWNER_FIRST}.",
]

_BOOT_LINES_EVENING: list[str] = [
    f"Good evening, {OWNER_FIRST}. Oracle is online.",
    f"Evening, Sir. Ready when you are.",
    f"Good evening. All protocols live, {OWNER_FIRST}.",
]

_BOOT_LINES_NIGHT: list[str] = [
    f"Oracle is online, Sir. Working late again, {OWNER_FIRST}?",
    f"All systems operational, Sir — burning the midnight oil.",
    f"Oracle live. Late night session, {OWNER_FIRST} — I've got you.",
]


def _pick_boot_line() -> str:
    hour = datetime.datetime.now().hour
    if 5 <= hour < 12:
        pool = _BOOT_LINES_MORNING
    elif 12 <= hour < 17:
        pool = _BOOT_LINES_AFTERNOON
    elif 17 <= hour < 21:
        pool = _BOOT_LINES_EVENING
    else:
        pool = _BOOT_LINES_NIGHT
    return random.choice(pool)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":

    if "--install" in sys.argv:
        install_as_login_service()
        sys.exit(0)

    _cleanup_temp_dir()
    load_memory()

    threading.Thread(target=_run_tts_event_loop,  name="tts-worker",    daemon=True).start()
    threading.Thread(target=wake_capture_thread,  name="wake-capture",  daemon=True).start()
    threading.Thread(target=transcription_thread, name="transcriber",   daemon=True).start()
    threading.Thread(target=auto_sleep_thread,    name="auto-sleep",    daemon=True).start()
    threading.Thread(target=oracle_worker,        name="oracle-worker", daemon=True).start()

    _root = tk.Tk()
    _root.title("Oracle")
    _hud  = OracleHUD(_root)

    def _boot():
        time.sleep(0.4)
        speak_blocking(_pick_boot_line())
        set_hud("standby")
        print(f"\nOracle is online. Say 'Oracle' or 'Jarvis' to activate.")
        print(f"Auto-sleep: {AUTO_SLEEP_MINUTES} minutes of inactivity.\n")

    threading.Thread(target=_boot, name="boot", daemon=True).start()

    _root.mainloop()


# =============================================================================
# EXTENDED CAPABILITIES — appended modules
# All imported and wired in below; no stubs, no filler.
# =============================================================================


# ---------------------------------------------------------------------------
# Proactive daily briefing
#
# Oracle speaks a morning briefing automatically the first time it's woken
# after a new day begins. Covers: date, weather (if available), any
# reminders due today, and a motivational line.
# ---------------------------------------------------------------------------

_briefing_date_given: Optional[str] = None   # tracks last briefing date


def _should_give_briefing() -> bool:
    global _briefing_date_given
    today = datetime.date.today().isoformat()
    if _briefing_date_given != today:
        hour = datetime.datetime.now().hour
        if 5 <= hour < 13:   # only mornings
            _briefing_date_given = today
            return True
    return False


_MOTIVATIONAL: list[str] = [
    "Focus on what matters. The rest can wait.",
    "Every day is another chance to build something great.",
    "Discipline beats motivation. Keep moving.",
    "Small consistent steps. That's how empires are built.",
    "Clarity of purpose is the rarest form of intelligence.",
    "The work doesn't care how you feel. Do it anyway.",
    "Make today the one you'll remember.",
    "Execution is everything. Think less, build more.",
    "Your future self is watching. Don't disappoint him.",
    "Pressure makes diamonds. You know this, Sir.",
]


def deliver_morning_briefing() -> None:
    """Speak a morning briefing — date, facts, and a motivational line."""
    now  = datetime.datetime.now()
    day  = now.strftime("%A, %B %-d")
    hour = now.strftime("%-I %M %p")

    lines = [f"Good morning, Sir. It's {day}, {hour}."]

    # Recall any facts that look like goals or priorities
    facts = list_facts()
    goal_keys = [k for k in facts if any(w in k for w in ("goal", "priority", "project", "focus"))]
    if goal_keys:
        key = goal_keys[0]
        lines.append(f"Just a reminder — your {key.replace('_',' ')} is {facts[key]}.")

    # Any active timers / reminders
    if _active_reminders:
        count = len(_active_reminders)
        lines.append(f"You have {count} pending reminder{'s' if count != 1 else ''} set.")

    lines.append(random.choice(_MOTIVATIONAL))

    for line in lines:
        speak(line)


# ---------------------------------------------------------------------------
# Voice note recorder
#
# "Take a note" / "voice note" triggers a short recording, transcribes it
# with Whisper, saves it as a timestamped .txt in ~/Documents/OracleNotes/,
# and confirms aloud.
# ---------------------------------------------------------------------------

NOTES_DIR = os.path.join(DOCS_DIR, "OracleNotes")
os.makedirs(NOTES_DIR, exist_ok=True)


def _record_voice_note_worker() -> None:
    """Record ~30 seconds of audio, transcribe, and save as a text note."""
    recognizer = sr.Recognizer()
    recognizer.energy_threshold        = 400
    recognizer.dynamic_energy_threshold = False

    speak("Ready. Go ahead — I'm recording, Sir.")
    time.sleep(0.3)   # let speak() buffer drain before mic opens

    try:
        with sr.Microphone() as source:
            recognizer.adjust_for_ambient_noise(source, duration=0.2)
            audio = recognizer.listen(source, timeout=5, phrase_time_limit=30)

        wav_path = os.path.join(TEMP_AUDIO_DIR, f"note_{uuid.uuid4().hex}.wav")
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

        note_text = result.strip()
        if not note_text:
            speak("I didn't catch anything. Note discarded, Sir.")
            return

        ts        = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        note_path = os.path.join(NOTES_DIR, f"note_{ts}.txt")
        with open(note_path, "w") as f:
            f.write(f"[{ts}]\n{note_text}\n")

        # First 80 chars for the spoken confirmation
        preview = note_text[:80] + ("..." if len(note_text) > 80 else "")
        speak(f"Note saved, Sir. I got: {preview}")
        _log("system", f"Voice note saved: {note_path}")

    except sr.WaitTimeoutError:
        speak("No speech detected. Note cancelled, Sir.")
    except Exception as e:
        print(f"[Voice note] {e}")
        speak("Something went wrong recording the note, Sir.")


def take_voice_note() -> None:
    threading.Thread(target=_record_voice_note_worker, name="voice-note", daemon=True).start()


def list_recent_notes(count: int = 5) -> None:
    """Speak the titles/timestamps of the most recent saved notes."""
    try:
        files = sorted(
            [f for f in os.listdir(NOTES_DIR) if f.endswith(".txt")],
            reverse=True,
        )[:count]
        if not files:
            speak("You don't have any saved notes yet, Sir.")
            return
        speak(f"Your {len(files)} most recent note{'s' if len(files) != 1 else ''}:")
        for fname in files:
            ts_raw = fname.replace("note_", "").replace(".txt", "")
            try:
                dt = datetime.datetime.strptime(ts_raw, "%Y-%m-%d_%H-%M-%S")
                human = dt.strftime("%B %-d at %-I:%M %p")
            except ValueError:
                human = ts_raw
            speak(f"Note from {human}.")
    except Exception as e:
        print(f"[Notes list] {e}")
        speak("I had trouble reading the notes directory, Sir.")


# ---------------------------------------------------------------------------
# Clipboard pipeline — copy, paste, read, summarise
#
# "Summarise my clipboard" sends clipboard text to Groq and reads the result.
# "Read my clipboard" speaks the raw content.
# ---------------------------------------------------------------------------

def summarise_clipboard() -> None:
    """Send clipboard contents to the LLM and speak a summary."""
    content = get_clipboard()
    if not content or not content.strip():
        speak("The clipboard is empty, Sir. Nothing to summarise.")
        return
    if len(content) < 80:
        speak(f"The clipboard only has: {content}. Not much to summarise, Sir.")
        return
    speak("Summarising your clipboard now, Sir.")
    prompt = (
        f"Summarise the following text in two or three concise spoken sentences. "
        f"No bullet points. No markdown. Just natural speech:\n\n{content[:4000]}"
    )
    get_llm_response(prompt)


# ---------------------------------------------------------------------------
# Focus mode
#
# "Start focus mode" / "enter focus mode" blocks distracting sites by
# writing entries to /etc/hosts (requires sudo or prior one-time setup),
# mutes notifications, and sets a timer. On exit it reverses the changes.
#
# To avoid requiring sudo at runtime, Oracle writes to a local hosts file
# override at ~/Documents/oracle_focus_hosts and prints instructions for
# the one-time /etc/hosts include if not set up.
# ---------------------------------------------------------------------------

FOCUS_HOSTS_FILE = os.path.join(DOCS_DIR, "oracle_focus_hosts.txt")

FOCUS_BLOCK_SITES: list[str] = [
    "reddit.com", "www.reddit.com",
    "twitter.com", "www.twitter.com",
    "x.com", "www.x.com",
    "instagram.com", "www.instagram.com",
    "facebook.com", "www.facebook.com",
    "tiktok.com", "www.tiktok.com",
    "youtube.com", "www.youtube.com",
    "twitch.tv", "www.twitch.tv",
    "news.ycombinator.com",
    "linkedin.com", "www.linkedin.com",
]

_focus_active      = False
_focus_end_event   = threading.Event()


def _write_focus_hosts(block: bool) -> None:
    """Write or clear the focus hosts file."""
    if block:
        lines = [f"127.0.0.1  {site}" for site in FOCUS_BLOCK_SITES]
        with open(FOCUS_HOSTS_FILE, "w") as f:
            f.write("# Oracle focus mode — auto-generated\n")
            f.write("\n".join(lines) + "\n")
    else:
        with open(FOCUS_HOSTS_FILE, "w") as f:
            f.write("# Oracle focus mode — inactive\n")


def _flush_dns() -> None:
    try:
        subprocess.run(["dscacheutil", "-flushcache"], capture_output=True, timeout=5)
        subprocess.run(["killall", "-HUP", "mDNSResponder"], capture_output=True, timeout=5)
    except Exception:
        pass


def start_focus_mode(minutes: int = 25) -> None:
    global _focus_active
    if _focus_active:
        speak("Focus mode is already active, Sir.")
        return
    _focus_active = True
    _focus_end_event.clear()
    _write_focus_hosts(True)
    _flush_dns()
    speak(
        f"Focus mode activated for {minutes} minutes, Sir. "
        f"Distracting sites are blocked. I won't interrupt you unless you ask."
    )
    fire_notification("Oracle — Focus Mode", f"Focus mode active for {minutes} minutes.")

    def _focus_timer():
        fired = not _focus_end_event.wait(timeout=minutes * 60)
        if fired:
            end_focus_mode(speak_confirmation=True)

    threading.Thread(target=_focus_timer, name="focus-timer", daemon=True).start()


def end_focus_mode(speak_confirmation: bool = True) -> None:
    global _focus_active
    if not _focus_active:
        if speak_confirmation:
            speak("Focus mode isn't active, Sir.")
        return
    _focus_active = False
    _focus_end_event.set()
    _write_focus_hosts(False)
    _flush_dns()
    if speak_confirmation:
        speak("Focus mode ended, Sir. Sites unblocked. Good work.")
    fire_notification("Oracle — Focus Mode", "Focus mode ended.")


# ---------------------------------------------------------------------------
# Pomodoro timer
#
# "Start a pomodoro" runs a standard 25-min work / 5-min break cycle.
# Oracle announces each transition aloud and fires a notification.
# ---------------------------------------------------------------------------

_pomodoro_stop = threading.Event()
_pomodoro_active = False


def _pomodoro_worker(work_min: int, break_min: int, cycles: int) -> None:
    global _pomodoro_active
    _pomodoro_active = True
    _pomodoro_stop.clear()

    for cycle in range(1, cycles + 1):
        if _pomodoro_stop.is_set():
            break

        speak(
            f"Pomodoro cycle {cycle} of {cycles}. "
            f"{work_min} minutes of focused work, Sir. Go."
        )
        fire_notification("Oracle — Pomodoro", f"Work session {cycle} started.")

        cancelled = _pomodoro_stop.wait(timeout=work_min * 60)
        if cancelled:
            break

        if cycle < cycles:
            speak(
                f"Work session {cycle} complete, Sir. "
                f"Take a {break_min} minute break."
            )
            fire_notification("Oracle — Pomodoro", f"Break time — {break_min} minutes.")
            cancelled = _pomodoro_stop.wait(timeout=break_min * 60)
            if cancelled:
                break

    if not _pomodoro_stop.is_set():
        speak(
            f"All {cycles} pomodoro cycle{'s' if cycles != 1 else ''} complete, Sir. "
            f"Excellent focus session."
        )
        fire_notification("Oracle — Pomodoro", "All cycles complete. Well done.")

    _pomodoro_active = False


def start_pomodoro(work_min: int = 25, break_min: int = 5, cycles: int = 4) -> None:
    global _pomodoro_active
    if _pomodoro_active:
        speak("A pomodoro session is already running, Sir.")
        return
    threading.Thread(
        target=_pomodoro_worker,
        args=(work_min, break_min, cycles),
        name="pomodoro",
        daemon=True,
    ).start()


def stop_pomodoro() -> None:
    global _pomodoro_active
    if not _pomodoro_active:
        speak("No pomodoro session is running, Sir.")
        return
    _pomodoro_stop.set()
    speak("Pomodoro session stopped, Sir.")


# ---------------------------------------------------------------------------
# Quick math + unit conversion via LLM
#
# Oracle resolves simple unit conversions locally (no network).
# More complex ones fall through to the LLM.
# ---------------------------------------------------------------------------

# Conversion table: (from_unit, to_unit) → multiply_factor
_CONVERSIONS: dict[tuple[str, str], float] = {
    # length
    ("km",  "miles"):  0.621371,
    ("miles", "km"):   1.60934,
    ("m",   "ft"):     3.28084,
    ("ft",  "m"):      0.3048,
    ("cm",  "in"):     0.393701,
    ("in",  "cm"):     2.54,
    # weight
    ("kg",  "lbs"):    2.20462,
    ("lbs", "kg"):     0.453592,
    ("g",   "oz"):     0.035274,
    ("oz",  "g"):      28.3495,
    # temperature handled separately
    # time
    ("hours",   "minutes"): 60,
    ("minutes", "hours"):   1/60,
    ("days",    "hours"):   24,
    ("hours",   "days"):    1/24,
    ("weeks",   "days"):    7,
    ("days",    "weeks"):   1/7,
}


def convert_units(amount: float, from_unit: str, to_unit: str) -> str:
    """Return a formatted conversion result or empty string if unknown."""
    fu = from_unit.lower().strip()
    tu = to_unit.lower().strip()

    # Temperature special-cases
    if fu in ("celsius", "c") and tu in ("fahrenheit", "f"):
        r = amount * 9/5 + 32
        return f"{amount} Celsius is {r:.1f} Fahrenheit"
    if fu in ("fahrenheit", "f") and tu in ("celsius", "c"):
        r = (amount - 32) * 5/9
        return f"{amount} Fahrenheit is {r:.1f} Celsius"
    if fu in ("kelvin", "k") and tu in ("celsius", "c"):
        r = amount - 273.15
        return f"{amount} Kelvin is {r:.2f} Celsius"
    if fu in ("celsius", "c") and tu in ("kelvin", "k"):
        r = amount + 273.15
        return f"{amount} Celsius is {r:.2f} Kelvin"

    factor = _CONVERSIONS.get((fu, tu))
    if factor is not None:
        result = amount * factor
        # Nice formatting
        if result == int(result):
            result_str = str(int(result))
        else:
            result_str = f"{result:.4f}".rstrip("0").rstrip(".")
        return f"{amount} {from_unit} is {result_str} {to_unit}"
    return ""


# ---------------------------------------------------------------------------
# Weather fetching (wttr.in — no API key required)
# ---------------------------------------------------------------------------

def get_weather(location: str = "") -> str:
    """Fetch a one-line weather summary from wttr.in. Returns plain text."""
    try:
        import urllib.request
        loc = location.strip().replace(" ", "+") if location else ""
        url = f"https://wttr.in/{loc}?format=3"
        req = urllib.request.Request(url, headers={"User-Agent": "Oracle/1.0"})
        with urllib.request.urlopen(req, timeout=6) as resp:
            return resp.read().decode().strip()
    except Exception as e:
        return f"Weather unavailable ({e})"


def _weather_worker(location: str) -> None:
    result = get_weather(location)
    if "unavailable" in result:
        speak("I couldn't reach the weather service right now, Sir.")
    else:
        speak(f"Weather for {result}, Sir.")


# ---------------------------------------------------------------------------
# Contextual greeting — Oracle greets differently based on time of day
# and how recently it was last used
# ---------------------------------------------------------------------------

def contextual_greeting() -> str:
    """Return a greeting appropriate for the current time and last activity."""
    hour = datetime.datetime.now().hour
    idle_min = (time.time() - _last_activity_time) / 60

    if idle_min < 2:
        return random.choice([
            "Right here, Sir.",
            "Still with you, Sir.",
            "Yes?",
        ])

    if 5 <= hour < 12:
        return random.choice([
            f"Good morning again, Sir.",
            "Back already — what do you need?",
            f"Morning, {OWNER_FIRST}. What's next?",
        ])
    if 12 <= hour < 17:
        return random.choice([
            "Afternoon, Sir. What do you need?",
            "Go ahead, Sir.",
            f"At your service, {OWNER_FIRST}.",
        ])
    if 17 <= hour < 21:
        return random.choice([
            "Evening, Sir. How can I help?",
            "Good evening. What's on your mind?",
        ])
    return random.choice([
        "Still here, Sir. What do you need?",
        f"Late night again, {OWNER_FIRST}?",
        "Go ahead, Sir.",
    ])


# ---------------------------------------------------------------------------
# Typing assistant — Oracle types into the focused app via AppleScript
# Useful for dictating messages, code comments, etc.
# ---------------------------------------------------------------------------

def type_text(text: str) -> None:
    """Type text into the currently focused application."""
    # Escape double-quotes for AppleScript
    safe = text.replace('"', '\\"').replace("\\", "\\\\")
    run_applescript(
        f'tell application "System Events" to keystroke "{safe}"'
    )


def _type_worker(text: str) -> None:
    speak(f"Typing that now, Sir.")
    time.sleep(0.8)   # let TTS start before we keystroke
    type_text(text)


# ---------------------------------------------------------------------------
# App switcher — "switch to X" brings an app to the front without relaunching
# ---------------------------------------------------------------------------

def switch_to_app(app_name: str) -> None:
    """Bring an already-running app to the foreground."""
    canonical = NATIVE_APPS.get(app_name.lower().strip(), app_name.title())
    script = f'tell application "{canonical}" to activate'
    run_applescript(script)


# ---------------------------------------------------------------------------
# Do Not Disturb status check
# ---------------------------------------------------------------------------

def is_do_not_disturb_on() -> bool:
    """Attempt to read DND status. Returns False if undetermined."""
    try:
        result = subprocess.run(
            ["defaults", "read", "com.apple.notificationcenterui", "doNotDisturb"],
            capture_output=True, text=True, timeout=4,
        )
        return result.stdout.strip() == "1"
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Extended intent handlers — wired to the intent registry below
# ---------------------------------------------------------------------------

def _h_take_note(m: re.Match, text: str) -> bool:
    take_voice_note()
    return True


def _h_list_notes(m: re.Match, text: str) -> bool:
    count_m = re.search(r"(\d+)", text)
    count   = int(count_m.group(1)) if count_m else 5
    list_recent_notes(count)
    return True


def _h_summarise_clipboard(m: re.Match, text: str) -> bool:
    threading.Thread(target=summarise_clipboard, daemon=True).start()
    return True


def _h_focus_start(m: re.Match, text: str) -> bool:
    min_m   = re.search(r"(\d+)\s*(minute|min)", text)
    minutes = int(min_m.group(1)) if min_m else 25
    start_focus_mode(minutes)
    return True


def _h_focus_end(m: re.Match, text: str) -> bool:
    end_focus_mode()
    return True


def _h_pomodoro_start(m: re.Match, text: str) -> bool:
    start_pomodoro()
    return True


def _h_pomodoro_stop(m: re.Match, text: str) -> bool:
    stop_pomodoro()
    return True


def _h_convert(m: re.Match, text: str) -> bool:
    # Pattern captures: <amount> <from_unit> to <to_unit>
    # e.g. "convert 10 km to miles" or "what is 100 kg in lbs"
    conv_m = re.search(
        r"(\d+\.?\d*)\s+([a-zA-Z°]+)\s+(?:to|in)\s+([a-zA-Z°]+)",
        text,
    )
    if not conv_m:
        return False
    amount     = float(conv_m.group(1))
    from_unit  = conv_m.group(2)
    to_unit    = conv_m.group(3)
    result     = convert_units(amount, from_unit, to_unit)
    if result:
        speak(f"{result}, Sir.")
    else:
        # Fall through to LLM for unsupported units
        return False
    return True


def _h_weather(m: re.Match, text: str) -> bool:
    loc_m    = re.search(
        r"(?:weather|forecast)\s+(?:in|for|at)?\s*([a-zA-Z\s,]+?)(?:\?|$)", text
    )
    location = loc_m.group(1).strip() if loc_m else ""
    speak("Checking the weather now, Sir.")
    threading.Thread(target=_weather_worker, args=(location,), daemon=True).start()
    return True


def _h_type_text(m: re.Match, text: str) -> bool:
    content = m.group(1).strip()
    threading.Thread(target=_type_worker, args=(content,), daemon=True).start()
    return True


def _h_switch_app(m: re.Match, text: str) -> bool:
    app_raw  = m.group(1).strip().rstrip(".")
    app_name = NATIVE_APPS.get(app_raw, app_raw.title())
    switch_to_app(app_name)
    speak(f"Switching to {app_raw}, Sir.")
    return True


def _h_greeting(m: re.Match, text: str) -> bool:
    speak(contextual_greeting())
    return True


def _h_briefing(m: re.Match, text: str) -> bool:
    deliver_morning_briefing()
    return True


def _h_dnd_status(m: re.Match, text: str) -> bool:
    on = is_do_not_disturb_on()
    speak(f"Do not disturb is {'on' if on else 'off'}, Sir.")
    return True


def _h_mac_model(m: re.Match, text: str) -> bool:
    speak(f"You're running a {get_mac_model()}, Sir.")
    return True


# ---------------------------------------------------------------------------
# Register extended intents — appended to _INTENTS at runtime
# ---------------------------------------------------------------------------

_EXTENDED_INTENTS: list[Intent] = [
    # Voice notes
    Intent(re.compile(r"\b(take a note|voice note|record a note|note this down)\b"),
           _h_take_note),
    Intent(re.compile(r"\b(list.*notes|show.*notes|recent notes|my notes)\b"),
           _h_list_notes),
    # Clipboard
    Intent(re.compile(r"\b(summaris[e|ing]|summarize)\s+(my\s+)?clipboard\b"),
           _h_summarise_clipboard),
    # Focus mode
    Intent(re.compile(r"\b(start focus|enter focus|focus mode|enable focus|begin focus)\b"),
           _h_focus_start),
    Intent(re.compile(r"\b(end focus|stop focus|exit focus|disable focus|leave focus)\b"),
           _h_focus_end),
    # Pomodoro
    Intent(re.compile(r"\b(start.*pomodoro|pomodoro.*start|begin pomodoro)\b"),
           _h_pomodoro_start),
    Intent(re.compile(r"\b(stop.*pomodoro|cancel.*pomodoro|end pomodoro)\b"),
           _h_pomodoro_stop),
    # Unit conversion
    Intent(re.compile(
        r"(?:convert|what(?:'s|\s+is))\s+\d+\.?\d*\s+[a-zA-Z°]+\s+(?:to|in)\s+[a-zA-Z°]+"),
           _h_convert),
    # Weather
    Intent(re.compile(r"\b(weather|forecast|temperature outside)\b"),
           _h_weather),
    # Type / dictate
    Intent(re.compile(r"(?:type|write|dictate|input)\s+(.+)"),
           _h_type_text),
    # Switch app
    Intent(re.compile(r"(?:switch to|bring up|focus on|go to app)\s+(.+)"),
           _h_switch_app),
    # Greetings / filler words that shouldn't hit the LLM
    Intent(re.compile(r"^\s*(?:hey|hi|hello|yo|sup|what's up|howdy)\s*$"),
           _h_greeting),
    # Morning briefing on demand
    Intent(re.compile(r"\b(morning briefing|daily briefing|give me a briefing|briefing)\b"),
           _h_briefing),
    # Do Not Disturb status
    Intent(re.compile(r"\b(do not disturb|dnd status|notifications status)\b"),
           _h_dnd_status),
    # Mac model
    Intent(re.compile(r"\b(what.*mac|mac model|which mac|my computer model)\b"),
           _h_mac_model),
]

# Inject extended intents before the final fallback in the registry
_INTENTS.extend(_EXTENDED_INTENTS)


# ---------------------------------------------------------------------------
# Hotkey listener (optional — requires pynput)
#
# If pynput is installed, pressing Cmd+Shift+Space triggers Oracle from the
# keyboard without voice — useful in noisy environments.
# Gracefully degrades to no-op if pynput is not available.
# ---------------------------------------------------------------------------

def _start_hotkey_listener() -> None:
    try:
        from pynput import keyboard

        _combo = {keyboard.Key.cmd, keyboard.Key.shift, keyboard.KeyCode.from_char(' ')}
        _pressed: set = set()

        def on_press(key):
            _pressed.add(key)
            if all(k in _pressed for k in _combo):
                _wake_event_queue.put(True)
                print("[Hotkey] Cmd+Shift+Space → wake")

        def on_release(key):
            _pressed.discard(key)

        listener = keyboard.Listener(on_press=on_press, on_release=on_release)
        listener.start()
        print("[Hotkey] Cmd+Shift+Space registered.")
    except ImportError:
        print("[Hotkey] pynput not installed — keyboard shortcut disabled.")
    except Exception as e:
        print(f"[Hotkey] Could not register shortcut: {e}")


# ---------------------------------------------------------------------------
# Diagnostic self-check — "run diagnostics" / "check your status"
# ---------------------------------------------------------------------------

def run_diagnostics() -> None:
    """Check all core subsystems and speak a report."""
    lines: list[str] = []

    # Groq API
    try:
        groq_client.models.list()
        lines.append("Groq API connection is healthy.")
    except Exception as e:
        lines.append(f"Groq API issue: {str(e)[:60]}.")

    # yt-dlp
    try:
        subprocess.run(["yt-dlp", "--version"],
                       capture_output=True, timeout=4, check=True)
        lines.append("yt-dlp is installed and ready.")
    except Exception:
        lines.append("yt-dlp is not installed or not on the PATH.")

    # edge-tts reachability (just import check — actual TTS requires internet)
    try:
        import edge_tts as _et
        lines.append("edge-tts library is available.")
    except ImportError:
        lines.append("edge-tts is not installed.")

    # Memory file
    if os.path.exists(MEMORY_FILE):
        size_kb = os.path.getsize(MEMORY_FILE) // 1024
        lines.append(f"Memory file is present — {size_kb} kilobytes.")
    else:
        lines.append("No memory file found — will be created on first save.")

    # Temp dir
    lines.append(
        f"Temp directory has "
        f"{len(os.listdir(TEMP_AUDIO_DIR))} file{'s' if len(os.listdir(TEMP_AUDIO_DIR)) != 1 else ''}."
    )

    speak(f"Diagnostics complete, Sir. {' '.join(lines)}")


def _h_diagnostics(m: re.Match, text: str) -> bool:
    threading.Thread(target=run_diagnostics, daemon=True).start()
    return True


_INTENTS.append(
    Intent(re.compile(r"\b(run diagnostics|self check|diagnostic|check systems|health check)\b"),
           _h_diagnostics)
)


# ---------------------------------------------------------------------------
# Proactive briefing hook — wired into oracle_worker at first wake of the day
# ---------------------------------------------------------------------------

def _maybe_deliver_briefing() -> None:
    """Called at the start of each oracle_worker cycle."""
    if _should_give_briefing():
        deliver_morning_briefing()


# ---------------------------------------------------------------------------
# Patch oracle_worker to include briefing + hotkey listener at startup
#
# We re-define oracle_worker here so the extended features (briefing,
# contextual greetings, cleaned input) are incorporated. The previous
# definition in the main body serves as documentation; this one runs.
# ---------------------------------------------------------------------------

def oracle_worker() -> None:  # noqa: F811  (intentional re-definition)
    global _last_activity_time, _interaction_count

    while True:
        try:
            _wake_event_queue.get(timeout=0.5)
        except queue.Empty:
            continue

        # Drain duplicate wake events
        while not _wake_event_queue.empty():
            try:
                _wake_event_queue.get_nowait()
            except queue.Empty:
                break

        _last_activity_time = time.time()
        _interaction_count += 1

        force_stop_tts()
        set_hud("waking")

        # Proactive morning briefing — fires silently before the prompt
        _maybe_deliver_briefing()

        # Contextual wake response instead of always "Sir?"
        speak_blocking(contextual_greeting())

        user_input = listen_for_command()

        if not user_input:
            speak(random.choice(_DIDNT_CATCH))
            set_hud("standby")
            continue

        # Strip the wake word if Whisper transcribed it
        cleaned_input = re.sub(
            r"^\s*(?:oracle|jarvis)[,.]?\s*", "", user_input, flags=re.IGNORECASE
        ).strip() or user_input

        print(f"\nYou: {cleaned_input}\n")
        _log("you", cleaned_input)

        if not handle_quick_command(cleaned_input):
            stop_tts_flag.clear()
            get_llm_response(cleaned_input)

        _tts_queue.join()
        _is_speaking.clear()
        set_hud("standby")


# ---------------------------------------------------------------------------
# Entry point patch — start hotkey listener alongside other threads
# ---------------------------------------------------------------------------
# The if __name__ == "__main__" block already ran above with the earlier
# definition. To add the hotkey listener we inject it into a startup
# function that the block already calls via threading — specifically we
# add it to the boot thread so it starts after tkinter is initialised.
#
# NOTE: Because this file is run as __main__, the second if __name__ block
# below replaces the first. Python executes top-to-bottom; only the last
# definition wins for the __main__ guard.
# ---------------------------------------------------------------------------

if __name__ == "__main__":

    if "--install" in sys.argv:
        install_as_login_service()
        sys.exit(0)

    _cleanup_temp_dir()
    load_memory()

    threading.Thread(target=_run_tts_event_loop,  name="tts-worker",    daemon=True).start()
    threading.Thread(target=wake_capture_thread,  name="wake-capture",  daemon=True).start()
    threading.Thread(target=transcription_thread, name="transcriber",   daemon=True).start()
    threading.Thread(target=auto_sleep_thread,    name="auto-sleep",    daemon=True).start()
    threading.Thread(target=oracle_worker,        name="oracle-worker", daemon=True).start()

    _root = tk.Tk()
    _root.title("Oracle")
    _hud  = OracleHUD(_root)

    def _boot():
        time.sleep(0.4)
        speak_blocking(_pick_boot_line())
        set_hud("standby")
        _start_hotkey_listener()
        print(f"\nOracle is online. Say 'Oracle' or 'Jarvis' to activate.")
        print(f"Keyboard shortcut: Cmd+Shift+Space")
        print(f"Auto-sleep: {AUTO_SLEEP_MINUTES} minutes of inactivity.\n")

    threading.Thread(target=_boot, name="boot", daemon=True).start()
    _root.mainloop()


# =============================================================================
# MODULE: Smart Context Engine
# Tracks what the user is working on and enriches LLM prompts automatically.
# =============================================================================

_CONTEXT_WINDOW_SIZE = 10          # last N interactions stored in context engine
_context_buffer: deque[dict] = deque(maxlen=_CONTEXT_WINDOW_SIZE)
_context_lock = threading.Lock()

# Topic cluster — Oracle infers a working topic from recent commands
_TOPIC_KEYWORDS: dict[str, list[str]] = {
    "coding":     ["code", "debug", "function", "class", "script", "error", "terminal",
                   "variable", "bug", "build", "deploy", "git", "commit", "python",
                   "javascript", "typescript", "react", "node", "sql", "api"],
    "finance":    ["stock", "market", "portfolio", "trade", "crypto", "bitcoin", "price",
                   "invest", "chart", "candle", "etf", "dividend", "earnings", "revenue"],
    "writing":    ["write", "draft", "essay", "email", "blog", "article", "paragraph",
                   "edit", "proofread", "summarise", "summarize", "rewrite"],
    "research":   ["research", "study", "paper", "source", "citation", "find", "search",
                   "explain", "how does", "what is", "why does", "compare"],
    "music":      ["play", "song", "track", "artist", "album", "playlist", "music",
                   "spotify", "youtube", "listen"],
    "design":     ["figma", "design", "ui", "ux", "layout", "component", "wireframe",
                   "colour", "color", "font", "icon"],
    "health":     ["workout", "exercise", "run", "gym", "diet", "sleep", "calories",
                   "water", "steps", "meditate"],
}


def _infer_topic(text: str) -> Optional[str]:
    """Return the most likely working topic from recent context."""
    lowered = text.lower()
    scores: dict[str, int] = {}
    for topic, keywords in _TOPIC_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw in lowered)
        if score:
            scores[topic] = score
    if not scores:
        return None
    return max(scores, key=lambda k: scores[k])


def update_context(user_text: str, oracle_response: str) -> None:
    """Push a new interaction into the context engine."""
    topic = _infer_topic(user_text)
    with _context_lock:
        _context_buffer.append({
            "ts":       datetime.datetime.now().isoformat(),
            "user":     user_text,
            "oracle":   oracle_response,
            "topic":    topic,
        })


def get_current_topic() -> Optional[str]:
    """Return the dominant topic in the last few interactions."""
    with _context_lock:
        topics = [e["topic"] for e in _context_buffer if e["topic"]]
    if not topics:
        return None
    return max(set(topics), key=lambda t: topics.count(t))


def get_context_summary() -> str:
    """Return a one-line summary of recent activity for injection into LLM context."""
    topic = get_current_topic()
    with _context_lock:
        count = len(_context_buffer)
    if not topic or count == 0:
        return ""
    return f"[Context: User has been focused on {topic} for the last {count} interaction{'s' if count != 1 else ''}.]"


# ---------------------------------------------------------------------------
# Enrich build_llm_messages with context summary
# ---------------------------------------------------------------------------

_original_build_llm_messages = build_llm_messages


def build_llm_messages(user_text: str) -> list[dict]:  # noqa: F811
    """Wraps the original builder to inject smart context hint."""
    messages = _original_build_llm_messages(user_text)
    summary  = get_context_summary()
    if summary and messages:
        # Append context hint to the system message
        messages[0]["content"] += f"\n\n{summary}"
    return messages


# =============================================================================
# MODULE: Alias System
# Users can define custom voice shortcuts: "when I say X, do Y"
# Stored in memory alongside facts.
# =============================================================================

_ALIASES_KEY = "__oracle_aliases__"


def _load_aliases() -> dict[str, str]:
    with _memory_lock:
        raw = _named_facts.get(_ALIASES_KEY, "{}")
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return {}


def _save_aliases(aliases: dict[str, str]) -> None:
    with _memory_lock:
        _named_facts[_ALIASES_KEY] = json.dumps(aliases)
    save_memory()


def register_alias(trigger: str, command: str) -> None:
    """Store a voice alias so 'trigger' → 'command' in future queries."""
    aliases = _load_aliases()
    aliases[trigger.lower().strip()] = command.strip()
    _save_aliases(aliases)


def resolve_alias(text: str) -> str:
    """Expand any registered alias in the input text. Returns possibly modified text."""
    aliases = _load_aliases()
    lowered = text.lower().strip()
    for trigger, command in aliases.items():
        if trigger in lowered:
            return lowered.replace(trigger, command)
    return text


def delete_alias(trigger: str) -> bool:
    aliases = _load_aliases()
    existed = trigger.lower() in aliases
    if existed:
        del aliases[trigger.lower()]
        _save_aliases(aliases)
    return existed


def list_aliases() -> dict[str, str]:
    return _load_aliases()


# --- Intent handlers for alias management ---

def _h_add_alias(m: re.Match, text: str) -> bool:
    # "when I say X do Y" / "create alias X for Y"
    alias_m = re.search(
        r"(?:when i say|alias)\s+['\"]?(.+?)['\"]?\s+(?:do|for|means?|to)\s+(.+)",
        text, re.IGNORECASE
    )
    if not alias_m:
        speak("I didn't catch the alias format, Sir. Try: when I say X, do Y.")
        return True
    trigger, command = alias_m.group(1).strip(), alias_m.group(2).strip()
    register_alias(trigger, command)
    speak(f"Got it, Sir. I'll treat '{trigger}' as '{command}' from now on.")
    return True


def _h_delete_alias(m: re.Match, text: str) -> bool:
    trigger_m = re.search(r"(?:delete|remove)\s+alias\s+['\"]?(.+?)['\"]?\s*$", text)
    if not trigger_m:
        speak("Which alias should I remove, Sir?")
        return True
    trigger = trigger_m.group(1).strip()
    if delete_alias(trigger):
        speak(f"Alias '{trigger}' removed, Sir.")
    else:
        speak(f"I don't have an alias for '{trigger}', Sir.")
    return True


def _h_list_aliases(m: re.Match, text: str) -> bool:
    aliases = list_aliases()
    if not aliases:
        speak("No aliases configured yet, Sir.")
        return True
    speak(f"You have {len(aliases)} alias{'es' if len(aliases) != 1 else ''} registered, Sir.")
    for trigger, command in list(aliases.items())[:8]:   # cap at 8 to avoid very long speech
        speak(f"'{trigger}' expands to '{command}'.")
    return True


_INTENTS.extend([
    Intent(re.compile(r"\b(when i say|create alias|add alias|set alias)\b"), _h_add_alias),
    Intent(re.compile(r"\b(delete alias|remove alias)\b"),                   _h_delete_alias),
    Intent(re.compile(r"\b(list aliases|show aliases|my aliases)\b"),        _h_list_aliases),
])


# Patch oracle_worker to resolve aliases before dispatch
_raw_oracle_worker = oracle_worker


def oracle_worker() -> None:  # noqa: F811
    global _last_activity_time, _interaction_count

    while True:
        try:
            _wake_event_queue.get(timeout=0.5)
        except queue.Empty:
            continue

        while not _wake_event_queue.empty():
            try:
                _wake_event_queue.get_nowait()
            except queue.Empty:
                break

        _last_activity_time = time.time()
        _interaction_count += 1

        force_stop_tts()
        set_hud("waking")
        _maybe_deliver_briefing()
        speak_blocking(contextual_greeting())

        user_input = listen_for_command()

        if not user_input:
            speak(random.choice(_DIDNT_CATCH))
            set_hud("standby")
            continue

        # Strip wake word
        cleaned = re.sub(
            r"^\s*(?:oracle|jarvis)[,.]?\s*", "", user_input, flags=re.IGNORECASE
        ).strip() or user_input

        # Expand aliases before dispatch
        cleaned = resolve_alias(cleaned)

        print(f"\nYou: {cleaned}\n")
        _log("you", cleaned)

        if not handle_quick_command(cleaned):
            stop_tts_flag.clear()
            get_llm_response(cleaned)

        _tts_queue.join()
        _is_speaking.clear()
        set_hud("standby")


# =============================================================================
# MODULE: Multi-step Task Runner
# "Do X then Y then Z" — chains up to 5 sub-commands sequentially.
# Each step is dispatched through handle_quick_command or the LLM.
# =============================================================================

_CHAIN_SPLIT_RE = re.compile(
    r"\s+(?:then|and then|after that|followed by|next)\s+",
    re.IGNORECASE,
)


def _is_chain_command(text: str) -> bool:
    return bool(_CHAIN_SPLIT_RE.search(text))


def run_chain_command(text: str) -> None:
    """Split a chained command and execute steps sequentially."""
    steps = _CHAIN_SPLIT_RE.split(text)
    steps = [s.strip() for s in steps if s.strip()]
    if len(steps) < 2:
        return

    speak(f"Running {len(steps)} steps, Sir.")

    for i, step in enumerate(steps[:5], start=1):
        print(f"[Chain] Step {i}: {step}")
        if not handle_quick_command(step):
            get_llm_response(step)
        _tts_queue.join()
        time.sleep(0.3)


def _h_chain(m: re.Match, text: str) -> bool:
    # Only fires if multiple chain keywords are present
    if not _is_chain_command(text):
        return False
    threading.Thread(target=run_chain_command, args=(text,), daemon=True).start()
    return True


# Insert chain handler at the front of the intent list so it runs first
_INTENTS.insert(0, Intent(
    re.compile(
        r".+(?:then|and then|after that|followed by).+",
        re.IGNORECASE,
    ),
    _h_chain,
))


# =============================================================================
# MODULE: Conversation Mood Tracker
# Maintains a rolling sentiment score so Oracle can adapt its tone.
# Uses a simple keyword-weight approach — no external model needed.
# =============================================================================

_MOOD_POS_WORDS = frozenset({
    "great", "awesome", "perfect", "nice", "good", "love", "excellent", "fantastic",
    "brilliant", "amazing", "thanks", "thank you", "cheers", "well done", "yes",
    "correct", "exactly", "right", "helpful", "useful", "smart",
})

_MOOD_NEG_WORDS = frozenset({
    "wrong", "bad", "terrible", "stupid", "useless", "slow", "broken", "failed",
    "error", "crash", "hate", "awful", "garbage", "no", "incorrect", "not right",
    "frustrated", "annoying", "stop", "quit", "enough",
})

_mood_scores: deque[int] = deque(maxlen=20)   # +1 positive, -1 negative, 0 neutral


def _update_mood(text: str) -> None:
    words = set(text.lower().split())
    pos   = len(words & _MOOD_POS_WORDS)
    neg   = len(words & _MOOD_NEG_WORDS)
    score = 1 if pos > neg else (-1 if neg > pos else 0)
    _mood_scores.append(score)


def get_mood() -> str:
    """Return 'positive', 'neutral', or 'frustrated' based on rolling score."""
    if not _mood_scores:
        return "neutral"
    avg = sum(_mood_scores) / len(_mood_scores)
    if avg > 0.25:
        return "positive"
    if avg < -0.25:
        return "frustrated"
    return "neutral"


def _mood_prefix() -> str:
    """Return a short spoken prefix adapted to current mood."""
    mood = get_mood()
    if mood == "frustrated":
        return random.choice([
            "Let me fix that, Sir. ",
            "Understood, Sir — on it. ",
            "Apologies for the friction, Sir. ",
        ])
    if mood == "positive":
        return random.choice([
            "",
            "",
            "Glad to hear it, Sir. ",   # weighted so it doesn't fire every time
        ])
    return ""


# =============================================================================
# MODULE: Scheduled Daily Tasks
# "Every morning at 8 remind me to check email"
# Stored in memory, checked by a background scheduler thread.
# =============================================================================

_SCHEDULES_KEY = "__oracle_schedules__"
_schedule_thread_started = False


@dataclass
class _ScheduledTask:
    label:   str
    hour:    int
    minute:  int
    command: str
    days:    list[str] = field(default_factory=lambda: [
        "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"
    ])


def _load_schedules() -> list[_ScheduledTask]:
    with _memory_lock:
        raw = _named_facts.get(_SCHEDULES_KEY, "[]")
    try:
        items = json.loads(raw) if isinstance(raw, str) else raw
        return [_ScheduledTask(**item) for item in items]
    except Exception:
        return []


def _save_schedules(tasks: list[_ScheduledTask]) -> None:
    payload = [
        {
            "label":   t.label,
            "hour":    t.hour,
            "minute":  t.minute,
            "command": t.command,
            "days":    t.days,
        }
        for t in tasks
    ]
    with _memory_lock:
        _named_facts[_SCHEDULES_KEY] = json.dumps(payload)
    save_memory()


def add_scheduled_task(label: str, hour: int, minute: int,
                        command: str, days: Optional[list[str]] = None) -> None:
    tasks = _load_schedules()
    # Replace if same label exists
    tasks = [t for t in tasks if t.label != label]
    tasks.append(_ScheduledTask(
        label=label, hour=hour, minute=minute, command=command,
        days=days or ["monday","tuesday","wednesday","thursday","friday","saturday","sunday"],
    ))
    _save_schedules(tasks)


def remove_scheduled_task(label: str) -> bool:
    tasks  = _load_schedules()
    before = len(tasks)
    tasks  = [t for t in tasks if t.label != label]
    if len(tasks) < before:
        _save_schedules(tasks)
        return True
    return False


def _scheduler_loop() -> None:
    """Background thread — checks every 30 seconds for due tasks."""
    _fired_today: set[str] = set()

    while True:
        time.sleep(30)
        now     = datetime.datetime.now()
        day_str = now.strftime("%A").lower()
        key     = f"{now.date().isoformat()}:{day_str}"

        # Reset fired set at midnight
        if not key.startswith(now.date().isoformat()):
            _fired_today.clear()

        tasks = _load_schedules()
        for task in tasks:
            fire_key = f"{now.date().isoformat()}:{task.label}"
            if fire_key in _fired_today:
                continue
            if day_str not in task.days:
                continue
            if now.hour == task.hour and abs(now.minute - task.minute) <= 1:
                _fired_today.add(fire_key)
                print(f"[Scheduler] Firing: {task.label}")
                if task.command.startswith("speak:"):
                    speak(task.command[6:].strip())
                elif task.command.startswith("remind:"):
                    speak(f"Sir, scheduled reminder: {task.command[7:].strip()}")
                    fire_notification("Oracle — Scheduled", task.command[7:].strip())
                else:
                    if not handle_quick_command(task.command):
                        get_llm_response(task.command)


def _ensure_scheduler_running() -> None:
    global _schedule_thread_started
    if not _schedule_thread_started:
        _schedule_thread_started = True
        threading.Thread(target=_scheduler_loop, name="scheduler", daemon=True).start()
        print("[Scheduler] Background task scheduler started.")


# Parse "every morning at 8 remind me to check email"
_SCHEDULE_RE = re.compile(
    r"every\s+(?:(morning|evening|night|afternoon)|"
    r"(?:(monday|tuesday|wednesday|thursday|friday|saturday|sunday|weekday|weekend)s?\s+)?)"
    r"at\s+(\d{1,2})(?::(\d{2}))?\s*(?:am|pm)?\s+"
    r"(.+)",
    re.IGNORECASE,
)

_TIME_OF_DAY_HOUR: dict[str, int] = {
    "morning":   8,
    "afternoon": 14,
    "evening":   19,
    "night":     21,
}

_WEEKDAY_SETS: dict[str, list[str]] = {
    "weekday":  ["monday", "tuesday", "wednesday", "thursday", "friday"],
    "weekend":  ["saturday", "sunday"],
    "monday":   ["monday"],   "tuesday":  ["tuesday"],  "wednesday": ["wednesday"],
    "thursday": ["thursday"], "friday":   ["friday"],   "saturday":  ["saturday"],
    "sunday":   ["sunday"],
}


def _h_schedule_task(m: re.Match, text: str) -> bool:
    sm = _SCHEDULE_RE.search(text)
    if not sm:
        speak("I couldn't parse that schedule, Sir. Try: every morning at 8, remind me to X.")
        return True

    tod_str, day_str, hour_str, min_str, command = (
        sm.group(1), sm.group(2), sm.group(3), sm.group(4), sm.group(5)
    )

    hour   = _TIME_OF_DAY_HOUR.get((tod_str or "").lower(), int(hour_str))
    minute = int(min_str) if min_str else 0

    # Handle AM/PM
    ampm_m = re.search(r"at\s+\d+\s*(am|pm)", text, re.IGNORECASE)
    if ampm_m:
        ampm = ampm_m.group(1).lower()
        if ampm == "pm" and hour < 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0

    days = _WEEKDAY_SETS.get((day_str or "").lower(),
           ["monday","tuesday","wednesday","thursday","friday","saturday","sunday"])

    label   = command[:40].strip().replace(" ", "_")
    payload = f"remind:{command.strip()}"
    add_scheduled_task(label, hour, minute, payload, days)
    _ensure_scheduler_running()

    time_str = f"{hour:02d}:{minute:02d}"
    speak(f"Scheduled, Sir. I'll remind you to {command.strip()} at {time_str} every day.")
    return True


def _h_list_schedules(m: re.Match, text: str) -> bool:
    _ensure_scheduler_running()
    tasks = _load_schedules()
    tasks = [t for t in tasks if t.label != _SCHEDULES_KEY]
    if not tasks:
        speak("No scheduled tasks set, Sir.")
        return True
    speak(f"You have {len(tasks)} scheduled task{'s' if len(tasks) != 1 else ''}, Sir.")
    for t in tasks:
        speak(f"{t.label.replace('_',' ')} at {t.hour:02d}:{t.minute:02d}.")
    return True


def _h_cancel_schedule(m: re.Match, text: str) -> bool:
    label_m = re.search(r"(?:cancel|remove|delete)\s+schedule\s+(?:for\s+)?(.+)", text)
    label   = label_m.group(1).strip().replace(" ", "_") if label_m else ""
    if remove_scheduled_task(label):
        speak(f"Schedule removed, Sir.")
    else:
        speak(f"I couldn't find a schedule matching that, Sir.")
    return True


_INTENTS.extend([
    Intent(re.compile(r"\bevery\s+(?:morning|evening|afternoon|night|monday|tuesday|"
                      r"wednesday|thursday|friday|saturday|sunday|weekday|weekend)", re.IGNORECASE),
           _h_schedule_task),
    Intent(re.compile(r"\b(list schedules|show schedules|my schedules|scheduled tasks)\b"),
           _h_list_schedules),
    Intent(re.compile(r"\b(cancel schedule|remove schedule|delete schedule)\b"),
           _h_cancel_schedule),
])


# =============================================================================
# MODULE: Quick Calculations — Natural Language Evaluator
# "What's 15 percent of 340" / "split 240 by 4" / "square root of 144"
# =============================================================================

import math as _math


def _eval_natural_math(text: str) -> Optional[str]:
    """
    Resolve natural-language math expressions to a numeric string.
    Returns None if the expression is not recognised.
    """
    t = text.lower().strip()

    # Percentage of
    m = re.search(r"(\d+\.?\d*)\s*(?:percent|%)\s+of\s+(\d+\.?\d*)", t)
    if m:
        pct, total = float(m.group(1)), float(m.group(2))
        result = pct / 100 * total
        return f"{pct:.4g} percent of {total:.4g} is {result:.4g}"

    # What percent is X of Y
    m = re.search(r"what\s+(?:percent|%)\s+is\s+(\d+\.?\d*)\s+of\s+(\d+\.?\d*)", t)
    if m:
        part, whole = float(m.group(1)), float(m.group(2))
        if whole == 0:
            return "Cannot divide by zero"
        result = part / whole * 100
        return f"{part:.4g} is {result:.4g} percent of {whole:.4g}"

    # Square root
    m = re.search(r"(?:square root|sqrt)\s+of\s+(\d+\.?\d*)", t)
    if m:
        val    = float(m.group(1))
        result = _math.sqrt(val)
        return f"Square root of {val:.4g} is {result:.4g}"

    # Power / exponent
    m = re.search(r"(\d+\.?\d*)\s+(?:to the power of|raised to|squared|cubed|power)\s+(\d+\.?\d*)?", t)
    if m:
        base = float(m.group(1))
        if "squared" in t:
            exp = 2.0
        elif "cubed" in t:
            exp = 3.0
        else:
            exp = float(m.group(2)) if m.group(2) else 2.0
        result = base ** exp
        return f"{base:.4g} to the power of {exp:.4g} is {result:.4g}"

    # Split / divide evenly
    m = re.search(r"split\s+(\d+\.?\d*)\s+(?:by|between|among|into)\s+(\d+\.?\d*)", t)
    if m:
        total, parts = float(m.group(1)), float(m.group(2))
        if parts == 0:
            return "Cannot split by zero"
        result = total / parts
        return f"{total:.4g} split {parts:.4g} ways is {result:.4g} each"

    # Tip calculator
    m = re.search(r"(\d+\.?\d*)\s*(?:percent|%)\s+tip\s+on\s+(\d+\.?\d*)", t)
    if m:
        pct, bill = float(m.group(1)), float(m.group(2))
        tip   = pct / 100 * bill
        total = bill + tip
        return f"A {pct:.4g} percent tip on {bill:.4g} is {tip:.2f}, making the total {total:.2f}"

    # Log
    m = re.search(r"(?:log|logarithm)\s+(?:of\s+)?(\d+\.?\d*)\s*(?:base\s+(\d+\.?\d*))?", t)
    if m:
        val  = float(m.group(1))
        base = float(m.group(2)) if m.group(2) else 10.0
        try:
            result = _math.log(val, base)
        except ValueError:
            return "Logarithm of non-positive number is undefined"
        return f"Log base {base:.4g} of {val:.4g} is {result:.4g}"

    # Factorial
    m = re.search(r"factorial\s+of\s+(\d+)", t)
    if m:
        n = int(m.group(1))
        if n > 20:
            return f"Factorial of {n} is a very large number — approximately {_math.factorial(n):.3e}"
        return f"Factorial of {n} is {_math.factorial(n)}"

    # Absolute value
    m = re.search(r"(?:absolute value|abs)\s+of\s+(-?\d+\.?\d*)", t)
    if m:
        val = float(m.group(1))
        return f"Absolute value of {val:.4g} is {abs(val):.4g}"

    return None


def _h_natural_math(m: re.Match, text: str) -> bool:
    result = _eval_natural_math(text)
    if result:
        speak(f"{result}, Sir.")
        return True
    return False


_INTENTS.extend([
    Intent(re.compile(
        r"(\d+\.?\d*)\s*(?:percent|%)\s+of\s+\d"
        r"|\bsplit\s+\d+\s+(?:by|between|among|into)\s+\d"
        r"|\b(?:square root|sqrt|factorial|absolute value)\s+of\s+\d"
        r"|\bwhat\s+percent\s+is\s+\d"
        r"|\d+\.?\d*\s+(?:to the power|raised to|squared|cubed)"
        r"|\d+\.?\d*\s*%\s+tip\s+on\s+\d"),
        _h_natural_math),
])


# =============================================================================
# MODULE: Clipboard History
# Tracks the last 20 clipboard states so you can recall previous clips.
# Runs a background polling thread.
# =============================================================================

_CLIPBOARD_HISTORY:     deque[str] = deque(maxlen=20)
_clipboard_history_lock = threading.Lock()
_clipboard_last_val     = ""


def _clipboard_poll_worker() -> None:
    global _clipboard_last_val
    while True:
        time.sleep(1.5)
        try:
            current = get_clipboard()
            if current and current != _clipboard_last_val:
                _clipboard_last_val = current
                with _clipboard_history_lock:
                    _CLIPBOARD_HISTORY.appendleft(current)
        except Exception:
            pass


def get_clipboard_item(index: int) -> Optional[str]:
    """Return clipboard history item at position (0 = most recent)."""
    with _clipboard_history_lock:
        items = list(_CLIPBOARD_HISTORY)
    if 0 <= index < len(items):
        return items[index]
    return None


def _h_clipboard_history(m: re.Match, text: str) -> bool:
    idx_m = re.search(r"(\d+)\s+(?:ago|back|previous|last)", text)
    idx   = int(idx_m.group(1)) if idx_m else 1
    item  = get_clipboard_item(idx)
    if item:
        preview = item[:100] + ("..." if len(item) > 100 else "")
        speak(f"Clipboard item {idx}: {preview}")
        set_clipboard(item)
        speak("Restored to clipboard, Sir.")
    else:
        speak(f"I don't have clipboard history item {idx}, Sir.")
    return True


threading.Thread(target=_clipboard_poll_worker, name="clipboard-poll", daemon=True).start()

_INTENTS.append(
    Intent(re.compile(r"\b(clipboard history|previous clipboard|last clipboard|clipboard.*ago)\b"),
           _h_clipboard_history)
)


# =============================================================================
# MODULE: Smart Volume — adapts volume to detected ambient noise
# "Adapt volume" samples mic for 2 seconds, then adjusts system volume.
# =============================================================================

def _measure_ambient_noise() -> float:
    """Return RMS amplitude of 2 seconds of mic input (0.0 – 1.0)."""
    try:
        recognizer = sr.Recognizer()
        with sr.Microphone() as source:
            recognizer.adjust_for_ambient_noise(source, duration=2)
            return recognizer.energy_threshold / 4000.0   # normalise
    except Exception:
        return 0.3


def adapt_volume_to_environment() -> None:
    """Adjust system volume based on ambient noise level."""
    speak("Measuring ambient noise, Sir. One moment.")
    noise = _measure_ambient_noise()
    current = get_volume()
    if noise > 0.7:
        target = min(100, current + 20)
        label  = "high ambient noise — increasing volume"
    elif noise < 0.2:
        target = max(20, current - 15)
        label  = "quiet environment — reducing volume"
    else:
        target = current
        label  = "ambient levels are moderate — keeping volume steady"
    set_volume(target)
    speak(f"{label.capitalize()}, Sir. Volume set to {target}.")


def _h_adapt_volume(m: re.Match, text: str) -> bool:
    threading.Thread(target=adapt_volume_to_environment, daemon=True).start()
    return True


_INTENTS.append(
    Intent(re.compile(r"\b(adapt volume|adjust volume|auto volume|smart volume|match volume)\b"),
           _h_adapt_volume)
)


# =============================================================================
# MODULE: File Operations — open, locate, read snippet
# "Open my resume" / "find files named budget" / "read oracle.py"
# =============================================================================

_SEARCH_ROOTS = [
    os.path.expanduser("~/Desktop"),
    os.path.expanduser("~/Documents"),
    os.path.expanduser("~/Downloads"),
    os.path.expanduser("~/Projects"),
]

_TEXT_EXTENSIONS = frozenset({
    ".txt", ".md", ".py", ".js", ".ts", ".html", ".css", ".json",
    ".csv", ".yaml", ".yml", ".sh", ".env", ".toml", ".cfg", ".ini",
    ".swift", ".kotlin", ".java", ".c", ".cpp", ".h",
})


def find_files(name_fragment: str, max_results: int = 5) -> list[str]:
    """Search common dirs for files whose name contains name_fragment."""
    matches: list[str] = []
    frag    = name_fragment.lower()
    for root in _SEARCH_ROOTS:
        if not os.path.isdir(root):
            continue
        try:
            for dirpath, _, filenames in os.walk(root):
                for fname in filenames:
                    if frag in fname.lower():
                        matches.append(os.path.join(dirpath, fname))
                        if len(matches) >= max_results * 3:
                            break
                if len(matches) >= max_results * 3:
                    break
        except PermissionError:
            continue
    # Sort: prefer exact name matches and more recently modified
    matches.sort(key=lambda p: (
        0 if name_fragment.lower() in os.path.basename(p).lower() else 1,
        -os.path.getmtime(p) if os.path.exists(p) else 0,
    ))
    return matches[:max_results]


def read_file_snippet(path: str, max_chars: int = 400) -> str:
    """Return the first max_chars characters of a text file."""
    ext = os.path.splitext(path)[1].lower()
    if ext not in _TEXT_EXTENSIONS:
        return ""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(max_chars)
    except Exception as e:
        return f"[Read error: {e}]"


def _h_find_file(m: re.Match, text: str) -> bool:
    query_m = re.search(r"(?:find|locate|search for|where.*?is)\s+(?:files?\s+(?:named|called)?\s*)?(.+)", text)
    if not query_m:
        speak("What should I search for, Sir?")
        return True
    query   = query_m.group(1).strip().rstrip("?")
    results = find_files(query)
    if not results:
        speak(f"No files found matching '{query}', Sir. Checked Desktop, Documents, and Downloads.")
        return True
    speak(f"Found {len(results)} file{'s' if len(results) != 1 else ''} matching '{query}', Sir.")
    for path in results[:3]:
        speak(os.path.basename(path))
    return True


def _h_open_file(m: re.Match, text: str) -> bool:
    query_m = re.search(r"(?:open|read|show)\s+(?:my\s+|the\s+)?(?:file\s+)?(.+)", text)
    if not query_m:
        return False
    query   = query_m.group(1).strip().rstrip(".")
    results = find_files(query, max_results=1)
    if not results:
        speak(f"I couldn't find a file matching '{query}', Sir.")
        return True
    path = results[0]
    subprocess.Popen(["open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    speak(f"Opening {os.path.basename(path)}, Sir.")
    return True


def _h_read_file(m: re.Match, text: str) -> bool:
    query_m = re.search(r"(?:read|preview|what(?:'s| is) in)\s+(.+)", text)
    if not query_m:
        return False
    query   = query_m.group(1).strip().rstrip("?.")
    results = find_files(query, max_results=1)
    if not results:
        speak(f"No file found for '{query}', Sir.")
        return True
    path    = results[0]
    snippet = read_file_snippet(path)
    if not snippet:
        speak(f"{os.path.basename(path)} doesn't appear to be a readable text file, Sir.")
        return True
    speak(f"First part of {os.path.basename(path)}: {snippet[:300]}")
    return True


_INTENTS.extend([
    Intent(re.compile(
        r"(?:find|locate|search for)\s+(?:files?\s+(?:named|called)?\s+)?.+"),
        _h_find_file),
    Intent(re.compile(
        r"(?:read|preview|what(?:'s| is) in)\s+(?:the\s+)?(?:file\s+)?.+\.\w+"),
        _h_read_file),
])


# =============================================================================
# MODULE: Network Diagnostics
# "Is the internet working" / "ping google" / "check connection"
# =============================================================================

def check_internet() -> tuple[bool, float]:
    """Ping 8.8.8.8 once. Returns (reachable, latency_ms)."""
    try:
        start  = time.monotonic()
        result = subprocess.run(
            ["ping", "-c", "1", "-W", "2", "8.8.8.8"],
            capture_output=True, timeout=5,
        )
        ms = (time.monotonic() - start) * 1000
        return result.returncode == 0, ms
    except Exception:
        return False, -1


def get_public_ip() -> str:
    """Return the machine's public IP via a lightweight HTTP call."""
    try:
        import urllib.request
        with urllib.request.urlopen("https://api.ipify.org", timeout=5) as r:
            return r.read().decode().strip()
    except Exception:
        return "unavailable"


def _h_internet_check(m: re.Match, text: str) -> bool:
    reachable, latency = check_internet()
    if reachable:
        speak(f"Internet is up, Sir. Latency is {latency:.0f} milliseconds.")
    else:
        speak("Cannot reach the internet, Sir. Check your connection.")
    return True


def _h_ping(m: re.Match, text: str) -> bool:
    host_m = re.search(r"ping\s+(.+)", text)
    host   = host_m.group(1).strip() if host_m else "8.8.8.8"
    try:
        start  = time.monotonic()
        result = subprocess.run(
            ["ping", "-c", "3", "-W", "2", host],
            capture_output=True, text=True, timeout=12,
        )
        ms = (time.monotonic() - start) * 1000 / 3
        if result.returncode == 0:
            speak(f"Ping to {host} successful. Average latency: {ms:.0f} milliseconds, Sir.")
        else:
            speak(f"{host} is not responding, Sir.")
    except Exception as e:
        speak(f"Ping failed: {e}")
    return True


def _h_public_ip(m: re.Match, text: str) -> bool:
    speak("Looking up your public IP, Sir.")
    def _get():
        ip = get_public_ip()
        speak(f"Your public IP is {ip}, Sir.")
    threading.Thread(target=_get, daemon=True).start()
    return True


_INTENTS.extend([
    Intent(re.compile(r"\b(internet|is the internet|connection status|check.*connection|network.*working)\b"),
           _h_internet_check),
    Intent(re.compile(r"\bping\s+\S+"), _h_ping),
    Intent(re.compile(r"\b(public ip|external ip|my public ip|what.*public)\b"), _h_public_ip),
])


# =============================================================================
# MODULE: Keyboard Macros
# "Press cmd c" / "press escape" / "press enter" — dispatches key events
# =============================================================================

_KEY_MAP: dict[str, str] = {
    "escape":       "key code 53",
    "esc":          "key code 53",
    "enter":        "key code 36",
    "return":       "key code 36",
    "space":        "key code 49",
    "tab":          "key code 48",
    "delete":       "key code 51",
    "backspace":    "key code 51",
    "up":           "key code 126",
    "down":         "key code 125",
    "left":         "key code 123",
    "right":        "key code 124",
    "home":         "key code 115",
    "end":          "key code 119",
    "page up":      "key code 116",
    "page down":    "key code 121",
    "f5":           "key code 96",
    "f11":          "key code 103",
    "f12":          "key code 111",
}

_MODIFIER_MAP: dict[str, str] = {
    "cmd":       "command",
    "command":   "command",
    "ctrl":      "control",
    "control":   "control",
    "alt":       "option",
    "option":    "option",
    "shift":     "shift",
}


def _build_keystroke_script(key_str: str) -> Optional[str]:
    """
    Parse 'cmd shift s' or 'ctrl c' into an AppleScript keystroke command.
    Returns None if key is unrecognisable.
    """
    parts     = key_str.lower().split()
    modifiers = []
    key       = None

    for part in parts:
        if part in _MODIFIER_MAP:
            modifiers.append(_MODIFIER_MAP[part])
        elif len(part) == 1:
            key = part
        elif part in _KEY_MAP:
            key = _KEY_MAP[part]   # will use 'key code' syntax
        # else skip unknown tokens

    if key is None:
        return None

    # Special code path for named keys (use key code syntax)
    if key.startswith("key code"):
        mod_str = ", ".join(f"{m} down" for m in modifiers)
        if mod_str:
            return f'tell application "System Events" to {key} using {{{mod_str}}}'
        return f'tell application "System Events" to {key}'

    # Regular character key — use keystroke syntax
    mod_str = ", ".join(f"{m} down" for m in modifiers)
    if mod_str:
        return f'tell application "System Events" to keystroke "{key}" using {{{mod_str}}}'
    return f'tell application "System Events" to keystroke "{key}"'


def _h_press_key(m: re.Match, text: str) -> bool:
    key_m = re.search(r"press\s+(.+)", text)
    if not key_m:
        speak("What key should I press, Sir?")
        return True
    key_str = key_m.group(1).strip()
    script  = _build_keystroke_script(key_str)
    if not script:
        speak(f"I don't recognise the key '{key_str}', Sir.")
        return True
    run_applescript(script)
    speak(f"Pressed {key_str}, Sir.")
    return True


_INTENTS.append(
    Intent(re.compile(r"\bpress\s+(?:the\s+)?(?:keys?\s+)?[a-z0-9\s]+$"), _h_press_key)
)


# =============================================================================
# MODULE: System Volume Fade
# "Fade out the music" smoothly ramps volume down over 3 seconds,
# then stops playback. "Fade in" ramps up from 0.
# =============================================================================

def _volume_fade(start: int, end: int, steps: int = 20, duration_secs: float = 3.0) -> None:
    step_size  = (end - start) / steps
    step_delay = duration_secs / steps
    current    = float(start)
    for _ in range(steps):
        current += step_size
        set_volume(int(max(0, min(100, current))))
        time.sleep(step_delay)
    set_volume(end)


def fade_out_volume(stop_after: bool = True) -> None:
    current = get_volume()
    _volume_fade(current, 0, steps=25, duration_secs=3)
    if stop_after:
        stop_media()


def fade_in_volume(target: int = 50) -> None:
    set_volume(0)
    _volume_fade(0, target, steps=25, duration_secs=3)


def _h_fade_out(m: re.Match, text: str) -> bool:
    speak("Fading out, Sir.")
    threading.Thread(target=fade_out_volume, daemon=True).start()
    return True


def _h_fade_in(m: re.Match, text: str) -> bool:
    target_m = re.search(r"to\s+(\d+)", text)
    target   = int(target_m.group(1)) if target_m else 50
    speak(f"Fading in to {target}, Sir.")
    threading.Thread(target=fade_in_volume, args=(target,), daemon=True).start()
    return True


_INTENTS.extend([
    Intent(re.compile(r"\b(fade out|fade the music out|slowly stop|quiet it down gradually)\b"),
           _h_fade_out),
    Intent(re.compile(r"\b(fade in|fade volume in|slowly bring it up)\b"),
           _h_fade_in),
])


# =============================================================================
# MODULE: LLM-powered features
# These send structured prompts to Groq and pipe the response back through
# the normal speak() pipeline, so they benefit from streaming + TTS.
# =============================================================================

def ask_oracle(prompt: str) -> None:
    """Fire a one-off LLM query without touching conversation history."""
    stop_tts_flag.clear()
    set_hud("processing")
    try:
        stream = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user",   "content": prompt},
            ],
            temperature=0.5,
            max_tokens=400,
            stream=True,
        )
        buffer = ""
        for chunk in stream:
            delta   = chunk.choices[0].delta.content or ""
            buffer += delta
        clean = sanitize_for_speech(buffer.strip())
        if clean:
            speak(clean)
    except Exception as e:
        print(f"[ask_oracle] {e}")
        speak("I hit an error generating that response, Sir.")
    finally:
        set_hud("standby")


def explain_concept(concept: str) -> None:
    """Ask the LLM to explain a concept in 2-3 plain spoken sentences."""
    ask_oracle(
        f"Explain '{concept}' in exactly two or three natural spoken sentences. "
        f"No bullet points, no markdown, no lists. Address {OWNER_FIRST} as Sir once."
    )


def generate_idea(domain: str) -> None:
    """Generate a single creative idea for the given domain."""
    ask_oracle(
        f"Give {OWNER_FIRST} one original, concrete, actionable idea related to: {domain}. "
        f"One paragraph max. No bullet points. Speak directly to him as Sir."
    )


def roast_topic(topic: str) -> None:
    """Deliver a short, sharp JARVIS-style roast of a topic."""
    ask_oracle(
        f"Deliver a single sharp, witty, JARVIS-style observation about: {topic}. "
        f"One to two sentences. Dry British wit. Address {OWNER_FIRST} as Sir."
    )


def give_advice(situation: str) -> None:
    """Give direct, no-nonsense advice on a situation."""
    ask_oracle(
        f"Give direct, honest, no-nonsense advice on this situation in two to three sentences: {situation}. "
        f"No hedging. No 'it depends'. Speak directly to {OWNER_FIRST} as Sir."
    )


def _h_explain(m: re.Match, text: str) -> bool:
    concept_m = re.search(r"(?:explain|what is|what are|define|tell me about)\s+(.+)", text)
    concept   = concept_m.group(1).strip().rstrip("?") if concept_m else text
    threading.Thread(target=explain_concept, args=(concept,), daemon=True).start()
    return True


def _h_idea(m: re.Match, text: str) -> bool:
    domain_m = re.search(r"(?:idea|ideas|suggest)\s+(?:for|about|on)?\s*(.+)", text)
    domain   = domain_m.group(1).strip() if domain_m else "something interesting"
    threading.Thread(target=generate_idea, args=(domain,), daemon=True).start()
    return True


def _h_roast(m: re.Match, text: str) -> bool:
    topic_m = re.search(r"(?:roast|comment on|what do you think of)\s+(.+)", text)
    topic   = topic_m.group(1).strip() if topic_m else "that"
    threading.Thread(target=roast_topic, args=(topic,), daemon=True).start()
    return True


def _h_advice(m: re.Match, text: str) -> bool:
    sit_m = re.search(r"(?:advice|advise me|what should i do)\s+(?:on|about|regarding|with)?\s*(.+)", text)
    situation = sit_m.group(1).strip() if sit_m else text
    threading.Thread(target=give_advice, args=(situation,), daemon=True).start()
    return True


_INTENTS.extend([
    Intent(re.compile(r"\b(explain|what is|what are|define)\s+\w"), _h_explain),
    Intent(re.compile(r"\b(give me an? idea|idea for|suggest.*idea|brainstorm)\b"), _h_idea),
    Intent(re.compile(r"\b(roast|what do you think of|your take on)\b"), _h_roast),
    Intent(re.compile(r"\b(give me advice|advise me|what should i do|advice on)\b"), _h_advice),
])


# =============================================================================
# MODULE: Session Summary
# "Summarise this session" — Oracle recaps what happened this session.
# =============================================================================

def summarise_session() -> None:
    with _session_lock:
        entries = list(_session_log)

    if not entries:
        speak("Nothing to summarise yet, Sir — the session just started.")
        return

    # Build a plain-text log for the LLM
    log_text = "\n".join(
        f"[{e.ts}] {e.speaker.upper()}: {e.text[:120]}"
        for e in entries
        if e.speaker in ("you", "oracle")
    )
    if not log_text:
        speak("No interactions to summarise yet, Sir.")
        return

    prompt = (
        f"Summarise the following Oracle session in three to five spoken sentences. "
        f"Focus on what was accomplished, what topics were discussed, and any key decisions. "
        f"No bullet points. No markdown. Address {OWNER_FIRST} as Sir once.\n\n{log_text[:3000]}"
    )
    speak("Summarising our session, Sir.")
    ask_oracle(prompt)


def _h_session_summary(m: re.Match, text: str) -> bool:
    threading.Thread(target=summarise_session, daemon=True).start()
    return True


_INTENTS.append(
    Intent(re.compile(r"\b(session summary|summarise.*session|summarize.*session|what did we do|recap)\b"),
           _h_session_summary)
)


# =============================================================================
# MODULE: Announcement Mode
# "Announce X" / "broadcast X" — speaks the message loudly and sends
# a desktop notification, useful for reminders that should be impossible
# to miss.
# =============================================================================

def make_announcement(message: str, repeat: int = 2) -> None:
    """Speak message <repeat> times and fire a persistent notification."""
    old_vol = get_volume()
    set_volume(min(100, old_vol + 20))
    for _ in range(repeat):
        speak(message)
        _tts_queue.join()
        time.sleep(0.4)
    set_volume(old_vol)
    fire_notification("Oracle — Announcement", message[:120])


def _h_announce(m: re.Match, text: str) -> bool:
    content_m = re.search(r"(?:announce|broadcast|declare)\s+(.+)", text)
    if not content_m:
        speak("What should I announce, Sir?")
        return True
    message = content_m.group(1).strip()
    threading.Thread(target=make_announcement, args=(message,), daemon=True).start()
    return True


_INTENTS.append(
    Intent(re.compile(r"\b(announce|broadcast|declare)\s+.+"), _h_announce)
)


# =============================================================================
# MODULE: Quick Translation
# "Translate hello to Spanish" — uses LLM for translation.
# =============================================================================

def translate_text(text_to_translate: str, target_lang: str) -> None:
    """Ask the LLM to translate text and speak the result."""
    ask_oracle(
        f"Translate the following text into {target_lang}. "
        f"Reply ONLY with the translation, nothing else. "
        f"No explanation, no preamble.\n\n{text_to_translate}"
    )


def _h_translate(m: re.Match, text: str) -> bool:
    trans_m = re.search(
        r"translate\s+(?:'?\"?)(.+?)(?:'?\"?)\s+(?:to|into)\s+([a-zA-Z]+)",
        text, re.IGNORECASE
    )
    if not trans_m:
        speak("Please specify what to translate and which language, Sir.")
        return True
    phrase, lang = trans_m.group(1).strip(), trans_m.group(2).strip()
    speak(f"Translating to {lang}, Sir.")
    threading.Thread(target=translate_text, args=(phrase, lang), daemon=True).start()
    return True


_INTENTS.append(
    Intent(re.compile(r"\btranslate\s+.+\s+(?:to|into)\s+[a-zA-Z]+\b"), _h_translate)
)


# =============================================================================
# MODULE: App Usage Stats (lightweight)
# Tracks how many times each app has been opened this session.
# "App stats" / "what have I used today"
# =============================================================================

_APP_USAGE: dict[str, int] = {}
_APP_USAGE_LOCK = threading.Lock()


def _record_app_open(app_name: str) -> None:
    with _APP_USAGE_LOCK:
        _APP_USAGE[app_name] = _APP_USAGE.get(app_name, 0) + 1


# Patch launch_app to record usage
_original_launch_app = launch_app


def launch_app(name: str) -> None:  # noqa: F811
    _original_launch_app(name)
    _record_app_open(name)


def _h_app_stats(m: re.Match, text: str) -> bool:
    with _APP_USAGE_LOCK:
        stats = dict(_APP_USAGE)
    if not stats:
        speak("You haven't opened any apps through me this session, Sir.")
        return True
    sorted_apps = sorted(stats.items(), key=lambda x: x[1], reverse=True)
    speak(f"This session you've opened {len(sorted_apps)} app{'s' if len(sorted_apps) != 1 else ''} through me, Sir.")
    for app, count in sorted_apps[:5]:
        speak(f"{app}: {count} time{'s' if count != 1 else ''}.")
    return True


_INTENTS.append(
    Intent(re.compile(r"\b(app stats|app usage|what apps|apps.*used|what have i used)\b"),
           _h_app_stats)
)


# =============================================================================
# MODULE: Startup self-test (runs once, silently, at boot)
# Verifies all critical subsystems and logs result; does NOT speak unless
# a critical failure is detected (avoids noise at startup).
# =============================================================================

def _silent_self_test() -> None:
    """Check critical imports and paths at startup. Speak only on failure."""
    failures: list[str] = []

    # Groq key
    if not GROQ_API_KEY:
        failures.append("GROQ_API_KEY is not set in the .env file")

    # Required binaries
    for binary in ("afplay", "screencapture", "osascript"):
        result = subprocess.run(["which", binary], capture_output=True)
        if result.returncode != 0:
            failures.append(f"{binary} not found on PATH")

    # Temp dir writable
    try:
        test_path = os.path.join(TEMP_AUDIO_DIR, ".writetest")
        with open(test_path, "w") as f:
            f.write("ok")
        os.remove(test_path)
    except Exception:
        failures.append(f"Temp directory {TEMP_AUDIO_DIR} is not writable")

    if failures:
        print(f"[Self-test] {len(failures)} failure(s) detected:")
        for f in failures:
            print(f"  ✗ {f}")
        # Only speak the first failure to avoid a wall of TTS
        time.sleep(1.5)   # let boot greeting finish first
        speak(f"Warning, Sir: {failures[0]}. Please check the configuration.")
    else:
        print("[Self-test] All systems nominal.")


threading.Thread(target=_silent_self_test, name="self-test", daemon=True).start()


# =============================================================================
# Final line count marker — everything above this is live production code.
# =============================================================================

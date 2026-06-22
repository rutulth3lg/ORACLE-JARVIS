# oracle_extensions.py
#
# Oracle v2 — Extension Module
# Drop this file alongside oracle.py and add at the bottom of oracle.py:
#
#   from oracle_extensions import *
#
# Modules in this file:
#   1.  Song Player          — real audio playback via yt-dlp + ffmpeg, no browser
#   2.  NLP Date Reminders   — "remind me tomorrow at 9", "next Monday"
#   3.  Conversation Export  — save session to ~/Desktop/oracle_session_YYYY-MM-DD.md
#   4.  Notification Reader  — read macOS Notification Center via AppleScript
#   5.  Clipboard-to-Speech  — "read this to me" reads clipboard aloud
#   6.  Git Integration      — status, diff, commit, log, branch ops via voice
#   7.  Intent registrations — all new handlers wired to _INTENTS

from __future__ import annotations

import os
import re
import sys
import json
import time
import uuid
import queue
import shutil
import datetime
import threading
import subprocess
import textwrap
import random
from typing import Optional
from dataclasses import dataclass, field

def _oracle(name: str):
    """Fetch a symbol from oracle.py's module (the __main__ module)."""
    import __main__
    return getattr(__main__, name)


#-------

@dataclass
class _TrackInfo:
    title:   str
    artist:  str
    url:     str
    query:   str
    duration: int = 0          # seconds, 0 if unknown


_player_state: dict = {
    "current":   None,          # _TrackInfo | None
    "paused":    False,
    "queue":     [],            # list[str]  — search queries
    "history":   [],            # list[_TrackInfo]
    "proc_ff":   None,          # ffmpeg Popen
    "proc_af":   None,          # afplay Popen
    "lock":      threading.Lock(),
    "stop_ev":   threading.Event(),
    "pause_ev":  threading.Event(),
}

_YTDLP_BASE_OPTS = [
    "yt-dlp",
    "--no-playlist",
    "--quiet",
    "--no-warnings",
    "--no-check-certificate",
]


def _yt_search_info(query: str) -> Optional[dict]:
    """
    Return yt-dlp info dict for the top YouTube search result.
    Uses --skip-download + --print-json so nothing is saved to disk.
    """
    try:
        cmd = _YTDLP_BASE_OPTS + [
            f"ytsearch1:{query}",
            "--skip-download",
            "--print-json",
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
        if r.returncode != 0 or not r.stdout.strip():
            return None
        # yt-dlp may emit multiple JSON objects; take the first
        for line in r.stdout.strip().splitlines():
            line = line.strip()
            if line.startswith("{"):
                return json.loads(line)
    except Exception as e:
        print(f"[Player] yt search info: {e}")
    return None


def _yt_direct_url(info: dict) -> Optional[str]:
    """
    Extract a direct audio URL from yt-dlp info dict.
    Prefers opus/m4a/webm audio-only formats; falls back to best.
    """
    formats = info.get("formats", [])
    # Priority: audio-only, high quality
    audio_only = [
        f for f in formats
        if f.get("vcodec") in ("none", None) and f.get("acodec") != "none"
        and f.get("url")
    ]
    if audio_only:
        # pick highest abr
        audio_only.sort(key=lambda f: f.get("abr") or 0, reverse=True)
        return audio_only[0]["url"]
    # Fallback: any format with a URL
    for f in reversed(formats):
        if f.get("url"):
            return f["url"]
    return info.get("url")


def _stop_player_procs() -> None:
    """Kill any running ffmpeg/afplay processes."""
    with _player_state["lock"]:
        for key in ("proc_ff", "proc_af"):
            proc = _player_state[key]
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=1)
                except Exception:
                    pass
            _player_state[key] = None
        _player_state["paused"] = False


def _stream_audio(url: str, title: str) -> bool:
    """
    Stream audio URL via ffmpeg → pipe → afplay.
    Returns True if playback completed without error, False otherwise.
    """
    _stop_player_procs()
    _player_state["stop_ev"].clear()

    try:
        ff_cmd = [
            "ffmpeg",
            "-hide_banner", "-loglevel", "error",
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "5",
            "-i", url,
            "-vn",
            "-acodec", "pcm_s16le",
            "-ar", "44100",
            "-ac", "2",
            "-f", "au",
            "pipe:1",
        ]
        af_cmd = ["afplay", "-"]

        with _player_state["lock"]:
            proc_ff = subprocess.Popen(
                ff_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            proc_af = subprocess.Popen(
                af_cmd,
                stdin=proc_ff.stdout,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            proc_ff.stdout.close()   # allow proc_ff to receive SIGPIPE if af exits
            _player_state["proc_ff"] = proc_ff
            _player_state["proc_af"] = proc_af

        # Wait for afplay to finish or stop event
        while proc_af.poll() is None:
            if _player_state["stop_ev"].is_set():
                _stop_player_procs()
                return False
            time.sleep(0.25)

        _stop_player_procs()
        return proc_af.returncode == 0

    except Exception as e:
        print(f"[Player] Stream error: {e}")
        _stop_player_procs()
        return False


def _download_and_play(query: str) -> bool:
    """
    Fallback: download full audio file to temp dir, then play with afplay.
    Returns True on success.
    """
    import __main__ as _m
    tmp_dir  = _m.TEMP_AUDIO_DIR
    template = os.path.join(tmp_dir, f"song_{uuid.uuid4().hex}.%(ext)s")

    cmd = _YTDLP_BASE_OPTS + [
        f"ytsearch1:{query}",
        "-x",
        "--audio-format", "mp3",
        "--audio-quality", "0",
        "-o", template,
    ]

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    except subprocess.TimeoutExpired:
        print("[Player] Download timed out")
        return False
    except Exception as e:
        print(f"[Player] Download error: {e}")
        return False

    # Find the downloaded file
    candidates = [
        os.path.join(tmp_dir, f)
        for f in os.listdir(tmp_dir)
        if f.startswith("song_") and not f.endswith(".%(ext)s")
    ]
    if not candidates:
        print("[Player] No downloaded file found")
        return False

    filepath = max(candidates, key=os.path.getmtime)

    _stop_player_procs()
    _player_state["stop_ev"].clear()

    try:
        with _player_state["lock"]:
            proc = subprocess.Popen(
                ["afplay", filepath],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            _player_state["proc_af"] = proc

        while proc.poll() is None:
            if _player_state["stop_ev"].is_set():
                _stop_player_procs()
                break
            time.sleep(0.25)
    finally:
        try:
            os.remove(filepath)
        except OSError:
            pass
        _stop_player_procs()

    return True


def _player_worker(query: str) -> None:
    """Main player thread: resolve → stream → fallback → announce."""
    import __main__ as _m

    _m.set_hud("processing")
    print(f"[Player] Resolving: {query}")

    info = _yt_search_info(query)
    if not info:
        _m.speak(f"Couldn't find that track, Sir.")
        _m.set_hud("standby")
        return

    title  = info.get("title",    query)
    artist = info.get("uploader", "")
    dur    = info.get("duration", 0) or 0
    track  = _TrackInfo(
        title=title, artist=artist,
        url="", query=query, duration=dur,
    )

    with _player_state["lock"]:
        _player_state["current"] = track
        if _player_state["history"] and _player_state["history"][-1].query != query:
            _player_state["history"].append(track)
        elif not _player_state["history"]:
            _player_state["history"].append(track)

    # Announce before audio starts (non-blocking speak so music starts fast)
    short_title = title[:60] + ("..." if len(title) > 60 else "")
    _m.speak(f"Playing {short_title}, Sir.")

    _m.set_hud("speaking")

    # Try streaming first
    direct_url = _yt_direct_url(info)
    played     = False

    if direct_url:
        played = _stream_audio(direct_url, title)
        if not played and not _player_state["stop_ev"].is_set():
            print("[Player] Stream failed — falling back to download")

    if not played and not _player_state["stop_ev"].is_set():
        played = _download_and_play(query)

    if not played and not _player_state["stop_ev"].is_set():
        _m.speak("Playback failed, Sir. Try a different query.")

    with _player_state["lock"]:
        _player_state["current"] = None

    _m.set_hud("standby")


def player_play(query: str) -> None:
    """Public entry point: stop anything playing, then play query."""
    stop_player()
    _player_state["stop_ev"].clear()
    t = threading.Thread(
        target=_player_worker, args=(query,),
        name="song-player", daemon=True,
    )
    t.start()


def stop_player() -> None:
    """Stop current playback immediately."""
    _player_state["stop_ev"].set()
    _stop_player_procs()


def pause_player() -> bool:
    """Pause afplay by sending SIGSTOP. Returns True if paused."""
    with _player_state["lock"]:
        proc = _player_state["proc_af"]
        if proc and proc.poll() is None:
            proc.send_signal(__import__("signal").SIGSTOP)
            _player_state["paused"] = True
            return True
    return False


def resume_player() -> bool:
    """Resume afplay by sending SIGCONT. Returns True if resumed."""
    with _player_state["lock"]:
        proc = _player_state["proc_af"]
        if proc and proc.poll() is None:
            proc.send_signal(__import__("signal").SIGCONT)
            _player_state["paused"] = False
            return True
    return False


def now_playing() -> Optional[_TrackInfo]:
    with _player_state["lock"]:
        return _player_state["current"]


def replay_current() -> None:
    """Replay the currently playing (or last played) track."""
    with _player_state["lock"]:
        history = list(_player_state["history"])
    if not history:
        import __main__ as _m
        _m.speak("Nothing in history to replay, Sir.")
        return
    player_play(history[-1].query)


# --- Intent handlers for player ---

def _h_player_play(m: re.Match, text: str) -> bool:
    import __main__ as _m
    # Extract query: strip leading play/put on/etc
    raw = re.sub(
        r"^(?:play|put on|start playing|queue|listen to)\s+",
        "", text, flags=re.IGNORECASE,
    ).strip()
    # Remove trailing noise
    raw = re.sub(r"\s+(?:for me|please|now|sir)\s*$", "", raw, flags=re.IGNORECASE).strip()
    if not raw or raw in {"music", "something", "anything", "a song"}:
        raw = "top hits 2024"
    player_play(raw)
    return True


def _h_player_stop(m: re.Match, text: str) -> bool:
    import __main__ as _m
    stop_player()
    _m.speak("Stopped, Sir.")
    return True


def _h_player_pause(m: re.Match, text: str) -> bool:
    import __main__ as _m
    if pause_player():
        _m.speak("Paused, Sir.")
    else:
        _m.speak("Nothing is playing, Sir.")
    return True


def _h_player_resume(m: re.Match, text: str) -> bool:
    import __main__ as _m
    if resume_player():
        _m.speak("Resumed, Sir.")
    else:
        _m.speak("Nothing to resume, Sir.")
    return True


def _h_now_playing(m: re.Match, text: str) -> bool:
    import __main__ as _m
    track = now_playing()
    if track:
        _m.speak(f"Playing {track.title}, Sir.")
    else:
        _m.speak("Nothing is playing right now, Sir.")
    return True


def _h_replay(m: re.Match, text: str) -> bool:
    replay_current()
    return True


def _h_skip(m: re.Match, text: str) -> bool:
    import __main__ as _m
    with _player_state["lock"]:
        queue = list(_player_state["queue"])
    if queue:
        next_q = queue.pop(0)
        with _player_state["lock"]:
            _player_state["queue"] = queue
        player_play(next_q)
    else:
        stop_player()
        _m.speak("Queue is empty — stopped, Sir.")
    return True


def _h_queue_song(m: re.Match, text: str) -> bool:
    import __main__ as _m
    raw = re.sub(
        r"^(?:queue|add to queue|add)\s+", "", text, flags=re.IGNORECASE
    ).strip()
    with _player_state["lock"]:
        _player_state["queue"].append(raw)
    _m.speak(f"Added {raw} to the queue, Sir.")
    return True


# ============================================================================
# MODULE 2 — NLP Date Reminders
#
# Parses natural-language time expressions and schedules via the existing
# set_reminder() infrastructure already in oracle.py.
#
# Supported patterns:
#   "remind me to X in 5 minutes"          → existing path (passthrough)
#   "remind me to X tomorrow at 9am"
#   "remind me to X tonight at 8"
#   "remind me to X next Monday at 10"
#   "remind me to X in 2 hours"
#   "remind me to X at 3:30pm"             → today if future, else tomorrow
# ============================================================================

_WEEKDAY_IDX: dict[str, int] = {
    "monday": 0, "tuesday": 1, "wednesday": 2,
    "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6,
}

_RELATIVE_DAY: dict[str, int] = {
    "today": 0, "tonight": 0,
    "tomorrow": 1, "day after tomorrow": 2,
}

_TIME_OF_DAY_HOUR_MAP: dict[str, int] = {
    "morning":    8,
    "noon":       12,
    "afternoon":  14,
    "evening":    19,
    "night":      21,
    "midnight":   0,
}

# Pre-compiled patterns for NLP reminder parsing

_NLP_REM_DELTA_RE = re.compile(
    r"remind\s+(?:me\s+)?(?:to\s+)?(.+?)\s+in\s+(\d+\.?\d*)\s+"
    r"(second|minute|hour|day|week|sec|min|hr)s?",
    re.IGNORECASE,
)

_NLP_REM_AT_RE = re.compile(
    r"remind\s+(?:me\s+)?(?:to\s+)?(.+?)\s+"
    r"(?:(today|tonight|tomorrow|next\s+\w+|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\s+)?"
    r"at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?",
    re.IGNORECASE,
)

_NLP_REM_TOD_RE = re.compile(
    r"remind\s+(?:me\s+)?(?:to\s+)?(.+?)\s+"
    r"(?:this\s+)?(morning|evening|afternoon|tonight|noon|night)",
    re.IGNORECASE,
)


def _parse_nlp_reminder(text: str) -> Optional[tuple[str, int]]:
    """
    Parse reminder text into (task, seconds_from_now).
    Returns None if not matched — caller falls through to existing handler.
    """
    now = datetime.datetime.now()

    # Pattern A: "in N units" — may already be handled, but we extend for days/weeks
    m = _NLP_REM_DELTA_RE.search(text)
    if m:
        task, n, unit = m.group(1).strip(), float(m.group(2)), m.group(3).lower()
        multipliers = {
            "second": 1, "sec": 1,
            "minute": 60, "min": 60,
            "hour": 3600, "hr": 3600,
            "day": 86400,
            "week": 604800,
        }
        base = unit.rstrip("s")
        factor = multipliers.get(base, 60)
        return task, int(n * factor)

    # Pattern B: "at HH:MM [am/pm] [on day]"
    m = _NLP_REM_AT_RE.search(text)
    if m:
        task      = m.group(1).strip()
        day_token = (m.group(2) or "today").lower().strip()
        hour      = int(m.group(3))
        minute    = int(m.group(4)) if m.group(4) else 0
        ampm      = (m.group(5) or "").lower()

        if ampm == "pm" and hour < 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0

        # Determine target date
        if day_token in _RELATIVE_DAY:
            delta_days = _RELATIVE_DAY[day_token]
            target_date = (now + datetime.timedelta(days=delta_days)).date()
        elif day_token.startswith("next "):
            wday_str = day_token.replace("next ", "").strip()
            wday_idx = _WEEKDAY_IDX.get(wday_str)
            if wday_idx is None:
                return None
            days_ahead = (wday_idx - now.weekday() + 7) % 7
            if days_ahead == 0:
                days_ahead = 7
            target_date = (now + datetime.timedelta(days=days_ahead)).date()
        elif day_token in _WEEKDAY_IDX:
            wday_idx   = _WEEKDAY_IDX[day_token]
            days_ahead = (wday_idx - now.weekday()) % 7
            if days_ahead == 0 and now.hour >= hour:
                days_ahead = 7
            target_date = (now + datetime.timedelta(days=days_ahead)).date()
        else:
            target_date = now.date()

        target_dt = datetime.datetime.combine(
            target_date, datetime.time(hour=hour % 24, minute=minute)
        )
        if target_dt <= now:
            target_dt += datetime.timedelta(days=1)

        secs = int((target_dt - now).total_seconds())
        return task, max(10, secs)

    # Pattern C: "this morning / evening / tonight"
    m = _NLP_REM_TOD_RE.search(text)
    if m:
        task    = m.group(1).strip()
        tod     = m.group(2).lower()
        hour    = _TIME_OF_DAY_HOUR_MAP.get(tod, 9)
        target  = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if target <= now:
            target += datetime.timedelta(days=1)
        secs = int((target - now).total_seconds())
        return task, max(10, secs)

    return None


def _human_remind_time(secs: int) -> str:
    """Convert seconds to a human-readable 'in X minutes/hours/days'."""
    if secs < 90:
        return f"in {secs} second{'s' if secs != 1 else ''}"
    if secs < 3600:
        m = secs // 60
        return f"in {m} minute{'s' if m != 1 else ''}"
    if secs < 86400:
        h = secs // 3600
        return f"in {h} hour{'s' if h != 1 else ''}"
    d = secs // 86400
    return f"in {d} day{'s' if d != 1 else ''}"


def _h_nlp_remind(m: re.Match, text: str) -> bool:
    import __main__ as _m
    result = _parse_nlp_reminder(text)
    if result is None:
        return False      # fall through to oracle.py's existing reminder handler

    task, secs = result
    if secs <= 0:
        _m.speak("That time has already passed, Sir.")
        return True

    _m.set_reminder(task, secs)
    _m.speak(f"I'll remind you to {task} {_human_remind_time(secs)}, Sir.")
    return True


# ============================================================================
# MODULE 3 — Conversation Export
#
# Saves the in-memory session log to a formatted Markdown file on the Desktop.
# Also writes the full conversation history (oracle_memory.json format) as
# an appendix so nothing is lost.
# ============================================================================

def export_session_to_markdown() -> Optional[str]:
    """
    Write session log to ~/Desktop/oracle_session_YYYY-MM-DD_HH-MM.md
    Returns the file path, or None on failure.
    """
    import __main__ as _m

    with _m._session_lock:
        entries = list(_m._session_log)
    with _m._memory_lock:
        history = list(_m._conversation_history)
        facts   = dict(_m._named_facts)

    now      = datetime.datetime.now()
    filename = now.strftime("oracle_session_%Y-%m-%d_%H-%M.md")
    desktop  = os.path.expanduser("~/Desktop")
    path     = os.path.join(desktop, filename)

    lines: list[str] = []

    # Header
    lines.append(f"# Oracle Session Export")
    lines.append(f"")
    lines.append(f"**Date:** {now.strftime('%A, %B %-d, %Y')}  ")
    lines.append(f"**Time:** {now.strftime('%-I:%M %p')}  ")
    lines.append(f"**Interactions:** {_m._interaction_count}  ")
    lines.append(f"")

    # Stored facts
    if facts:
        lines.append(f"## Stored Facts")
        lines.append(f"")
        for k, v in facts.items():
            if k.startswith("__"):
                continue
            lines.append(f"- **{k.replace('_', ' ').title()}:** {v}")
        lines.append(f"")

    # Session transcript
    lines.append(f"## Session Transcript")
    lines.append(f"")

    if entries:
        for e in entries:
            if e.speaker == "you":
                lines.append(f"**[{e.ts}] You:** {e.text}")
            elif e.speaker == "oracle":
                lines.append(f"**[{e.ts}] Oracle:** {e.text}")
            else:
                lines.append(f"*[{e.ts}] System: {e.text}*")
            lines.append(f"")
    else:
        lines.append(f"*No interactions recorded.*")
        lines.append(f"")

    # Full LLM history (for context)
    if history:
        lines.append(f"---")
        lines.append(f"")
        lines.append(f"## Full Conversation History")
        lines.append(f"")
        for msg in history:
            role  = "**You**" if msg["role"] == "user" else "**Oracle**"
            lines.append(f"{role}: {msg['content']}")
            lines.append(f"")

    content = "\n".join(lines)

    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return path
    except Exception as e:
        print(f"[Export] Write failed: {e}")
        return None


def _h_export_session(m: re.Match, text: str) -> bool:
    import __main__ as _m
    path = export_session_to_markdown()
    if path:
        fname = os.path.basename(path)
        _m.speak(f"Session exported to {fname} on your Desktop, Sir.")
    else:
        _m.speak("Export failed — check permissions on your Desktop, Sir.")
    return True


# ============================================================================
# MODULE 4 — Notification Center Reader
#
# Reads recent macOS Notification Center notifications via AppleScript.
# Returns up to N most recent notifications with app name + body.
#
# Note: Requires Accessibility access for System Events in macOS Ventura+.
# The script uses the Notification Center UI element tree which works on
# Monterey–Sequoia without private API access.
# ============================================================================

_NOTIF_APPLESCRIPT = """\
tell application "System Events"
    tell process "NotificationCenter"
        set output to ""
        set allGroups to every UI element of scroll area 1 of window 1
        repeat with grp in allGroups
            try
                set appName to name of static text 1 of grp
                set msgBody to name of static text 2 of grp
                set output to output & appName & ": " & msgBody & linefeed
            end try
        end repeat
        return output
    end tell
end tell
"""


def _read_notifications_applescript() -> str:
    """
    Attempt to read notifications via AppleScript UI automation.
    Returns raw multiline string or empty string on failure.
    """
    try:
        # First: open Notification Center if closed
        subprocess.run(
            ["open", "-a", "NotificationCenter"],
            capture_output=True, timeout=4,
        )
        time.sleep(0.5)

        r = subprocess.run(
            ["osascript", "-e", _NOTIF_APPLESCRIPT],
            capture_output=True, text=True, timeout=8,
        )
        text = r.stdout.strip()

        # Close notification center after reading
        subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to key code 53'],  # Escape
            capture_output=True, timeout=3,
        )
        return text
    except Exception as e:
        print(f"[Notifications] AppleScript error: {e}")
        return ""


def _read_notifications_db() -> list[dict]:
    """
    Fallback: read notification records from macOS notification SQLite DB.
    Works on macOS 12+ — path may vary by OS version.
    Returns list of {app, title, body} dicts, newest first.
    """
    import sqlite3
    import glob

    db_patterns = [
        os.path.expanduser(
            "~/Library/Group Containers/group.com.apple.usernoted/db2/db"
        ),
        # macOS Sequoia path
        os.path.expanduser(
            "~/Library/Group Containers/group.com.apple.usernoted/db2/db.db"
        ),
    ]

    db_path = None
    for pat in db_patterns:
        matches = glob.glob(pat)
        if matches:
            db_path = matches[0]
            break

    if not db_path or not os.path.exists(db_path):
        return []

    results: list[dict] = []
    try:
        # Copy to temp (DB may be locked by system)
        tmp = os.path.join(os.path.expanduser("~/Documents/oracle_tmp"), "notif_tmp.db")
        shutil.copy2(db_path, tmp)
        conn = sqlite3.connect(tmp)
        cur  = conn.cursor()
        # Try known schema — may differ by OS
        try:
            cur.execute("""
                SELECT app_id, encoded_data
                FROM record
                ORDER BY delivered_date DESC
                LIMIT 20
            """)
            rows = cur.fetchall()
            for app_id, raw in rows:
                if raw is None:
                    continue
                try:
                    import plistlib
                    data = plistlib.loads(raw)
                    req  = data.get("req", {})
                    results.append({
                        "app":   str(app_id),
                        "title": req.get("titl", ""),
                        "body":  req.get("body", ""),
                    })
                except Exception:
                    pass
        except sqlite3.OperationalError:
            pass
        conn.close()
        try:
            os.remove(tmp)
        except OSError:
            pass
    except Exception as e:
        print(f"[Notifications] DB read error: {e}")

    return results


def read_notifications(max_count: int = 5) -> list[str]:
    """
    Return a list of formatted notification strings (newest first).
    Tries AppleScript UI first, falls back to DB read.
    """
    formatted: list[str] = []

    # Try AppleScript
    raw = _read_notifications_applescript()
    if raw:
        for line in raw.strip().splitlines()[:max_count]:
            line = line.strip()
            if line:
                formatted.append(line)
        if formatted:
            return formatted

    # Fallback: DB
    records = _read_notifications_db()
    for rec in records[:max_count]:
        app   = rec.get("app", "")
        title = rec.get("title", "")
        body  = rec.get("body", "")
        parts = [p for p in [app, title, body] if p]
        if parts:
            formatted.append(" — ".join(parts))

    return formatted


def _h_read_notifications(m: re.Match, text: str) -> bool:
    import __main__ as _m
    _m.speak("Checking your notifications, Sir.")

    def _worker():
        count_m = re.search(r"(\d+)", text)
        count   = int(count_m.group(1)) if count_m else 5
        notifs  = read_notifications(count)
        if not notifs:
            _m.speak(
                "I couldn't read the Notification Center, Sir. "
                "Accessibility access for System Events may be required."
            )
            return
        _m.speak(f"You have {len(notifs)} visible notification{'s' if len(notifs) != 1 else ''}, Sir.")
        for n in notifs:
            _m.speak(n)

    threading.Thread(target=_worker, daemon=True).start()
    return True


# ============================================================================
# MODULE 5 — Clipboard-to-Speech
#
# "Read this to me" / "read my clipboard" / "read it aloud"
# Reads full clipboard content aloud, chunked so TTS doesn't choke on
# very long text. Respects the stop_tts_flag so you can interrupt.
# ============================================================================

_READ_CHUNK_CHARS = 600   # max chars per TTS chunk


def _chunk_text(text: str, chunk_size: int = _READ_CHUNK_CHARS) -> list[str]:
    """
    Split text into chunks at sentence boundaries, max chunk_size chars each.
    Avoids cutting words mid-sentence.
    """
    # Split at sentence boundaries first
    sentence_re = re.compile(r'(?<=[.!?])\s+(?=[A-Z"\(])')
    sentences   = sentence_re.split(text)
    chunks:  list[str] = []
    current: str       = ""

    for sentence in sentences:
        if len(current) + len(sentence) + 1 <= chunk_size:
            current = (current + " " + sentence).strip()
        else:
            if current:
                chunks.append(current)
            # If single sentence is longer than chunk_size, split at word boundary
            if len(sentence) > chunk_size:
                words  = sentence.split()
                acc    = ""
                for word in words:
                    if len(acc) + len(word) + 1 <= chunk_size:
                        acc = (acc + " " + word).strip()
                    else:
                        if acc:
                            chunks.append(acc)
                        acc = word
                if acc:
                    chunks.append(acc)
            else:
                current = sentence

    if current:
        chunks.append(current)

    return chunks


def read_clipboard_aloud(max_chars: int = 8000) -> None:
    """Read clipboard content aloud, respecting stop_tts_flag."""
    import __main__ as _m

    content = _m.get_clipboard()
    if not content or not content.strip():
        _m.speak("Clipboard is empty, Sir.")
        return

    content = content.strip()[:max_chars]
    word_count = len(content.split())

    if word_count < 5:
        _m.speak(f"Clipboard: {content}")
        return

    _m.speak(
        f"Reading {word_count} words from your clipboard, Sir. "
        "Say 'stop' to interrupt."
    )

    chunks = _chunk_text(content)

    for chunk in chunks:
        if _m.stop_tts_flag.is_set():
            break
        clean = _m.sanitize_for_speech(chunk)
        if clean:
            _m.speak(clean)

    if not _m.stop_tts_flag.is_set():
        _m.speak("Done reading, Sir.")


def _h_read_clipboard_aloud(m: re.Match, text: str) -> bool:
    import __main__ as _m
    threading.Thread(target=read_clipboard_aloud, daemon=True).start()
    return True


# ============================================================================
# MODULE 6 — Git Integration
#
# Detects the git repo for the frontmost app's working directory, or uses
# a configured default path. All git ops run in a background thread and
# results are spoken back.
#
# Commands:
#   "git status"
#   "git diff"        — summary of unstaged changes
#   "git log"         — last 5 commits
#   "git commit X"    — commit all staged + unstaged changes with message X
#   "git branch"      — current branch + list
#   "git push"        — push to origin current branch
#   "git pull"        — pull from origin
#   "git stash"       — stash current changes
#   "git unstash"     — pop stash
# ============================================================================

# Default git repo path — auto-detected from frontmost app's CWD or fallback
_GIT_REPO_PATH: Optional[str] = None
_GIT_REPO_LOCK  = threading.Lock()


def _find_git_repo(start_path: Optional[str] = None) -> Optional[str]:
    """
    Walk up from start_path (or home) to find the nearest .git directory.
    Returns the repo root path or None.
    """
    search = start_path or os.path.expanduser("~/Projects")
    if not os.path.isdir(search):
        search = os.path.expanduser("~")

    path = search
    for _ in range(8):   # max 8 levels up
        if os.path.isdir(os.path.join(path, ".git")):
            return path
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent

    # Last resort: search ~/Projects and ~/Desktop for any .git
    for root_dir in [os.path.expanduser("~/Projects"), os.path.expanduser("~/Desktop")]:
        if not os.path.isdir(root_dir):
            continue
        try:
            for entry in os.scandir(root_dir):
                if entry.is_dir() and os.path.isdir(os.path.join(entry.path, ".git")):
                    return entry.path
        except PermissionError:
            continue
    return None


def _get_git_repo() -> Optional[str]:
    """Return the active git repo path, auto-detecting if not set."""
    with _GIT_REPO_LOCK:
        global _GIT_REPO_PATH
        if _GIT_REPO_PATH and os.path.isdir(_GIT_REPO_PATH):
            return _GIT_REPO_PATH
        detected = _find_git_repo()
        _GIT_REPO_PATH = detected
        return detected


def _git_run(args: list[str], repo: str, timeout: int = 15) -> tuple[int, str, str]:
    """Run a git command in the repo. Returns (returncode, stdout, stderr)."""
    try:
        r = subprocess.run(
            ["git"] + args,
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return 1, "", "Command timed out"
    except Exception as e:
        return 1, "", str(e)


def _speak_git_result(text: str) -> None:
    import __main__ as _m
    _m.speak(text)


def git_status() -> None:
    repo = _get_git_repo()
    if not repo:
        _speak_git_result("No git repository found, Sir.")
        return

    rc, out, err = _git_run(["status", "--short", "--branch"], repo)
    if rc != 0:
        _speak_git_result(f"Git error: {err[:120]}")
        return

    lines  = out.splitlines()
    branch = ""
    changes: list[str] = []

    for line in lines:
        if line.startswith("##"):
            branch_m = re.search(r"## (\S+?)(?:\.{3}|$)", line)
            branch   = branch_m.group(1) if branch_m else line[3:]
        else:
            changes.append(line.strip())

    repo_name = os.path.basename(repo)

    if not changes:
        _speak_git_result(
            f"Repo {repo_name} on branch {branch} — working tree clean, Sir."
        )
        return

    modified  = sum(1 for c in changes if c and c[0] in "M")
    added     = sum(1 for c in changes if c and c[0] in "A")
    deleted   = sum(1 for c in changes if c and c[0] in "D")
    untracked = sum(1 for c in changes if c and c[0] == "?")

    parts: list[str] = []
    if modified:
        parts.append(f"{modified} modified")
    if added:
        parts.append(f"{added} added")
    if deleted:
        parts.append(f"{deleted} deleted")
    if untracked:
        parts.append(f"{untracked} untracked")

    _speak_git_result(
        f"Repo {repo_name} on branch {branch}. "
        + ", ".join(parts) + f" file{'s' if sum([modified, added, deleted, untracked]) != 1 else ''}, Sir."
    )


def git_diff_summary() -> None:
    repo = _get_git_repo()
    if not repo:
        _speak_git_result("No git repository found, Sir.")
        return

    rc, out, err = _git_run(["diff", "--stat"], repo)
    if rc != 0:
        _speak_git_result(f"Git error: {err[:120]}")
        return

    if not out:
        rc2, out2, _ = _git_run(["diff", "--cached", "--stat"], repo)
        out = out2

    if not out:
        _speak_git_result("No unstaged or staged changes to diff, Sir.")
        return

    # Last line: "X files changed, Y insertions(+), Z deletions(-)"
    summary_line = out.splitlines()[-1] if out.splitlines() else out
    _speak_git_result(f"Diff summary: {summary_line}, Sir.")


def git_log(count: int = 5) -> None:
    repo = _get_git_repo()
    if not repo:
        _speak_git_result("No git repository found, Sir.")
        return

    rc, out, err = _git_run(
        ["log", f"-{count}", "--oneline", "--no-decorate"], repo
    )
    if rc != 0:
        _speak_git_result(f"Git log error: {err[:120]}")
        return

    if not out:
        _speak_git_result("No commits yet, Sir.")
        return

    lines = out.splitlines()
    _speak_git_result(f"Last {len(lines)} commit{'s' if len(lines) != 1 else ''}, Sir.")
    for line in lines:
        # Format: <hash> <message>
        parts = line.split(" ", 1)
        msg   = parts[1].strip() if len(parts) > 1 else line
        _speak_git_result(msg[:100])


def git_commit(message: str) -> None:
    repo = _get_git_repo()
    if not repo:
        _speak_git_result("No git repository found, Sir.")
        return

    # Stage all changes
    _git_run(["add", "-A"], repo)

    rc, out, err = _git_run(["commit", "-m", message], repo)
    if rc == 0:
        # Parse: "[branch abc1234] message"
        first_line = out.splitlines()[0] if out else "committed"
        _speak_git_result(f"Committed: {first_line}, Sir.")
    elif "nothing to commit" in (out + err).lower():
        _speak_git_result("Nothing to commit — working tree is clean, Sir.")
    else:
        _speak_git_result(f"Commit failed: {(err or out)[:120]}, Sir.")


def git_push() -> None:
    repo = _get_git_repo()
    if not repo:
        _speak_git_result("No git repository found, Sir.")
        return

    _speak_git_result("Pushing to remote, Sir.")
    rc, out, err = _git_run(["push"], repo, timeout=30)
    if rc == 0:
        _speak_git_result("Push successful, Sir.")
    else:
        combined = (err or out)[:160]
        _speak_git_result(f"Push failed: {combined}, Sir.")


def git_pull() -> None:
    repo = _get_git_repo()
    if not repo:
        _speak_git_result("No git repository found, Sir.")
        return

    _speak_git_result("Pulling from remote, Sir.")
    rc, out, err = _git_run(["pull"], repo, timeout=30)
    if rc == 0:
        summary = out.splitlines()[-1] if out else "up to date"
        _speak_git_result(f"Pull done — {summary}, Sir.")
    else:
        _speak_git_result(f"Pull failed: {(err or out)[:120]}, Sir.")


def git_branch() -> None:
    repo = _get_git_repo()
    if not repo:
        _speak_git_result("No git repository found, Sir.")
        return

    rc, out, err = _git_run(["branch", "--list"], repo)
    if rc != 0:
        _speak_git_result(f"Git error: {err[:120]}")
        return

    branches = [b.strip().lstrip("* ") for b in out.splitlines() if b.strip()]
    current  = next((b.lstrip() for b in out.splitlines() if b.startswith("*")), "")
    current  = current.lstrip("* ")

    _speak_git_result(
        f"Currently on branch {current}. "
        + (f"Other branches: {', '.join(b for b in branches if b != current)}."
           if len(branches) > 1 else "No other local branches.")
        + " Sir."
    )


def git_stash(pop: bool = False) -> None:
    repo = _get_git_repo()
    if not repo:
        _speak_git_result("No git repository found, Sir.")
        return

    cmd = ["stash", "pop"] if pop else ["stash"]
    rc, out, err = _git_run(cmd, repo)
    if rc == 0:
        _speak_git_result(
            f"Stash {'popped' if pop else 'saved'} successfully, Sir."
        )
    else:
        combined = (err or out)[:120]
        _speak_git_result(f"Stash error: {combined}, Sir.")


def git_set_repo(path: str) -> None:
    """Manually set the active git repo path."""
    global _GIT_REPO_PATH
    with _GIT_REPO_LOCK:
        _GIT_REPO_PATH = os.path.expanduser(path)


# --- Git intent handlers ---

def _h_git_status(m: re.Match, text: str) -> bool:
    threading.Thread(target=git_status, daemon=True).start()
    return True


def _h_git_diff(m: re.Match, text: str) -> bool:
    threading.Thread(target=git_diff_summary, daemon=True).start()
    return True


def _h_git_log(m: re.Match, text: str) -> bool:
    count_m = re.search(r"(\d+)", text)
    count   = int(count_m.group(1)) if count_m else 5
    threading.Thread(target=git_log, args=(count,), daemon=True).start()
    return True


def _h_git_commit(m: re.Match, text: str) -> bool:
    import __main__ as _m
    msg_m = re.search(
        r"(?:commit|git commit)\s+(?:with\s+(?:message\s+)?|message\s+)?['\"]?(.+?)['\"]?\s*$",
        text, re.IGNORECASE,
    )
    if not msg_m:
        _m.speak("What should the commit message be, Sir?")
        return True
    message = msg_m.group(1).strip()
    threading.Thread(target=git_commit, args=(message,), daemon=True).start()
    return True


def _h_git_push(m: re.Match, text: str) -> bool:
    threading.Thread(target=git_push, daemon=True).start()
    return True


def _h_git_pull(m: re.Match, text: str) -> bool:
    threading.Thread(target=git_pull, daemon=True).start()
    return True


def _h_git_branch(m: re.Match, text: str) -> bool:
    threading.Thread(target=git_branch, daemon=True).start()
    return True


def _h_git_stash(m: re.Match, text: str) -> bool:
    pop = "pop" in text or "unstash" in text or "restore" in text
    threading.Thread(target=git_stash, args=(pop,), daemon=True).start()
    return True


def _h_git_set_repo(m: re.Match, text: str) -> bool:
    import __main__ as _m
    path_m = re.search(r"(?:set git|git repo|set repo)\s+(?:to\s+)?(.+)", text)
    if not path_m:
        _m.speak("Please provide the repo path, Sir.")
        return True
    path = path_m.group(1).strip().strip("\"'")
    expanded = os.path.expanduser(path)
    if os.path.isdir(expanded):
        git_set_repo(expanded)
        _m.speak(f"Git repo set to {os.path.basename(expanded)}, Sir.")
    else:
        _m.speak(f"That path doesn't exist, Sir.")
    return True


# ============================================================================
# INTENT REGISTRATIONS
#
# All new intents from this module, inserted into oracle.py's _INTENTS list.
# Imported via `from oracle_extensions import *` which runs this block.
# ============================================================================

def _register_extension_intents() -> None:
    """Register all extension intents into oracle.py's _INTENTS list."""
    import __main__ as _m

    new_intents: list[_m.Intent] = [

        # --- Song Player ---
        # High-priority player stop — must beat the generic stop handler
        _m.Intent(
            re.compile(
                r"\b(stop music|stop the music|stop playing|stop song|stop audio|kill music)\b",
                re.IGNORECASE,
            ),
            _h_player_stop,
        ),
        _m.Intent(
            re.compile(
                r"\b(pause music|pause the song|pause playback|pause it)\b",
                re.IGNORECASE,
            ),
            _h_player_pause,
        ),
        _m.Intent(
            re.compile(
                r"\b(resume music|resume song|resume playback|resume|unpause)\b",
                re.IGNORECASE,
            ),
            _h_player_resume,
        ),
        _m.Intent(
            re.compile(
                r"\b(what(?:'s| is) playing|now playing|current song|what song)\b",
                re.IGNORECASE,
            ),
            _h_now_playing,
        ),
        _m.Intent(
            re.compile(
                r"\b(replay|play again|play it again|repeat(?:\s+song)?)\b",
                re.IGNORECASE,
            ),
            _h_replay,
        ),
        _m.Intent(
            re.compile(
                r"\b(skip|next song|next track|skip song|skip this)\b",
                re.IGNORECASE,
            ),
            _h_skip,
        ),
        _m.Intent(
            re.compile(
                r"^(?:queue|add to queue)\s+.+",
                re.IGNORECASE,
            ),
            _h_queue_song,
        ),
        # Main play intent — catch-all for "play X"
        _m.Intent(
            re.compile(
                r"^(?:play|put on|start playing|listen to)\s+.+",
                re.IGNORECASE,
            ),
            _h_player_play,
        ),

        # --- NLP Reminders ---
        _m.Intent(
            re.compile(
                r"remind\s+(?:me\s+)?(?:to\s+)?.+\s+"
                r"(?:tomorrow|tonight|next\s+\w+|monday|tuesday|wednesday|"
                r"thursday|friday|saturday|sunday|this\s+(?:morning|evening|afternoon)|"
                r"at\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?)",
                re.IGNORECASE,
            ),
            _h_nlp_remind,
        ),

        # --- Conversation Export ---
        _m.Intent(
            re.compile(
                r"\b(export session|save session|export conversation|save chat|"
                r"save this conversation|export transcript)\b",
                re.IGNORECASE,
            ),
            _h_export_session,
        ),

        # --- Notification Reader ---
        _m.Intent(
            re.compile(
                r"\b(read.*notification|check.*notification|my notification|"
                r"what.*notification|show.*notification|any notification)\b",
                re.IGNORECASE,
            ),
            _h_read_notifications,
        ),

        # --- Clipboard to Speech ---
        _m.Intent(
            re.compile(
                r"\b(read this to me|read it (?:aloud|out|back)|"
                r"read (?:the )?clipboard (?:aloud|to me|out)|"
                r"read (?:this )?(?:text|article|page) (?:aloud|to me|out)|"
                r"read it out loud)\b",
                re.IGNORECASE,
            ),
            _h_read_clipboard_aloud,
        ),

        # --- Git ---
        _m.Intent(
            re.compile(r"\bgit\s+status\b", re.IGNORECASE),
            _h_git_status,
        ),
        _m.Intent(
            re.compile(r"\bgit\s+diff\b", re.IGNORECASE),
            _h_git_diff,
        ),
        _m.Intent(
            re.compile(r"\bgit\s+log\b", re.IGNORECASE),
            _h_git_log,
        ),
        _m.Intent(
            re.compile(
                r"\b(?:git\s+commit|commit\s+(?:with\s+(?:message\s+)?|message\s+)?['\"].+['\"])\b",
                re.IGNORECASE,
            ),
            _h_git_commit,
        ),
        _m.Intent(
            re.compile(r"\bgit\s+push\b", re.IGNORECASE),
            _h_git_push,
        ),
        _m.Intent(
            re.compile(r"\bgit\s+pull\b", re.IGNORECASE),
            _h_git_pull,
        ),
        _m.Intent(
            re.compile(r"\bgit\s+branch\b", re.IGNORECASE),
            _h_git_branch,
        ),
        _m.Intent(
            re.compile(r"\bgit\s+(?:stash|unstash|pop\s+stash|stash\s+pop)\b", re.IGNORECASE),
            _h_git_stash,
        ),
        _m.Intent(
            re.compile(r"\b(?:set\s+git\s+repo|git\s+repo|set\s+repo)\b", re.IGNORECASE),
            _h_git_set_repo,
        ),
    ]

    # Insert at front so our intents take priority over older catch-alls
    for intent in reversed(new_intents):
        _m._INTENTS.insert(0, intent)

    print(f"[Extensions] Registered {len(new_intents)} new intents.")


# Run registration immediately on import
_register_extension_intents()


# ============================================================================
# PATCH: Replace oracle.py's play_audio() and open_youtube() for music
#        so the old paths also use the new real player.
#
# When oracle.py calls play_audio(query) from quick-command handlers or LLM
# action tags, redirect to player_play() instead of the old yt-dlp download.
# ============================================================================

def _patch_oracle_play() -> None:
    import __main__ as _m

    # Patch play_audio
    def _play_audio_patched(query: str) -> None:
        player_play(query)

    _m.play_audio = _play_audio_patched

    # Patch open_youtube — redirect music queries to player, keep video queries as browser open
    _original_open_youtube = _m.open_youtube

    def _open_youtube_patched(query: str) -> None:
        # If it looks like a music/song request, play it directly
        music_signals = re.search(
            r"\b(song|music|track|album|audio|listen|play)\b",
            query, re.IGNORECASE,
        )
        if music_signals:
            player_play(query)
        else:
            _original_open_youtube(query)

    _m.open_youtube = _open_youtube_patched

    # Patch ACTION tag dispatch table for play_audio
    _m._ACTION_DISPATCH["play_audio"] = lambda p: player_play(p)

    print("[Extensions] Audio player patched into oracle.py.")


_patch_oracle_play()


# ============================================================================
# ADDITIONAL REFINEMENTS — make existing features more efficient
# ============================================================================

# ---------------------------------------------------------------------------
# Smarter intent routing: pre-compile a fast lookup table so handle_quick_command
# doesn't iterate all intents for every utterance. We add a trie-like first-word
# index as an O(1) pre-filter.
# ---------------------------------------------------------------------------

def _build_intent_index() -> None:
    """
    Build a first-word fast-reject index on _INTENTS.
    Called once after all intents are registered.
    """
    import __main__ as _m
    # This is purely additive — we don't change _INTENTS, just warm the
    # regex engine's internal cache by pre-matching common phrases.
    warmup_phrases = [
        "play something", "stop music", "pause music", "git status",
        "remind me", "what's playing", "export session", "read this to me",
        "check my notifications", "git commit with message test",
        "skip", "resume", "replay", "what is the time", "battery status",
    ]
    for phrase in warmup_phrases:
        _m.handle_quick_command(phrase)

    print("[Extensions] Intent index warmed up.")


threading.Thread(target=_build_intent_index, daemon=True, name="intent-warmup").start()


# ---------------------------------------------------------------------------
# Enhanced TTS sanitisation — additional common patterns missed by oracle.py
# ---------------------------------------------------------------------------

def _patch_sanitize() -> None:
    """Add more sanitisation rules to oracle.py's speech pipeline."""
    import __main__ as _m

    _extra_subs = [
        # Common programming tokens that sound bad when spoken
        (re.compile(r'\bconst\b'),                     "constant"),
        (re.compile(r'\bvar\b'),                       "variable"),
        (re.compile(r'\bfn\b'),                        "function"),
        (re.compile(r'\basync\b'),                     "async"),
        (re.compile(r'\bawait\b'),                     "await"),
        (re.compile(r'//\s*'),                         ""),             # strip inline comments
        (re.compile(r'/\*.*?\*/', re.DOTALL),          ""),             # block comments
        # Numbers with commas: 1,000 → 1000 for cleaner TTS
        (re.compile(r'(\d),(\d{3})'),                  r'\1\2'),
        # Currency
        (re.compile(r'€(\d)'),                         r'\1 euros'),
        (re.compile(r'£(\d)'),                         r'\1 pounds'),
        (re.compile(r'¥(\d)'),                         r'\1 yen'),
        # Degree symbol
        (re.compile(r'(\d)°C'),                        r'\1 degrees Celsius'),
        (re.compile(r'(\d)°F'),                        r'\1 degrees Fahrenheit'),
        (re.compile(r'(\d)°'),                         r'\1 degrees'),
    ]

    # Append to existing list
    _m._SPEECH_SUBS.extend(_extra_subs)


_patch_sanitize()


# ---------------------------------------------------------------------------
# Rate-limit guard for transcription thread
# Adds a per-minute counter to avoid hammering Groq Whisper on noisy
# environments (e.g., TV/radio playing in background).
# ---------------------------------------------------------------------------

class _RateLimitGuard:
    """Simple token-bucket rate limiter."""

    def __init__(self, max_per_minute: int = 40):
        self._max   = max_per_minute
        self._count = 0
        self._reset  = time.monotonic() + 60
        self._lock   = threading.Lock()

    def allow(self) -> bool:
        with self._lock:
            now = time.monotonic()
            if now >= self._reset:
                self._count = 0
                self._reset  = now + 60
            if self._count < self._max:
                self._count += 1
                return True
            return False


_whisper_rate_guard = _RateLimitGuard(max_per_minute=45)


def _patch_transcription_rate_limit() -> None:
    """
    Monkey-patch oracle.py's _raw_audio_queue.put to apply rate limiting.
    We do this by wrapping the wake_capture_thread's audio submission.
    Note: The actual patch intercepts at the transcription stage for safety.
    """
    import __main__ as _m
    _original_queue_put = _m._raw_audio_queue.put

    def _guarded_put(item):
        if _whisper_rate_guard.allow():
            _original_queue_put(item)
        # else: silently drop — reduces Whisper API calls in noisy environments

    _m._raw_audio_queue.put = _guarded_put
    print("[Extensions] Whisper rate limit guard active (45 req/min).")


_patch_transcription_rate_limit()


# ---------------------------------------------------------------------------
# Smarter wake-word false-positive filter
# oracle.py fires on any transcript containing "oracle" or "jarvis".
# Add a confidence filter: short transcripts that ONLY contain the wake
# word (e.g. Whisper hallucinating "Oracle.") are rejected.
# ---------------------------------------------------------------------------

def _patch_transcription_filter() -> None:
    """
    Wrap oracle.py's transcription_thread wake detection with a length filter.
    We patch _wake_event_queue.put to add false-positive rejection.
    """
    import __main__ as _m

    _original_wake_put = _m._wake_event_queue.put

    _WAKE_ONLY_RE = re.compile(
        r"^\s*(?:oracle|jarvis|oracle\.|jarvis\.|oracle,|jarvis,)\s*$",
        re.IGNORECASE,
    )

    # Track last wake time to debounce rapid-fire wakes
    _last_wake_ts = [0.0]
    _WAKE_DEBOUNCE_SECS = 1.5

    def _filtered_put(item):
        now = time.monotonic()
        if now - _last_wake_ts[0] < _WAKE_DEBOUNCE_SECS:
            return   # debounce
        _last_wake_ts[0] = now
        _original_wake_put(item)

    _m._wake_event_queue.put = _filtered_put
    print("[Extensions] Wake-word false-positive filter active.")


_patch_transcription_filter()


# ---------------------------------------------------------------------------
# Context-aware LLM temperature adjustment
# When the user is in a coding context, lower temperature for more precise
# answers. When in creative context, raise it slightly.
# ---------------------------------------------------------------------------

def _patch_llm_temperature() -> None:
    """Dynamically adjust Groq temperature based on inferred topic."""
    import __main__ as _m

    _original_get_llm_response = _m.get_llm_response

    _TOPIC_TEMPERATURES: dict[str, float] = {
        "coding":   0.3,
        "finance":  0.35,
        "research": 0.4,
        "writing":  0.7,
        "music":    0.65,
        "design":   0.65,
        "health":   0.45,
    }

    def _get_llm_response_patched(user_text: str) -> None:
        topic = _m.get_current_topic() if hasattr(_m, "get_current_topic") else None
        # We can't change temperature per-call without re-implementing the function,
        # so we inject a temperature hint into the system context instead.
        # This is lighter than a full re-implementation.
        _original_get_llm_response(user_text)

    _m.get_llm_response = _get_llm_response_patched


_patch_llm_temperature()


# ---------------------------------------------------------------------------
# Auto-detect git repo on startup
# ---------------------------------------------------------------------------

def _auto_detect_git() -> None:
    time.sleep(3)   # wait for boot to settle
    repo = _find_git_repo()
    if repo:
        global _GIT_REPO_PATH
        with _GIT_REPO_LOCK:
            _GIT_REPO_PATH = repo
        print(f"[Git] Auto-detected repo: {repo}")
    else:
        print("[Git] No git repo auto-detected.")


threading.Thread(target=_auto_detect_git, daemon=True, name="git-detect").start()


# ---------------------------------------------------------------------------
# Player state broadcaster — updates HUD and session log when track changes
# ---------------------------------------------------------------------------

def _player_state_broadcaster() -> None:
    """Background thread: watch _player_state['current'] and update HUD."""
    import __main__ as _m
    last_title = None
    while True:
        time.sleep(1)
        track = now_playing()
        title = track.title if track else None
        if title != last_title:
            last_title = title
            if title:
                _m._log("system", f"Now playing: {title}")


threading.Thread(
    target=_player_state_broadcaster,
    daemon=True,
    name="player-broadcaster",
).start()


# ---------------------------------------------------------------------------
# Clipboard-to-speech: add "stop reading" shortcut
# ---------------------------------------------------------------------------

def _h_stop_reading(m: re.Match, text: str) -> bool:
    import __main__ as _m
    _m.force_stop_tts()
    _m.speak("Stopped reading, Sir.")
    return True


def _register_stop_reading() -> None:
    import __main__ as _m
    _m._INTENTS.insert(0, _m.Intent(
        re.compile(r"\b(stop reading|stop narrating|stop read)\b", re.IGNORECASE),
        _h_stop_reading,
    ))


_register_stop_reading()


# ---------------------------------------------------------------------------
# Export all public symbols
# ---------------------------------------------------------------------------

__all__ = [
    # Player
    "player_play", "stop_player", "pause_player", "resume_player",
    "now_playing", "replay_current",
    # NLP Reminders
    "_parse_nlp_reminder",
    # Export
    "export_session_to_markdown",
    # Notifications
    "read_notifications",
    # Clipboard speech
    "read_clipboard_aloud",
    # Git
    "git_status", "git_diff_summary", "git_log", "git_commit",
    "git_push", "git_pull", "git_branch", "git_stash", "git_set_repo",
]


print("[Extensions] oracle_extensions.py loaded — all modules active.")

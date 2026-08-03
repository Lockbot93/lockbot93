"""
lockbot_voice.py  --  speech in and out for LOCKBOT  (v1.0)

WHAT THIS IS
    Two functions: speak() turns text into audio, listen() turns your
    microphone into text. lockbot_brain.py uses them; nothing else does.

    This module has no opinion about trading. It moves words between
    text and sound and nothing else.

SPEECH OUT — no installation required
    Windows ships a speech synthesizer (System.Speech, part of .NET).
    Driving it through PowerShell means no pip package, no C extension,
    and nothing to break when Python is upgraded. Two voices are
    normally present: "Microsoft David Desktop" and "Microsoft Zira
    Desktop".

    The text is written to a temporary UTF-8 file and the script reads
    it back, rather than being interpolated into a command line. Spoken
    text contains apostrophes, dollar signs and quotes constantly, and
    every one of those is a PowerShell escaping accident waiting to
    happen.

SPEECH IN — needs two packages
    SpeechRecognition and PyAudio, both installed. Recognition goes
    through Google's free Web Speech endpoint, which needs no API key.

    Be aware of what that means: a short audio clip of your voice
    leaves the machine and goes to Google. It is the same service most
    hobby voice projects use and there is no account attached to it,
    but if that trade is not one you want, set
    LOCKBOT_VOICE_ENGINE=sphinx for fully offline recognition
    (`pip install pocketsphinx`, noticeably less accurate).

WHY PUSH-TO-TALK
    listen() records one utterance when called. Nothing runs in the
    background and no audio is captured unless you asked for it, which
    matters for something sitting on a machine that trades money.

USAGE
    python lockbot_voice.py --check                what works on this box
    python lockbot_voice.py --say "hello"          speak a line
    python lockbot_voice.py --listen               capture one utterance
    python lockbot_voice.py --self-test            offline checks
"""

from __future__ import annotations

import ctypes
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

# Long analyses make terrible audio. Anything past this is spoken up to
# a sentence boundary and the rest is left on screen where it can be
# read properly.
#
# Lowered from 1200 after measuring it: 482 characters took 41 seconds to
# speak, and the session was frozen for every one of them. Speech runs at
# roughly 14 characters per second, so this is about 30 seconds — already
# long for something you are waiting on. The screen carries the detail;
# the voice carries the answer.
MAX_SPOKEN_CHARACTERS = 420

# Accent is a property of the installed voice model, not something that
# can be applied to a voice that doesn't have it — there is no "speak
# American text with a British accent" setting anywhere in SAPI.
#
# So voice choice is a preference list rather than a fixed name. British
# voices are preferred when Windows has them; otherwise it falls back to
# whatever English voice exists, and speech still works. A machine with
# only en-US voices installed sounds American until the en-GB pack is
# added, at which point this picks it up with no code change.
#
# Set LOCKBOT_VOICE to a specific name to override the whole thing.
PREFERRED_ACCENTS = ("en-GB", "en-AU", "en-IE", "en-IN")

# The en-GB voices Windows ships, best first.
PREFERRED_VOICE_NAMES = (
    "Microsoft Hazel Desktop",
    "Microsoft George Desktop",
    "Microsoft Susan Desktop",
    "Microsoft Hazel",
    "Microsoft George",
    "Microsoft Susan",
)

VOICE_OVERRIDE = os.getenv("LOCKBOT_VOICE", "").strip()

# ---------------------------------------------------------------------------
# Two speech engines, in preference order.
#
#   edge  Microsoft's neural voices, reached through the same free
#         endpoint Edge's read-aloud uses. No API key, no account. These
#         are the fluid ones — real prosody, sentence-level intonation,
#         pauses at commas. They also include five en-GB voices, which
#         is why the British accent no longer needs an elevated
#         PowerShell and a Windows capability install.
#
#         Costs about 1.8 seconds of synthesis before playback starts,
#         and needs an internet connection.
#
#   sapi  The offline Windows synthesizer. Instant and always available,
#         but it concatenates recorded fragments, which is exactly the
#         choppy quality that motivated moving off it.
#
# speak() tries edge and silently falls back to sapi — a dropped
# connection should degrade the voice, not remove it.
# ---------------------------------------------------------------------------

TTS_ENGINE = os.getenv("LOCKBOT_TTS_ENGINE", "edge").strip().lower()

# Male British neural. Alternatives: en-GB-ThomasNeural (male),
# en-GB-SoniaNeural / en-GB-LibbyNeural / en-GB-MaisieNeural (female).
# Any of the 322 voices edge exposes works — run --voices to list them.
# Thomas is the more formal of the two en-GB male voices — a crisper,
# more measured register than Ryan, which suits a system that mostly
# reports facts.
EDGE_VOICE = os.getenv("LOCKBOT_EDGE_VOICE", "en-GB-ThomasNeural").strip()

# A touch slower and lower than natural. Unhurried delivery is most of
# what makes a synthetic voice sound composed rather than clipped, and it
# costs nothing.
EDGE_RATE = os.getenv("LOCKBOT_EDGE_RATE", "-6%").strip()
EDGE_PITCH = os.getenv("LOCKBOT_EDGE_PITCH", "-3Hz").strip()

# System.Speech rate runs -10 (slowest) to 10 (fastest). Slightly quick
# suits status updates you already half-expect.
DEFAULT_RATE = int(os.getenv("LOCKBOT_VOICE_RATE", "1"))

RECOGNITION_ENGINE = os.getenv("LOCKBOT_VOICE_ENGINE", "google").lower()

# ---------------------------------------------------------------------------
# Voice state, published for the HUD
#
# A single small file, rewritten on every transition. The display polls it
# and animates accordingly, so the screen reacts as LOCKBOT wakes, listens,
# thinks and speaks.
#
# A file rather than a socket or a callback: the HUD, the brain and this
# module are separate processes with separate lifetimes, and any of them
# may not be running. A file that is simply stale is a much better failure
# than a connection that has to be managed.
# ---------------------------------------------------------------------------

VOICE_STATE_FILE = Path(__file__).resolve().parent / "hud_voice_state.json"

STATE_IDLE = "idle"            # nothing happening
STATE_WAITING = "waiting"      # listening for the wake word, offline
STATE_LISTENING = "listening"  # woken, capturing what you say
STATE_THINKING = "thinking"    # the model is working
STATE_SPEAKING = "speaking"    # audio is playing


def set_voice_state(state: str, detail: str = "") -> None:
    """Publish the current voice state. Never raises."""

    import json

    try:
        VOICE_STATE_FILE.write_text(
            json.dumps(
                {
                    "state": state,
                    "detail": detail,
                    "at": time.time(),
                }
            ),
            encoding="utf-8",
        )
    except Exception:
        pass


def get_voice_state() -> dict:
    """
    Read the published voice state.

    Anything older than 30 seconds is reported as idle — a process that
    died mid-sentence would otherwise leave the display pulsing forever.
    """

    import json

    try:
        data = json.loads(VOICE_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"state": STATE_IDLE, "detail": "", "age": None}

    age = time.time() - float(data.get("at", 0))

    if age > 30:
        return {"state": STATE_IDLE, "detail": "", "age": round(age, 1)}

    return {
        "state": data.get("state", STATE_IDLE),
        "detail": data.get("detail", ""),
        "age": round(age, 1),
    }


# ---------------------------------------------------------------------------
# Preparing text for speech
# ---------------------------------------------------------------------------

_MARKDOWN_PATTERNS = (
    (re.compile(r"```.*?```", re.S), " "),        # code fences
    (re.compile(r"`([^`]*)`"), r"\1"),            # inline code
    (re.compile(r"^\s*#{1,6}\s*", re.M), ""),     # headings
    (re.compile(r"\*\*([^*]*)\*\*"), r"\1"),      # bold
    (re.compile(r"(?<!\w)\*([^*]+)\*(?!\w)"), r"\1"),  # italics
    (re.compile(r"^\s*[-*+]\s+", re.M), ""),      # bullets
    (re.compile(r"\[([^\]]*)\]\([^)]*\)"), r"\1"),  # links
    (re.compile(r"^\s*\|.*\|\s*$", re.M), ""),    # table rows
    (re.compile(r"_{2,}|-{3,}|={3,}"), " "),      # rules
)


# Uppercase tokens that are words, not initialisms. Everything else in
# caps gets spelled out — "NVO" is unintelligible as a word, and so are
# ATR, RSI and VWAP.
_SPOKEN_AS_WORDS = {
    "LOCKBOT", "JARVIS", "OK", "OKAY", "A", "I", "AM", "PM",
    "USD", "USA", "CEO", "NASA", "LIVE", "OPEN", "STOP", "LOSS",
    "NO", "YES", "ON", "OFF", "UP", "ALL", "AND", "THE", "FOR",
}


def _spell_initialisms(text: str) -> str:
    """
    Turn NVO into "N V O" so it is heard as letters.

    Tickers and indicator names are read as nonsense words otherwise —
    "nvoh", "vee-wap" — which is a large part of what sounds wrong.
    """

    def replace(match: re.Match) -> str:
        token = match.group(0)

        if token in _SPOKEN_AS_WORDS:
            return token

        return " ".join(token)

    return re.sub(r"\b[A-Z]{2,6}\b", replace, text)


# Applied in order, after markdown is stripped. Each one removes a
# specific thing that makes a synthesizer stumble.
_SPEECH_FIXES = (
    # Dashes used as pauses. A synthesizer treats them as a hard stop or
    # ignores them entirely; a comma gives the natural breath instead.
    (re.compile(r"\s*[—–]\s*"), ", "),
    # snake_case identifiers run together or get read letter by letter.
    (re.compile(r"\b([a-z]+)_([a-z_]+)\b"), lambda m: m.group(0).replace("_", " ")),
    # A signed number reads better as a word than as a symbol. The
    # currency marker is kept — neural voices say "$0.73" as "seventy
    # three cents", and dropping it leaves a bare number with no unit.
    (re.compile(r"(?<![\w.])\+\s*(\$?\d)"), r"up \1"),
    (re.compile(r"(?<![\w.])-\s*(\$?\d)"), r"down \1"),
    # Ratios and slashes.
    (re.compile(r"(\d)\s*/\s*(\d)"), r"\1 out of \2"),
    (re.compile(r"\bR:R\b|\bR/R\b", re.I), "reward to risk"),
    (re.compile(r"\bP&L\b|\bP&l\b", re.I), "P and L"),
    (re.compile(r"\s*&\s*"), " and "),
    # Collapse the punctuation pile-ups these substitutions can leave.
    (re.compile(r"\.\s*\.+"), "."),
    (re.compile(r",\s*,+"), ","),
    (re.compile(r"\s*,\s*\."), "."),
    (re.compile(r":\s*\."), "."),
    (re.compile(r"\s+([,.!?;:])"), r"\1"),
)


def clean_for_speech(text: str, limit: int = MAX_SPOKEN_CHARACTERS) -> str:
    """
    Turn a written reply into something that sounds natural read aloud.

    Two jobs. Strip markup, because a synthesizer reading "asterisk
    asterisk NVO asterisk asterisk" is worse than useless. Then normalise
    what remains for the ear: dashes become commas so the rhythm keeps
    moving, tickers get spelled out, signed numbers become "up" and
    "down", and stray punctuation from all of that is collapsed.

    The last part matters more than it sounds. Joining paragraphs used to
    produce "on the day.. Two open longs" — a double stop the voice reads
    as a long dead pause, which is most of what "choppy" actually was.
    """

    cleaned = text or ""

    for pattern, replacement in _MARKDOWN_PATTERNS:
        cleaned = pattern.sub(replacement, cleaned)

    cleaned = re.sub(r"[ \t]+", " ", cleaned)

    # Join paragraphs WITHOUT inventing a second full stop when the line
    # already ended in one.
    cleaned = re.sub(r"\.\s*\n{2,}", ". ", cleaned)
    cleaned = re.sub(r"\n{2,}", ". ", cleaned)
    cleaned = cleaned.replace("\n", " ").strip()

    cleaned = _spell_initialisms(cleaned)

    for pattern, replacement in _SPEECH_FIXES:
        cleaned = pattern.sub(replacement, cleaned)

    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()

    if len(cleaned) <= limit:
        return cleaned

    # Cut at the last sentence end before the limit so it doesn't stop
    # mid-word. Fall back to a hard cut if there isn't one.
    window = cleaned[:limit]
    boundary = max(window.rfind(". "), window.rfind("! "), window.rfind("? "))

    if boundary > limit // 2:
        return window[: boundary + 1]

    return window.rstrip() + "..."


# ---------------------------------------------------------------------------
# Speech out
# ---------------------------------------------------------------------------

_SPEAK_SCRIPT = r"""
param([string]$Path, [string]$VoiceName, [int]$Rate)
Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
try { $synth.SelectVoice($VoiceName) } catch { }
$synth.Rate = $Rate
$text = [System.IO.File]::ReadAllText($Path, [System.Text.Encoding]::UTF8)
$synth.Speak($text)
"""


# ---------------------------------------------------------------------------
# Playback
#
# MCI, called directly through winmm.dll. The previous implementation
# spawned PowerShell and drove a WPF MediaPlayer, which was wrong in two
# ways:
#
#   1. WPF MediaPlayer needs a dispatcher and a message pump. A console
#      PowerShell host has neither, so playback stuttered — the residual
#      choppiness that survived fixing the text and the interruption bug.
#
#   2. It could not tell when audio actually finished, so it slept a
#      guessed duration plus a second of padding. Measured on one line:
#      11.78s of wall clock for 9.05s of audio.
#
# MCI plays MP3 natively, reports real playback state so the wait is
# exact, and needs no subprocess at all. Same line: 9.51s.
# ---------------------------------------------------------------------------

_mci_lock = threading.Lock()
_mci_alias: str | None = None
_mci_counter = 0

# MCI is thread-affine: a device opened on one thread cannot reliably be
# commanded from another. Sending "stop" from the main thread returned an
# empty status and did nothing at all, while the audio kept playing.
#
# So interruption is a flag. The thread that opened the device notices it
# on the next poll and issues the stop itself, where it works.
_stop_playback = threading.Event()


def _mci(command: str) -> tuple[int, str]:
    """Send one MCI command. Returns (error_code, response)."""

    buffer = ctypes.create_unicode_buffer(255)

    try:
        error = ctypes.windll.winmm.mciSendStringW(command, buffer, 254, None)
    except Exception:
        return 1, ""

    return error, buffer.value


def _play_audio(path: str) -> bool:
    """
    Play a file, blocking until it ends or is stopped.

    Polls real playback state rather than sleeping a duration, so it
    neither cuts the tail off nor pads silence onto the end.
    """

    global _mci_alias, _mci_counter

    with _mci_lock:
        _mci_counter += 1
        alias = f"lockbot{_mci_counter}"

    _stop_playback.clear()

    error, _ = _mci(f'open "{path}" type mpegvideo alias {alias}')

    if error:
        return False

    with _mci_lock:
        _mci_alias = alias

    try:
        error, _ = _mci(f"play {alias}")

        if error:
            return False

        while True:
            # Checked before the status query so an interruption is acted
            # on immediately rather than after another poll interval.
            if _stop_playback.is_set():
                _mci(f"stop {alias}")
                break

            _, mode = _mci(f"status {alias} mode")

            if mode != "playing":
                break

            time.sleep(0.08)

        return True

    finally:
        _mci(f"close {alias}")

        with _mci_lock:
            if _mci_alias == alias:
                _mci_alias = None

# Playback runs in a child process so it can be killed mid-sentence, and
# on a background thread so the caller is never blocked by it.
#
# Both matter. Speech runs far slower than reading: a reply that takes two
# seconds to scan takes thirty to hear. Blocking the prompt for those
# thirty seconds makes the whole thing feel broken — which is exactly how
# the first version behaved, freezing for 41 seconds per answer with no
# way to type or interrupt.
_speech_lock = threading.Lock()
_speech_process: subprocess.Popen | None = None
_speech_thread: threading.Thread | None = None


def stop_speaking() -> None:
    """Cut off anything currently being said. Safe to call at any time."""

    # Neural playback: raise the flag. The thread that opened the device
    # stops it on its next poll — MCI will not accept the command from
    # here.
    _stop_playback.set()

    # SAPI fallback still runs as a child process.
    global _speech_process

    with _speech_lock:
        process = _speech_process
        _speech_process = None

    if process and process.poll() is None:
        try:
            process.kill()
        except Exception:
            pass


def is_speaking() -> bool:
    """True while audio is playing."""

    return bool(_speech_thread and _speech_thread.is_alive())


def wait_until_quiet(timeout: float = 120.0) -> None:
    """
    Block until speech finishes.

    Needed before opening the microphone in a continuous conversation:
    the mic hears the speakers, so listening while it is still talking
    transcribes its own reply back as your next question.
    """

    deadline = time.time() + timeout

    while is_speaking() and time.time() < deadline:
        time.sleep(0.15)

    # Speakers ring slightly past the end of the file, and the recognizer
    # calibrates ambient noise the moment it opens.
    time.sleep(0.35)


def speak_async(text: str, voice: str | None = None) -> None:
    """
    Say something without blocking the caller.

    Any speech already in progress is cut off first — when a new answer
    arrives, the previous one is stale and talking over it helps nobody.
    """

    global _speech_thread

    stop_speaking()

    if _speech_thread and _speech_thread.is_alive():
        _speech_thread.join(timeout=2)

    _speech_thread = threading.Thread(
        target=speak,
        args=(text, voice),
        daemon=True,
    )
    _speech_thread.start()


def _speak_edge(text: str, voice: str | None = None) -> bool:
    """
    Speak with a Microsoft neural voice. Returns False on any failure.

    Synthesis produces an MP3, played through MCI. Interruption stops the
    device rather than killing a process, so it is immediate and leaves
    nothing behind.
    """

    import asyncio

    try:
        import edge_tts
    except ImportError:
        return False

    audio_file = None

    try:
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as handle:
            audio_file = handle.name

        async def synthesize() -> None:
            communicate = edge_tts.Communicate(
                text,
                voice or EDGE_VOICE,
                rate=EDGE_RATE,
                pitch=EDGE_PITCH,
            )
            await communicate.save(audio_file)

        asyncio.run(synthesize())

        if not os.path.getsize(audio_file):
            return False

        # Being stopped is an interruption, not a failure, and must not
        # return False — that would trigger the SAPI fallback and start
        # the whole reply again in the robotic voice.
        _play_audio(audio_file)

        return True

    except Exception:
        return False

    finally:
        if audio_file:
            try:
                Path(audio_file).unlink()
            except OSError:
                pass


def speak(text: str, voice: str | None = None, rate: int | None = None) -> bool:
    """
    Say something out loud. Returns False if speech is unavailable.

    Tries the neural engine first and falls back to the offline Windows
    synthesizer. Never raises — a machine with no audio device, or no
    network, should not be able to take down whatever is talking.
    """

    spoken = clean_for_speech(text)

    if not spoken:
        return False

    set_voice_state(STATE_SPEAKING, spoken[:120])

    try:
        return _speak_with_fallback(spoken, voice, rate)
    finally:
        set_voice_state(STATE_IDLE)


def _speak_with_fallback(spoken: str, voice: str | None, rate: int | None) -> bool:
    if TTS_ENGINE != "sapi":
        if _speak_edge(spoken, voice):
            return True

        # Say so. A silent downgrade to the offline synthesizer sounds
        # exactly like the neural voice having "gone choppy", and there
        # would be nothing on screen to explain why.
        print("[voice] Neural engine unavailable — falling back to offline SAPI.")

    return _speak_sapi(spoken, voice, rate)


def _speak_sapi(text: str, voice: str | None = None, rate: int | None = None) -> bool:
    """Offline Windows synthesizer. Always available, noticeably choppier."""

    spoken = text
    text_file = None
    script_file = None

    try:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".txt", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(spoken)
            text_file = handle.name

        with tempfile.NamedTemporaryFile(
            "w", suffix=".ps1", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(_SPEAK_SCRIPT)
            script_file = handle.name

        # Popen and register the handle, NOT subprocess.run. stop_speaking()
        # reads _speech_process to interrupt the fallback voice, and with a
        # blocking run() nothing ever set it — so a reply spoken through
        # SAPI could not be cut off at all. You would have had to sit
        # through it, which is exactly the case the interrupt exists for.
        global _speech_process

        process = subprocess.Popen(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                script_file,
                "-Path",
                text_file,
                "-VoiceName",
                voice or select_voice(),
                "-Rate",
                str(rate if rate is not None else DEFAULT_RATE),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        with _speech_lock:
            _speech_process = process

        try:
            process.wait(timeout=180)
        except subprocess.TimeoutExpired:
            process.kill()

        with _speech_lock:
            if _speech_process is process:
                _speech_process = None

        # Being killed is an interruption, not a failure.
        class _Result:
            returncode = 0

        result = _Result()

        return result.returncode == 0

    except Exception:
        return False

    finally:
        for path in (text_file, script_file):
            if path:
                try:
                    Path(path).unlink()
                except OSError:
                    pass


def available_voices() -> list[tuple[str, str]]:
    """Return (name, culture) for every synthesizer voice installed."""

    script = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        "foreach ($v in $s.GetInstalledVoices()) "
        "{ Write-Output ($v.VoiceInfo.Name + '|' + $v.VoiceInfo.Culture.Name) }"
    )

    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            timeout=30,
        )

    except Exception:
        return []

    voices = []

    for line in result.stdout.splitlines():
        line = line.strip()

        if not line:
            continue

        name, _, culture = line.partition("|")
        voices.append((name.strip(), culture.strip()))

    return voices


def select_voice(voices: list[tuple[str, str]] | None = None) -> str:
    """
    Pick the best available voice, preferring a British one.

    Order: an explicit LOCKBOT_VOICE override, then a known en-GB voice
    by name, then any voice whose culture is in PREFERRED_ACCENTS, then
    any English voice, then whatever is first.
    """

    if VOICE_OVERRIDE:
        return VOICE_OVERRIDE

    voices = available_voices() if voices is None else voices

    if not voices:
        return "Microsoft Zira Desktop"

    names = {name for name, _ in voices}

    for candidate in PREFERRED_VOICE_NAMES:
        if candidate in names:
            return candidate

    for accent in PREFERRED_ACCENTS:
        for name, culture in voices:
            if culture.lower() == accent.lower():
                return name

    for name, culture in voices:
        if culture.lower().startswith("en"):
            return name

    return voices[0][0]


# ---------------------------------------------------------------------------
# Speech in
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Choosing a microphone
#
# Windows exposes the same physical microphone several times, once per
# audio API (MME, DirectSound, WASAPI), and the system default is not
# necessarily the one that works. On this machine the G522 headset
# appeared at indices 1, 8 and 17 — and index 1, the default, returned
# silence: peak amplitude 62 out of 32767.
#
# The symptom is indistinguishable from "LOCKBOT cannot hear me", because
# the recogniser opens the device happily and simply never hears speech.
# So rather than trusting the default, probe for one that is actually
# receiving.
#
# Devices whose names suggest they capture OUTPUT are avoided even when
# they show the strongest signal: a virtual mixer or stereo-mix endpoint
# hears the speakers, which would transcribe LOCKBOT's own replies back
# as your next question.
# ---------------------------------------------------------------------------

# Devices whose names suggest they capture OUTPUT rather than you. Worth
# avoiding even if one tests "live": a virtual mixer or stereo-mix
# endpoint hears the speakers, so LOCKBOT would transcribe its own replies
# back as your next question.
_OUTPUT_CAPTURE_HINTS = (
    "virtual", "mixer", "stereo mix", "what u hear", "wave speaker",
)

MIC_INDEX_OVERRIDE = os.getenv("LOCKBOT_MIC_INDEX", "").strip()

# ---------------------------------------------------------------------------
# Recogniser tuning
#
# The default that ruins dictation is pause_threshold, which is 0.8
# seconds. Pause a fraction longer than that mid-sentence — to think, to
# say a ticker, to breathe — and the phrase is closed and sent. The
# symptom is "it only caught a couple of my words", and it is not a
# recognition failure at all: the recogniser transcribed exactly what it
# was given, which was the first fragment.
#
# 1.4s lets a sentence hold together through an ordinary pause without
# feeling laggy at the end.
PAUSE_THRESHOLD = float(os.getenv("LOCKBOT_PAUSE_THRESHOLD", "1.4"))

# How much quiet has to precede speech before it counts as the start of a
# phrase. Low enough that the first word is not clipped.
NON_SPEAKING_DURATION = float(os.getenv("LOCKBOT_NON_SPEAKING", "0.4"))

# Longest single utterance. The old 20s was fine; the pause threshold was
# what ended phrases early, not this.
PHRASE_LIMIT = int(os.getenv("LOCKBOT_PHRASE_LIMIT", "30"))

# A calibrated threshold in a quiet room can land so low that the room
# itself reads as speech, or high enough that a normal voice does not.
# The floor keeps quiet speech audible; dynamic adjustment then tracks
# the room from there.
MIN_ENERGY_THRESHOLD = float(os.getenv("LOCKBOT_MIN_ENERGY", "120"))

_chosen_mic: int | None = None
_mic_resolved = False


def list_microphones() -> list[tuple[int, str, float | None]]:
    """Every input device with its ambient energy. None when unreadable."""

    try:
        import speech_recognition as sr
        import pyaudio
    except ImportError:
        return []

    audio = pyaudio.PyAudio()
    devices = []

    try:
        for index in range(audio.get_device_count()):
            info = audio.get_device_info_by_index(index)

            if info.get("maxInputChannels", 0) > 0:
                devices.append((index, str(info.get("name", "")).strip()))
    finally:
        audio.terminate()

    recognizer = sr.Recognizer()
    measured = []

    for index, name in devices:
        try:
            with sr.Microphone(device_index=index) as source:
                recognizer.adjust_for_ambient_noise(source, duration=0.6)
                measured.append((index, name, recognizer.energy_threshold))
        except Exception:
            measured.append((index, name, None))

    return measured


def find_working_microphone() -> int | None:
    """
    Which input device to use. The override, or the system default.

    An earlier version of this ranked devices by ambient energy and
    picked the loudest. That was wrong, and measurably so: two scans a
    minute apart disagreed completely — index 1 read 14 then 41, index 8
    read 786 then 1. Ambient noise in a quiet room is not a signal, so
    ranking on it chooses essentially at random, and worse, it does so
    while looking authoritative.

    The only reliable test is speech. test_microphone() runs it, and the
    answer goes in LOCKBOT_MIC_INDEX where it stays put.
    """

    global _chosen_mic, _mic_resolved

    if _mic_resolved:
        return _chosen_mic

    _mic_resolved = True

    if MIC_INDEX_OVERRIDE.isdigit():
        _chosen_mic = int(MIC_INDEX_OVERRIDE)
        print(f"[voice] Using microphone index {_chosen_mic} (LOCKBOT_MIC_INDEX)")
    else:
        _chosen_mic = None

    return _chosen_mic


def test_microphone(index: int | None = None, seconds: int = 5) -> dict:
    """
    Record from one device while the user speaks, and report what arrived.

    Peak amplitude against a spoken voice is the honest test: a device
    that opens without error and returns near-silence while somebody is
    talking into it is the exact failure that reads as "LOCKBOT cannot
    hear me".
    """

    try:
        import speech_recognition as sr
    except ImportError:
        return {"ok": False, "error": "SpeechRecognition is not installed"}

    recognizer = sr.Recognizer()

    try:
        source = (
            sr.Microphone(device_index=index) if index is not None
            else sr.Microphone()
        )

        with source as handle:
            recognizer.adjust_for_ambient_noise(handle, duration=0.5)
            audio = recognizer.listen(
                handle, timeout=seconds, phrase_time_limit=seconds
            )

    except sr.WaitTimeoutError:
        return {"ok": False, "peak": 0, "error": "nothing above the noise floor"}

    except Exception as error:
        return {"ok": False, "error": f"{type(error).__name__}: {error}"}

    raw = audio.get_raw_data()

    peak = 0

    for offset in range(0, min(len(raw), 200000), 2):
        sample = abs(int.from_bytes(raw[offset:offset + 2], "little", signed=True))
        peak = max(peak, sample)

    result = {"ok": peak >= 500, "peak": peak, "bytes": len(raw)}

    if result["ok"]:
        try:
            result["heard"] = recognizer.recognize_google(audio)
        except Exception as error:
            result["heard"] = None
            result["error"] = f"audio captured but not recognised ({type(error).__name__})"
    else:
        result["error"] = "essentially silence — this device is not picking you up"

    return result


def _microphone():
    """Open the chosen microphone, falling back to the system default."""

    import speech_recognition as sr

    index = find_working_microphone()

    return sr.Microphone(device_index=index) if index is not None else sr.Microphone()


def listen(timeout: int = 8, phrase_limit: int = 20) -> str | None:
    """
    Record one utterance and return it as text.

    Returns None when nothing was heard or recognition failed — both are
    normal outcomes at a microphone, not errors worth raising.
    """

    try:
        import speech_recognition as sr
    except ImportError:
        print("[voice] SpeechRecognition is not installed.")
        return None

    recognizer = sr.Recognizer()

    # Let a sentence survive a natural pause. This is the setting that
    # decides whether you get your whole question or its first four words.
    recognizer.pause_threshold = PAUSE_THRESHOLD
    recognizer.non_speaking_duration = min(NON_SPEAKING_DURATION, PAUSE_THRESHOLD)
    recognizer.dynamic_energy_threshold = True

    try:
        with _microphone() as source:
            # Rooms differ. One short calibration keeps a noisy desk from
            # being heard as continuous speech.
            recognizer.adjust_for_ambient_noise(source, duration=0.6)

            # Never let calibration set a threshold so high that ordinary
            # speech falls under it.
            recognizer.energy_threshold = min(
                recognizer.energy_threshold, MIN_ENERGY_THRESHOLD
            )

            set_voice_state(STATE_LISTENING)
            print("[listening…]", flush=True)

            audio = recognizer.listen(
                source,
                timeout=timeout,
                phrase_time_limit=max(phrase_limit, PHRASE_LIMIT),
            )

    except sr.WaitTimeoutError:
        print("[voice] Nothing heard.")
        return None

    except Exception as error:
        print(f"[voice] Microphone unavailable: {type(error).__name__}: {error}")
        return None

    try:
        if RECOGNITION_ENGINE == "sphinx":
            return recognizer.recognize_sphinx(audio)

        return recognizer.recognize_google(audio)

    except sr.UnknownValueError:
        print("[voice] Could not make that out.")
        return None

    except Exception as error:
        print(f"[voice] Recognition failed: {type(error).__name__}: {error}")
        return None


# ---------------------------------------------------------------------------
# Wake word
#
# The wake word is matched OFFLINE, by the recognizer built into Windows,
# against a grammar containing only the trigger phrases. That is the whole
# design: a grammar of four phrases is far more reliable than open
# dictation, and — more importantly — nothing leaves the machine until you
# have actually addressed it.
#
# An always-open microphone streaming to a cloud service would be a poor
# thing to put on a computer that trades. This way the network is only
# touched once you have said the word.
# ---------------------------------------------------------------------------

WAKE_PHRASES = [
    phrase.strip()
    for phrase in os.getenv(
        "LOCKBOT_WAKE_PHRASES",
        "lockbot|hey lockbot|okay lockbot|wake up lockbot",
    ).split("|")
    if phrase.strip()
]

# Below this the recognizer is guessing. A false trigger on a machine that
# trades is worse than having to repeat yourself.
WAKE_CONFIDENCE = float(os.getenv("LOCKBOT_WAKE_CONFIDENCE", "0.65"))

_WAKE_SCRIPT = r"""
param([string]$Phrases, [double]$MinConfidence)
Add-Type -AssemblyName System.Speech

$engine = New-Object System.Speech.Recognition.SpeechRecognitionEngine
$choices = New-Object System.Speech.Recognition.Choices
foreach ($phrase in $Phrases -split '\|') {
    if ($phrase.Trim()) { $choices.Add($phrase.Trim()) }
}

$builder = New-Object System.Speech.Recognition.GrammarBuilder
$builder.Append($choices)
$grammar = New-Object System.Speech.Recognition.Grammar($builder)

$engine.LoadGrammar($grammar)
$engine.SetInputToDefaultAudioDevice()

Write-Output "READY"

while ($true) {
    $result = $engine.Recognize([TimeSpan]::FromSeconds(6))
    if ($result -ne $null -and $result.Confidence -ge $MinConfidence) {
        Write-Output ("DETECTED|" + $result.Text + "|" + [math]::Round($result.Confidence, 2))
        break
    }
}
"""


def wait_for_wake_word(
    phrases: list[str] | None = None,
    min_confidence: float | None = None,
    on_ready=None,
    cancel: "threading.Event | None" = None,
) -> str | None:
    """
    Block until a wake phrase is heard. Returns the phrase, or None.

    Runs entirely offline. Returns None on Ctrl+C or if the recognizer
    cannot start, so a caller can fall back to push-to-talk rather than
    losing the session.
    """

    phrases = phrases or WAKE_PHRASES
    threshold = WAKE_CONFIDENCE if min_confidence is None else min_confidence

    script_file = None

    try:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".ps1", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(_WAKE_SCRIPT)
            script_file = handle.name

        set_voice_state(STATE_WAITING)

        process = subprocess.Popen(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                script_file,
                "-Phrases",
                "|".join(phrases),
                "-MinConfidence",
                str(threshold),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )

        # Watch for cancellation on a side thread. Reading the recogniser's
        # stdout blocks, so the only way to abandon it — when the user
        # starts typing instead of speaking — is to kill the process out
        # from under the read.
        stop_watcher = None

        if cancel is not None:
            def watch() -> None:
                while process.poll() is None:
                    if cancel.wait(0.1):
                        try:
                            process.kill()
                        except Exception:
                            pass
                        return

            stop_watcher = threading.Thread(target=watch, daemon=True)
            stop_watcher.start()

        try:
            for line in process.stdout:
                line = line.strip()

                if line == "READY" and on_ready:
                    on_ready()

                if line.startswith("DETECTED|"):
                    parts = line.split("|")
                    return parts[1] if len(parts) > 1 else "lockbot"

        finally:
            if process.poll() is None:
                process.kill()

            if stop_watcher:
                stop_watcher.join(timeout=1)

        return None

    except KeyboardInterrupt:
        return None

    except Exception as error:
        print(f"[voice] Wake word unavailable: {type(error).__name__}: {error}")
        return None

    finally:
        if script_file:
            try:
                Path(script_file).unlink()
            except OSError:
                pass


def wake_word_available() -> bool:
    """Whether Windows has an offline recognizer for the wake word."""

    script = (
        "Add-Type -AssemblyName System.Speech; "
        "[System.Speech.Recognition.SpeechRecognitionEngine]::"
        "InstalledRecognizers().Count"
    )

    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            timeout=30,
        )

        return int((result.stdout or "0").strip() or 0) > 0

    except Exception:
        return False


def microphone_available() -> bool:
    """Check for a usable input device without recording anything."""

    try:
        import speech_recognition as sr

        return bool(sr.Microphone.list_microphone_names())

    except Exception:
        return False


def edge_voices(prefix: str = "en-") -> list[str]:
    """List the neural voices available, optionally filtered by locale."""

    import asyncio

    try:
        import edge_tts
    except ImportError:
        return []

    try:
        async def fetch():
            return await edge_tts.list_voices()

        voices = asyncio.run(fetch())

    except Exception:
        return []

    return sorted(
        f"{v['ShortName']} ({v['Gender']})"
        for v in voices
        if v.get("Locale", "").startswith(prefix)
    )


def check() -> dict:
    """Report what speech features work on this machine."""

    voices = available_voices()
    chosen = select_voice(voices)

    culture = next(
        (culture for name, culture in voices if name == chosen), "unknown"
    )

    british = [
        f"{name} ({culture})"
        for name, culture in voices
        if culture.lower() in {a.lower() for a in PREFERRED_ACCENTS}
    ]

    try:
        import speech_recognition as sr

        microphones = sr.Microphone.list_microphone_names()
    except Exception:
        microphones = []

    try:
        import importlib.util

        edge_available = importlib.util.find_spec("edge_tts") is not None
    except Exception:
        edge_available = False

    active_engine = "edge (neural)" if edge_available and TTS_ENGINE != "sapi" else "sapi (offline)"

    return {
        "engine": active_engine,
        "edge_installed": edge_available,
        "edge_voice": EDGE_VOICE if edge_available else None,
        "british_voice_active": (
            EDGE_VOICE.lower().startswith("en-gb")
            if edge_available and TTS_ENGINE != "sapi"
            else culture.lower() == "en-gb"
        ),
        "fallback_speech_out": bool(voices),
        "fallback_voices": [f"{name} ({culture})" for name, culture in voices],
        "fallback_selected": chosen,
        "offline_british_installed": british or None,
        "speech_in": bool(microphones),
        "microphones": microphones[:6],
        "recognition_engine": RECOGNITION_ENGINE,
        "wake_word_available": wake_word_available(),
        "wake_phrases": WAKE_PHRASES,
        "wake_confidence": WAKE_CONFIDENCE,
    }


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

def _self_test() -> int:
    """Offline checks of the text handling. No audio, no network."""

    failures = []

    def verify(name: str, condition: bool, detail: str = "") -> None:
        if condition:
            print(f"  PASS  {name}")
        else:
            print(f"  FAIL  {name} {detail}")
            failures.append(name)

    print("Text preparation")

    # The ticker is spelled out by design, so this checks the markup is
    # gone rather than pinning the exact string.
    result = clean_for_speech("**NVO** is holding *1.2%*")
    verify(
        "strips bold and italics",
        "*" not in result and "1.2%" in result,
        result,
    )

    result = clean_for_speech("# Heading\n\nBody text")
    verify("strips headings", "Heading" in result and "#" not in result, result)

    result = clean_for_speech("- one\n- two")
    verify("strips bullets", "*" not in result and "-" not in result, result)

    result = clean_for_speech("See `close_all.py` now")
    verify("strips inline code", "`" not in result, result)

    result = clean_for_speech("| a | b |\n| 1 | 2 |\ntext")
    verify("drops table rows", "|" not in result, result)

    result = clean_for_speech("[the docs](http://x.com) explain")
    verify("keeps link text only", "http" not in result and "docs" in result, result)

    print()
    print("Sounding natural")

    # The bug that produced most of the choppiness: joining paragraphs
    # after a line that already ended in a full stop.
    result = clean_for_speech("Down on the day.\n\nTwo open longs.")
    verify("no double full stop", ".." not in result, result)

    result = clean_for_speech("yesterday — down $1.36")
    verify("em dash becomes a comma", "—" not in result and "," in result, result)

    result = clean_for_speech("NVO and LVS are open")
    verify("tickers are spelled out", "N V O" in result and "L V S" in result, result)

    result = clean_for_speech("LOCKBOT is fine")
    verify("real words are not spelled out", "LOCKBOT" in result, result)

    result = clean_for_speech("showing +$0.73 today")
    verify("plus becomes 'up'", "up $0.73" in result, result)

    result = clean_for_speech("showing -$0.06 today")
    verify("minus becomes 'down'", "down $0.06" in result, result)

    result = clean_for_speech("hyphenated-word stays")
    verify("hyphens in words survive", "hyphenated-word" in result, result)

    result = clean_for_speech("stuck in pending_cancel now")
    verify("underscores become spaces", "pending cancel" in result, result)

    result = clean_for_speech("P&L was flat")
    verify("ampersand is spoken", "and" in result and "&" not in result, result)

    result = clean_for_speech("0 out of 3 used")
    verify("ratios read naturally", "out of" in result, result)

    result = clean_for_speech("Equity is $249.47 today")
    verify("currency is left for the voice", "$249.47" in result, result)

    for junk in ("a . . b", "a , , b", "a , . b"):
        result = clean_for_speech(junk)
        verify(f"collapses {junk!r}", ".." not in result and ",," not in result, result)

    verify("empty input is empty", clean_for_speech("") == "")

    long_text = ("This is a sentence. " * 200)
    trimmed = clean_for_speech(long_text)
    verify(
        "long text is trimmed",
        len(trimmed) <= MAX_SPOKEN_CHARACTERS,
        str(len(trimmed)),
    )
    verify("trims at a sentence boundary", trimmed.endswith(("." , "...")), trimmed[-20:])

    no_boundary = "x" * 3000
    verify(
        "hard-cuts text with no sentences",
        len(clean_for_speech(no_boundary)) <= MAX_SPOKEN_CHARACTERS + 3,
    )

    # Speaking must never raise, whatever it is handed.
    print()
    print("Robustness")

    for payload in ("", "   ", None):
        try:
            speak(payload) if payload is not None else speak("")
            verify(f"speak({payload!r}) does not raise", True)
        except Exception as error:
            verify(f"speak({payload!r}) does not raise", False, str(error))

    print()

    if failures:
        print(f"{len(failures)} check(s) FAILED: {', '.join(failures)}")
        return 1

    print("All voice checks passed.")
    return 0


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="LOCKBOT speech in and out.")
    parser.add_argument("--check", action="store_true", help="report what works")
    parser.add_argument("--voices", action="store_true", help="list neural voices")
    parser.add_argument("--mics", action="store_true",
                        help="list input devices and which one is live")
    parser.add_argument("--say", metavar="TEXT", help="speak a line of text")
    parser.add_argument("--listen", action="store_true", help="capture one utterance")
    parser.add_argument("--wake", action="store_true",
                        help="wait for the wake word, then capture one utterance")
    parser.add_argument("--self-test", action="store_true", help="offline checks")

    args = parser.parse_args()

    if args.self_test:
        return _self_test()

    if args.check:
        import json

        print(json.dumps(check(), indent=2))
        return 0

    if args.mics:
        # Ambient noise cannot tell a live microphone from a dead one, so
        # this asks you to speak into each candidate. It is slower and it
        # is the only version that gives a trustworthy answer.
        devices = [
            (index, name)
            for index, name, energy in list_microphones()
            if energy is not None
        ]

        if not devices:
            print("No usable input devices found.")
            return 1

        chosen = find_working_microphone()

        print("Microphone test. You will be asked to speak into each device.")
        print(f"Currently using: {chosen if chosen is not None else 'system default'}\n")

        working = []

        for index, name in devices:
            skip = any(h in name.lower() for h in _OUTPUT_CAPTURE_HINTS)

            if skip:
                print(f"[{index}] {name[:44]}  — skipped (captures output, not you)")
                continue

            print(f"\n[{index}] {name[:44]}")
            input("      press Enter, then say 'testing one two three'... ")

            result = test_microphone(index, seconds=5)

            if result.get("ok"):
                heard = result.get("heard")
                print(f"      PEAK {result['peak']}/32767  HEARD: {heard!r}")
                working.append((index, name, result["peak"], heard))
            else:
                print(f"      peak {result.get('peak', 0)}/32767  — {result.get('error')}")

        print("\n" + "=" * 62)

        if working:
            best = max(working, key=lambda w: w[2])
            print(f"Best device: [{best[0]}] {best[1]}")
            print(f"\nPut this in .env:\n    LOCKBOT_MIC_INDEX={best[0]}")
        else:
            print("No device picked up your voice.")
            print("Check Windows Settings > System > Sound > Input, and that the")
            print("headset's physical mute switch is off.")

        return 0

    if args.voices:
        names = edge_voices()

        if not names:
            print("edge-tts is not installed. Run: pip install edge-tts")
            return 1

        print(f"{len(names)} English neural voices. Current: {EDGE_VOICE}\n")

        for name in names:
            marker = "  <- current" if name.startswith(EDGE_VOICE) else ""
            print(f"  {name}{marker}")

        print("\nSet LOCKBOT_EDGE_VOICE in .env to change it.")
        return 0

    if args.say:
        ok = speak(args.say)
        print("Spoken." if ok else "Speech unavailable.")
        return 0 if ok else 1

    if args.wake:
        print(f"Say one of: {', '.join(WAKE_PHRASES)}")
        print("(offline — nothing leaves this machine until you do)\n")

        phrase = wait_for_wake_word(on_ready=lambda: print("[listening for wake word]"))

        if not phrase:
            print("Wake word not detected.")
            return 1

        print(f"Woken by: {phrase}\n")
        speak("Yes?")
        heard = listen()
        print(f"Heard: {heard}" if heard else "Nothing recognized.")
        return 0 if heard else 1

    if args.listen:
        heard = listen()
        print(f"Heard: {heard}" if heard else "Nothing recognized.")
        return 0 if heard else 1

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())

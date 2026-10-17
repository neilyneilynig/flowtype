#!/usr/bin/env python3
"""flowtype - push-to-talk dictation anywhere (a from-scratch Wispr Flow clone).

Press the hotkey, talk, press it again: your words get cleaned up and typed into
whatever window has focus.

    flowtype daemon        run the background service (keeps the model warm)
    flowtype start|stop    begin / end a recording (scripting)
    flowtype toggle        start or stop (bound to Super+Alt+D)
    flowtype stats         words dictated, speed, time saved
    flowtype history [n]   last n transcripts
"""
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

HOME = Path.home()
STATE = (Path(os.environ["XDG_RUNTIME_DIR"]) if os.environ.get("XDG_RUNTIME_DIR")
         else HOME / ".cache") / "flowtype"
SOCK = STATE / "flowtype.sock"
DATA = HOME / ".local/share/flowtype"
HISTORY = DATA / "history.jsonl"
CONFIG = HOME / ".config/flowtype/config.json"

DEFAULT_CONFIG = {
    "model": "small.en",
    "device": "auto",  # auto | cuda | cpu
    "type_output": True,  # type into focused window
    "copy_output": True,  # also put on clipboard
    "remove_fillers": True,
    "min_seconds": 0.35,  # ignore accidental taps
    "max_seconds": 300,  # auto-stop if you forget the second press
    # spoken word -> replacement, applied case-insensitively on whole words
    "dictionary": {"omarchy": "Omarchy", "hyprland": "Hyprland", "claude code": "Claude Code"},
    # say the trigger alone, get the expansion
    "snippets": {"my email": "you@example.com"},
}

FILLERS = r"(?:,\s*)?\b(?:um+|uh+|erm+|er|ah+|hmm+|mm+)\b[,.]?"
VOICE_COMMANDS = [
    (r"\s*\bnew paragraph\b[,.]?\s*", "\n\n"),
    (r"\s*\bnew line\b[,.]?\s*", "\n"),
    (r"\s*\bperiod\b(?=\s|$)", "."),
    (r"\s*\bcomma\b(?=\s|$)", ","),
    (r"\s*\bquestion mark\b(?=\s|$)", "?"),
    (r"\s*\bexclamation (?:mark|point)\b(?=\s|$)", "!"),
]


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG.exists():
        try:
            cfg.update(json.loads(CONFIG.read_text()))
        except json.JSONDecodeError as e:
            print(f"flowtype: bad config {CONFIG}: {e}", file=sys.stderr)
    else:
        CONFIG.parent.mkdir(parents=True, exist_ok=True)
        CONFIG.write_text(json.dumps(DEFAULT_CONFIG, indent=2) + "\n")
    return cfg


# ---------------------------------------------------------------- cleanup


def clean(text, cfg):
    t = " ".join(text.split())
    if not t:
        return ""

    low = t.lower().strip(" .!?,")
    for trigger, expansion in cfg["snippets"].items():
        if low == trigger.lower():
            return expansion

    if cfg["remove_fillers"]:
        t = re.sub(FILLERS, "", t, flags=re.I)
        # "I I think" -> "I think" (stutter repeats)
        t = re.sub(r"\b(i|the|a|an|to|and|so|we|it|is|that|this)(?:,?\s+\1\b)+", r"\1", t, flags=re.I)

    for pattern, repl in VOICE_COMMANDS:
        t = re.sub(pattern, repl, t, flags=re.I)

    for spoken, written in cfg["dictionary"].items():
        t = re.sub(rf"\b{re.escape(spoken)}\b", written, t, flags=re.I)

    # tidy punctuation spacing left behind by removals
    t = re.sub(r"[ \t]+([,.!?])", r"\1", t)
    t = re.sub(r"([,.!?])\1+", r"\1", t)
    t = re.sub(r"^[,.\s]+", "", t)
    t = re.sub(r"[ \t]{2,}", " ", t)

    # capitalise sentence starts (including after inserted newlines)
    t = re.sub(r"(^|[.!?]\s+|\n\s*)([a-z])", lambda m: m.group(1) + m.group(2).upper(), t)
    return t.strip(" ")


# ---------------------------------------------------------------- daemon


class Notifier:
    """One notification that updates in place instead of stacking up."""

    def __init__(self):
        self.id = None

    def show(self, title, body="", urgency="low", timeout=0):
        cmd = ["notify-send", "-p", "-a", "flowtype", "-u", urgency, "-t", str(timeout)]
        if self.id:
            cmd += ["-r", self.id]
        try:
            out = subprocess.run(cmd + [title, body], capture_output=True, text=True, timeout=3)
            self.id = out.stdout.strip() or self.id
        except (OSError, subprocess.TimeoutExpired):
            pass

    def close(self):
        if self.id:
            # replacing with a 1ms timeout makes it vanish
            self.show("flowtype", "", timeout=1)
        self.id = None


class Daemon:
    def __init__(self):
        self.cfg = load_config()
        self.model = None
        self.rec = None
        self.rec_path = STATE / "clip.wav"
        self.rec_started = 0.0
        self.lock = threading.Lock()
        self.notify = Notifier()

    def load_model(self):
        from faster_whisper import WhisperModel

        dev = self.cfg["device"]
        tries = [("cuda", "float16"), ("cpu", "int8")] if dev == "auto" else [
            (dev, "float16" if dev == "cuda" else "int8")]
        for device, compute in tries:
            try:
                self.model = WhisperModel(self.cfg["model"], device=device, compute_type=compute)
                # warm up so the first real dictation is fast
                import numpy as np
                list(self.model.transcribe(np.zeros(16000, dtype="float32"))[0])
                print(f"flowtype: {self.cfg['model']} ready on {device}", flush=True)
                return
            except Exception as e:  # CUDA libs missing etc.
                print(f"flowtype: {device} failed ({e}), trying next", flush=True)
        raise SystemExit("flowtype: could not load any whisper model")

    def start(self):
        with self.lock:
            if self.rec:
                return "already recording"
            self.rec_path.unlink(missing_ok=True)
            self.rec = subprocess.Popen(
                ["pw-record", "--rate", "16000", "--channels", "1", "--format", "s16", str(self.rec_path)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.rec_started = started = time.time()
        threading.Timer(self.cfg["max_seconds"], self.auto_stop, args=(started,)).start()
        self.notify.show("🎙  Listening…", "press Super+Alt+D again to type")
        return "recording"

    def stop(self):
        with self.lock:
            if not self.rec:
                return "not recording"
            rec, self.rec = self.rec, None
            seconds = time.time() - self.rec_started
        rec.send_signal(signal.SIGINT)
        try:
            rec.wait(timeout=2)
        except subprocess.TimeoutExpired:
            rec.kill()
        if seconds < self.cfg["min_seconds"]:
            self.notify.close()
            return "too short"
        threading.Thread(target=self.finish, args=(seconds,), daemon=True).start()
        return "transcribing"

    def auto_stop(self, started):
        if self.rec and self.rec_started == started:
            self.stop()

    def auto_stop(self, started):
        if self.rec and self.rec_started == started:
            self.stop()

    def toggle(self):
        return self.stop() if self.rec else self.start()

    def finish(self, seconds):
        self.notify.show("✍  Transcribing…")
        t0 = time.time()
        try:
            segments, _ = self.model.transcribe(
                str(self.rec_path), language="en", beam_size=5, vad_filter=True,
                initial_prompt="Hello, this is a dictated message with proper punctuation.")
            raw = "".join(s.text for s in segments).strip()
        except Exception as e:
            self.notify.show("flowtype error", str(e), urgency="critical", timeout=4000)
            return
        text = clean(raw, self.cfg)
        took = time.time() - t0
        if not text:
            self.notify.show("🤷  Didn't catch that", timeout=1500)
            return
        self.notify.close()
        self.output(text)
        self.log(raw, text, seconds, took)

    def output(self, text):
        if self.cfg["copy_output"]:
            subprocess.run(["wl-copy", "--", text], check=False)
        if self.cfg["type_output"]:
            # small pause so the hotkey's modifiers are fully released first
            time.sleep(0.08)
            subprocess.run(["wtype", "-s", "4", "--", text], check=False)

    def log(self, raw, text, seconds, took):
        DATA.mkdir(parents=True, exist_ok=True)
        with HISTORY.open("a") as f:
            f.write(json.dumps({"t": time.time(), "raw": raw, "text": text,
                                "audio_s": round(seconds, 2), "stt_s": round(took, 2),
                                "words": len(text.split())}) + "\n")

    def serve(self):
        STATE.mkdir(parents=True, exist_ok=True)
        STATE.chmod(0o700)
        SOCK.unlink(missing_ok=True)
        self.load_model()
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(SOCK))
        srv.listen(8)
        actions = {"start": self.start, "stop": self.stop, "toggle": self.toggle,
                   "ping": lambda: "pong", "reload": self.reload}
        while True:
            conn, _ = srv.accept()
            with conn:
                cmd = conn.recv(64).decode().strip()
                fn = actions.get(cmd)
                reply = fn() if fn else f"unknown command {cmd!r}"
                conn.sendall(reply.encode())

    def reload(self):
        model = self.cfg["model"]
        self.cfg = load_config()
        if self.cfg["model"] != model:
            self.load_model()
        return "reloaded"


# ---------------------------------------------------------------- client


def send(cmd):
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(5)
            s.connect(str(SOCK))
            s.sendall(cmd.encode())
            return s.recv(256).decode()
    except (FileNotFoundError, ConnectionRefusedError):
        subprocess.run(["notify-send", "-u", "critical", "flowtype isn't running",
                        "systemctl --user start flowtype"], check=False)
        sys.exit("flowtype: daemon not running (systemctl --user start flowtype)")


def read_history():
    if not HISTORY.exists():
        return []
    return [json.loads(line) for line in HISTORY.read_text().splitlines() if line.strip()]


def stats():
    h = read_history()
    if not h:
        print("No dictations yet. Press Super+Alt+D and talk.")
        return
    words = sum(e["words"] for e in h)
    talk = sum(e["audio_s"] for e in h)
    wpm = words / (talk / 60) if talk else 0
    typing_wpm = 40  # average typist
    saved = words / typing_wpm * 60 - talk
    today = time.strftime("%Y-%m-%d")
    today_words = sum(e["words"] for e in h if time.strftime("%Y-%m-%d", time.localtime(e["t"])) == today)
    avg_stt = sum(e["stt_s"] for e in h) / len(h)
    print(f"""
  flowtype stats
  ──────────────────────────────
  dictations     {len(h)}
  words total    {words}
  words today    {today_words}
  speaking pace  {wpm:.0f} wpm  ({wpm / typing_wpm:.1f}x typing)
  time saved     {saved / 60:.1f} min vs typing at {typing_wpm} wpm
  avg transcribe {avg_stt:.2f}s
""")


def history(n):
    for e in read_history()[-n:]:
        ts = time.strftime("%m-%d %H:%M", time.localtime(e["t"]))
        print(f"{ts}  {e['text']}")


def main():
    # transcripts and recordings are private: keep everything we create user-only
    os.umask(0o077)
    args = sys.argv[1:] or ["help"]
    cmd = args[0]
    if cmd == "daemon":
        Daemon().serve()
    elif cmd in ("start", "stop", "toggle", "ping", "reload"):
        print(send(cmd))
    elif cmd == "stats":
        stats()
    elif cmd == "history":
        history(int(args[1]) if len(args) > 1 else 10)
    elif cmd == "clean":  # debug: flowtype clean "um so new line hello"
        print(repr(clean(" ".join(args[1:]), load_config())))
    else:
        print(__doc__)


if __name__ == "__main__":
    main()

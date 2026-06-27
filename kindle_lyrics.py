#!/usr/bin/env python3
import logging
import os
import re
import shlex
import struct
import subprocess
import sys
import tempfile
import threading
import time

import requests
import spotipy
from dotenv import load_dotenv
from spotipy.oauth2 import SpotifyOAuth

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("lyrics")

FBINK = os.getenv("FBINK_PATH", "/mnt/us/koreader/fbink")
KINDLE_HOST = os.environ["KINDLE_IP"]
KINDLE_USER = os.getenv("KINDLE_SSH_USER", "root")
KINDLE_PORT = int(os.getenv("KINDLE_PORT", "2222"))
KINDLE_KEY = os.getenv("KINDLE_KEY") or None
POLL_SECONDS = float(os.getenv("POLL_SECONDS", "2"))
LYRIC_OFFSET_S = float(os.getenv("LYRIC_OFFSET_S", "1.5"))

FONT_PATH = os.getenv("FONT_PATH")
ROTA = os.getenv("KINDLE_ROTA", "0")
_ROTATE_CMD = f"echo {shlex.quote(ROTA)} > /sys/class/graphics/fb0/rotate"

FB_BACKUP = "/tmp/lyrical_fb.bak"
LYRIC_PX = os.getenv("LYRIC_PX", "120")
HEADER_PX = os.getenv("HEADER_PX", "48")


def _font(px):
    if FONT_PATH:
        return f"-t regular={shlex.quote(FONT_PATH)},px={px}"
    return f"-S {max(1, round(int(px) / 40))}"

LRC_LINE = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]")


def parse_lrc(text):
    out = []
    for raw in text.splitlines():
        stamps = LRC_LINE.findall(raw)
        if not stamps:
            continue
        words = LRC_LINE.sub("", raw).strip()
        for mm, ss in stamps:
            out.append((int(mm) * 60 + float(ss), words))
    out.sort(key=lambda x: x[0])
    return out


def current_line(lyrics, position_s):
    line = ""
    for start, text in lyrics:
        if start <= position_s:
            line = text
        else:
            break
    return line


def fetch_lyrics(name, artist, duration_s):
    try:
        r = requests.get(
            "https://lrclib.net/api/get",
            params={
                "track_name": name,
                "artist_name": artist,
                "duration": round(duration_s),
            },
            headers={"User-Agent": "kindle-lyrics/1.0"},
            timeout=10,
        )
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return parse_lrc(r.json().get("syncedLyrics") or "")
    except requests.RequestException as e:
        log.warning("LRCLIB fetch failed: %s", e)
        return None


_CONTROL_PATH = os.path.join(tempfile.gettempdir(), f"kindle-lyrics-{os.getuid()}.sock")


def _ssh_argv():
    key = os.path.expanduser(KINDLE_KEY) if KINDLE_KEY else None
    if key and not os.path.isfile(key):
        raise FileNotFoundError(
            f"KINDLE_KEY={key!r} not found. "
            "Run setup-ssh-key.sh or set KINDLE_KEY to your private key path."
        )
    argv = [
        "ssh",
        "-p", str(KINDLE_PORT),
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ConnectTimeout=10",
        "-o", "PasswordAuthentication=no",
        "-o", "NumberOfPasswordPrompts=0",
        "-o", "ControlMaster=auto",
        "-o", f"ControlPath={_CONTROL_PATH}",
        "-o", "ControlPersist=60",
    ]
    if key:
        argv += ["-i", key, "-o", "IdentitiesOnly=yes"]
    argv.append(f"{KINDLE_USER}@{KINDLE_HOST}")
    return argv


def _fbink_cmds(lyric, header):
    if not lyric and not header:
        return f"{FBINK} -c -f -B WHITE"
    cmds = [f"{FBINK} -c -B WHITE -C BLACK {_font(HEADER_PX)} "
            f"-y 0 -m {shlex.quote(header)}"]
    if lyric:
        cmds.append(f"{FBINK} -o -C BLACK {_font(LYRIC_PX)} "
                    f"-m -M {shlex.quote(lyric)}")
    return " ; ".join(cmds)


def _read_rotation():
    try:
        r = subprocess.run(
            _ssh_argv() + ["cat /sys/class/graphics/fb0/rotate"],
            check=True, timeout=15, text=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        return r.stdout.strip()
    except (subprocess.SubprocessError, OSError) as e:
        log.warning("Couldn't read screen rotation (won't restore on exit): %s", e)
        return None


class Kindle:

    def __init__(self):
        self._rotated = False

    def show(self, lyric, header=""):
        cmd = _fbink_cmds(lyric, header)
        if not self._rotated:
            cmd = f"{_ROTATE_CMD} ; {cmd}"
        try:
            subprocess.run(
                _ssh_argv() + [cmd],
                check=True, timeout=15,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
            )
            self._rotated = True
            return True
        except subprocess.CalledProcessError as e:
            log.warning("Kindle ssh failed (exit %s): %s", e.returncode,
                        (e.stderr or "").strip())
            return False
        except (subprocess.TimeoutExpired, OSError) as e:
            log.warning("Kindle ssh failed, will retry: %s", e)
            return False

    def snapshot(self):
        return self._run(f"cat /dev/fb0 > {shlex.quote(FB_BACKUP)}",
                         "Couldn't snapshot screen (exit will clear to white)")

    def restore(self, rotation, snapshot_ok):
        if snapshot_ok:
            inner = f"cat {shlex.quote(FB_BACKUP)} > /dev/fb0 ; {FBINK} -f -s"
        else:
            inner = _fbink_cmds("", "")
        if rotation:
            inner = f"echo {shlex.quote(rotation)} > /sys/class/graphics/fb0/rotate ; " + inner
        self._run(inner, "Kindle restore failed")

    @staticmethod
    def _run(remote_cmd, warn):
        try:
            subprocess.run(
                _ssh_argv() + [remote_cmd], check=True, timeout=15,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            return True
        except (subprocess.SubprocessError, OSError) as e:
            log.warning("%s: %s", warn, e)
            return False


STOP = threading.Event()
_TOUCH_PROC = None

TOUCH_EVENT = os.getenv("KINDLE_TOUCH_EVENT", "/dev/input/event1")

_EVENT_FMT = "<llHHi"
_EVENT_SIZE = struct.calcsize(_EVENT_FMT)
_EV_KEY, _BTN_TOUCH = 0x01, 0x14A
_EV_ABS, _ABS_MT_TRACKING_ID = 0x03, 0x39


def _is_touch_down(etype, code, value):
    return ((etype == _EV_KEY and code == _BTN_TOUCH and value == 1) or
            (etype == _EV_ABS and code == _ABS_MT_TRACKING_ID and value >= 0))


def _watch_touch():
    global _TOUCH_PROC
    _TOUCH_PROC = proc = subprocess.Popen(
        _ssh_argv() + [f"cat {shlex.quote(TOUCH_EVENT)}"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )
    try:
        while not STOP.is_set():
            buf = proc.stdout.read(_EVENT_SIZE)
            if len(buf) < _EVENT_SIZE:
                log.warning("Touch watcher stopped; tap-to-exit disabled "
                            "(check KINDLE_TOUCH_EVENT=%s)", TOUCH_EVENT)
                return
            _, _, etype, code, value = struct.unpack(_EVENT_FMT, buf)
            if _is_touch_down(etype, code, value):
                log.info("Tap detected — exiting")
                STOP.set()
                return
    finally:
        proc.terminate()


def main():
    sp = spotipy.Spotify(
        auth_manager=SpotifyOAuth(
            client_id=os.environ["SPOTIFY_CLIENT_ID"],
            client_secret=os.environ["SPOTIFY_CLIENT_SECRET"],
            redirect_uri=os.environ["SPOTIFY_REDIRECT_URI"],
            scope="user-read-currently-playing",
        )
    )
    kindle = Kindle()
    orig_rota = _read_rotation()
    snap_ok = kindle.snapshot()
    threading.Thread(target=_watch_touch, daemon=True).start()

    track_id = None
    lyrics = None
    header = ""
    shown = object()

    try:
        while not STOP.is_set():
            try:
                t_poll = time.monotonic()
                playing = sp.currently_playing()
                elapsed = time.monotonic() - t_poll
            except Exception as e:
                log.warning("Spotify poll failed: %s", e)
                time.sleep(POLL_SECONDS)
                continue

            item = playing.get("item") if playing else None
            if not item or not playing.get("is_playing"):
                if shown != ("", ""):
                    if kindle.show("", ""):
                        shown = ("", "")
                time.sleep(POLL_SECONDS)
                continue

            if item["id"] != track_id:
                track_id = item["id"]
                name = item["name"]
                artist = ", ".join(a["name"] for a in item["artists"])
                header = f"{name} — {artist}"
                lyrics = fetch_lyrics(name, artist, item["duration_ms"] / 1000)
                shown = object()
                log.info("Now playing: %s (%s)", header,
                         "synced" if lyrics else "no lyrics")

            position_s = playing["progress_ms"] / 1000 + elapsed + LYRIC_OFFSET_S
            lyric = current_line(lyrics, position_s) if lyrics else "No lyrics found"

            key = (header, lyric)
            if key != shown:
                if kindle.show(lyric, header):
                    shown = key

            time.sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        pass
    finally:
        STOP.set()
        if _TOUCH_PROC:
            _TOUCH_PROC.terminate()
        kindle.restore(orig_rota, snap_ok)


def selftest():
    lrc = "[00:01.00]hello\n[00:03.50]world\n[01:00.00][01:30.00]repeat"
    p = parse_lrc(lrc)
    assert p == [
        (1.0, "hello"), (3.5, "world"), (60.0, "repeat"), (90.0, "repeat")
    ], p
    assert current_line(p, 0) == ""
    assert current_line(p, 2) == "hello"
    assert current_line(p, 3.5) == "world"
    assert current_line(p, 95) == "repeat"
    evil = 'x"; reboot #'
    assert shlex.quote(evil) == "'x\"; reboot #'", shlex.quote(evil)
    c = _fbink_cmds("a line", "Song — Artist")
    if FONT_PATH:
        assert f"px={HEADER_PX}" in c and f"px={LYRIC_PX}" in c, c
    else:
        assert "-S " in c, c
    assert " ; " in c, c
    assert shlex.quote(evil) in _fbink_cmds(evil, "h"), "lyric not escaped"
    assert _fbink_cmds("", "") == f"{FBINK} -c -f -B WHITE"
    assert " ; " not in _fbink_cmds("", "Song — Artist")
    assert _ROTATE_CMD == f"echo {shlex.quote(ROTA)} > /sys/class/graphics/fb0/rotate"
    assert _EVENT_SIZE == 16, _EVENT_SIZE
    assert _is_touch_down(_EV_KEY, _BTN_TOUCH, 1)
    assert not _is_touch_down(_EV_KEY, _BTN_TOUCH, 0)
    assert _is_touch_down(_EV_ABS, _ABS_MT_TRACKING_ID, 42)
    assert not _is_touch_down(_EV_ABS, _ABS_MT_TRACKING_ID, -1)
    assert not _is_touch_down(_EV_ABS, 0x35, 100)
    print("selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        main()

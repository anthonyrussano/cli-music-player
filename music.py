#!/usr/bin/env python3
"""music - a keyboard-only album player for YouTube (mpv + yt-dlp).

Run `music` with no arguments for an interactive shell, or `music --help`
for the one-shot commands. The library lives in a single YAML file.
"""

import argparse
import fcntl
import glob
import hashlib
import json
import os
import random
import re
import select
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from queue import Empty, Queue
from pathlib import Path
from urllib.parse import parse_qs, urlparse

try:
    import yaml
except ImportError:
    sys.exit("music: PyYAML is required (Debian/Ubuntu: sudo apt install python3-yaml)")

VERSION = "2.2.0"
SCRIPT_DIR = Path(os.path.realpath(__file__)).parent
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "music-player"
RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR") or STATE_DIR)
SOCKET_PATH = RUNTIME_DIR / "music-player.sock"
QUEUE_FILE = STATE_DIR / "queue.json"
M3U_FILE = STATE_DIR / "queue.m3u"
MPV_LOG = STATE_DIR / "mpv.log"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "music-player"
DEFAULT_MUSIC_DIR = Path("/mnt/c/dev/music")
AUDIO_RESTART_FILE = STATE_DIR / "audio-restart.json"
AUDIO_RESTART_LOG = STATE_DIR / "audio-restart.log"


class MusicError(Exception):
    pass


# --------------------------------------------------------------------------
# Output helpers
# --------------------------------------------------------------------------

COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def style(text, code):
    return f"\033[{code}m{text}\033[0m" if COLOR else str(text)


def bold(t): return style(t, "1")
def dim(t): return style(t, "2")
def green(t): return style(t, "32")
def cyan(t): return style(t, "36")
def yellow(t): return style(t, "33")


def fmt_time(seconds):
    if seconds is None:
        return "--:--"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def parse_time(text):
    """'90', '1:30' or '1:02:03' -> seconds."""
    parts = text.split(":")
    if not all(p.isdigit() for p in parts) or len(parts) > 3:
        raise MusicError(f"Bad time: {text!r} (use seconds, m:ss or h:mm:ss)")
    total = 0
    for p in parts:
        total = total * 60 + int(p)
    return total


def confirm(question, default=True):
    if not sys.stdin.isatty():
        return default
    hint = "[Y/n]" if default else "[y/N]"
    try:
        answer = input(f"{question} {hint} ").strip().lower()
    except EOFError:
        return default
    return default if not answer else answer.startswith("y")


# --------------------------------------------------------------------------
# Library (a single YAML file)
# --------------------------------------------------------------------------

LIBRARY_HEADER = """\
# Music library for the `music` CLI.
#
# albums: played front to back.   songs: standalone tracks.
# Edit by hand or with `music add URL`. Track fields: title, url, duration (s).
# Optional settings:
#   settings:
#     mpv_args: [--ao=pulse]   # extra flags passed to mpv
#     volume: 80               # starting volume
#     music_dir: /mnt/c/dev/music   # where downloaded songs are kept
"""


class _Dumper(yaml.SafeDumper):
    """Indent block sequences under their parent key, for readability."""

    def increase_indent(self, flow=False, indentless=False):
        return super().increase_indent(flow, False)


def default_library_path():
    env = os.environ.get("MUSIC_LIBRARY")
    return Path(env).expanduser() if env else SCRIPT_DIR / "library.yaml"


class Library:
    def __init__(self, path):
        self.path = Path(path)
        self.data = {"albums": [], "songs": []}
        if self.path.exists():
            with open(self.path, encoding="utf-8") as fh:
                loaded = yaml.safe_load(fh) or {}
            if not isinstance(loaded, dict):
                raise MusicError(f"{self.path}: top level must be a mapping")
            self.data.update(loaded)
        self.data["albums"] = self.data.get("albums") or []
        self.data["songs"] = self.data.get("songs") or []
        self._validate()

    def _validate(self):
        for album in self.data["albums"]:
            if not isinstance(album, dict) or not album.get("title"):
                raise MusicError(f"{self.path}: every album needs a title: {album!r}")
            album["tracks"] = album.get("tracks") or []
            for track in album["tracks"]:
                if not isinstance(track, dict) or not track.get("url"):
                    raise MusicError(f"{self.path}: track without url in {album['title']!r}")
        for song in self.data["songs"]:
            if not isinstance(song, dict) or not song.get("url"):
                raise MusicError(f"{self.path}: every song needs a url: {song!r}")

    @property
    def settings(self):
        return self.data.get("settings") or {}

    def save(self):
        ordered = {k: self.data[k] for k in ("settings", "albums", "songs") if self.data.get(k)}
        ordered.update({k: v for k, v in self.data.items() if k not in ordered and v})
        body = yaml.dump(ordered, Dumper=_Dumper, sort_keys=False,
                         allow_unicode=True, width=1000, default_flow_style=False)
        tmp = self.path.with_suffix(".yaml.tmp")
        tmp.write_text(LIBRARY_HEADER + "\n" + body, encoding="utf-8")
        tmp.replace(self.path)

    # Items are what `list` shows and numbers: albums (sorted), then songs.
    def items(self):
        albums = sorted(self.data["albums"], key=lambda a: (
            str(a.get("artist") or "").lower(), a.get("year") or 0, str(a["title"]).lower()))
        songs = sorted(self.data["songs"], key=lambda s: (
            str(s.get("artist") or "").lower(), str(s.get("title") or "").lower()))
        return [("album", a) for a in albums] + [("song", s) for s in songs]

    def find(self, query):
        """Resolve a number or search words to a list of matching items."""
        items = self.items()
        query = query.strip()
        if query.isdigit():
            n = int(query)
            if not 1 <= n <= len(items):
                raise MusicError(f"No item #{n} (library has {len(items)}; see `list`)")
            return [items[n - 1]]
        words = query.lower().split()
        matches = [it for it in items if all(w in item_label(it).lower() for w in words)]
        exact = [it for it in matches if item_name(it).lower() == query.lower()]
        return exact or matches

    def contains_url(self, url):
        for album in self.data["albums"]:
            if album.get("url") == url:
                return item_label(("album", album))
        for song in self.data["songs"]:
            if song.get("url") == url:
                return item_label(("song", song))
        return None

    def remove(self, item):
        kind, obj = item
        self.data["albums" if kind == "album" else "songs"].remove(obj)


def item_name(item):
    return str(item[1].get("title") or item[1].get("url"))


def item_label(item):
    kind, obj = item
    artist = obj.get("artist")
    return f"{artist} - {item_name(item)}" if artist else item_name(item)


def item_tracks(item):
    kind, obj = item
    if kind == "song":
        return [{"title": item_name(item), "url": obj["url"], "duration": obj.get("duration")}]
    return obj["tracks"]


def describe_item(n, item):
    kind, obj = item
    if kind == "album":
        tracks = obj["tracks"]
        total = sum(t.get("duration") or 0 for t in tracks)
        year = f" ({obj['year']})" if obj.get("year") else ""
        extra = f"{len(tracks)} tracks" + (f", {fmt_time(total)}" if total else "")
        return f"{n:>3}. {bold(item_name(item))}{year} {dim('— ' + str(obj.get('artist') or '?'))}  {dim(extra)}"
    artist = f" {dim('— ' + str(obj['artist']))}" if obj.get("artist") else ""
    dur = f"  {dim(fmt_time(obj['duration']))}" if obj.get("duration") else ""
    return f"{n:>3}. {item_name(item)}{artist}{dur}"


# --------------------------------------------------------------------------
# URL classification + metadata via yt-dlp
# --------------------------------------------------------------------------

def classify_url(url):
    """Guess what a URL points at: 'album', 'song', 'ambiguous' or 'unknown'.

    'ambiguous' is a watch URL that also carries a playlist (e.g. a track
    opened from an album page); the caller decides which one was meant.
    """
    parsed = urlparse(url)
    host = parsed.netloc.lower().removeprefix("www.").removeprefix("m.")
    query = parse_qs(parsed.query)
    list_id = (query.get("list") or [""])[0]
    if host in ("youtube.com", "music.youtube.com", "youtu.be"):
        if parsed.path.startswith("/browse/MPREb"):
            return "album", None
        if parsed.path == "/playlist" and list_id:
            return "album", list_id
        is_video = (host == "youtu.be" and len(parsed.path) > 1) or "v" in query \
            or parsed.path.startswith(("/shorts/", "/live/"))
        if is_video:
            # RD... lists are auto-generated mixes/radios, never albums.
            if list_id and not list_id.startswith("RD"):
                return "ambiguous", list_id
            return "song", None
    return "unknown", None


def run_ytdlp(args):
    if not shutil.which("yt-dlp"):
        raise MusicError("yt-dlp not found on PATH")
    try:
        proc = subprocess.run(["yt-dlp", "--ignore-config", "--socket-timeout", "20",
                               "--retries", "2", "-J", *args],
                              capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        raise MusicError("Playlist metadata lookup timed out; try the link again") from None
    if proc.returncode != 0 or not proc.stdout.strip():
        errors = [l for l in proc.stderr.splitlines() if "ERROR" in l] or proc.stderr.splitlines()[-3:]
        raise MusicError("yt-dlp failed:\n  " + "\n  ".join(errors))
    return json.loads(proc.stdout)


def clean_artist(name):
    return re.sub(r"\s+-\s+Topic$", "", name or "").strip() or None


_JUNK = re.compile(r"\s*[(\[][^)\]]*(official|video|audio|lyric|visuali[sz]er|remaster|\b4k\b|\bhd\b)[^)\]]*[)\]]", re.I)


def clean_title(title, artist):
    """'Artist - Song (Official Video) [4K]' -> 'Song'."""
    if not title:
        return None
    if artist and title.lower().startswith(artist.lower() + " - "):
        title = title[len(artist) + 3:]
    return _JUNK.sub("", title).strip() or title


def track_artist(info):
    artists = info.get("artists") or []
    return (info.get("album_artist") or (artists[0] if artists else None)
            or info.get("artist") or info.get("creator")
            or clean_artist(info.get("channel") or info.get("uploader")))


def fetch_album(url, enrich=True):
    info = run_ytdlp(["--flat-playlist", url])
    if info.get("_type") != "playlist":
        return None
    tracks = []
    for e in info.get("entries") or []:
        if not e:
            continue
        title = e.get("title") or ""
        if not e or not e.get("id") or title in ("[Private video]", "[Deleted video]"):
            continue
        track_url = e.get("url") or e.get("webpage_url")
        if not track_url or not track_url.startswith("http"):
            track_url = f"https://www.youtube.com/watch?v={e['id']}"
        track = {"title": title, "url": track_url}
        if e.get("duration"):
            track["duration"] = int(e["duration"])
        tracks.append(track)
    if not tracks:
        raise MusicError("That playlist has no playable tracks")
    album = {
        "title": re.sub(r"^Album\s+-\s+", "", info.get("title") or "Untitled"),
        "artist": None,
        "url": info.get("webpage_url") or url,
        "tracks": tracks,
    }
    # Flat playlists lack proper artist/year; the first track's full metadata has them.
    if enrich:
        try:
            first = run_ytdlp(["--no-playlist", tracks[0]["url"]])
            album["artist"] = track_artist(first)
            if first.get("release_year"):
                album["year"] = int(first["release_year"])
        except MusicError:
            pass
    if not album["artist"]:
        entries = info.get("entries") or [{}]
        album["artist"] = clean_artist(info.get("uploader") or info.get("channel")
                                       or (entries[0] or {}).get("channel")) or "Unknown artist"
    order = ("title", "artist", "year", "url", "tracks")
    return {k: album[k] for k in order if k in album}


def fetch_song(url):
    info = run_ytdlp(["--no-playlist", url])
    if info.get("_type") == "playlist":
        return None
    artist = track_artist(info)
    song = {
        "title": info.get("track") or clean_title(info.get("title"), artist) or url,
        "artist": artist,
        "url": info.get("webpage_url") or url,
    }
    if info.get("album"):
        song["album"] = info["album"]
    if info.get("duration"):
        song["duration"] = int(info["duration"])
    return {k: v for k, v in song.items() if v}


def resolve_add(url, force_kind=None):
    """Return ('album'|'song', metadata) for a URL."""
    kind, list_id = classify_url(url)
    if force_kind:
        if force_kind == "album" and list_id and kind == "ambiguous":
            url = f"https://www.youtube.com/playlist?list={list_id}"
        kind = force_kind
    elif kind == "ambiguous":
        album_like = list_id.startswith("OLAK5uy_")
        if sys.stdin.isatty():
            what = "an album" if album_like else "a playlist"
            print(f"This link is a track inside {what}.")
            want_album = confirm("Add the whole album/playlist? (no = just this song)", default=album_like)
        else:
            want_album = album_like
        if want_album:
            kind, url = "album", f"https://www.youtube.com/playlist?list={list_id}"
        else:
            kind = "song"

    print(dim("Looking up " + url + " ..."))
    if kind == "song":
        song = fetch_song(url)
        if song:
            return "song", song
        kind = "album"
    album = fetch_album(url)
    if album:
        if kind == "unknown" or force_kind == "album" or classify_url(url)[0] == "album":
            return "album", album
    if kind == "unknown":
        song = fetch_song(url)
        if song:
            return "song", song
    if album:
        return "album", album
    raise MusicError("Could not work out what that URL is")


def fetch_url_item(url, kind, enrich=True):
    """('album'|'song', metadata) for a URL already classified as `kind`."""
    if kind == "song":
        meta = fetch_song(url)
        item = ("song", meta) if meta else None
    else:
        meta = fetch_album(url, enrich=enrich)
        item = ("album", meta) if meta else None
        if item is None and kind == "unknown":
            meta = fetch_song(url)
            item = ("song", meta) if meta else None
    if item is None:
        raise MusicError("Could not load this playlist/song")
    return item


# --------------------------------------------------------------------------
# mpv control over its JSON IPC socket
# --------------------------------------------------------------------------

class Player:
    def __init__(self, socket_path=SOCKET_PATH):
        self.socket_path = str(socket_path)
        self._req = 0

    def command(self, *args, timeout=1.0):
        self._req += 1
        payload = json.dumps({"command": list(args), "request_id": self._req}) + "\n"
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(timeout)
                sock.connect(self.socket_path)
                sock.sendall(payload.encode())
                buf = b""
                deadline = time.monotonic() + timeout
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return None
                    sock.settimeout(remaining)
                    chunk = sock.recv(65536)
                    if not chunk:
                        return None
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        msg = json.loads(line)
                        if msg.get("request_id") == self._req:
                            if msg.get("error") not in ("success", None):
                                return None
                            return msg.get("data")
        except (OSError, ValueError):
            return None

    def get(self, prop):
        return self.command("get_property", prop)

    def set(self, prop, value):
        return self.command("set_property", prop, value)

    def running(self):
        return self.get("pid") is not None

    def stop(self):
        queue = load_queue()
        running = Path(queue["job_dir"], "running") if queue.get("job_dir") else None
        if (queue.get("worker_pid") and running and running.exists()
                and not owns_worker(queue["worker_pid"], queue["job_dir"])):
            # The worker died without cleaning up (e.g. WSL shut down); nothing will remove this.
            running.unlink(missing_ok=True)
        if queue.get("worker_pid") and running and running.exists():
            try:
                os.kill(queue["worker_pid"], signal.SIGTERM)
            except OSError:
                pass
            # The worker handles SIGTERM and reaps its active downloader.
            for _ in range(100):
                if not running.exists():
                    break
                time.sleep(0.05)
            else:
                raise MusicError("The previous download job is still stopping; try `stop` again")
        pid = self.command("get_property", "pid", timeout=0.2)
        if pid is None and owns_mpv(queue.get("pid"), self.socket_path):
            pid = queue["pid"]
        self.command("quit", timeout=0.2)
        deadline = time.monotonic() + 1
        while pid and owns_mpv(pid, self.socket_path) and time.monotonic() < deadline:
            time.sleep(0.05)
        if pid and owns_mpv(pid, self.socket_path):
            # A wedged mpv cannot answer IPC. Verify its command line before killing it.
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            os.unlink(self.socket_path)
        except OSError:
            pass
        current = load_queue()
        if current.get("job_dir") == queue.get("job_dir") and current:
            current["stopped"] = True
            if not owns_worker(current.get("worker_pid"), current.get("job_dir")):
                current["worker_pid"] = None
            save_queue(current)

    def start(self, tracks, start_index=0, extra_args=(), volume=None):
        if not shutil.which("mpv"):
            raise MusicError("mpv not found on PATH")
        self.stop()
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        lines = ["#EXTM3U"]
        for t in tracks:
            lines.append(f"#EXTINF:{int(t.get('duration') or -1)},{t.get('title', '')}")
            lines.append(t["url"])
        M3U_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
        cmd = ["mpv", "--no-video", "--no-terminal", "--idle=yes" if not tracks else "--idle=no",
               f"--log-file={MPV_LOG}",
               f"--input-ipc-server={self.socket_path}",
               "--ytdl-format=bestaudio/best",
               *([f"--playlist-start={start_index}", f"--playlist={M3U_FILE}"] if tracks else []),
               *extra_args]
        if volume is not None:
            cmd.append(f"--volume={volume}")
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if self.command("get_property", "pid", timeout=0.15) is not None:
                return proc.pid
            if proc.poll() is not None:
                break
            time.sleep(0.05)
        raise MusicError(f"mpv failed to start; see {MPV_LOG}")


def owns_mpv(pid, socket_path):
    """Validate a recorded Linux pid so recovery never kills a reused/unrelated pid."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        args = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        return (Path(os.fsdecode(args[0])).name == "mpv"
                and os.fsencode(f"--input-ipc-server={socket_path}") in args)
    except (OSError, IndexError):
        return False


def owns_worker(pid, job_dir):
    """True if `pid` is still the download worker for `job_dir` (not a dead or reused pid)."""
    if not isinstance(pid, int) or pid <= 0 or not job_dir:
        return False
    try:
        args = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        return b"_download" in args and os.fsencode(str(job_dir)) in args
    except OSError:
        return False


class PlaybackMonitor:
    """Observe mpv on one background connection; UI reads and commands never wait."""

    PROPERTIES = ("pid", "playlist-pos", "playlist-count", "media-title",
                  "time-pos", "duration", "pause", "idle-active", "volume")

    def __init__(self, socket_path):
        self.socket_path = str(socket_path)
        self.values = {}
        self.commands = Queue()
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._run, name="mpv-monitor", daemon=True)
        self.thread.start()

    def get(self, prop):
        return self.values.get(prop)

    def running(self):
        return self.get("pid") is not None

    def command(self, *args):
        self.commands.put(list(args))

    def set(self, prop, value):
        self.command("set_property", prop, value)

    def close(self):
        self.stopped.set()
        self.thread.join(timeout=0.5)

    def _run(self):
        while not self.stopped.is_set():
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                    sock.settimeout(0.2)
                    sock.connect(self.socket_path)
                    subscriptions = [{"command": ["observe_property", i, prop]}
                                     for i, prop in enumerate(self.PROPERTIES)]
                    sock.sendall("".join(json.dumps(c) + "\n" for c in subscriptions).encode())
                    buf = b""
                    while not self.stopped.is_set():
                        pending = []
                        # Bound a single batch so key repeats cannot starve property events.
                        for _ in range(64):
                            try:
                                pending.append({"command": self.commands.get_nowait()})
                            except Empty:
                                break
                        if pending:
                            sock.sendall("".join(json.dumps(c) + "\n" for c in pending).encode())
                        readable, _, _ = select.select([sock], [], [], 0.05)
                        if not readable:
                            continue
                        chunk = sock.recv(65536)
                        if not chunk:
                            break
                        buf += chunk
                        while b"\n" in buf:
                            line, buf = buf.split(b"\n", 1)
                            event = json.loads(line)
                            if event.get("event") == "property-change":
                                self.values[event["name"]] = event.get("data")
            except (OSError, ValueError):
                pass
            self.values = {}
            # Controls queued for a disconnected player must not hit its replacement.
            while True:
                try:
                    self.commands.get_nowait()
                except Empty:
                    break
            self.stopped.wait(0.1)


def load_queue():
    try:
        return json.loads(QUEUE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_queue(queue):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    write_json(QUEUE_FILE, queue)


def write_json(path, value):
    """Readers must never see a half-written queue or control request."""
    path = Path(path)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as fh:
        json.dump(value, fh, indent=2)
        tmp = Path(fh.name)
    tmp.replace(path)


def audio_restart_status():
    try:
        status = json.loads(AUDIO_RESTART_FILE.read_text())
        if status.get("state") == "restarting":
            with open(STATE_DIR / "audio-restart.lock", "a") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return status
                status.update(state="failed", message="Audio restart stopped unexpectedly; press r to retry.")
                write_json(AUDIO_RESTART_FILE, status)
        return status
    except (OSError, ValueError):
        return {}


def start_audio_restart(library_path, player_only=False, snapshot=None):
    """Launch recovery outside the UI; the inherited lock prevents duplicate restarts."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(STATE_DIR / "audio-restart.lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return "Audio restart already in progress."
        recovery_id = uuid.uuid4().hex
        write_json(AUDIO_RESTART_FILE, {"id": recovery_id, "state": "restarting",
                                       "message": "Restarting audio…", "snapshot": snapshot})
        try:
            with open(AUDIO_RESTART_LOG, "w") as log:
                subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "_restart-audio",
                                  str(Path(library_path).resolve()), str(int(player_only)), recovery_id,
                                  str(lock.fileno())], pass_fds=(lock.fileno(),),
                                 stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                 start_new_session=True)
        except OSError as exc:
            write_json(AUDIO_RESTART_FILE, {"id": recovery_id, "state": "failed", "message": str(exc)})
            raise MusicError(f"Could not start audio recovery: {exc}") from exc
    return "Restarting audio in the background."


# This fixed script targets only WSLg's supervised PulseAudio server.
# WSLGd recreates it; Weston and the user's distributions are left running.
RESTART_WSLG_AUDIO = r'''
found=0
for path in /proc/[0-9]*; do
    IFS= read -r process_name < "$path/comm" 2>/dev/null || continue
    [ "$process_name" = pulseaudio ] || continue
    case "$(tr '\000' ' ' < "$path/cmdline" 2>/dev/null)" in
        *"socket=/mnt/wslg/PulseServer "*)
            kill -KILL "${path##*/}" || exit 1
            found=1
            ;;
    esac
done
[ "$found" = 1 ] || { echo 'WSLg PulseAudio server not found' >&2; exit 1; }
'''


def restart_wslg_audio():
    wsl = shutil.which("wsl.exe")
    if not wsl and Path("/mnt/c/Windows/System32/wsl.exe").exists():
        wsl = "/mnt/c/Windows/System32/wsl.exe"
    if not os.environ.get("WSL_DISTRO_NAME") or not wsl:
        raise MusicError("WSLg audio restart is unavailable here; use `restart-audio --player-only` to restart mpv")
    if not shutil.which("pactl"):
        raise MusicError("Audio recovery needs pactl; install pulseaudio-utils before retrying")
    result = subprocess.run([wsl, "--system", "--cd", "/", "--exec", "sh", "-c", RESTART_WSLG_AUDIO],
                            capture_output=True, timeout=8)
    if result.returncode:
        encoding = "utf-16-le" if b"\0" in result.stderr else "utf-8"
        detail = result.stderr.decode(encoding, errors="replace").strip()
        raise MusicError("WSLg audio restart failed: " + (detail or "see audio-restart.log"))
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        try:
            probe = subprocess.run(["pactl", "info"], capture_output=True, text=True, timeout=1)
            if probe.returncode == 0:
                return
        except subprocess.TimeoutExpired:
            pass
        time.sleep(0.2)
    raise MusicError("WSLg audio is still unavailable after restart; see audio-restart.log")


def recover_audio(library_path, player_only, recovery_id):
    def report(state, message):
        write_json(AUDIO_RESTART_FILE, {"id": recovery_id, "state": state, "message": message})
    try:
        app = App(library_path)
        queue = load_queue()
        snapshot = audio_restart_status().get("snapshot") or {}
        if snapshot.get("job_dir") != queue.get("job_dir"):
            snapshot = {}
        saved = queue.get("resume_state") or {}
        live = {key: app.player.command("get_property", prop, timeout=0.2)
                for key, prop in (("position", "time-pos"), ("paused", "pause"), ("volume", "volume"))}
        index = queue.get("playing_index")
        if index is None:
            index = queue.get("waiting_index")
        if index is None:
            index = queue.get("last_index", 0)
        if saved.get("index") != index:
            saved = {}
        restore = {"index": index}
        for key, default in (("position", 0), ("paused", False), ("volume", None)):
            restore[key] = next((values[key] for values in (snapshot, live, saved)
                                 if values.get(key) is not None), default)
        if not player_only:
            report("restarting", "Restarting WSLg audio service…")
            restart_wslg_audio()
        current = load_queue()
        if (current.get("job_dir") != queue.get("job_dir")
                or (current.get("stopped") and not queue.get("stopped"))):
            report("done", "Audio restarted; playback changed during recovery and was left as selected.")
            return
        if not queue.get("tracks"):
            report("done", "Audio service restarted. No playlist to resume.")
            return
        report("restarting", "Reconnecting player and restoring playback…")
        app.play_tracks(queue["name"], queue.get("artist"), queue["tracks"], index,
                        queue.get("shuffled", False), restore=restore)
        restored_job = load_queue().get("job_dir")
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            current = load_queue()
            if current.get("job_dir") != restored_job or current.get("stopped"):
                report("done", "Audio restarted; playback was changed or stopped during recovery.")
                return
            if current.get("playback_error") or current.get("worker_error"):
                raise MusicError(current.get("playback_error") or current["worker_error"])
            if current.get("playing_index") == index and not current.get("restore"):
                report("done", "Audio restarted; playback restored" + (" (paused)." if restore["paused"] else "."))
                return
            time.sleep(0.1)
        raise MusicError("Audio restarted, but playback has not recovered yet; select a ready song to retry")
    except (MusicError, OSError, subprocess.TimeoutExpired, yaml.YAMLError) as exc:
        report("failed", "Audio restart failed: " + str(exc))
        print(str(exc), file=sys.stderr)


def now_playing(player, queue=None):
    """Snapshot of the current playback, or None if nothing is playing."""
    if queue is None:
        queue = load_queue()
    tracks = queue.get("tracks") or []
    if queue.get("job_dir"):
        if not player.running() or not tracks:
            return None
        pos = queue.get("waiting_index")
        if pos is None:
            pos = queue.get("playing_index")
        if pos is None:
            pos = queue.get("last_index", 0)
        count = len(tracks)
    else:
        pos = player.get("playlist-pos")
        if pos is None:
            return None
        count = player.get("playlist-count") or len(tracks)
    track = tracks[pos] if 0 <= pos < len(tracks) else {}
    playback_time = player.get("time-pos")
    return {
        "name": queue.get("name", "?"),
        "artist": queue.get("artist"),
        "title": track.get("title") or player.get("media-title") or "?",
        "track_artist": track.get("artist"),
        "index": pos + 1,
        "count": count,
        "time": playback_time,
        "duration": player.get("duration") or track.get("duration"),
        "paused": bool(player.get("pause")),
        "loading": bool(player.get("idle-active")) or playback_time is None,
        "volume": player.get("volume"),
        "finished": queue.get("finished", False),
        "waiting": queue.get("waiting_index") is not None,
        "playback_error": queue.get("playback_error"),
    }


def progress_bar(pos, total, width):
    if not total or pos is None or width < 5:
        return "─" * max(width, 0)
    filled = min(width, int(width * pos / total))
    return "━" * filled + ("●" if filled < width else "") + "─" * max(width - filled - 1, 0)


def status_lines(np, width=80):
    state = yellow("⏸ paused") if np["paused"] else (dim("… loading") if np["loading"] else green("▶ playing"))
    if np.get("finished"):
        state = dim("✓ playlist finished")
    elif np.get("playback_error"):
        state = yellow("Playback stopped: " + np["playback_error"])
    elif np.get("waiting"):
        state = yellow("… waiting for download")
    who = np["track_artist"] or np["artist"]
    heading = f"{bold(np['title'])}" + (f" {dim('— ' + who)}" if who else "")
    context = f"{np['name']}  ·  track {np['index']}/{np['count']}"
    vol = f"vol {int(np['volume'])}%" if np["volume"] is not None else ""
    times = f"{fmt_time(np['time'])} / {fmt_time(np['duration'])}"
    bar_w = max(10, min(60, width - len(times) - 4))
    return [
        f"{state}  {heading}",
        dim(context) + (f"  {dim('·')}  {dim(vol)}" if vol else ""),
        f"{cyan(progress_bar(np['time'], np['duration'], bar_w))}  {times}",
    ]


# --------------------------------------------------------------------------
# Detached download/playback coordinator. Only completed local files reach mpv.
# --------------------------------------------------------------------------

def cached_audio(track):
    key = hashlib.sha256(track["url"].encode()).hexdigest()
    folder = CACHE_DIR / key
    folder.mkdir(parents=True, exist_ok=True)
    return folder


AUDIO_SUFFIXES = {".m4a", ".webm", ".opus", ".mp3", ".ogg", ".mp4", ".wav", ".mkv", ".flac"}


def completed_audio(folder):
    for path in sorted(folder.glob("audio.*")):
        if is_audio(path):
            return path
    return None


def track_id(url):
    """The YouTube video id, or a short hash for other sites; tags each stored file."""
    parsed = urlparse(url)
    video = (parse_qs(parsed.query).get("v") or [""])[0]
    if not video and parsed.netloc.lower().endswith("youtu.be"):
        video = parsed.path.strip("/")
    return video if re.fullmatch(r"[\w-]{6,20}", video or "") else hashlib.sha256(url.encode()).hexdigest()[:11]


def safe_filename(name):
    """Make a title usable as a file name on Linux and Windows (/mnt/c) drives."""
    name = re.sub(r'[\x00-\x1f<>:"/\\|?*]', "_", str(name))
    return name.strip(" .")[:150] or "untitled"


def track_files(item, music_dir):
    """Copies of an item's tracks, each with `file`: its permanent path minus extension.

    Albums go to `Artist - Album (Year)/NN - Title [id]`, songs to
    `Singles/Artist - Title [id]`. The [id] lets renamed or renumbered
    tracks still be found.
    """
    kind, obj = item
    artist = obj.get("artist")
    tracks = []
    if kind == "album":
        year = f" ({obj['year']})" if obj.get("year") else ""
        folder = Path(music_dir) / safe_filename(f"{artist} - {item_name(item)}{year}"
                                                 if artist else item_name(item) + year)
        for n, t in enumerate(obj["tracks"], 1):
            stem = f"{n:02d} - {safe_filename(t.get('title') or 'Track')} [{track_id(t['url'])}]"
            tracks.append(dict(t, file=str(folder / stem)))
    else:
        for t in item_tracks(item):
            name = safe_filename(f"{artist} - {t['title']}" if artist else t["title"])
            tracks.append(dict(t, file=str(Path(music_dir) / "Singles" / f"{name} [{track_id(t['url'])}]")))
    return tracks


def is_audio(path):
    return path.suffix.lower() in AUDIO_SUFFIXES and path.stat().st_size > 0


def find_audio(track):
    """A finished download of a track: its permanent file, else the old hashed cache."""
    if track.get("file"):
        folder, tag = Path(track["file"]).parent, f"[{track_id(track['url'])}]"
        if folder.is_dir():
            # The stem check skips yt-dlp's `.part` and `.temp.m4a` work files.
            for path in sorted(folder.glob("*" + glob.escape(tag) + ".*")):
                if path.stem.endswith(tag) and is_audio(path):
                    return path
    legacy = CACHE_DIR / hashlib.sha256(track["url"].encode()).hexdigest()
    return completed_audio(legacy) if legacy.is_dir() else None


def download_cmd(track):
    if track.get("file"):
        Path(track["file"]).parent.mkdir(parents=True, exist_ok=True)
        output = track["file"] + ".%(ext)s"
    else:
        output = str(cached_audio(track) / "audio.%(ext)s")
    # m4a plays in nearly every player; tags need ffmpeg.
    tags = ["--embed-metadata"] if shutil.which("ffmpeg") else []
    return ["yt-dlp", "--ignore-config", "--no-playlist", "--no-progress",
            "--socket-timeout", "20", "--retries", "2", "--fragment-retries", "2",
            "-f", "bestaudio[ext=m4a]/bestaudio/best", *tags, "-o", output,
            "--", track["url"]]


def select_track(player, index):
    queue = load_queue()
    if not 0 <= index < len(queue.get("tracks") or []):
        return
    if queue.get("job_dir"):
        write_json(Path(queue["job_dir"]) / "request.json", {"index": index, "id": uuid.uuid4().hex})
    else:
        player.set("playlist-pos", index)


def step_track(player, step):
    queue = load_queue()
    if queue.get("job_dir"):
        index = queue.get("waiting_index")
        if index is None:
            index = queue.get("playing_index")
        if index is None:
            index = queue.get("last_index", 0)
        select_track(player, index + step)
    else:
        player.command("playlist-next" if step > 0 else "playlist-prev")


class DownloadSession:
    def __init__(self, job_dir):
        self.job_dir = Path(job_dir)
        self.queue = load_queue()
        self.player = Player()
        self.download = None
        self.download_log = None
        self.download_index = None
        self.cancelled = False
        self.request_id = None
        self.events = None

    def publish(self):
        # A replaced session must never overwrite the new active queue.
        if load_queue().get("job_dir") == str(self.job_dir):
            save_queue(self.queue)

    def request(self, index):
        self.queue.update(waiting_index=index, finished=False, playback_error=None)
        self.publish()

    def advance(self, index):
        tracks = self.queue["tracks"]
        while index < len(tracks) and tracks[index].get("download_status") == "failed":
            index += 1
        if index < len(tracks):
            self.request(index)
        else:
            self.queue.update(waiting_index=None, playing_index=None, finished=True)
            self.publish()

    def start_download(self, index):
        track = self.queue["tracks"][index]
        self.download_log = open(self.job_dir / f"track-{index + 1}.log", "w")
        self.download = subprocess.Popen(download_cmd(track), stdin=subprocess.DEVNULL,
                                         stdout=self.download_log, stderr=self.download_log,
                                         start_new_session=True)
        self.download_index = index
        track["download_status"] = "downloading"
        self.publish()

    def finish_download(self):
        index = self.download_index
        track = self.queue["tracks"][index]
        path = find_audio(track) if self.download.returncode == 0 else None
        self.download_log.close()
        self.download_log = None
        if path:
            track.update(download_status="ready", local_path=str(path))
        else:
            log = (self.job_dir / f"track-{index + 1}.log").read_text(errors="replace")
            errors = [line for line in log.splitlines() if "ERROR" in line]
            track.update(download_status="failed", error=(errors[-1] if errors else "Download failed; see track log"))
        self.download = None
        self.publish()

    def run(self):
        def cancel(_signum, _frame):
            self.cancelled = True
        signal.signal(signal.SIGTERM, cancel)
        signal.signal(signal.SIGINT, cancel)
        try:
            if self.queue.get("job_dir") != str(self.job_dir):
                return
            self.queue["worker_pid"] = os.getpid()
            self.publish()
            self.events = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.events.connect(self.player.socket_path)
            self.events.sendall("".join(json.dumps({"command": ["observe_property", i, prop]}) + "\n"
                                        for i, prop in enumerate(("time-pos", "pause", "volume"))).encode())
            self.events.setblocking(False)
            event_buffer = b""
            last_snapshot = time.monotonic()
            while not self.cancelled and load_queue().get("job_dir") == str(self.job_dir):
                # Process EOF before a new selection; manual loadfile's stop event is ignored.
                ready, _, _ = select.select([self.events], [], [], 0.1)
                if ready:
                    chunk = self.events.recv(65536)
                    if not chunk:
                        break
                    event_buffer += chunk
                    while b"\n" in event_buffer:
                        line, event_buffer = event_buffer.split(b"\n", 1)
                        event = json.loads(line)
                        if event.get("event") == "property-change" and event.get("data") is not None:
                            state = self.queue.setdefault("resume_state", {})
                            key = {"time-pos": "position", "pause": "paused", "volume": "volume"}.get(event.get("name"))
                            if key:
                                state[key] = event["data"]
                                if key == "position":
                                    state["index"] = self.queue.get("playing_index")
                        if event.get("event") == "file-loaded" and self.queue.get("restore"):
                            restore = self.queue.pop("restore")
                            self.player.command("seek", max(0, restore.get("position") or 0), "absolute+exact")
                            if restore.get("volume") is not None:
                                self.player.set("volume", restore["volume"])
                            self.player.set("pause", bool(restore.get("paused")))
                            self.queue["resume_state"] = restore
                            self.publish()
                        if event.get("event") == "end-file" and event.get("reason") in ("eof", "error"):
                            index = self.queue.get("playing_index")
                            if index is not None:
                                error = event.get("file_error") or "mpv could not play the downloaded file"
                                if error == "audio output initialization failed":
                                    # A broken output device affects all songs. Keep their cache/status
                                    # and wait for an explicit selection to retry after it is fixed.
                                    self.queue.update(playing_index=None, waiting_index=None,
                                                      playback_error=error)
                                    self.publish()
                                    continue
                                if event["reason"] == "error":
                                    self.queue["tracks"][index].update(download_status="failed", error=error)
                                self.queue["playing_index"] = None
                                if self.queue.get("waiting_index") is None:
                                    self.advance(index + 1)
                try:
                    request = json.loads((self.job_dir / "request.json").read_text())
                except (OSError, ValueError):
                    request = {}
                if request.get("id") and request["id"] != self.request_id:
                    self.request_id = request["id"]
                    index = request.get("index")
                    if isinstance(index, int) and 0 <= index < len(self.queue["tracks"]):
                        self.queue.pop("restore", None)
                        self.request(index)
                if self.download and self.download.poll() is not None:
                    self.finish_download()
                waiting = self.queue.get("waiting_index")
                if waiting is not None:
                    track = self.queue["tracks"][waiting]
                    if track["download_status"] == "failed":
                        # Keep the current song playing if a manual selection failed.
                        if self.queue.get("playing_index") is not None:
                            self.queue["waiting_index"] = None
                            self.publish()
                        else:
                            self.advance(waiting + 1)
                    elif track["download_status"] == "ready":
                        if self.queue.get("restore"):
                            self.player.set("pause", True)
                        self.player.command("loadfile", track["local_path"], "replace")
                        if not self.queue.get("restore"):
                            self.player.set("pause", False)
                            self.queue.setdefault("resume_state", {}).update(index=waiting, position=0, paused=False)
                        self.queue.update(playing_index=waiting, last_index=waiting, waiting_index=None)
                        self.publish()
                if self.download is None:
                    pending = [i for i, t in enumerate(self.queue["tracks"]) if t["download_status"] == "queued"]
                    waiting = self.queue.get("waiting_index")
                    if pending:
                        self.start_download(waiting if waiting in pending else pending[0])
                if time.monotonic() - last_snapshot >= 1:
                    self.publish()
                    last_snapshot = time.monotonic()
        except Exception as exc:
            self.queue["worker_error"] = str(exc)
            self.publish()
            raise
        finally:
            if self.download and self.download.poll() is None:
                os.killpg(self.download.pid, signal.SIGTERM)
                try:
                    self.download.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(self.download.pid, signal.SIGKILL)
                    self.download.wait()
            if self.download_log:
                self.download_log.close()
            if self.events:
                self.events.close()
            self.queue["worker_pid"] = None
            self.publish()
            (self.job_dir / "running").unlink(missing_ok=True)


# --------------------------------------------------------------------------
# Live playlist view with independent selection and playback markers
# --------------------------------------------------------------------------

NOW_KEYS = "r restart audio · ↑/↓ browse · Enter play · space pause · n/p next/prev · ←/→ seek · +/- vol · a URL · q"


def playlist_window(count, selected, offset, rows):
    rows = max(1, rows)
    offset = max(0, min(offset, max(0, count - rows)))
    if selected < offset:
        offset = selected
    elif selected >= offset + rows:
        offset = selected - rows + 1
    return offset, min(count, offset + rows)


def live_view(player, app=None):
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise MusicError("The live view needs an interactive terminal")
    import curses
    if hasattr(curses, "set_escdelay"):
        curses.set_escdelay(40)

    monitor = PlaybackMonitor(player.socket_path)

    def view(screen):
        # All mpv reads and transport controls below use the asynchronous connection.
        player = monitor
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        screen.keypad(True)
        screen.timeout(200)
        selected, offset, session = 0, 0, None
        notice = ""

        def put(row, text, attr=0):
            height, width = screen.getmaxyx()
            if 0 <= row < height and width > 1:
                # Remote metadata must not inject terminal control characters.
                text = "".join(c if c.isprintable() else " " for c in str(text))
                try:
                    screen.addnstr(row, 0, text, width - 1, attr)
                except curses.error:
                    pass

        while True:
            queue = load_queue()
            recovery = audio_restart_status()
            recovering = recovery.get("state") == "restarting"
            tracks = queue.get("tracks") or []
            np = now_playing(player, queue)
            job = queue.get("job_dir") or queue.get("pid")
            if job != session:
                session = job
                selected = max(0, (np or {}).get("index", 1) - 1)
                offset = 0
            selected = max(0, min(selected, max(0, len(tracks) - 1)))
            height, width = screen.getmaxyx()
            screen.erase()
            put(0, f"music {VERSION}  ·  {queue.get('name', 'Active playlist')}", curses.A_BOLD)
            actual = queue.get("playing_index") if queue.get("job_dir") else ((np or {}).get("index", 0) - 1)
            if recovering:
                put(1, recovery["message"], curses.A_BOLD)
                put(2, "You can keep browsing the playlist while audio recovers.")
            elif queue.get("worker_error"):
                put(1, "Download worker stopped: " + queue["worker_error"])
            elif queue.get("playback_error"):
                put(1, "Playback stopped: " + queue["playback_error"])
                put(2, "Press r to restart audio and retry playback.")
            elif not np:
                put(1, "Playback stopped. q returns to the prompt.")
            elif np.get("finished"):
                put(1, "Playlist finished. Select a track and press Enter to replay.")
            else:
                playing = tracks[actual].get("title", "?") if actual is not None and 0 <= actual < len(tracks) else "Waiting for first download"
                state = "Preparing" if actual is None else ("Paused" if np["paused"] else "Playing")
                put(1, f"{state}: {playing}", curses.A_BOLD)
                if queue.get("waiting_index") is not None:
                    put(2, f"Waiting for download: {tracks[queue['waiting_index']].get('title', '?')}")
                else:
                    put(2, f"{progress_bar(np['time'], np['duration'], max(5, min(45, width - 25)))}  "
                        f"{fmt_time(np['time'])} / {fmt_time(np['duration'])}  vol {int(np['volume'] or 0)}%")
            ready = sum(t.get("download_status") == "ready" for t in tracks)
            failed = sum(t.get("download_status") == "failed" for t in tracks)
            put(3, f"{len(tracks)} tracks  ·  {ready} downloaded  ·  {failed} failed" if queue.get("job_dir") else f"{len(tracks)} tracks")
            rows = max(1, height - 8)
            offset, end = playlist_window(len(tracks), selected, offset, rows)
            put(4, f"Active playlist  ({offset + 1}–{end} of {len(tracks)})", curses.A_BOLD)
            for row, index in enumerate(range(offset, end), 5):
                track = tracks[index]
                marker = ">" if index == selected else " "
                playing_marker = "▶" if index == actual else " "
                state = track.get("download_status", "stream")
                label = f"{marker}{playing_marker} {index + 1:>3}. [{state}] {track.get('title', track['url'])}"
                put(row, label, curses.A_REVERSE if index == selected else 0)
            selected_error = tracks[selected].get("error", "") if tracks else ""
            put(height - 3, notice or recovery.get("message") or selected_error)
            put(height - 2, NOW_KEYS)
            put(height - 1, "PgUp/PgDn scroll · Home/End first/last · g current song · 1–9 jump · q keeps music playing")
            if height < 9:
                screen.erase()
                put(0, "Resize the terminal to at least 9 rows to see the playlist.")
                put(1, "q returns to the prompt; playback and downloads continue.")
            screen.refresh()
            try:
                key = screen.get_wch()
            except curses.error:
                continue
            # Accept both normal (CSI) and application (SS3) cursor-key modes.
            if key == "\x1b":
                tail = ""
                screen.timeout(40)
                try:
                    for _ in range(5):
                        char = screen.get_wch()
                        if not isinstance(char, str):
                            break
                        tail += char
                        if char.isalpha() or char == "~":
                            break
                except curses.error:
                    pass
                finally:
                    screen.timeout(200)
                key = {"[A": curses.KEY_UP, "OA": curses.KEY_UP,
                       "[B": curses.KEY_DOWN, "OB": curses.KEY_DOWN,
                       "[C": curses.KEY_RIGHT, "OC": curses.KEY_RIGHT,
                       "[D": curses.KEY_LEFT, "OD": curses.KEY_LEFT,
                       "[H": curses.KEY_HOME, "OH": curses.KEY_HOME,
                       "[F": curses.KEY_END, "OF": curses.KEY_END,
                       "[5~": curses.KEY_PPAGE, "[6~": curses.KEY_NPAGE}.get(tail, key)
            notice = ""
            if key in ("q", "Q", "\x1b", "\x04"):
                break
            if recovering and (key in (" ", "n", "N", "p", "P", "\n", "\r", curses.KEY_ENTER,
                                       curses.KEY_LEFT, curses.KEY_RIGHT, "+", "-", "=", "a", "A")
                               or (isinstance(key, str) and key in "123456789")):
                notice = "Audio restart in progress; playback controls will return shortly."
                continue
            if key in (curses.KEY_UP, "k"):
                selected = max(0, selected - 1)
            elif key in (curses.KEY_DOWN, "j"):
                selected = min(len(tracks) - 1, selected + 1)
            elif key == curses.KEY_PPAGE:
                selected = max(0, selected - rows)
            elif key == curses.KEY_NPAGE:
                selected = min(len(tracks) - 1, selected + rows)
            elif key == curses.KEY_HOME:
                selected = 0
            elif key == curses.KEY_END:
                selected = max(0, len(tracks) - 1)
            elif key in ("g", "G") and actual is not None:
                selected = max(0, actual)
            elif key in ("\n", "\r", curses.KEY_ENTER):
                if tracks and tracks[selected].get("download_status") == "failed":
                    notice = "Track unavailable: " + tracks[selected].get("error", "download failed")
                else:
                    select_track(player, selected)
            elif key == " ":
                player.command("cycle", "pause")
            elif key in ("n", "N"):
                step_track(player, 1)
            elif key in ("p", "P"):
                step_track(player, -1)
            elif key in (curses.KEY_RIGHT, "l", "."):
                player.command("seek", 10)
            elif key in (curses.KEY_LEFT, "h", ","):
                player.command("seek", -10)
            elif key in ("+", "="):
                player.command("add", "volume", 5)
            elif key == "-":
                player.command("add", "volume", -5)
            elif isinstance(key, str) and key in "123456789":
                select_track(player, int(key) - 1)
            elif key in ("r", "R") and app:
                snapshot = {"job_dir": queue.get("job_dir"), "position": player.get("time-pos"),
                            "paused": player.get("pause"), "volume": player.get("volume")}
                try:
                    notice = start_audio_restart(app.library_path,
                                                 player_only=not os.environ.get("WSL_DISTRO_NAME"),
                                                 snapshot=snapshot)
                except MusicError as exc:
                    notice = str(exc)
            elif key in ("a", "A") and app:
                curses.def_prog_mode()
                curses.endwin()
                try:
                    url = input("Paste playlist/song URL (blank cancels): ").strip()
                    if url:
                        app.open_url(url, show_view=False)
                except (MusicError, yaml.YAMLError, OSError) as exc:
                    notice = str(exc)
                except (EOFError, KeyboardInterrupt):
                    pass
                finally:
                    curses.reset_prog_mode()
                    screen.clear()
                    screen.refresh()
    try:
        curses.wrapper(view)
    except KeyboardInterrupt:
        pass
    finally:
        monitor.close()


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

class App:
    def __init__(self, library_path):
        self.library_path = Path(library_path)
        self.player = Player()
        self.interactive = False

    @property
    def lib(self):
        # Re-read every time so hand edits to the YAML are picked up immediately.
        return Library(self.library_path)

    def music_dir(self, override=None):
        """Where downloads are kept for good: --output, $MUSIC_DOWNLOAD_DIR, settings.music_dir, a default."""
        folder = (override or os.environ.get("MUSIC_DOWNLOAD_DIR") or self.lib.settings.get("music_dir")
                  or (DEFAULT_MUSIC_DIR if Path("/mnt/c").is_dir() else Path.home() / "Music"))
        folder = Path(folder).expanduser()
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise MusicError(f"Cannot use music folder {folder}: {exc.strerror}") from None
        return folder

    def tracks_of(self, item):
        return track_files(item, self.music_dir())

    # --- selection helpers -------------------------------------------------

    def pick(self, query, kind=None):
        lib = self.lib
        if query:
            matches = lib.find(query)
        else:
            matches = lib.items()
        if kind:
            matches = [m for m in matches if m[0] == kind]
        if not matches:
            raise MusicError(f"Nothing in the library matches {query!r}" if query
                             else "Your library is empty. Add something with `add URL`.")
        if len(matches) == 1:
            return matches[0]
        if not sys.stdin.isatty():
            names = "\n  ".join(item_label(m) for m in matches[:10])
            raise MusicError(f"{len(matches)} matches for {query!r}; be more specific:\n  {names}")
        return self.choose(matches, query)

    def choose(self, matches, query=None):
        labels = [f"{item_label(m)}{'' if m[0] == 'album' else '  [song]'}" for m in matches]
        if shutil.which("fzf"):
            proc = subprocess.run(
                ["fzf", "--height=40%", "--reverse", "--prompt=music> ", "--query", query or ""],
                input="\n".join(labels), stdout=subprocess.PIPE, text=True)
            choice = proc.stdout.strip()
            if not choice:
                raise MusicError("Cancelled")
            return matches[labels.index(choice)]
        for i, label in enumerate(labels, 1):
            print(f"{i:>3}. {label}")
        try:
            answer = input("Pick a number: ").strip()
        except EOFError:
            answer = ""
        if not answer.isdigit() or not 1 <= int(answer) <= len(matches):
            raise MusicError("Cancelled")
        return matches[int(answer) - 1]

    def play_tracks(self, name, artist, tracks, start=0, shuffled=False, restore=None):
        if not tracks:
            raise MusicError("This playlist has no tracks")
        if not shutil.which("yt-dlp"):
            raise MusicError("yt-dlp not found on PATH")
        settings = self.lib.settings
        extra = settings.get("mpv_args") or []
        if isinstance(extra, str):
            extra = shlex.split(extra)
        pid = self.player.start([], extra_args=extra, volume=settings.get("volume"))
        job_dir = STATE_DIR / "jobs" / uuid.uuid4().hex
        job_dir.mkdir(parents=True)
        prepared = []
        for original in tracks:
            track = {k: v for k, v in original.items() if k not in ("download_status", "local_path", "error")}
            path = find_audio(track)
            track["download_status"] = "ready" if path else "queued"
            if path:
                track["local_path"] = str(path)
            prepared.append(track)
        save_queue({"name": name, "artist": artist, "tracks": prepared,
                    "shuffled": shuffled, "pid": pid, "job_dir": str(job_dir),
                    "playing_index": None, "waiting_index": start, "last_index": start,
                    "finished": False, "restore": restore, "resume_state": restore or {}})
        (job_dir / "running").touch()
        with open(job_dir / "worker.log", "w") as log:
            worker = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "_download", str(job_dir)],
                                      stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                      start_new_session=True)
        # The worker publishes its pid itself; wait until it has claimed the job.
        for _ in range(60):
            if load_queue().get("worker_pid") == worker.pid:
                break
            if worker.poll() is not None:
                raise MusicError(f"Download worker failed to start; see {job_dir / 'worker.log'}")
            time.sleep(0.05)
        else:
            worker.terminate()
            worker.wait(timeout=5)
            raise MusicError("Download worker did not start in time")
        track = tracks[start]
        mode = " (shuffled)" if shuffled else ""
        print(f"{green('▶')} {bold(name)}{mode}" + (f" {dim('— ' + artist)}" if artist else ""))
        print(f"  {start + 1}/{len(tracks)}  {track.get('title', '')}")
        if not self.interactive:
            print(dim("  `music now` for the live view, `music` for the interactive shell"))
        else:
            live_view(self.player, self)

    def library_item(self, url, enrich=False):
        """The library entry for a URL, saving it first if it is new.

        A pasted watch+list URL always means its entire playlist.
        """
        if urlparse(url).scheme not in ("http", "https"):
            raise MusicError("Paste an http:// or https:// playlist/song URL")
        kind, list_id = classify_url(url)
        if list_id and kind == "ambiguous":
            url = f"https://www.youtube.com/playlist?list={list_id}"
            kind = "album"
        lib = self.lib
        item = next((it for it in lib.items() if it[1].get("url") == url), None)
        if item is None:
            print(dim("Loading playlist metadata ..."))
            item = fetch_url_item(url, kind, enrich=enrich)
            meta = item[1]
            existing = next((it for it in lib.items() if it[1].get("url") == meta.get("url")), None)
            if existing:
                item = existing
            else:
                lib.data["albums" if item[0] == "album" else "songs"].append(meta)
                lib.save()
        return item

    def open_url(self, url, show_view=True):
        item = self.library_item(url)
        was_interactive = self.interactive
        try:
            if not show_view:
                self.interactive = False
            self.play_tracks(item_name(item), item[1].get("artist"), self.tracks_of(item))
        finally:
            self.interactive = was_interactive

    def cmd_open(self, a):
        self.open_url(a.url)

    def cmd_download(self, a):
        if not shutil.which("yt-dlp"):
            raise MusicError("yt-dlp not found on PATH")
        if urlparse(a.query or "").scheme in ("http", "https"):
            # Saved like `open`, so later plays find these files under the same names.
            item = self.library_item(a.query, enrich=True)
        else:
            item = self.pick(a.query)
        tracks = track_files(item, self.music_dir(a.output))
        folder = Path(tracks[0]["file"]).parent
        artist = item[1].get("artist")
        print(f"{bold(item_name(item))}{dim(' — ' + artist) if artist else ''} → {folder}")
        failed = 0
        width = len(str(len(tracks)))
        for n, track in enumerate(tracks, 1):
            print(f"  {n:>{width}}/{len(tracks)} {track.get('title') or track['url']}  ", end="", flush=True)
            path = find_audio(track)
            if path and path.is_relative_to(folder):
                print(dim("already there"))
                continue
            if path:
                # Downloaded by an older version into the hashed cache; keep that copy.
                dest = Path(track["file"] + path.suffix)
                partial = dest.with_name(dest.name + ".part")
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, partial)
                partial.replace(dest)
                print(green("ok") + dim(" (from cache)"))
                continue
            proc = subprocess.run(download_cmd(track), stdin=subprocess.DEVNULL, capture_output=True, text=True)
            if proc.returncode == 0 and find_audio(track):
                print(green("ok"))
            else:
                failed += 1
                errors = [line for line in proc.stderr.splitlines() if "ERROR" in line]
                print(yellow("failed") + dim(": " + (errors[-1] if errors else "download failed")))
        if failed:
            raise MusicError(f"{failed} of {len(tracks)} tracks failed; run the same command again to retry")
        print(green("Done.") + dim(f" {len(tracks)} tracks in {folder}"))

    def cmd_restart_audio(self, a):
        print(start_audio_restart(self.library_path,
                                  player_only=a.player_only or not os.environ.get("WSL_DISTRO_NAME")))
        print(dim("Use `now` to see progress, or `status` for the result."))

    # --- commands ------------------------------------------------------------

    def cmd_list(self, a):
        items = self.lib.items()
        if not items:
            print("Library is empty. Add an album with: add <youtube url>")
            return
        words = (a.query or "").lower().split()
        shown_kind = None
        for n, item in enumerate(items, 1):
            if words and not all(w in item_label(item).lower() for w in words):
                continue
            if item[0] != shown_kind:
                shown_kind = item[0]
                print(bold("Albums" if shown_kind == "album" else "Songs"))
            print(describe_item(n, item))

    def cmd_show(self, a):
        if not a.query and self.player.running():
            q = load_queue()
            tracks, name, artist = q.get("tracks") or [], q.get("name"), q.get("artist")
        else:
            item = self.pick(a.query)
            tracks, name, artist = item_tracks(item), item_name(item), item[1].get("artist")
        np = now_playing(self.player)
        current = np["index"] if np and np["name"] == name else None
        if not a.query and self.player.running() and q.get("job_dir"):
            current = q["playing_index"] + 1 if q.get("playing_index") is not None else None
        print(bold(name) + (f" {dim('— ' + artist)}" if artist else ""))
        for i, t in enumerate(tracks, 1):
            mark = green("▶") if i == current else " "
            dur = dim(fmt_time(t["duration"])) if t.get("duration") else ""
            print(f" {mark}{i:>3}. {t.get('title', t['url'])}  {dur}")

    def cmd_add(self, a):
        lib = self.lib
        existing = lib.contains_url(a.url)
        if existing:
            print(f"Already in your library: {existing}")
            return
        force = "album" if a.album else "song" if a.song else None
        kind, meta = resolve_add(a.url, force)
        if a.title:
            meta["title"] = a.title
        if a.artist:
            meta["artist"] = a.artist
        existing = lib.contains_url(meta.get("url"))
        if existing:
            print(f"Already in your library: {existing}")
            return
        if kind == "album":
            year = f" ({meta['year']})" if meta.get("year") else ""
            print(f"\n{bold('Album')}: {meta['title']}{year} — {meta['artist']}")
            for i, t in enumerate(meta["tracks"], 1):
                print(f"  {i:>2}. {t['title']}  {dim(fmt_time(t.get('duration')))}")
        else:
            print(f"\n{bold('Song')}: {meta['title']} — {meta.get('artist', '?')}"
                  f"  {dim(fmt_time(meta.get('duration')))}")
            if meta.get("album"):
                print(dim(f"  (from the album {meta['album']!r}; paste the album link to add all of it)"))
        if not a.yes and not confirm("\nSave to library?"):
            print("Not saved.")
            return
        lib.data["albums" if kind == "album" else "songs"].append(meta)
        lib.save()
        print(green("Saved.") + dim(f" ({lib.path})"))
        if a.play:
            self.play_tracks(meta["title"], meta.get("artist"), self.tracks_of((kind, meta)))

    def cmd_play(self, a):
        if a.query and urlparse(a.query).scheme in ("http", "https"):
            self.open_url(a.query)
            return
        if not a.query and a.track is None and self.player.running():
            if self.player.get("pause"):
                self.player.set("pause", False)
                print("Resumed.")
            else:
                self.cmd_status(a)
            return
        item = self.pick(a.query)
        tracks = self.tracks_of(item)
        start = 0
        if a.track is not None:
            if not 1 <= a.track <= len(tracks):
                raise MusicError(f"{item_name(item)} has {len(tracks)} tracks")
            start = a.track - 1
        self.play_tracks(item_name(item), item[1].get("artist"), tracks, start)

    def cmd_random(self, a):
        lib = self.lib
        if a.songs:
            pool = [dict(t, artist=obj.get("artist")) for kind, obj in lib.items()
                    for t in self.tracks_of((kind, obj))]
            if not pool:
                raise MusicError("Your library is empty")
            random.shuffle(pool)
            self.play_tracks("Random songs", None, pool, shuffled=True)
            return
        albums = [it for it in lib.items() if it[0] == "album"]
        if not albums:
            raise MusicError("No albums in your library yet")
        current = load_queue().get("name") if self.player.running() else None
        choices = [it for it in albums if item_name(it) != current] or albums
        item = random.choice(choices)
        self.play_tracks(item_name(item), item[1].get("artist"), self.tracks_of(item))

    def cmd_shuffle(self, a):
        if a.query:
            item = self.pick(a.query)
            name, artist, tracks = item_name(item), item[1].get("artist"), self.tracks_of(item)
        else:
            q = load_queue()
            if not self.player.running() or not q.get("tracks"):
                raise MusicError("Nothing playing. Use `shuffle ALBUM`, or `random --songs` for the whole library")
            name, artist, tracks = q["name"], q.get("artist"), list(q["tracks"])
        random.shuffle(tracks)
        self.play_tracks(name, artist, tracks, shuffled=True)

    def require_playing(self):
        if not self.player.running():
            raise MusicError("Nothing is playing. Start something with `play`.")

    def cmd_pause(self, a):
        self.require_playing()
        self.player.command("cycle", "pause")
        print("Paused." if self.player.get("pause") else "Playing.")

    def cmd_next(self, a):
        self.require_playing()
        np = now_playing(self.player)
        if np and np["index"] >= np["count"]:
            raise MusicError("Already on the last track")
        step_track(self.player, 1)
        self._print_track()

    def cmd_prev(self, a):
        self.require_playing()
        if (self.player.get("time-pos") or 0) > 5:
            self.player.command("seek", 0, "absolute")  # like a CD player: restart first
        else:
            step_track(self.player, -1)
        self._print_track()

    def cmd_track(self, a):
        self.require_playing()
        queue = load_queue()
        count = len(queue["tracks"]) if queue.get("job_dir") else (self.player.get("playlist-count") or 0)
        if not 1 <= a.number <= count:
            raise MusicError(f"Pick a track between 1 and {count}")
        select_track(self.player, a.number - 1)
        self._print_track()

    def _print_track(self):
        time.sleep(0.15)
        np = now_playing(self.player)
        if np:
            print(f"{green('▶')} {np['index']}/{np['count']}  {np['title']}")

    def cmd_seek(self, a):
        self.require_playing()
        pos = a.position
        if pos[0] in "+-":
            self.player.command("seek", (1 if pos[0] == "+" else -1) * parse_time(pos[1:]))
        else:
            self.player.command("seek", parse_time(pos), "absolute")
        time.sleep(0.1)
        print(f"At {fmt_time(self.player.get('time-pos'))}")

    def cmd_volume(self, a):
        self.require_playing()
        if a.level:
            if not re.fullmatch(r"[+-]?\d+", a.level):
                raise MusicError("Volume is a number: 60, +10 or -10")
            if a.level[0] in "+-":
                self.player.command("add", "volume", int(a.level))
            else:
                self.player.set("volume", int(a.level))
        print(f"Volume {int(self.player.get('volume') or 0)}%")

    def cmd_stop(self, a):
        if self.player.running() or load_queue().get("worker_pid"):
            self.player.stop()
            print("Stopped.")
        else:
            print("Nothing is playing.")

    def cmd_status(self, a):
        recovery = audio_restart_status()
        if recovery:
            print(recovery["message"])
        np = now_playing(self.player)
        if not np:
            print("Nothing is playing.")
            return
        for line in status_lines(np, shutil.get_terminal_size().columns):
            print(line)

    def cmd_now(self, a):
        live_view(self.player, self)

    def cmd_remove(self, a):
        item = self.pick(a.query)
        if not a.yes and not confirm(f"Remove {item_label(item)!r} from the library?", default=False):
            print("Kept.")
            return
        lib = self.lib
        for candidate in lib.items():
            if candidate[1] == item[1]:
                lib.remove(candidate)
                break
        lib.save()
        print(f"Removed {item_label(item)}.")

    def cmd_edit(self, a):
        lib = self.lib
        if not lib.path.exists():
            lib.save()
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
        subprocess.run([*shlex.split(editor), str(lib.path)])
        try:
            self.lib
        except (MusicError, yaml.YAMLError) as exc:
            print(yellow(f"Warning: the library no longer parses:\n{exc}"))

    def cmd_import(self, a):
        """Convert old `label | url` playlist .txt files into albums."""
        lib = self.lib
        added = 0
        for path in a.files:
            path = Path(path)
            tracks = []
            for line in path.read_text(encoding="utf-8").splitlines():
                if "|" not in line:
                    continue
                label, url = (part.strip() for part in line.split("|", 1))
                tracks.append({"title": label.replace("_", " ").strip().title(), "url": url})
            if not tracks:
                print(f"{path}: no `label | url` lines, skipped")
                continue
            title = path.stem.replace("_", " ").title()
            if any(al["title"] == title for al in lib.data["albums"]):
                print(f"{path}: an album called {title!r} already exists, skipped")
                continue
            lib.data["albums"].append({"title": title, "artist": a.artist or "Unknown artist",
                                       "tracks": tracks})
            print(f"{path}: imported {len(tracks)} tracks as {title!r}")
            added += 1
        if added:
            lib.save()
            print(dim("Tip: `music add <album url>` gives proper titles, artist and year."))

    def cmd_doctor(self, a):
        ok = True
        for tool, why in (("mpv", "playback"), ("yt-dlp", "YouTube streams + metadata"),
                          ("ffmpeg", "optional: tags in downloaded files"), ("fzf", "optional: fuzzy picker")):
            path = shutil.which(tool)
            if path:
                version = ""
                if tool == "yt-dlp":
                    version = subprocess.run([tool, "--version"], capture_output=True, text=True).stdout.strip()
                print(f"{green('ok')}   {tool:<7} {path} {dim(version)}")
            else:
                ok = ok and tool in ("ffmpeg", "fzf")
                print(f"{yellow('miss')} {tool:<7} ({why})")
        try:
            lib = self.lib
            counts = f"({len(lib.data['albums'])} albums, {len(lib.data['songs'])} songs)"
            print(f"{green('ok')}   library {lib.path} {dim(counts)}")
        except (MusicError, yaml.YAMLError) as exc:
            ok = False
            print(f"{yellow('bad')}  library {self.library_path}: {exc}")
        print(f"     socket  {SOCKET_PATH}")
        print(f"     mpv log {MPV_LOG}")
        try:
            print(f"{green('ok')}   music   {self.music_dir()}")
        except MusicError as exc:
            ok = False
            print(f"{yellow('bad')}  music   {exc}")
        print(f"     old audio cache {CACHE_DIR}")
        if not ok:
            raise MusicError("Some required pieces are missing")


# --------------------------------------------------------------------------
# Argument parsing (shared by one-shot mode and the interactive shell)
# --------------------------------------------------------------------------

EPILOG = """\
examples:
  music                                   interactive shell
  music add 'https://www.youtube.com/playlist?list=OLAK5uy_...'
  music play gettin off stage             play an album front to back
  music play 3 -t 4                       album #3 from `list`, starting at track 4
  music -dl 'https://www.youtube.com/playlist?list=...'   download only (also: music dl gettin)
  music random                            a random album, in order
  music random --songs                    shuffle every song in the library
  music now                               live view with single-key controls
"""


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise MusicError(f"{message}\n(try `{self.prog} --help`)")


def build_parser():
    p = _Parser(prog="music", description="Play albums from YouTube in your terminal. "
                "Run with no command for an interactive shell.",
                epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--library", metavar="FILE", help="library YAML (default: $MUSIC_LIBRARY "
                   "or library.yaml next to this script)")
    p.add_argument("--version", action="version", version=f"music {VERSION}")
    sub = p.add_subparsers(dest="command", metavar="COMMAND", title="commands", parser_class=_Parser)

    def cmd(name, help_text, aliases=()):
        sp = sub.add_parser(name, help=help_text, description=help_text, aliases=list(aliases))
        sp.set_defaults(func=name)
        return sp

    s = cmd("add", "add a YouTube album/playlist or song by URL (type is detected)")
    s.add_argument("url")
    kind = s.add_mutually_exclusive_group()
    kind.add_argument("--album", action="store_true", help="treat the URL as an album/playlist")
    kind.add_argument("--song", action="store_true", help="treat the URL as a single song")
    s.add_argument("--title", help="override the detected title")
    s.add_argument("--artist", help="override the detected artist")
    s.add_argument("-y", "--yes", action="store_true", help="save without asking")
    s.add_argument("--play", action="store_true", help="start playing it right away")

    s = cmd("open", "load a URL, save its playlist, download in the background and play")
    s.add_argument("url")

    s = cmd("download", "download a playlist URL or library album to the music folder, without playing", ["dl"])
    s.add_argument("query", nargs="*", help="playlist/song URL, or album/song number or name words")
    s.add_argument("-o", "--output", metavar="DIR", help="music folder (default: $MUSIC_DOWNLOAD_DIR, "
                   f"settings.music_dir, or {DEFAULT_MUSIC_DIR})")

    s = cmd("list", "list albums and songs (numbered)", ["ls"])
    s.add_argument("query", nargs="*", help="filter words")

    s = cmd("show", "show an album's track list (default: what's playing)", ["tracks"])
    s.add_argument("query", nargs="*", help="album number or name")

    s = cmd("play", "play an album front to back (no args: resume, or pick with fzf)")
    s.add_argument("query", nargs="*", help="album/song number or name words")
    s.add_argument("-t", "--track", type=int, help="start at this track number")

    s = cmd("random", "play a random album, in order", ["rand"])
    s.add_argument("--songs", action="store_true", help="shuffle all songs in the library instead")

    s = cmd("shuffle", "shuffle an album's tracks (default: what's playing)")
    s.add_argument("query", nargs="*", help="album number or name")

    cmd("pause", "toggle pause / resume", ["toggle"])
    cmd("next", "next track", ["n"])
    cmd("prev", "restart track, or previous if near the start", ["previous"])
    s = cmd("track", "jump to track N of what's playing", ["jump"])
    s.add_argument("number", type=int)
    s = cmd("seek", "seek: +30, -10, 1:30 (absolute)")
    s.add_argument("position")
    s = cmd("volume", "show or set volume: 70, +10, -10", ["vol"])
    s.add_argument("level", nargs="?")
    cmd("stop", "stop playback")
    s = cmd("restart-audio", "restart audio in the background and restore the current song", ["audio-reset"])
    s.add_argument("--player-only", action="store_true", help="restart mpv without restarting WSLg PulseAudio")
    cmd("status", "show what's playing", ["st"])
    cmd("now", "live now-playing view with key controls", ["np"])

    s = cmd("remove", "remove an album or song from the library", ["rm"])
    s.add_argument("query", nargs="+")
    s.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    cmd("edit", "open the library YAML in $EDITOR")
    s = cmd("import-txt", "import old `label | url` playlist .txt files as albums")
    s.add_argument("files", nargs="+")
    s.add_argument("--artist", help="artist to record for the imported albums")
    cmd("doctor", "check dependencies and paths")
    return p


def dispatch(app, args):
    for key in ("query",):
        if isinstance(getattr(args, key, None), list):
            setattr(args, key, " ".join(getattr(args, key)))
    handler = getattr(app, "cmd_" + args.func.replace("-txt", "").replace("-", "_"))
    handler(args)


# --------------------------------------------------------------------------
# Interactive shell
# --------------------------------------------------------------------------

SHELL_HELP = """\
{title}
  restart-audio         restart audio and restore playback (also: r in `now`)
  paste a URL           save playlist, download in background, play and browse
  open URL             same workflow (watch+list loads the entire playlist)
  download URL|ALBUM    download to the music folder without playing (also: dl)
  play [ALBUM] [-t N]   play an album (no args: fuzzy pick / resume)
  random [--songs]      random album in order, or shuffle every song
  shuffle [ALBUM]       shuffle an album (default: current one)
  pause | next | prev   transport (also: n, toggle, previous)
  track N               jump to track N         seek +30 | -10 | 1:30
  volume [N|+N|-N]      show/set volume         stop
  now                   playlist view (↑/↓ browse, Enter play, q back)
  status | show [ALBUM] what's playing / track list
  list [WORDS]          library                 add URL [--album|--song]
  remove ALBUM | edit | import-txt FILE | doctor
  help [COMMAND]        details for a command    quit / Ctrl-D (music keeps playing)
ALBUM is a number from `list` or any words from the artist/title. Tab completes.
"""

COMMANDS_WITH_ITEMS = {"download", "dl", "play", "show", "tracks", "shuffle", "remove", "rm", "list", "ls"}


def interactive(app, parser):
    app.interactive = True
    try:
        import readline
    except ImportError:
        readline = None

    command_names = sorted({n for a in parser._subparsers._group_actions for n in a.choices}
                           | {"help", "quit", "exit"})

    if readline:
        history = STATE_DIR / "history"
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        try:
            readline.read_history_file(history)
        except OSError:
            pass
        readline.set_history_length(1000)

        def complete(text, state):
            line = readline.get_line_buffer()
            words = line.split()
            if len(words) == 0 or (len(words) == 1 and not line.endswith(" ")):
                options = [c + " " for c in command_names if c.startswith(text)]
            elif words[0] in COMMANDS_WITH_ITEMS or words[0] == "help":
                if words[0] == "help":
                    options = [c for c in command_names if c.startswith(text)]
                else:
                    # Complete whole album names even though they contain spaces:
                    # match on everything typed after the command, return the
                    # remainder starting at the current word.
                    typed = line.lstrip().split(None, 1)[1] if len(words) > 1 else ""
                    prefix_len = len(typed) - len(text)
                    try:
                        names = sorted({item_name(i) for i in app.lib.items()})
                    except Exception:
                        names = []
                    options = [n[prefix_len:] for n in names if n.lower().startswith(typed.lower())]
            else:
                options = []
            return options[state] if state < len(options) else None

        readline.set_completer(complete)
        readline.set_completer_delims(" \t")
        readline.parse_and_bind("tab: complete")

    print(bold("music") + dim(f" {VERSION} — paste a playlist URL or type `help`; Ctrl-D leaves (music keeps playing)"))
    try:
        app.cmd_status(None)
    except MusicError:
        pass

    while True:
        np = now_playing(app.player)
        if np:
            icon = "⏸" if np["paused"] else "♪"
            title = np["title"] if len(np["title"]) <= 30 else np["title"][:29] + "…"
            prompt = f"{icon} {np['index']}/{np['count']} {title} › "
        else:
            prompt = "music › "
        # \001..\002 tell readline which bytes are invisible (colour codes).
        prompt = f"\001\033[36m\002{prompt}\001\033[0m\002" if COLOR else prompt
        try:
            line = input(prompt).strip()
        except EOFError:
            print()
            break
        except KeyboardInterrupt:
            print()
            continue
        if not line:
            continue
        if urlparse(line).scheme in ("http", "https"):
            try:
                app.open_url(line)
            except (MusicError, yaml.YAMLError, OSError) as exc:
                print(f"{yellow('!')} {exc}")
            except KeyboardInterrupt:
                print()
            continue
        try:
            argv = shlex.split(line)
        except ValueError:
            argv = line.split()  # e.g. an unquoted apostrophe in "Gettin' Off Stage"
        if argv[0] in ("quit", "exit", "q"):
            break
        if argv[0] not in command_names and argv[0] not in ("?", "h"):
            print(f"{yellow('!')} Unknown command {argv[0]!r}. Type `help` for the list.")
            continue
        if argv[0] in ("help", "?", "h"):
            if len(argv) > 1:
                argv = [argv[1], "--help"]
            else:
                print(SHELL_HELP.format(title=bold("Commands")))
                continue
        try:
            args = parser.parse_args(argv)
            if args.command is None:
                continue
            dispatch(app, args)
        except SystemExit:
            pass  # --help / --version inside the shell
        except MusicError as exc:
            print(f"{yellow('!')} {exc}")
        except yaml.YAMLError as exc:
            print(f"{yellow('!')} library YAML error: {exc}")
        except KeyboardInterrupt:
            print()

    if readline:
        try:
            readline.write_history_file(STATE_DIR / "history")
        except OSError:
            pass


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    if len(argv) == 2 and argv[0] == "_download":
        DownloadSession(argv[1]).run()
        return 0
    if len(argv) == 5 and argv[0] == "_restart-audio":
        try:
            recover_audio(argv[1], bool(int(argv[2])), argv[3])
        finally:
            os.close(int(argv[4]))
        return 0
    # `music -dl URL` is shorthand for `music download URL`.
    argv = ["download" if arg == "-dl" else arg for arg in argv]
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        app = App(Path(args.library).expanduser() if args.library else default_library_path())
        if args.command is None:
            if not sys.stdin.isatty():
                parser.print_help()
                return 0
            interactive(app, parser)
        else:
            dispatch(app, args)
    except MusicError as exc:
        print(f"music: {exc}", file=sys.stderr)
        return 1
    except yaml.YAMLError as exc:
        print(f"music: library YAML error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())

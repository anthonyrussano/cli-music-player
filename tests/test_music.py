"""End-to-end checks using real mpv and a deterministic, offline yt-dlp fixture."""

import fcntl
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import pty
import select
import signal
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader("music", str(ROOT / "music"))
spec = importlib.util.spec_from_loader("music", loader)
music = importlib.util.module_from_spec(spec)
loader.exec_module(music)

FAKE_YTDLP = '''#!/usr/bin/env python3
import json, os, sys, time, wave
from pathlib import Path
args = sys.argv[1:]
url = args[-1]
with open(os.environ["TEST_CALLS"], "a") as fh:
    fh.write(json.dumps({"args": args, "pid": os.getpid()}) + "\\n")
if "-J" in args:
    entries = [{"id": str(i), "title": "Song " + str(i),
                "url": "https://example.test/" + str(i), "duration": 4}
               for i in range(1, int(os.environ.get("TEST_COUNT", "3")) + 1)]
    print(json.dumps({"_type": "playlist", "title": "Fixture playlist",
                      "webpage_url": url, "entries": entries, "uploader": "Fixture artist"}))
    sys.exit(0)
index = url.rsplit("/", 1)[-1]
time.sleep(float(os.environ.get("TEST_DELAY_" + index, "0.1")))
if index == os.environ.get("TEST_FAIL"):
    print("ERROR: Fixture unavailable", file=sys.stderr)
    sys.exit(1)
path = Path(args[args.index("-o") + 1].replace("%(ext)s", "wav"))
with wave.open(str(path), "wb") as fh:
    fh.setnchannels(1); fh.setsampwidth(2); fh.setframerate(8000)
    fh.writeframes(b"\\x00\\x00" * int(8000 * float(os.environ.get("TEST_LENGTH", "4"))))
'''


class SelectionTests(unittest.TestCase):
    def test_scrolling_keeps_every_track_reachable(self):
        offset = 0
        for selected in list(range(100)) + list(reversed(range(100))):
            offset, end = music.playlist_window(100, selected, offset, 12)
            self.assertLessEqual(offset, selected)
            self.assertLess(selected, end)
            self.assertLessEqual(end - offset, 12)

    def test_ipc_deadline_is_bounded_even_with_unrelated_events(self):
        with tempfile.TemporaryDirectory(prefix="ipc-test-") as directory:
            socket_path = str(Path(directory) / "mpv.sock")
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                server.bind(socket_path)
                server.listen()

                def events():
                    connection, _ = server.accept()
                    with connection:
                        connection.recv(4096)
                        try:
                            for _ in range(40):
                                connection.sendall(b'{"event":"tick"}\n')
                                time.sleep(0.02)
                        except OSError:
                            pass

                thread = threading.Thread(target=events, daemon=True)
                thread.start()
                started = time.monotonic()
                self.assertIsNone(music.Player(socket_path).command("get_property", "pid", timeout=0.15))
                self.assertLess(time.monotonic() - started, 0.4)
                thread.join(timeout=1)


@unittest.skipUnless(shutil.which("mpv"), "mpv required for playback integration checks")
class PlaybackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="music-test-")
        self.base = Path(self.tmp.name)
        bin_dir = self.base / "bin"
        bin_dir.mkdir()
        fake = bin_dir / "yt-dlp"
        fake.write_text(FAKE_YTDLP)
        fake.chmod(0o755)
        self.library = self.base / "library.yaml"
        self.library.write_text("settings:\n  mpv_args: [--ao=null, --audio-display=no]\n")
        self.env = dict(os.environ, XDG_STATE_HOME=str(self.base / "state"),
                        XDG_CACHE_HOME=str(self.base / "cache"),
                        XDG_RUNTIME_DIR=str(self.base / "runtime"),
                        MUSIC_DOWNLOAD_DIR=str(self.base / "music"),
                        MUSIC_LIBRARY=str(self.library), TERM="xterm-256color",
                        TEST_CALLS=str(self.base / "calls.jsonl"),
                        PATH=str(bin_dir) + os.pathsep + os.environ["PATH"])
        self.queue_path = self.base / "state/music-player/queue.json"
        self.player = music.Player(self.base / "runtime/music-player.sock")
        self.shell = None
        self.master = None
        self.terminal_output = b""

    def tearDown(self):
        if self.shell and self.shell.poll() is None:
            self.shell.terminate()
            self.shell.wait(timeout=5)
        deadline = time.monotonic() + 12
        while self.recovery().get("state") == "restarting" and time.monotonic() < deadline:
            time.sleep(0.1)
        self.cli("stop")
        if self.master is not None:
            os.close(self.master)
        self.tmp.cleanup()

    def cli(self, *args):
        return subprocess.run([sys.executable, str(ROOT / "music"), *args], env=self.env,
                              capture_output=True, text=True, timeout=12)

    def queue(self):
        try:
            return json.loads(self.queue_path.read_text())
        except FileNotFoundError:
            return {}

    def drain(self):
        if self.master is not None:
            while select.select([self.master], [], [], 0)[0]:
                try:
                    self.terminal_output += os.read(self.master, 65536)
                except OSError:
                    break

    def wait_for(self, predicate, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.drain()
            queue = self.queue()
            if predicate(queue):
                return queue
            if queue.get("worker_error"):
                self.fail(queue["worker_error"])
            time.sleep(0.05)
        logs = list((self.base / "state/music-player/jobs").glob("*/worker.log"))
        self.fail(f"Timed out. Queue: {self.queue()} Logs: {[p.read_text() for p in logs]} "
                  f"Terminal: {self.terminal_output[-2000:]!r}")

    def open(self):
        result = self.cli("open", "https://www.youtube.com/watch?v=JaTuWOh_0g4&list=PLfixture")
        self.assertEqual(result.returncode, 0, result.stderr)

    def calls(self):
        return [json.loads(line) for line in (self.base / "calls.jsonl").read_text().splitlines()]

    def recovery(self):
        path = self.queue_path.parent / "audio-restart.json"
        return json.loads(path.read_text()) if path.exists() else {}

    def fake_audio_service(self, delay=0, fail=False):
        self.env["WSL_DISTRO_NAME"] = "Fixture"
        self.env["TEST_WSL_CALLS"] = str(self.base / "wsl-calls.jsonl")
        wsl = self.base / "bin/wsl.exe"
        wsl.write_text(f'''#!/usr/bin/env python3
import json, os, sys, time
with open(os.environ["TEST_WSL_CALLS"], "a") as fh:
    fh.write(json.dumps(sys.argv[1:]) + "\\n")
time.sleep({delay})
print("fixture service error" if {fail!r} else "restarted", file=sys.stderr)
sys.exit({int(fail)})
''')
        wsl.chmod(0o755)
        pactl = self.base / "bin/pactl"
        pactl.write_text("#!/bin/sh\necho 'Server Name: fixture'\n")
        pactl.chmod(0o755)

    def start_terminal(self, *args):
        self.master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 100, 0, 0))
        self.shell = subprocess.Popen([sys.executable, str(ROOT / "music"), *args], env=self.env,
                                      stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
        os.close(slave)

    def test_keyboard_stays_responsive_when_mpv_never_replies(self):
        runtime = self.base / "runtime"
        runtime.mkdir()
        self.queue_path.parent.mkdir(parents=True)
        job = self.base / "job"
        job.mkdir()
        self.queue_path.write_text(json.dumps({"name": "Stalled player", "job_dir": str(job),
                                              "tracks": [{"title": f"Song {i}", "url": f"https://example.test/{i}",
                                                          "download_status": "queued"} for i in range(100)]}))
        stop = threading.Event()
        connections = []
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(runtime / "music-player.sock"))
            server.listen()
            server.settimeout(0.05)

            def stall():
                while not stop.is_set():
                    try:
                        connection, _ = server.accept()
                        connections.append(connection)
                    except socket.timeout:
                        pass

            thread = threading.Thread(target=stall, daemon=True)
            thread.start()
            try:
                self.start_terminal("now")
                self.wait_for(lambda _: b"Active playlist" in self.terminal_output, timeout=2)
                # A burst of arrows, followed by async playback controls, must not wait on mpv.
                started = time.monotonic()
                os.write(self.master, b"\x1bOB" * 99 + b" \x1b[C+q")
                self.wait_for(lambda _: self.shell.poll() is not None, timeout=1)
                self.assertLess(time.monotonic() - started, 1)
                self.assertEqual(self.shell.returncode, 0)
                self.assertEqual(len(connections), 1)
            finally:
                stop.set()
                thread.join(timeout=1)
                for connection in connections:
                    connection.close()

    def test_downloads_while_playing_and_advances_in_order(self):
        self.env.update(TEST_DELAY_2="1.5", TEST_LENGTH="0.8")
        self.open()
        queue = self.wait_for(lambda q: q.get("playing_index") == 0 and q["tracks"][1]["download_status"] == "downloading")
        self.assertEqual(len(queue["tracks"]), 3)
        first = Path(queue["tracks"][0]["local_path"])
        self.assertTrue(first.is_file())
        self.assertEqual(first.parent, self.base / "music/Fixture artist - Fixture playlist")
        self.assertTrue(first.name.startswith("01 - Song 1 ["))
        self.wait_for(lambda q: q.get("waiting_index") == 1 and q.get("playing_index") is None)
        self.wait_for(lambda q: q.get("playing_index") == 1)
        self.wait_for(lambda q: q.get("playing_index") == 2)
        self.wait_for(lambda q: q.get("finished") is True)
        # Replay uses cached files without invoking yt-dlp for audio again.
        self.assertEqual(self.cli("play", "fixture").returncode, 0)
        self.wait_for(lambda q: q.get("playing_index") == 0)
        audio_calls = [c for c in self.calls() if "-J" not in c["args"]]
        self.assertEqual(len(audio_calls), 3)

    def test_download_saves_to_music_folder_and_playback_reuses_it(self):
        url = "https://www.youtube.com/watch?v=JaTuWOh_0g4&list=PLfixture"
        result = self.cli("-dl", url)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.queue_path.exists())
        folder = self.base / "music/Fixture artist - Fixture playlist"
        self.assertEqual(sorted(p.name.split(" [")[0] for p in folder.iterdir()),
                         ["01 - Song 1", "02 - Song 2", "03 - Song 3"])
        # Downloading again by library name, then playing, reuses the files.
        result = self.cli("dl", "fixture")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count("already there"), 3)
        self.open()
        queue = self.wait_for(lambda q: q.get("playing_index") == 0)
        self.assertTrue(all(t["download_status"] == "ready" for t in queue["tracks"]))
        self.assertEqual(len([c for c in self.calls() if "-J" not in c["args"]]), 3)

    def test_restart_restores_paused_song_position_volume_and_cache(self):
        self.env["TEST_LENGTH"] = "30"
        self.open()
        self.wait_for(lambda q: all(t["download_status"] == "ready" for t in q["tracks"]))
        self.assertEqual(self.cli("track", "2").returncode, 0)
        old = self.wait_for(lambda q: q.get("playing_index") == 1)
        self.player.command("seek", 7, "absolute+exact")
        self.player.set("pause", True)
        self.player.set("volume", 37)
        self.wait_for(lambda _: self.player.get("pause") is True and (self.player.get("time-pos") or 0) >= 7)
        result = self.cli("restart-audio", "--player-only")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.wait_for(lambda _: self.recovery().get("state") in ("done", "failed"))
        self.assertEqual(self.recovery()["state"], "done", self.recovery())
        new = self.queue()
        self.assertNotEqual(new["pid"], old["pid"])
        self.assertEqual(new["playing_index"], 1)
        self.assertEqual([t["local_path"] for t in new["tracks"]], [t["local_path"] for t in old["tracks"]])
        self.assertTrue(self.player.get("pause"))
        self.assertAlmostEqual(self.player.get("volume"), 37)
        self.assertGreaterEqual(self.player.get("time-pos"), 7)
        self.assertLess(self.player.get("time-pos"), 8)
        self.assertEqual(len([c for c in self.calls() if "-J" not in c["args"]]), 3)
        self.player.set("pause", False)
        self.wait_for(lambda _: (self.player.get("time-pos") or 0) > 7.5)

    def test_restart_replaces_mpv_that_cannot_answer_ipc(self):
        self.env["TEST_LENGTH"] = "30"
        self.open()
        old = self.wait_for(lambda q: q.get("playing_index") == 0 and q.get("resume_state", {}).get("position", 0) > 0.5)
        os.kill(old["pid"], signal.SIGSTOP)
        self.assertEqual(self.cli("restart-audio", "--player-only").returncode, 0)
        self.wait_for(lambda _: self.recovery().get("state") in ("done", "failed"))
        self.assertEqual(self.recovery()["state"], "done", self.recovery())
        self.assertFalse(music.owns_mpv(old["pid"], self.player.socket_path))
        self.assertNotEqual(self.queue()["pid"], old["pid"])
        self.assertGreater(self.player.get("time-pos"), 0.5)

    def test_r_restarts_service_in_background_and_duplicate_requests_are_ignored(self):
        self.env["TEST_LENGTH"] = "30"
        self.fake_audio_service(delay=2)
        self.open()
        old = self.wait_for(lambda q: q.get("playing_index") == 0)
        self.start_terminal("now")
        self.wait_for(lambda _: b"r restart audio" in self.terminal_output)
        os.write(self.master, b"r")
        self.wait_for(lambda _: self.recovery().get("state") == "restarting")
        recovery_id = self.recovery()["id"]
        result = self.cli("restart-audio")
        self.assertIn("already in progress", result.stdout)
        self.assertEqual(self.recovery()["id"], recovery_id)
        started = time.monotonic()
        os.write(self.master, b"\x1bOB\x1bOBq")
        self.wait_for(lambda _: self.shell.poll() is not None, timeout=1)
        self.assertLess(time.monotonic() - started, 1)
        self.wait_for(lambda _: self.recovery().get("state") in ("done", "failed"))
        self.assertEqual(self.recovery()["state"], "done", self.recovery())
        self.assertNotEqual(self.queue()["pid"], old["pid"])
        calls = [json.loads(line) for line in (self.base / "wsl-calls.jsonl").read_text().splitlines()]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][:6], ["--system", "--cd", "/", "--exec", "sh", "-c"])
        self.assertIn("socket=/mnt/wslg/PulseServer", calls[0][-1])
        self.assertNotIn("weston", calls[0][-1])
        self.assertNotIn("--shutdown", calls[0])

    def test_service_restart_failure_is_reported_and_leaves_player_untouched(self):
        self.env["TEST_LENGTH"] = "30"
        self.fake_audio_service(fail=True)
        self.open()
        old = self.wait_for(lambda q: q.get("playing_index") == 0)
        self.assertEqual(self.cli("restart-audio").returncode, 0)
        self.wait_for(lambda _: self.recovery().get("state") == "failed")
        self.assertIn("fixture service error", self.recovery()["message"])
        self.assertEqual(self.queue()["pid"], old["pid"])

    def test_recovery_does_not_replace_playlist_changed_from_another_terminal(self):
        self.env["TEST_LENGTH"] = "30"
        self.fake_audio_service(delay=2)
        self.open()
        old = self.wait_for(lambda q: q.get("playing_index") == 0)
        self.assertEqual(self.cli("restart-audio").returncode, 0)
        self.wait_for(lambda _: (self.base / "wsl-calls.jsonl").exists())
        self.assertEqual(self.cli("play", "fixture", "-t", "2").returncode, 0)
        new = self.wait_for(lambda q: q.get("job_dir") != old["job_dir"] and q.get("playing_index") == 1)
        self.wait_for(lambda _: self.recovery().get("state") in ("done", "failed"))
        self.assertEqual(self.recovery()["state"], "done", self.recovery())
        self.assertEqual(self.queue()["job_dir"], new["job_dir"])
        self.assertEqual(self.queue()["playing_index"], 1)

    def test_failed_track_is_visible_and_skipped(self):
        self.env.update(TEST_FAIL="2", TEST_LENGTH="0.6")
        self.open()
        queue = self.wait_for(lambda q: q.get("finished") is True)
        self.assertEqual(queue["tracks"][1]["download_status"], "failed")
        self.assertIn("Fixture unavailable", queue["tracks"][1]["error"])
        self.assertEqual(queue["last_index"], 2)

    def test_audio_device_failure_preserves_downloads_and_stops_auto_skipping(self):
        drivers = subprocess.run(["mpv", "--ao=help"], capture_output=True, text=True).stdout
        if "pulse" not in drivers:
            self.skipTest("mpv's PulseAudio driver required for this audio-error check")
        self.library.write_text("settings:\n  mpv_args: [--ao=pulse]\n")
        self.env["PULSE_SERVER"] = "unix:" + str(self.base / "missing-pulse.sock")
        self.open()
        queue = self.wait_for(lambda q: q.get("playback_error") and all(t["download_status"] == "ready" for t in q["tracks"]))
        self.assertEqual(queue["playback_error"], "audio output initialization failed")
        self.assertEqual(queue["last_index"], 0)
        self.assertFalse(queue["finished"])
        self.assertIn("Connection refused", (self.queue_path.parent / "mpv.log").read_text())
        # Explicit selection retries the cached song; it does not skip the entire queue.
        self.assertEqual(self.cli("track", "2").returncode, 0)
        self.wait_for(lambda q: q.get("playback_error") and q.get("last_index") == 1)

    def test_selection_prioritizes_pending_track_and_stop_cancels_download(self):
        self.env.update(TEST_COUNT="5", TEST_DELAY_1="0.5", TEST_DELAY_4="8", TEST_LENGTH="12")
        self.open()
        self.wait_for(lambda q: q["tracks"][0]["download_status"] == "downloading")
        self.assertEqual(self.cli("track", "4").returncode, 0)
        queue = self.wait_for(lambda q: q["tracks"][3]["download_status"] == "downloading")
        self.assertEqual(queue["waiting_index"], 3)
        self.wait_for(lambda _: len([c for c in self.calls() if "-J" not in c["args"]]) == 2)
        audio_calls = [c for c in self.calls() if "-J" not in c["args"]]
        self.assertEqual([c["args"][-1] for c in audio_calls], ["https://example.test/1", "https://example.test/4"])
        download_pid = audio_calls[-1]["pid"]
        self.assertEqual(self.cli("stop").returncode, 0)
        self.assertFalse(Path(queue["job_dir"], "running").exists())
        with self.assertRaises(ProcessLookupError):
            os.kill(download_pid, 0)

    def test_stop_and_play_recover_from_worker_that_died_without_cleanup(self):
        self.open()
        queue = self.wait_for(lambda q: q.get("playing_index") == 0)
        # Simulate WSL shutting down: worker and mpv vanish, the running marker stays.
        os.kill(queue["worker_pid"], signal.SIGKILL)
        os.kill(queue["pid"], signal.SIGKILL)
        self.wait_for(lambda _: not music.owns_worker(queue["worker_pid"], queue["job_dir"]))
        Path(queue["job_dir"], "running").touch()
        result = self.cli("stop")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("still stopping", result.stdout + result.stderr)
        self.assertFalse(Path(queue["job_dir"], "running").exists())
        self.assertIsNone(self.queue().get("worker_pid"))
        self.assertEqual(self.cli("play", "fixture").returncode, 0)
        self.wait_for(lambda q: q.get("job_dir") != queue["job_dir"] and q.get("playing_index") == 0)

    def test_replacing_playlist_cancels_old_job_and_preserves_new_queue(self):
        self.env.update(TEST_DELAY_2="8", TEST_LENGTH="12")
        self.open()
        old = self.wait_for(lambda q: q.get("playing_index") == 0 and q["tracks"][1]["download_status"] == "downloading")
        self.wait_for(lambda _: len([c for c in self.calls() if "-J" not in c["args"]]) == 2)
        download_pid = self.calls()[-1]["pid"]
        # Reusing this URL reuses its library entry and first cached track.
        self.open()
        new = self.wait_for(lambda q: q.get("job_dir") != old["job_dir"] and q.get("playing_index") == 0)
        self.assertFalse(Path(old["job_dir"], "running").exists())
        with self.assertRaises(ProcessLookupError):
            os.kill(download_pid, 0)
        self.assertEqual(new["name"], "Fixture playlist")
        self.assertEqual(len(self.library.read_text().split("title: Fixture playlist")), 2)

    def test_interactive_paste_arrow_enter_pause_and_detach(self):
        self.env.update(TEST_COUNT="25", TEST_LENGTH="12")
        self.start_terminal()
        self.wait_for(lambda _: b"music" in self.terminal_output)
        os.write(self.master, b"https://www.youtube.com/watch?v=JaTuWOh_0g4&list=PLfixture\n")
        self.wait_for(lambda q: q.get("playing_index") == 0 and b"Active playlist" in self.terminal_output)
        # Application-mode arrows are the sequence sent by xterm with keypad enabled.
        os.write(self.master, b"\x1bOB\r")
        self.wait_for(lambda q: q.get("playing_index") == 1)
        self.assertIn(b"Song 1", self.terminal_output)
        self.assertIn(b"Song 2", self.terminal_output)
        self.assertIn(b"downloaded", self.terminal_output)
        # Normal-mode arrows are also accepted (e.g. terminals that send CSI).
        os.write(self.master, b"\x1b[B\r")
        self.wait_for(lambda q: q.get("playing_index") == 2)
        os.write(self.master, b" ")
        self.wait_for(lambda _: self.player.get("pause") is True)
        os.write(self.master, b"\x1bOF")  # End: reach the 25th track below the fold.
        self.wait_for(lambda _: b"Song 25" in self.terminal_output)
        os.write(self.master, b"\r")
        self.wait_for(lambda q: q.get("playing_index") == 24)
        old_job = self.queue()["job_dir"]
        os.write(self.master, b"a")
        self.wait_for(lambda _: b"Paste playlist/song URL" in self.terminal_output)
        os.write(self.master, b"https://www.youtube.com/playlist?list=PLreplacement\n")
        self.wait_for(lambda q: q.get("job_dir") != old_job and q.get("playing_index") == 0)
        self.assertFalse(Path(old_job, "running").exists())
        os.write(self.master, b"q")
        self.wait_for(lambda _: b"quit" not in self.terminal_output[-100:] and b"\xe2\x80\xba" in self.terminal_output[-100:])
        os.write(self.master, b"quit\n")
        self.shell.wait(timeout=5)
        self.assertEqual(self.shell.returncode, 0)
        self.assertIsNotNone(self.queue().get("worker_pid"))
        self.assertEqual(self.queue()["playing_index"], 0)
        metadata_call = self.calls()[0]["args"]
        self.assertEqual(metadata_call[-1], "https://www.youtube.com/playlist?list=PLfixture")


if __name__ == "__main__":
    unittest.main()

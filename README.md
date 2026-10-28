# Command Line Music Player

`music` plays YouTube albums and playlists from your terminal using `mpv` and
`yt-dlp`. Paste a link into the interactive CLI to save the playlist, download
its audio in the background, and start playing as soon as the first song is
ready. The rest downloads while you listen. Your library lives in one YAML file.

Every song is kept in a music folder (`/mnt/c/dev/music` by default), named
`Artist - Album (Year)/01 - Title [id].m4a`. Songs you have heard once play
instantly afterwards, and other music players can use the same files. To only
download, without playing:

```bash
uv run music -dl 'https://www.youtube.com/playlist?list=OLAK5uy_...'
```

## Paste a playlist and play

Run `./music` (or `music` if installed), then paste your URL at the prompt:

```text
music › https://www.youtube.com/watch?v=JaTuWOh_0g4&list=PLbmTrF3K6gB1q26lgBX8a_7MbR_GZEwFu
```

A link containing both a video and a playlist loads the **entire playlist**.
This includes YouTube mixes (`list=RD…`). A mix is saved as it was when you
pasted it; YouTube generates a new one each time.
It opens a full-screen view showing the active song, playback progress, and
every track's download status. Use **↑/↓** to browse and **Enter** to play the
selected song. Longer playlists scroll; **PgUp/PgDn** and **Home/End** reach
the rest. Browsing leaves the current song playing until you press Enter.
Selecting a song that is still queued prioritizes its download after the
current download finishes; the current song keeps playing until it is ready.

Press **a** in the view to paste another link, or **q** to return to the command
prompt. `now` reopens the view. `stop` cancels downloads and stops playback.
Leaving the shell keeps playback and downloads running.

Audio recovers by itself. While a playlist is active, the background worker
restarts audio and puts the current song back at the same position when:

- playback is stuck (the position stops moving for 6 seconds), or mpv stops
  responding or crashes;
- the audio output fails to start;
- on WSL, WSLg's audio connection to Windows breaks. This happens when Windows
  switches devices, for example between Teams and Bluetooth headphones, or after
  sleep. It is noticed while playing and when you resume;
- on WSL, you resume after a pause of 5 minutes or more. Windows may have
  changed devices in the meantime, so audio is reconnected before resuming
  (`reset_audio_after_pause`; see [The library file](#the-library-file)).

Each recovery retries up to three times and only reports success once playback
is moving again. Automatic restarts stop after three in 10 minutes, so a missing
device does not cause an endless loop. The view then shows the error, and **r**
still works.

To restart audio by hand, press **r**. Recovery runs in the background and
restores the current song, last known position, volume, and pause state. The
queue and completed downloads are reused. If it restores a paused song, press
Space to resume.

On WSL this restarts WSLg's shared PulseAudio service, briefly interrupting
other WSL audio streams, then restarts the player. It does not restart Windows,
the WSL distributions, or the graphical desktop. Use `restart-audio` at the
interactive prompt, or `music restart-audio` from another terminal. Use
`music restart-audio --player-only` to restart mpv alone. On other systems,
the default restarts mpv alone. Progress and failures appear in the playlist
view and `status`; diagnostic output is in `audio-restart.log` in the state folder.

For one-shot use, quote the URL:

```bash
music open 'https://www.youtube.com/watch?v=JaTuWOh_0g4&list=PLbmTrF3K6gB1q26lgBX8a_7MbR_GZEwFu'
music now
```

```text
$ music
music 2.3.0 — paste a playlist URL or type `help`; Ctrl-D leaves (music keeps playing)
music › add https://www.youtube.com/playlist?list=OLAK5uy_n3hzWL4YDrVBv1fVtpv9HbhkpRnwWbIbw
Album: The Gettin' Off Stage, Step 2 (2016) — People Under The Stairs
   1. Back from the Dead  6:09
   ...
Save to library? [Y/n]
music › play gettin
▶ The Gettin' Off Stage, Step 2 — People Under The Stairs
♪ 1/6 Back from the Dead › now
▶ playing  Back from the Dead — People Under The Stairs
The Gettin' Off Stage, Step 2  ·  track 1/6  ·  vol 100%
━━━━━━━━━━●─────────────────────────────────────  1:04 / 6:08
```

## Setup

```bash
# Ubuntu/Debian (WSL included)
sudo apt install mpv python3-yaml fzf      # fzf is optional (fuzzy album picker)
# The distro's yt-dlp is usually too old for YouTube; use the standalone release:
curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp -o ~/.local/bin/yt-dlp
chmod +x ~/.local/bin/yt-dlp

# macOS
brew install mpv yt-dlp fzf && pip3 install pyyaml

# Put `music` on your PATH
ln -s "$PWD/music" ~/.local/bin/music
music doctor                               # checks everything is found
```

With [uv](https://docs.astral.sh/uv/), you don't need `python3-yaml`; run it from the
repository with `uv run music ...`. mpv and yt-dlp are still installed as above.
`ffmpeg` is optional; with it, downloaded files get title/artist/album tags.

For the tested WSL2/WSLg setup and audio troubleshooting, see [WSL.md](WSL.md).

## Two ways to use it

- **Interactive:** run `music` with no arguments. Paste a link or use commands
  with history and Tab completion. Starting a playlist opens the playlist view.
  Leaving it with `quit` or Ctrl-D does not stop the music or downloads.
- **One-shot:** `music play 3`, `music next`, `music pause`. Useful for key
  bindings and scripts. Both modes run the same commands.

`music --help` lists every command. `music <command> --help` (or `help <command>`
inside the shell) shows the details for one command.

## Commands

| Command | What it does |
| --- | --- |
| `open URL` (or paste a bare URL in the shell) | Save the whole playlist, download audio in the background, and play. |
| `download URL\|ALBUM [-o DIR]` (`dl`, `-dl`) | Download a playlist, song or library album to the music folder without playing. |
| `add URL` | Add an album/playlist or a single song. The URL type is detected (see below). |
| `list [WORDS]` (`ls`) | Numbered library: albums first, then songs. Words filter it. |
| `show [ALBUM]` (`tracks`) | Track list of an album, or of what's playing. |
| `play [ALBUM] [-t N]` | Play an album front to back, optionally from track N. With no album it resumes a paused track, or opens a picker. |
| `random` (`rand`) | Play a random album, in order. |
| `random --songs` | Shuffle every song in the library. |
| `shuffle [ALBUM]` | Shuffle one album's tracks (default: the current album). |
| `pause` (`toggle`) | Pause or resume. |
| `next` (`n`) / `prev` | Next track / restart the track, or go back if within 5s of its start. |
| `track N` (`jump`) | Jump to track N. |
| `seek +30` / `seek -10` / `seek 1:30` | Relative or absolute seek. |
| `volume [70\|+10\|-10]` (`vol`) | Show or set the volume. |
| `status` (`st`) | One-shot "now playing". |
| `now` (`np`) | Scrollable active playlist with current song and download status. |
| `stop` | Stop playback and cancel background downloads. |
| `restart-audio` (`audio-reset`) | Restart audio in the background and restore the current song; `--player-only` restarts only mpv. |
| `remove ALBUM` (`rm`) | Remove from the library (asks first). |
| `edit` | Open the library in `$EDITOR`. |
| `import-txt FILE...` | Convert old `label \| url` playlist files to albums. |
| `doctor` | Check dependencies, library and paths. |

**ALBUM** can be the number from `list`, or any words from the artist and title
(`play gettin`, `play people stairs 2`). If several albums match, you get a
picker: `fzf` if it's installed, otherwise a numbered menu.

### Live view keys (`now`)

| Key | Action |
| --- | --- |
| `space` | Pause / resume |
| `n` / `p` | Next / previous track |
| `←` / `→` | Seek 10s back / forward |
| `↑` / `↓` | Move the playlist selection |
| `Enter` | Play the selected track (waits for its download if necessary) |
| `PgUp` / `PgDn`, `Home` / `End` | Scroll pages, or select the first / last track |
| `g` | Move selection to the current song |
| `+` / `-` | Volume up / down |
| `1`–`9` | Jump to that track |
| `a` | Paste another playlist or song URL |
| `r` | Restart audio and restore playback |
| `q` | Return to the prompt (music and downloads keep running) |

## Adding music

Pasting a bare URL or using `open URL` saves and plays immediately. Playlist
links always load their whole playlist in this workflow. Existing library
entries and downloaded audio are reused.

`music add URL` keeps the explicit library-management workflow. It asks
`yt-dlp` for the metadata, then
shows you what it found and asks before saving:

| Link | Saved as |
| --- | --- |
| `youtube.com/playlist?list=…` or `music.youtube.com/browse/MPREb…` | Album: title, artist, year, all tracks |
| `watch?v=…` / `youtu.be/…` | Song |
| `watch?v=…&list=OLAK5uy_…` (a track opened from an album) | Asks. The default is the whole album. |
| `watch?v=…&list=PL…` (a track inside a playlist) | Asks. The default is just the song. |
| `watch?v=…&list=RD…` (a YouTube mix/radio) | Asks. The default is just the song. |
| Other sites supported by yt-dlp (e.g. Bandcamp) | Album or song, whichever yt-dlp reports |

Overrides: `--album`, `--song`, `--title "…"`, `--artist "…"`. Use `-y` to skip
the confirmation, and `--play` to start playing it right away.

The best source is the album playlist from YouTube Music. Open the album on
music.youtube.com and copy the page URL, or use Share on the album. Those links
include the proper artist and year.

## Downloading without playing

`music download` (also `dl` or `-dl`) saves audio to the music folder and exits.
It does not play anything, so it works in scripts and cron jobs:

```bash
music -dl 'https://www.youtube.com/playlist?list=OLAK5uy_...'   # a new playlist (saved to the library too)
music dl gettin                                                 # an album already in the library
music dl 3 -o ~/Music                                           # a different folder, this time only
```

Files that are already there are skipped. If YouTube refuses a track (for
example `HTTP Error 403`), the command finishes the others and exits non-zero;
run it again to retry. Tracks played with an older version of `music`, which
lived in `~/.cache/music-player/`, are copied over instead of downloaded again.

The music folder is, in order: `-o DIR`, `$MUSIC_DOWNLOAD_DIR`, `music_dir` in
the library's `settings`, then `/mnt/c/dev/music` (or `~/Music` when there is
no `/mnt/c`). Tracks are found again by the `[id]` in their name, inside the
folder named from the library's artist, title and year. If you rename an album
in the library, its songs download again into a new folder.

## The library file

The library is `library.yaml`, next to the script. You can point elsewhere with
`--library FILE` or `MUSIC_LIBRARY=/path/file.yaml`. The file is git-ignored,
so your collection stays out of the repo. See
[library.example.yaml](library.example.yaml) for its structure:

```yaml
settings:                 # optional
  mpv_args: [--ao=pulse]  # extra mpv flags
  volume: 80              # starting volume
  music_dir: /mnt/c/dev/music   # where downloaded songs are kept
  auto_recover_audio: true      # restart audio by itself when it breaks (default)
  reset_audio_after_pause: 300  # WSL: reconnect audio on resume after this many seconds paused; 0 = never

albums:
  - title: The Gettin' Off Stage, Step 2
    artist: People Under The Stairs
    year: 2016
    url: https://www.youtube.com/playlist?list=OLAK5uy_...
    tracks:
      - title: Back from the Dead
        url: https://www.youtube.com/watch?v=zK_-8YpVxkg
        duration: 369

songs:
  - title: Never Gonna Give You Up
    artist: Rick Astley
    url: https://www.youtube.com/watch?v=dQw4w9WgXcQ
```

It is plain YAML, so you can reorder tracks, fix titles or delete entries by
hand. `music` re-reads it on every command. Only `title` and `url` are
required. When `add` or `remove` rewrites the file, comments you added by hand
are lost.

## How it works

`play` starts a detached download coordinator and an `mpv` process. `yt-dlp`
downloads one track at a time to the music folder (see
[Downloading without playing](#downloading-without-playing)), preferring m4a
audio. Only completed local files are handed to mpv.
The first ready track starts immediately; playback continues in playlist order,
waiting when the next download is still running. Unavailable tracks are marked
`failed`, show an error when selected, and are skipped during automatic playback.
Completed downloads are reused on future plays and remain after `stop`.
Songs already in the old `~/.cache/music-player/` cache still play from there.

You control playback over mpv's JSON IPC socket at
`$XDG_RUNTIME_DIR/music-player.sock`, so any terminal can control the same
playback. The queue and shell history are kept in `~/.local/state/music-player/`.
mpv's output goes to `mpv.log` in that folder. Download and worker logs live
under `jobs/` there. Starting another playlist cancels the previous download
job before creating the new one. `doctor` prints the cache and log locations.
The playlist view observes playback over a background connection, so an
unresponsive mpv does not block keyboard navigation or quitting the view.

## Verification

```bash
python3 -m unittest discover -s tests -v     # or: uv run python -m unittest discover -s tests -v
```

The integration checks use real mpv with silent audio output, isolated temporary
state/cache/library paths, and an offline yt-dlp fixture. They cover playback
advancing between downloads, cache reuse, unavailable tracks, download
cancellation, and pasting/navigating a 25-track playlist in a terminal.
They also cover keyboard responsiveness with a stalled player and preserving
downloaded tracks when the audio device is unavailable.
Recovery checks cover paused playback, position and volume restoration,
replacing a stalled mpv, background recovery, duplicate requests, and service
restart failures. Automatic recovery is checked for a frozen and a crashed mpv,
resuming after a long pause, a lost WSLg connection, and giving up after
repeated failures. The WSL service and its logs are simulated in the automated
tests; the real one is never touched.

## Troubleshooting

- `music doctor` shows what's missing and where the socket and logs are.
- If downloads fail, inspect the selected track's error and its log under
  `~/.local/state/music-player/jobs/`. An outdated yt-dlp can cause YouTube
  failures: run `yt-dlp -U`. Check `mpv.log` for playback errors.
- If you hear nothing, check the system volume and output device. On WSL, see
  [WSL.md](WSL.md).
- An audio-device initialization error stops playback without skipping the
  whole playlist, and audio restarts automatically. If that keeps failing, fix
  the device and press **r**. Downloaded files remain cached. Details are in
  `mpv.log` and `audio-restart.log`.

## Legal Notice
This tool downloads and caches audio for personal playback. Use it only for
content you have permission to download, and comply with applicable copyright
laws and the source site's terms.

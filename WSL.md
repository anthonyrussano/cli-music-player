# WSL music player quick start

## Current setup

Tested on September 28, 2026, in Ubuntu 24.04 under WSL2 with WSLg.
`mpv`, PyYAML (`python3-yaml`), `fzf` and Zsh are installed. The existing current `yt-dlp` at
`~/.local/bin/yt-dlp` takes precedence over Ubuntu's older packaged version.
WSLg supplies `PULSE_SERVER=unix:/mnt/wslg/PulseServer` and the `RDPSink` output.

A ten-second YouTube playback test succeeded through PulseAudio. The project's
play, pause, resume, and stop controls also passed. This confirms audio delivery
to WSLg; speaker audibility must be checked at the Windows end.

## Play music

`music` is installed as `~/.local/bin/music`, a link to
`~/cli-music-player/music`. It works from any directory and needs nothing
sourced. Your default shell is Zsh with Oh My Zsh (`ZSH_THEME="fino-time"`).
The old `music()` helper in `~/.zshrc` was removed, because it would have hidden
the new command. A backup of `~/.zshrc` from before that edit is in
`~/.zshrc.backup-20260928-134650`.

```zsh
music               # interactive shell: help, play, now, ...
music ls            # or one-shot commands
music play gettin
music now           # live view; q leaves it, music keeps playing
music stop
```

In the interactive shell, paste a YouTube playlist URL directly at the prompt.
For example, a `watch?v=...&list=...` link loads the entire playlist, downloads
audio in the background, and opens the active playlist view. Playback starts
when the first download is ready. Use ↑/↓ to browse, Enter to play a selected
song, and PgUp/PgDn or Home/End to reach tracks below the screen. The current
song has a ▶ marker; selection has its own highlight. Use `+`/`-` for volume.
Press `a` to paste another URL or `q` to return to the prompt. `stop` cancels
downloads as well as playback. Completed audio is cached in
`~/.cache/music-player/` and reused on later plays.

Your library is `~/cli-music-player/library.yaml`. It contains **The Gettin'
Off Stage, Step 2** by People Under the Stairs, re-added from its YouTube Music
album link, with the proper track titles, artist and year. The library is
git-ignored, so it won't come with a fresh clone. To add an album, paste its
link:

```zsh
music add 'https://www.youtube.com/playlist?list=OLAK5uy_n3hzWL4YDrVBv1fVtpv9HbhkpRnwWbIbw'
```

Keep URLs quoted, because they often contain `&`. See [README.md](README.md) for
all commands and how links are detected. The old
`the_gettin_off_stage_step_2.txt` is no longer used; you can delete it. Any other
old `label | url` files can be converted with `music import-txt FILE.txt`.

The library sets `mpv_args: [--ao=pulse]`. mpv's default audio probe prints a
PipeWire warning before it falls back to PulseAudio, and this flag skips it.

## Shell configuration and recovery

The previous Zsh configuration was preserved in
`~/.zshrc.backup-20260928-091750`. Existing Zsh settings were retained, and the
main CLI paths, editor preferences, and common aliases were carried over from
Bash. Bash-specific completion scripts were not sourced into Zsh.

Check the active theme and registered default shell:

```zsh
echo "$ZSH_THEME"
getent passwd "$USER" | cut -d: -f7
```

Use a dark terminal background with 256-color support for `fino-time`.
If a terminal profile explicitly starts `bash`, change that profile to launch
the WSL distribution without a Bash override. Changing the account default
does not replace shells already running.

To return to Bash as your default:

```zsh
chsh -s /bin/bash
```

To restore the previous Zsh settings, first save any newer edits, then:

```zsh
cp ~/.zshrc.backup-20260928-091750 ~/.zshrc
```

## Troubleshooting

Check dependencies and WSLg's audio connection:

```zsh
music doctor
yt-dlp --version
echo "$PULSE_SERVER"
pactl info
pactl list short sinks
```

`pactl` is provided by Ubuntu's `pulseaudio-utils` package. An idle sink may
show `SUSPENDED`; that alone is not an error. Check Windows volume and the
selected output device if playback advances but you hear nothing.

Audio normally recovers by itself. When WSLg's link to Windows breaks (weston
restarting, or PulseAudio logging `data_send: send failed` in
`/mnt/wslg/pulseaudio.log*`), the player restarts WSLg's PulseAudio. It waits
until PulseAudio logs `RDP Sink - Connected`, then resumes the song, retrying up
to three times. Resuming after a pause of 5 minutes or more does the same,
because Windows may have switched devices during a Teams call. Stuck or crashed
playback is restarted too. See README.md for the settings.

If audio is still silent, press **r** in the playlist view, or run
`music restart-audio`. This restarts WSLg's PulseAudio service and
the player, then restores the current song at its last known position with the
same volume and pause state. It briefly interrupts other WSL audio streams.
The playlist view stays responsive while recovery runs. A paused song stays
paused; press Space to resume after recovery finishes. Cached songs are reused.
`music restart-audio --player-only` restarts only the player.

WSLg's supervisor recreates its audio server after it exits; see the
[WSLg architecture documentation](https://github.com/microsoft/wslg#WSLGd).
Recovery does not stop Weston or shut down Ubuntu/Docker. If restarting audio
does not recover the connection, the view shows the failure and writes details
to `~/.local/state/music-player/audio-restart.log`.

mpv's output from the last `play` is in `~/.local/state/music-player/mpv.log`.
To troubleshoot the first track directly:

```zsh
mpv --no-video --ao=pulse --length=10 \
  'https://www.youtube.com/watch?v=zK_-8YpVxkg'
```

The tested default audio selection printed a PipeWire configuration warning
before successfully falling back to PulseAudio. Explicit `--ao=pulse` avoids
that probe in the diagnostic command.

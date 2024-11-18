# music_player.ps1

# Full path to mpv.exe
$mpvPath = "mpv.exe"  # Replace with the full path if mpv.exe is not in your PATH

# Path for the mpv IPC named pipe
$PipeName = "\\.\pipe\mpvsocket"

# Default playlist
$Script:CurrentPlaylist = "playlist.txt"

# Function definitions
function Start-Mpv {
    param(
        [string]$Playlist
    )
    if (-not $Playlist) {
        $Playlist = $Script:CurrentPlaylist
    }

    if (-not (Test-Path $Playlist)) {
        Write-Host "Playlist not found: $Playlist"
        return
    }

    # Extract URLs from the playlist
    $PlaylistEntries = Get-Content $Playlist
    $Urls = @()
    foreach ($Entry in $PlaylistEntries) {
        $Parts = $Entry -split '\s*\|\s*'
        if ($Parts.Count -ge 2) {
            $Urls += $Parts[1]
        }
    }

    if (-not $Urls) {
        Write-Host "No valid URLs found in the playlist."
        return
    }

    # Save URLs to a temporary file
    $TempPlaylist = [System.IO.Path]::Combine([System.IO.Path]::GetTempPath(), "current_playlist.txt")
    $Urls | Set-Content $TempPlaylist

    # Start mpv with IPC server and the playlist
    Start-Process -FilePath $mpvPath `
        -ArgumentList "--no-video", "--really-quiet", "--input-ipc-server=$PipeName", "--playlist=$TempPlaylist" `
        -WindowStyle Hidden
    Start-Sleep -Seconds 1
}

function Send-MpvCommand {
    param(
        [hashtable]$Command
    )
    $JsonCommand = $Command | ConvertTo-Json -Compress
    try {
        $PipeHandle = New-Object System.IO.Pipes.NamedPipeClientStream('.', 'mpvsocket', [System.IO.Pipes.PipeDirection]::InOut, [System.IO.Pipes.PipeOptions]::None)
        $PipeHandle.Connect(1000)
        $StreamWriter = New-Object System.IO.StreamWriter($PipeHandle)
        $StreamReader = New-Object System.IO.StreamReader($PipeHandle)
        $StreamWriter.AutoFlush = $true

        $StreamWriter.WriteLine($JsonCommand)
        $Response = $StreamReader.ReadLine()

        $PipeHandle.Close()
        if ($Response) {
            return $Response | ConvertFrom-Json
        }
    } catch {
        Write-Host "Failed to send command to mpv: $_"
    }
}

function Get-MpvProperty {
    param(
        [string]$Property
    )
    $Command = @{ "command" = @("get_property", $Property) }
    $Result = Send-MpvCommand -Command $Command
    if ($Result -and $Result.data -ne $null) {
        return $Result.data
    } else {
        return $null
    }
}

function Ensure-PlayerRunning {
    if (-not (Get-Process -Name mpv -ErrorAction SilentlyContinue)) {
        Start-Mpv
    }
}

function List-Songs {
    if (-not (Test-Path $Script:CurrentPlaylist)) {
        Write-Host "Playlist not found: $Script:CurrentPlaylist"
        return
    }

    $Index = 1
    Get-Content $Script:CurrentPlaylist | ForEach-Object {
        $Parts = $_ -split '\s*\|\s*'
        $Label = $Parts[0]
        Write-Host "$Index. $Label"
        $Index++
    }
}

function Play-Song {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Search
    )

    if (-not (Test-Path $Script:CurrentPlaylist)) {
        Write-Host "Playlist not found: $Script:CurrentPlaylist"
        return
    }

    $Playlist = Get-Content $Script:CurrentPlaylist
    $SongLine = $null

    if ($Search -match '^\d+$') {
        $Index = [int]$Search - 1
        if ($Index -ge 0 -and $Index -lt $Playlist.Count) {
            $SongLine = $Playlist[$Index]
        }
    } else {
        $SongLine = $Playlist | Where-Object { $_ -match $Search } | Select-Object -First 1
    }

    if (-not $SongLine) {
        Write-Host "Song not found."
        List-Songs
        return
    }

    $Parts = $SongLine -split '\s*\|\s*'
    $Label = $Parts[0]
    $Url = $Parts[1]

    if (-not $Url) {
        Write-Host "URL not found for song."
        return
    }

    # Stop mpv if running
    Get-Process -Name mpv -ErrorAction SilentlyContinue | Stop-Process -Force

    # Start mpv with the selected song
    Start-Process -FilePath "mpv.exe" `
        -ArgumentList "--no-video", "--really-quiet", "--input-ipc-server=$PipeName", "$Url" `
        -WindowStyle Hidden
    Write-Host "Playing: $Label"
}

function Play-Playlist {
    # Stop mpv if running
    Get-Process -Name mpv -ErrorAction SilentlyContinue | Stop-Process -Force

    Start-Mpv
    Write-Host "Playing playlist: $Script:CurrentPlaylist"
}

function Shuffle-Play {
    if (-not (Test-Path $Script:CurrentPlaylist)) {
        Write-Host "Playlist not found: $Script:CurrentPlaylist"
        return
    }

    # Stop mpv if running
    Get-Process -Name mpv -ErrorAction SilentlyContinue | Stop-Process -Force

    # Read the playlist and extract URLs
    $PlaylistEntries = Get-Content $Script:CurrentPlaylist
    $Urls = @()
    foreach ($Entry in $PlaylistEntries) {
        $Parts = $Entry -split '\s*\|\s*'
        if ($Parts.Count -ge 2) {
            $Urls += $Parts[1]
        }
    }

    if (-not $Urls) {
        Write-Host "No valid URLs found in the playlist."
        return
    }

    # Shuffle URLs
    $ShuffledUrls = $Urls | Get-Random -Count ($Urls.Count)

    # Save shuffled URLs to a temporary file
    $TempPlaylist = [System.IO.Path]::Combine([System.IO.Path]::GetTempPath(), "shuffled_playlist.txt")
    $ShuffledUrls | Set-Content $TempPlaylist

    # Start mpv with the shuffled playlist
    Start-Process -FilePath $mpvPath `
        -ArgumentList "--no-video", "--really-quiet", "--input-ipc-server=$PipeName", "--playlist=$TempPlaylist" `
        -WindowStyle Hidden

    Write-Host "Shuffled playlist started."
}


function Switch-Playlist {
    param(
        [Parameter(Mandatory = $true)]
        [string]$PlaylistName
    )

    if (-not $PlaylistName.EndsWith(".txt")) {
        $PlaylistName += ".txt"
    }

    if (Test-Path $PlaylistName) {
        $Script:CurrentPlaylist = $PlaylistName
        Write-Host "Switched to playlist: $PlaylistName"
    } else {
        Write-Host "Playlist not found: $PlaylistName"
        List-Playlists
    }
}

function List-Playlists {
    $Playlists = Get-ChildItem -Path . -Filter "*.txt"
    Write-Host "Available playlists:"
    foreach ($Playlist in $Playlists) {
        if ($Playlist.Name -eq $Script:CurrentPlaylist) {
            Write-Host "* $($Playlist.Name) (current)"
        } else {
            Write-Host "  $($Playlist.Name)"
        }
    }
}

function Toggle-Pause {
    Ensure-PlayerRunning
    $Command = @{ "command" = @("cycle", "pause") }
    Send-MpvCommand -Command $Command
    Write-Host "Toggled play/pause."
}

function Next-Song {
    Ensure-PlayerRunning
    $Command = @{ "command" = @("playlist-next") }
    Send-MpvCommand -Command $Command
    Write-Host "Skipped to next song."
}

function Previous-Song {
    Ensure-PlayerRunning
    $Command = @{ "command" = @("playlist-prev") }
    Send-MpvCommand -Command $Command
    Write-Host "Went to previous song."
}

function Seek {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Amount
    )
    Ensure-PlayerRunning
    $Command = @{ "command" = @("seek", $Amount, "relative") }
    Send-MpvCommand -Command $Command
    Write-Host "Seeked $Amount seconds."
}

function Seek-Forward {
    Seek "+30"
    Write-Host "Skipped forward 30 seconds."
}

function Seek-Backward {
    Seek "-30"
    Write-Host "Skipped backward 30 seconds."
}

function Adjust-Volume {
    param(
        [Parameter(Mandatory = $true)]
        [int]$Change
    )
    Ensure-PlayerRunning
    $Command = @{ "command" = @("add", "volume", $Change) }
    Send-MpvCommand -Command $Command
    Write-Host "Adjusted volume by $Change."
}

function Stop-Playback {
    Get-Process -Name mpv -ErrorAction SilentlyContinue | Stop-Process -Force
    Write-Host "Playback stopped."
}

function Show-Status {
    Ensure-PlayerRunning

    $Title = Get-MpvProperty -Property "media-title"
    $Position = Get-MpvProperty -Property "time-pos"
    $Duration = Get-MpvProperty -Property "duration"

    Write-Host "Now Playing: $Title"

    if ($Position -and $Duration) {
        $PosMinutes = [int]($Position / 60)
        $PosSeconds = [int]($Position % 60)
        $DurMinutes = [int]($Duration / 60)
        $DurSeconds = [int]($Duration % 60)
        Write-Host ("[{0}:{1:D2} / {2}:{3:D2}]" -f $PosMinutes, $PosSeconds, $DurMinutes, $DurSeconds)
    } else {
        Write-Host "[0:00 / 0:00]"
    }
}

function Show-LiveStatus {
    Ensure-PlayerRunning
    Write-Host "Press 'q' to exit live status."
    $Host.UI.RawUI.CursorVisible = $false

    try {
        while ($true) {
            if ($Host.UI.RawUI.KeyAvailable) {
                $Key = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
                if ($Key.Character -eq 'q') {
                    break
                }
            }

            $Title = Get-MpvProperty -Property "media-title"
            $Position = Get-MpvProperty -Property "time-pos"
            $Duration = Get-MpvProperty -Property "duration"

            $Host.UI.RawUI.CursorPosition = @{X=0;Y=($Host.UI.RawUI.CursorPosition.Y - 2)}
            Write-Host "Now Playing: $Title " -NoNewline

            if ($Position -and $Duration) {
                $PosMinutes = [int]($Position / 60)
                $PosSeconds = [int]($Position % 60)
                $DurMinutes = [int]($Duration / 60)
                $DurSeconds = [int]($Duration % 60)
                Write-Host ("[{0}:{1:D2} / {2}:{3:D2}]" -f $PosMinutes, $PosSeconds, $DurMinutes, $DurSeconds)

                # Create progress bar
                $Width = 50
                $Progress = [int](($Position / $Duration) * $Width)
                $Bar = "=" * $Progress + ">" + " " * ($Width - $Progress - 1)
                Write-Host "[$Bar]"
            } else {
                Write-Host "[0:00 / 0:00]"
                Write-Host "[                                                  ]"
            }

            Start-Sleep -Milliseconds 500
        }
    } finally {
        $Host.UI.RawUI.CursorVisible = $true
        Write-Host ""
    }
}

function Show-Help {
    @"
Music Player Commands:

Playback Control:
  Play-Song NUMBER/LABEL - Play song by number or label
  Play-Playlist          - Start playing full playlist
  Shuffle-Play           - Shuffle and play playlist
  Next-Song              - Skip to next song
  Previous-Song          - Go to previous song
  Toggle-Pause           - Pause/unpause playback
  Stop-Playback          - Stop playing

Navigation:
  Seek +/-N              - Seek N seconds forward/backward
  Seek-Forward           - Seek forward 30 seconds
  Seek-Backward          - Seek backward 30 seconds

Status and Information:
  Show-Status            - Show current track info
  Show-LiveStatus        - Show live updating status
  List-Songs             - Show all available songs

Playlist Management:
  List-Playlists         - Show all available playlists
  Switch-Playlist NAME   - Switch to a different playlist

Volume Control:
  Adjust-Volume +/-N     - Adjust volume by N units

Tips:
- All playlists are .txt files in the current directory
- You can use partial matches for song labels

Examples:
  .\music_player.ps1 List-Songs                     # List available songs
  .\music_player.ps1 Play-Song 3                    # Play the third song
  .\music_player.ps1 Play-Song "Drake"              # Play first matching song
  .\music_player.ps1 Shuffle-Play                   # Play playlist in random order
  .\music_player.ps1 Seek "+30"                     # Skip forward 30 seconds
  .\music_player.ps1 Show-Status                    # Show current track info
  .\music_player.ps1 Switch-Playlist "rock"         # Switch to rock.txt playlist
  .\music_player.ps1 Adjust-Volume +10              # Increase volume
"@
}

# Aliases for quick commands (optional, if you still want to source the script)
Set-Alias px Play-Song
Set-Alias pn Next-Song
Set-Alias pp Previous-Song
Set-Alias pt Toggle-Pause
Set-Alias ps Show-Status
Set-Alias pl List-Songs
Set-Alias psh Shuffle-Play
Set-Alias sw Switch-Playlist
Set-Alias stp Stop-Playback
Set-Alias hlp Show-Help
Set-Alias psl List-Playlists
Set-Alias pf Seek-Forward
Set-Alias pb Seek-Backward
Set-Alias plv Show-LiveStatus

# Command-line parsing
if ($args.Count -gt 0) {
    $command = $args[0]
    $params = $args[1..($args.Count - 1)]

    switch ($command.ToLower()) {
        "play-song" {
            Play-Song @params
        }
        "play-playlist" {
            Play-Playlist
        }
        "shuffle-play" {
            Shuffle-Play
        }
        "next-song" {
            Next-Song
        }
        "previous-song" {
            Previous-Song
        }
        "toggle-pause" {
            Toggle-Pause
        }
        "stop-playback" {
            Stop-Playback
        }
        "seek" {
            Seek @params
        }
        "seek-forward" {
            Seek-Forward
        }
        "seek-backward" {
            Seek-Backward
        }
        "adjust-volume" {
            Adjust-Volume @params
        }
        "show-status" {
            Show-Status
        }
        "show-livestatus" {
            Show-LiveStatus
        }
        "list-songs" {
            List-Songs
        }
        "switch-playlist" {
            Switch-Playlist @params
        }
        "list-playlists" {
            List-Playlists
        }
        "show-help" {
            Show-Help
        }
        default {
            Write-Host "Unknown command: $command"
            Show-Help
        }
    }
} else {
    Show-Help
}

# End of music_player.ps1

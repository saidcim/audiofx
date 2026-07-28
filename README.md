# audiofx

Local, ffmpeg-based tool that turns an audio file - or a whole folder of them -
into **slowed**, **sped up**, **reverb** and **slowed + reverb** versions.
It ships with a Tkinter interface and a full command line interface.

Everything runs on your machine; nothing is uploaded anywhere.

```bash
python -m audiofx gui                       # graphical interface
python -m audiofx convert song.mp3 --preset default
python -m audiofx batch ./music -o ./out --preset dreamy --recursive
```

## Requirements

1. **ffmpeg** (with `ffprobe`):

   ```bash
   winget install Gyan.FFmpeg      # Windows
   brew install ffmpeg             # macOS
   sudo apt install ffmpeg         # Debian/Ubuntu
   ```

   A build with `--enable-librubberband` and `--enable-libsoxr` gives better
   quality; the gyan.dev "full" builds have both. Everything still works
   without them.

2. **Python 3.11+** and the two runtime dependencies:

   ```bash
   pip install -r requirements.txt
   ```

3. Optional, to get an `audiofx` command on your PATH:

   ```bash
   pip install -e .
   ```

   Without it, use `python -m audiofx` instead of `audiofx` in every example.

Check what your ffmpeg build supports:

```bash
python -m audiofx check
```

## The interface

1. Copy songs into the **`songs/`** folder.
2. Double click **`audiofx.bat`** (Windows) or run `python -m audiofx gui`.
3. Pick songs on the left, set the effect on the right, press **Convert selected**.
4. Results land in **`output/`**.

What the panels do:

- **Song list** - mirrors `songs/`, shows length, format and size, supports
  multi-select, and double click opens a file in your default player. Press
  **Refresh** after adding files.
- **Preset** - the entries from `presets.yaml`; picking one fills every control
  below. Changing anything by hand switches back to `Custom`.
- **Effect** - slowed / sped up / reverb only / slowed + reverb.
- **Speed** - the slider range follows the effect (0.40-0.99 for slowed,
  1.01-2.50 for sped up); `-`/`+` step by 0.01 and the label reads out
  "0.85x (15% slower)".
- **Keep the original pitch** - changes the tempo without the chipmunk/deep
  voice effect. **Pitch shift** moves the pitch on its own, in semitones.
- **Reverb** - room size, amount, and a **bass cut** control (see below).
- **Output** - quality, tag copying, and the output folder.
- Conversions run in the background with a progress bar, a log pane and a
  **Cancel** button. One failing file does not stop the rest.

Folder choices and your last settings are stored in `~/.audiofx-gui.json`.

## Quality

| Option | What it does |
| --- | --- |
| Same as source (high bitrate) | Keeps the extension; re-encodes lossy formats at 320 kbps or higher |
| FLAC (lossless) | No quality loss at all after processing (bigger files) |
| WAV (lossless) | Lossless and uncompressed |
| MP3 320 kbps | Constant 320 kbps mp3 |

What the tool does to protect quality:

- resampling goes through **libsoxr** at precision 28 when the build has it;
- **rubberband** handles tempo changes when the pitch has to stay put;
- lossy sources are re-encoded at 320 kbps or above, and lossless output is one
  click away;
- the reverb can never clip the mix (see the next section).

Re-encoding an mp3 as an mp3 always costs a little quality, no matter the
bitrate. Choose FLAC if you want none of that.

## How the processing works

| Case | Filter chain |
| --- | --- |
| Classic slowed/nightcore (pitch follows tempo) | `asetrate=SR*F,aresample=SR` |
| Keep the pitch, no rubberband | `atempo=F` (chained when needed) |
| Keep the pitch, rubberband available | `rubberband=tempo=F:pitch=1` |
| Explicit pitch shift at unchanged tempo | `asetrate,aresample` plus a compensating `atempo` |

`atempo` only accepts 0.5-2.0, so anything outside that range is chained
automatically (0.4 becomes `atempo=0.5,atempo=0.8`) - you never have to think
about it. `--engine auto` picks rubberband when tempo and pitch move
independently, and the cheaper classic chain otherwise.

### The reverb

A naive `aecho=0.8:0.88:60:0.4` sums the dry signal with delayed copies of
itself. Low frequencies stay correlated across those delays, so the bass piles
up and the sum runs past 0 dBFS: the result sounds bass-boosted and distorted,
especially on vocal-heavy or already loud masters.

audiofx builds the reverb as a proper send/return instead:

```
[0:a] <time/pitch>, asplit=2 [dry][send];
[send] highpass=f=200:poles=2,          # low end never enters the tail
       aecho=1:1:29|41:0.5|0.42,        # two chained stages ->
       aecho=1:1:71|113:0.42|0.32,      #   4 taps become 9 dense echoes
       lowpass=f=7000,                  # damping, like a real room
       volume=<mix / power gain>        # tail normalised, so "mix" is honest
       [wet];
[dry][wet] amix=inputs=2:normalize=0,   # dry signal is never touched
           volume=1/(1+mix),            # headroom for the wet signal
           alimiter=limit=0.98          # safety net, silent in normal use
           [out]
```

Measured on a loud pop master (48 kHz mp3, slowed to 0.85):

| | peak | samples at/above -0.5 dBFS | bass-vs-treble balance |
| --- | --- | --- | --- |
| source | 0.00 dB | 67475 | +3.3 dB |
| naive `aecho` chain | 0.00 dB (clipping) | 14126 | +5.3 dB (boomy) |
| audiofx | **-0.20 dB** (no clipping) | **3846** | **+2.3 dB** |

If a track still sounds too heavy, raise **bass cut** (200 Hz by default) or
lower the reverb amount. Slowing a song down moves the whole spectrum lower on
its own, so some extra weight is inherent to the effect - the reverb just
should not add to it.

`audiofx/assets/ir/` holds two impulse responses (`room_small.wav`,
`hall_large.wav`) generated synthetically from decaying pink noise, so they
carry no third-party rights.

## Command line

### `convert <file>`

| Flag | Description |
| --- | --- |
| `-o, --output DIR` | output folder (default: next to the input) |
| `--mode {slow,speed,reverb,slowed_reverb}` | effect (optional when `--preset` is given) |
| `--factor F` | tempo multiplier; 0.85 = 15% slower, 1.25 = 25% faster |
| `--pitch-shift N` | pitch offset in semitones (`-2`, `+3.5`) |
| `--preserve-pitch` | keep the pitch while the tempo changes |
| `--preset NAME` | preset from `presets.yaml` |
| `--engine {auto,classic,rubberband}` | time/pitch engine (default `auto`) |
| `--reverb-size {small,medium,large}` | reverb tap layout |
| `--reverb-mix 0-1` | how much reverb is mixed in (default 0.35) |
| `--reverb-bass-cut HZ` | high-pass on the reverb send (default 200, 0 disables) |
| `--reverb-damping HZ` | low-pass on the reverb send (default 7000, 0 disables) |
| `--ir-file X.wav` | impulse response for convolution reverb (afir) |
| `--format mp3` | change the output extension |
| `--bitrate 192k` | output bitrate (default: the source bitrate) |
| `--resampler {auto,soxr,swr}` | resampler; `auto` prefers soxr |
| `--no-overwrite` | do not replace existing output files |
| `--no-metadata` | skip tag and cover copying |
| `--dry-run` | print the ffmpeg command instead of running it |
| `-q, --quiet` | suppress progress output |

### `batch <folder>`

Same flags plus:

| Flag | Description |
| --- | --- |
| `--recursive` | include subfolders |
| `--ext .mp3 .wav` | extensions to process (default: mp3, wav, flac, m4a, aac, ogg, opus, wma) |

Without `-o` the output goes to `<folder>_audiofx`, mirroring the subfolder
layout. A failing file does not stop the run; the command exits with 1 if any
file failed.

### `presets list` / `presets show <name>` / `check` / `gui`

`--presets-file X.yaml` points at your own preset file. `check` reports the
ffmpeg/ffprobe paths and which filters are available. `gui --songs ... -o ...`
opens the interface with different folders.

## Presets

| Preset | Mode | Factor | Description |
| --- | --- | --- | --- |
| `default` | slowed_reverb | 0.85 | Classic slowed + reverb |
| `slowed` | slow | 0.85 | Slow down only, pitch drops with it |
| `deep_slowed` | slow | 0.75 | Heavier slowdown |
| `dreamy` | slowed_reverb | 0.80 | Long, wide reverb tail |
| `nightcore` | speed | 1.25 | Faster and higher |
| `sped_up` | speed | 1.15 | Faster, pitch unchanged |
| `reverb_room` | reverb | 1.00 | Small room, tempo untouched |
| `reverb_hall` | reverb | 1.00 | IR convolution hall |

Add your own by following [audiofx/presets.yaml](audiofx/presets.yaml); every
key is documented at the top of that file.

## Tags

After the conversion, title, artist, album, date, genre, track number and the
cover image are copied to the output with mutagen - across container changes
too (mp3 to flac). Failures are reported as warnings and never abort the
conversion. Use `--no-metadata` to skip it.

## Optional: downloading with spotdl

The interface has an optional box that shells out to
[spotdl](https://github.com/spotdl/spotify-downloader) and saves into `songs/`.
spotdl is **not** a dependency of this project - it is not installed, bundled or
vendored here, and when it is missing the box is simply disabled:

```bash
pip install spotdl        # only if you want that box
```

You are responsible for what you download and for having the rights to it.
Downloading commercial music you do not own generally violates the streaming
services' terms of use and copyright law in most countries. Nothing about this
feature changes that.

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest
```

The tests run against `tests/fixtures/sample.wav` (a 2 second 440 Hz sine) and
check the generated filter graphs, the output durations against the requested
factor, and that the reverb cannot clip. Tests that need ffmpeg skip themselves
when it is missing; the GUI tests skip when no display is available.

## Credits

- [FFmpeg](https://ffmpeg.org/) - all audio processing (LGPL/GPL depending on the build)
- [mutagen](https://github.com/quodlibet/mutagen) - tag and cover handling (GPL-2.0)
- [PyYAML](https://pyyaml.org/) - preset files (MIT)
- [spotdl](https://github.com/spotdl/spotify-downloader) - optional download box (MIT)

## License

[MIT](LICENSE). This project only transforms files you already have; it does not
grant you any rights to the audio you run through it.

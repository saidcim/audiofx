# Design notes

Why the code looks the way it does. The user-facing documentation lives in the
[README](../README.md).

## Scope

A local tool that produces slowed / sped up / reverb versions of audio files
you already have. Distribution is explicitly out of scope: uploading music or
remixes to podcast platforms is not something this project does or helps with.

## Why raw ffmpeg command lines

`subprocess` with hand-built argument lists instead of a binding library. The
filter graph is the interesting part of this project, so it is worth keeping it
visible, string-comparable and testable without ffmpeg installed. Every graph
builder in `ffmpeg_runner.py` is a pure function; only `convert_file` touches
the process.

## Time and pitch

Both are expressed as one pair: `tempo` and `pitch_ratio`.

- `pitch_ratio == tempo` - the classic slowed/nightcore sound; `asetrate` plus
  `aresample` does it in one step and costs nothing.
- `pitch_ratio == 1` - tempo only; `rubberband` when the build has it,
  otherwise a chain of `atempo` factors.
- Anything else - `asetrate` for the pitch, then `atempo` to put the tempo back
  where it was asked to be. The compensation uses the *rounded* sample rate
  that ffmpeg actually gets, not the ideal one.

`atempo` is limited to 0.5-2.0, so `atempo_chain()` splits any ratio into
factors inside that window. The tests assert the product equals the request.

## Reverb: send/return, not a sum

The first implementation was `aecho=0.8:0.88:60:0.4`. It sounded bass-boosted
and distorted on real music, and measurement showed why:

- one delay tap at 60 ms comb-filters the signal, and bass stays correlated
  across that delay, so the low end adds constructively;
- `aecho` mixes dry and wet internally, and with those gains the sum peaks at
  roughly `(0.8 + 0.8 * 0.4) * 0.88 = 0.99` before any correlation is taken into
  account - real material pushes it past full scale and clips.

The current design splits the signal instead:

1. `asplit` into a dry path and a send;
2. high-pass the send (200 Hz by default) so the tail carries no bass;
3. build the tail from two chained `aecho` stages - chaining convolves the taps,
   so 2+2 taps become 9 echoes, which is what makes it sound like a room rather
   than a slap-back;
4. low-pass the send for damping;
5. scale the tail by `mix / echo_power_gain(...)`, where the power gain is
   `prod(sqrt(1 + sum(decay^2)))` per stage - without it, adding taps would
   quietly make the reverb louder;
6. `amix` with `normalize=0`, trim by `1/(1+mix)` to hand the wet signal its
   headroom, and finish with `alimiter=limit=0.98:level=0` as a guarantee that
   nothing can clip even when every tap lines up (a sustained full-scale tone).

`test_reverb_never_clips_even_at_full_scale` pins step 6 down; the rest is
covered by graph-shape tests.

The IR path swaps step 3 for `afir=dry=0:wet=1` with the impulse response as a
second input. `afir` normalises the IR itself, so step 5 is skipped there.

## Quality choices

- `aresample` uses `resampler=soxr:precision=28` when the build supports it.
- Lossy output defaults to the source bitrate, and the GUI raises that to at
  least 320 kbps; lossless output is one dropdown away.
- ffmpeg's `-map_metadata` runs anyway, and mutagen re-applies the common tags
  and the cover afterwards so container changes do not lose them.

## Layout

```
audiofx/
  ffmpeg_runner.py   filter graphs, probing, process handling
  presets.py         presets.yaml -> validated Preset objects
  metadata.py        mutagen tag/cover copying (never raises)
  downloader.py      optional spotdl wrapper for the GUI box
  cli.py             argparse front end
  gui.py             Tkinter front end
tests/               unit tests + ffmpeg integration tests
```

The GUI holds no audio logic: it builds a `JobOptions`, turns it into the same
`FxSpec` the CLI uses, and calls `convert_file`.

## Acceptance criteria

- `audiofx convert song.mp3 --mode slowed_reverb --preset default` produces a
  valid file.
- Out-of-range `atempo` factors are chained automatically.
- `batch` walks a folder and mirrors its structure into the output folder.
- A missing ffmpeg produces a readable error, never a silent failure.
- The reverb never clips the output.
- `pytest` passes.

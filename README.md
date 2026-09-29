# GBA Video Player - Empty Template

A minimal, reusable Game Boy Advance video player. No menu, no secrets, no
baked-in content - just one looping video with sound, built as a plain 64MB
ROM. Drop in your own clip and rebuild.

Ships with a tiny procedurally-generated placeholder clip (a color-cycling
ring) so the project builds and runs out of the box with zero real assets
checked in.

## Playing

- The video loops forever.
- **START** - pause / resume
- **A + Right** - fast-forward
- **A + Left** - rewind

## Replacing the video

Requires python3, Pillow, numpy, and ffmpeg on PATH.

```
python3 tools/gbatool.py video path/to/your_clip.mp4
```

This overwrites `frames1.bin`, `frames2.bin`, `frames_idx.bin`,
`palette.bin`, `audio.bin`, and `audio_state.bin` in the project root with
your video, encoded to the format `main.c` expects (120x68 @ 5fps, RLE
compressed frames, mono 4-bit ADPCM audio at ~9078.5 Hz).

Useful flags:
- `--stretch` - stretch to fill 120x68 instead of letterboxing
- `--gain 1.5` - boost audio volume
- `--dither` - Floyd-Steinberg dither the picture (can help banding, costs a
  little sharpness)

To regenerate the built-in placeholder clip instead:

```
python3 tools/gbatool.py placeholder
```

The tool prints the total asset size against the 64MB ROM limit when it's
done - a long or high-motion clip is easy to blow the budget on, so check
that output.

## Building

With [devkitPro](https://devkitpro.org/) / devkitARM installed:

```
make
```

produces `video.gba`. A GitHub Actions workflow (`.github/workflows/build.yml`)
also builds it automatically on every push and uploads `video.gba` as an
artifact, so you don't need a local toolchain at all.

## How it works

- `main.c` decodes RLE-compressed 120x68 8bpp frames into a scratch buffer,
  doubles them to 240x136 into a Mode 4 VRAM page, and page-flips each frame.
- Audio is 4-bit ADPCM, decoded a "chunk" (304 samples) at a time into a
  double buffer, DMA'd to FIFO A at ~9078.5 Hz, refilled from the vblank
  IRQ. It's driven by the same 5fps clock as the video, so picture and sound
  never drift.
- `frames_idx.bin` holds a byte offset per frame into the RLE stream, so any
  frame can be seeked to directly (used for fast-forward/rewind) without
  decoding every frame before it.
- `audio_state.bin` holds the ADPCM decoder state (predictor + step index)
  at the start of every chunk, so seeking resumes audio bit-exactly instead
  of restarting the codec from silence.
- Frame data is split across `frames1.bin` + `frames2.bin` purely so no
  single file exceeds GitHub's 25MB upload cap; `main.c` `.incbin`s them
  back to back as one stream.

## ROM budget

The ROM is fixed at 64MB (`gbafix`). Asset size scales with clip length,
motion (RLE compresses static scenes far better than busy ones), and
palette complexity. `gbatool.py` reports the total after every encode -
trim length or resolution of the source clip if you're over budget.

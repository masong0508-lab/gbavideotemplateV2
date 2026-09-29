#!/usr/bin/env python3
"""
gbatool.py - asset converter for the GBA video-player template.

Sub-commands
  video <input.mp4>       video+audio -> frames/palette/audio .bin
  placeholder              regenerate the built-in test-pattern clip

Needs: python3, Pillow, numpy, and ffmpeg on PATH (ffmpeg only for `video`).

Formats (must match main.c):
  frames  : 120x68, 8-bit palette indices, 5 fps, RLE-compressed, frames back to back
  palette : 256 x u16 little-endian, GBA BGR555 (shared by all frames)
  audio   : 4-bit ADPCM, mono, ~9078.5 Hz, 152 bytes per chunk (304 samples), low nibble first
"""
import argparse, math, os, struct, subprocess, sys
import numpy as np
from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VID_W, VID_H = 120, 68
FRAME_BYTES = VID_W * VID_H
FPS_NUM, FPS_DEN = 5486, 65536          # frame index = vblank * 5486 / 65536  (~5 fps at 59.7275 Hz)
AUDIO_HZ = 16777216 / 1848              # 9078.5 Hz (timer period 1848)
SAMPLES_PER_CHUNK = 304
CHUNK_BYTES = SAMPLES_PER_CHUNK // 2
ROM_LIMIT = 64 * 1024 * 1024
GITHUB_FILE_LIMIT = 24 * 1024 * 1024    # stay safely under GitHub's 25MB single-file cap

STEP = [7,8,9,10,11,12,13,14,16,17,19,21,23,25,28,31,34,37,41,45,50,55,60,66,73,80,88,97,107,118,130,143,157,173,190,209,230,253,279,307,337,371,408,449,494,544,598,658,724,796,876,963,1060,1166,1282,1411,1552,1707,1878,2066,2272,2499,2749,3024,3327,3660,4026,4428,4871,5358,5894,6484,7132,7847,8631,9494,10442,11487,12635,13899,15289,16818,18500,20350,22385,24623,27086,29794,32767]
IDX = [-1, -1, -1, -1, 2, 4, 6, 8]

# ---------------------------------------------------------------- ADPCM
def adpcm_encode(samples):
    """samples: iterable of int16. Mirrors decode_chunk() in main.c exactly
    (including the 'pred -= pred >> 9' leak) so encoder and player never drift."""
    pred = 0; sidx = 0
    out = bytearray(); low = None
    for s in samples:
        s = int(s)
        step = STEP[sidx]
        d = s - pred; nib = 0
        if d < 0: nib = 8; d = -d
        st = step
        if d >= st: nib |= 4; d -= st
        st >>= 1
        if d >= st: nib |= 2; d -= st
        st >>= 1
        if d >= st: nib |= 1
        diff = step >> 3
        if nib & 1: diff += step >> 2
        if nib & 2: diff += step >> 1
        if nib & 4: diff += step
        pred = pred - diff if nib & 8 else pred + diff
        pred -= pred >> 9
        pred = max(-32768, min(32767, pred))
        sidx = max(0, min(88, sidx + IDX[nib & 7]))
        if low is None: low = nib
        else: out.append(low | (nib << 4)); low = None
    return bytes(out)

def adpcm_states(ab):
    """Decoder state at the start of every chunk -> u32 each: pred(s16) | sidx<<16.
    main.c loads this after a seek so audio resumes bit-exactly as if it had played on."""
    pred = 0; sidx = 0; out = []
    for c in range(len(ab) // CHUNK_BYTES):
        out.append((pred & 0xFFFF) | (sidx << 16))
        for b in ab[c * CHUNK_BYTES:(c + 1) * CHUNK_BYTES]:
            for nib in (b & 15, b >> 4):
                step = STEP[sidx]; diff = step >> 3
                if nib & 1: diff += step >> 2
                if nib & 2: diff += step >> 1
                if nib & 4: diff += step
                pred = pred - diff if nib & 8 else pred + diff
                pred -= pred >> 9
                pred = max(-32768, min(32767, pred))
                sidx = max(0, min(88, sidx + IDX[nib & 7]))
    return struct.pack(f'<{len(out)}I', *out)

def chunks_for_frames(nframes):
    vblanks = math.ceil(nframes * FPS_DEN / FPS_NUM)
    return max(1, math.ceil(vblanks / 2))

def encode_audio(pcm16, nframes):
    """pcm16: int16 mono @ AUDIO_HZ. Padded/trimmed to exactly what the video needs."""
    want = chunks_for_frames(nframes) * SAMPLES_PER_CHUNK
    pcm = np.zeros(want, dtype=np.int16)
    n = min(want, len(pcm16)); pcm[:n] = pcm16[:n]
    return adpcm_encode(pcm)

# ---------------------------------------------------------------- palettes / frames
def bgr555(rgb_triplets):
    words = []
    for r, g, b in rgb_triplets:
        words.append((r >> 3) | ((g >> 3) << 5) | ((b >> 3) << 10))
    words += [0] * (256 - len(words))
    return struct.pack('<256H', *words[:256])

def pal_rgb(img_p, count=256):
    flat = img_p.getpalette() or []
    flat = flat + [0] * (768 - len(flat))
    return [tuple(flat[i*3:i*3+3]) for i in range(count)]

def rle_encode_frame(px):
    """px: flat uint8 array of FRAME_BYTES palette indices -> RLE bytes (run,value pairs, run 1-255)."""
    out = bytearray()
    i = 0; n = len(px)
    while i < n:
        v = px[i]; j = i + 1
        limit = min(n, i + 255)
        while j < limit and px[j] == v: j += 1
        out.append(j - i); out.append(int(v))
        i = j
    return bytes(out)

def encode_frames(frames, dither):
    """frames: list of 120x68 RGB PIL images -> (frames_rle_bytes, index_bytes, palette_bytes).
    Frames are RLE-compressed since raw indices don't fit a long video in the ROM; index[] holds
    the byte offset of each frame in the compressed stream, index[n] = total length, so main.c
    can seek to any frame without decoding the ones before it."""
    step = max(1, len(frames) // 64)
    sample = frames[::step][:64]
    cols = 8
    rows = math.ceil(len(sample) / cols)
    sheet = Image.new('RGB', (cols * VID_W, rows * VID_H))
    for i, f in enumerate(sample):
        sheet.paste(f, ((i % cols) * VID_W, (i // cols) * VID_H))
    pal_img = sheet.quantize(colors=256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
    d = Image.Dither.FLOYDSTEINBERG if dither else Image.Dither.NONE
    buf = bytearray()
    offsets = [0]
    for f in frames:
        q = f.quantize(palette=pal_img, dither=d)
        px = np.asarray(q, dtype=np.uint8).reshape(-1)
        buf += rle_encode_frame(px)
        offsets.append(len(buf))
    idx = struct.pack(f'<{len(offsets)}I', *offsets)
    return bytes(buf), idx, bgr555(pal_rgb(pal_img))

def write(name, data):
    with open(os.path.join(ROOT, name), 'wb') as f: f.write(data)
    print(f'  wrote {name:16s} {len(data):>10,} bytes')

def write_frames_split(data):
    """Frame data is always written as frames1.bin + frames2.bin (matching the fixed
    two-part .incbin pair in main.c), so no single file exceeds GitHub's 25MB cap.
    Two 24MB parts cover up to 48MB, well beyond the ROM limit, so 2 is always enough;
    part 2 is written even when empty so main.c's .incbin still finds it."""
    p1 = data[:GITHUB_FILE_LIMIT]
    p2 = data[GITHUB_FILE_LIMIT:]
    write('frames1.bin', p1)
    write('frames2.bin', p2)

def check_size():
    total = 0
    for name in ('frames1.bin', 'frames2.bin', 'frames_idx.bin', 'palette.bin', 'audio.bin', 'audio_state.bin'):
        p = os.path.join(ROOT, name)
        if os.path.exists(p): total += os.path.getsize(p)
    print(f'  asset total: {total:,} bytes ({total/1024/1024:.1f} MiB) of a 64 MiB ROM')
    if total > ROM_LIMIT: print('  WARNING: over the 64MiB ROM limit!')

# ---------------------------------------------------------------- ffmpeg input
def ffmpeg_frames(path, stretch):
    if stretch: vf = f'fps=5,scale={VID_W}:{VID_H}:flags=lanczos'
    else:       vf = (f'fps=5,scale={VID_W}:{VID_H}:force_original_aspect_ratio=decrease:flags=lanczos,'
                      f'pad={VID_W}:{VID_H}:(ow-iw)/2:(oh-ih)/2:black')
    raw = subprocess.run(['ffmpeg', '-v', 'error', '-i', path, '-an', '-vf', vf,
                          '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'],
                         check=True, stdout=subprocess.PIPE).stdout
    n = len(raw) // (VID_W * VID_H * 3)
    arr = np.frombuffer(raw[:n * VID_W * VID_H * 3], dtype=np.uint8).reshape(n, VID_H, VID_W, 3)
    return [Image.fromarray(a) for a in arr]

def ffmpeg_audio(path, gain):
    r = subprocess.run(['ffmpeg', '-v', 'error', '-i', path, '-vn', '-ac', '1', '-ar', str(round(AUDIO_HZ)),
                        '-af', f'volume={gain}', '-f', 's16le', '-'], stdout=subprocess.PIPE)
    return np.frombuffer(r.stdout, dtype='<i2') if r.returncode == 0 else np.zeros(0, dtype=np.int16)

# ---------------------------------------------------------------- commands
def build_video(frames, pcm, dither):
    fb, ib, pb = encode_frames(frames, dither)
    ab = encode_audio(pcm, len(frames))
    write_frames_split(fb); write('frames_idx.bin', ib); write('palette.bin', pb); write('audio.bin', ab)
    write('audio_state.bin', adpcm_states(ab))
    secs = len(frames) / 5
    raw = len(frames) * FRAME_BYTES
    print(f'  {len(frames)} frames = {secs:.1f} s ({secs/60:.1f} min), {len(ab)//CHUNK_BYTES} audio chunks')
    print(f'  frame data: {len(fb):,} bytes RLE-compressed (was {raw:,} raw, {len(fb)/raw*100:.0f}%)')

def cmd_video(a):
    frames = ffmpeg_frames(a.input, a.stretch)
    if not frames: sys.exit('ffmpeg produced no frames - is that a valid video file?')
    build_video(frames, ffmpeg_audio(a.input, a.gain), a.dither)
    check_size()

def cmd_placeholder(a):
    """A tiny procedurally-generated test clip so the ROM builds and runs with no
    real assets checked in: a colour-cycling ring with a bouncing dot, ~4 seconds."""
    import random
    n = 20
    frames = []
    for i in range(n):
        img = Image.new('RGB', (VID_W, VID_H), (10, 10, 20))
        d = ImageDraw.Draw(img)
        hue = (i / n) * 360
        import colorsys
        r, g, b = [int(c * 255) for c in colorsys.hsv_to_rgb(hue / 360, 0.7, 0.9)]
        cx, cy = VID_W // 2, VID_H // 2
        rad = 18 + 4 * math.sin(i / n * 2 * math.pi * 2)
        d.ellipse([cx - rad, cy - rad, cx + rad, cy + rad], outline=(r, g, b), width=3)
        dx = int(cx + (rad - 6) * math.cos(i / n * 2 * math.pi))
        dy = int(cy + (rad - 6) * math.sin(i / n * 2 * math.pi))
        d.ellipse([dx - 3, dy - 3, dx + 3, dy + 3], fill=(255, 255, 255))
        d.text((6, 6), 'TEMPLATE', fill=(255, 255, 255))
        frames.append(img)
    t = np.linspace(0, 1, chunks_for_frames(n) * SAMPLES_PER_CHUNK, endpoint=False)
    pcm = (np.sin(2 * math.pi * 440 * t * (n / 5)) * 6000).astype(np.int16)
    build_video(frames, pcm, dither=False)
    check_size()

# ---------------------------------------------------------------- CLI
def main():
    p = argparse.ArgumentParser(description='GBA video-player template asset tool')
    sub = p.add_subparsers(dest='cmd', required=True)

    pv = sub.add_parser('video', help='video+audio -> frames/palette/audio .bin')
    pv.add_argument('input'); pv.add_argument('--stretch', action='store_true')
    pv.add_argument('--gain', default='1.0'); pv.add_argument('--dither', action='store_true')
    pv.set_defaults(func=cmd_video)

    pp = sub.add_parser('placeholder', help='regenerate the built-in test-pattern clip')
    pp.set_defaults(func=cmd_placeholder)

    a = p.parse_args()
    a.func(a)

if __name__ == '__main__':
    main()

"""Steps before editing: create a project, transcribe the source, and look at it."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .common import (TONEMAP, CaptionCutError, Project, audio_streams, clock, display_size, ffmpeg,
                     frame_rate, is_hdr, probe, require, rotation, run, srt_time, video_stream)

DEFAULT_MODEL = Path('~/.cache/whisper-cpp/ggml-large-v3-turbo-q5_0.bin').expanduser()
ZH_TW_PROMPT = '以下是繁體中文的句子，包含標點符號。'


def describe(source):
    info = probe(source)
    video = video_stream(info)
    width, height = display_size(video)
    return {
        'duration': round(float(info['format']['duration']), 3),
        'size': f'{width}x{height}',
        'orientation': 'portrait' if height > width else 'landscape',
        'fps': round(frame_rate(video), 3),
        'rotation': rotation(video),
        'hdr': is_hdr(video),
        'color_transfer': video.get('color_transfer'),
        'audio_streams': len(audio_streams(info)),
    }


def init(source, folder=None, style='playful', force=False):
    source = Path(source).expanduser().resolve()
    require(source.is_file(), f'Source video not found: {source}')
    folder = Path(folder).expanduser().resolve() if folder else source.parent / f'{source.stem}-edit'
    target = folder / 'project.json'
    require(force or not target.exists(), f'{target} already exists (use --force to overwrite).')
    folder.mkdir(parents=True, exist_ok=True)
    facts = describe(source)
    require(facts['audio_streams'] > 0, 'The source has no audio stream.')
    project = {
        'source': os.path.relpath(source, folder),
        'title': source.stem,
        'output': f'{source.stem}-字幕版.mp4',
        'fps': 30,
        'style': style,
        'loudness': {'I': -16, 'TP': -1.5, 'LRA': 11},
        'source_info': facts,
        'cuts': [],
        'captions': [],
        'notes': [],
        'titles': [],
    }
    target.write_text(json.dumps(project, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'project': str(folder), **facts}, ensure_ascii=False, indent=2))
    return folder


def _whisper():
    binary = os.environ.get('WHISPER_CLI') or shutil.which('whisper-cli')
    require(binary, 'whisper-cli not found. Install whisper.cpp (macOS: brew install whisper-cpp) or set WHISPER_CLI.')
    return binary


def _to_traditional(text):
    try:
        import opencc  # optional
    except ImportError:
        return text
    return opencc.OpenCC('s2twp').convert(text)


PUNCTUATION = set('，。！？、；：,.!?;:…')


def extract_wav(source, out, start=0.0, end=None):
    cut = ['-ss', f'{start:.3f}'] + (['-t', f'{end - start:.3f}'] if end is not None else [])
    run([ffmpeg(), '-hide_banner', '-nostdin', '-y', '-v', 'error', *cut, '-i', str(source),
         '-map', '0:a:0', '-vn', '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', str(out)])


def speech_regions(wav, noise_db=-35, min_silence=0.35, pad=0.08):
    """Exact speech spans from FFmpeg silencedetect; the most reliable cut points there are."""
    log = subprocess.run([ffmpeg(), '-hide_banner', '-nostdin', '-i', str(wav), '-af',
                          f'silencedetect=noise={noise_db}dB:d={min_silence}', '-f', 'null', '-'],
                         capture_output=True, text=True).stderr
    duration = float(probe(wav)['format']['duration'])
    starts = [float(x) for x in re.findall(r'silence_start: ([\d.]+)', log)]
    ends = [float(x) for x in re.findall(r'silence_end: ([\d.]+)', log)]
    regions, cursor = [], 0.0
    for s, e in zip(starts, ends + [duration] * (len(starts) - len(ends))):
        if s - cursor > 0.05:
            regions.append((max(0.0, cursor - pad), min(duration, s + pad)))
        cursor = e
    if duration - cursor > 0.05:
        regions.append((max(0.0, cursor - pad), duration))
    return [(round(a, 2), round(b, 2)) for a, b in regions]


def _whisper_tokens(wav, model, language, prompt, base, log):
    cmd = [_whisper(), '-m', str(model), '-l', language, '-ojf', '-of', str(base), '-f', str(wav)]
    if prompt:
        cmd += ['--prompt', prompt]
    run(cmd, log=log)
    raw = json.loads(Path(f'{base}.json').read_text())
    return [{'text': t['text'], 'from': t['offsets']['from'] / 1000, 'to': t['offsets']['to'] / 1000,
             'p': t.get('p', 1.0)}
            for segment in raw.get('transcription', []) for t in segment.get('tokens', [])
            if not t['text'].startswith('[_')]


def sentences(tokens, offset=0.0, clamp=None):
    """Split whisper tokens into caption-sized chunks at punctuation, with source times."""
    chunks, current = [], []

    def flush():
        words = [t for t in current if t['text'].strip() and not set(t['text'].strip()) <= PUNCTUATION]
        text = _to_traditional(''.join(t['text'] for t in current).strip())
        if words and text:
            a, b = offset + words[0]['from'], offset + max(t['to'] for t in words)
            if clamp:
                a, b = max(a, clamp[0]), min(max(b, a + 0.2), clamp[1])
            chunks.append({'start': round(a, 2), 'end': round(b, 2), 'text': text,
                           'confidence': round(min(t['p'] for t in words), 2)})
        current.clear()

    for token in tokens:
        current.append(token)
        if token['text'].strip() and token['text'].strip()[-1] in PUNCTUATION:
            flush()
    flush()
    return chunks


def transcribe(folder, model=None, language='zh', start=None, end=None, name=None, prompt=None,
               by_pauses=False):
    """Run whisper.cpp on the source (or a range of it); write JSON, SRT and a readable TXT.

    Every time is in source seconds, so a focused re-run of a doubtful range lines up with
    the full pass. Whisper's own token times can drift around long pauses; `by_pauses`
    transcribes each silence-delimited speech region on its own, which keeps every line
    inside exact speech boundaries.
    """
    project = Project(folder)
    model = Path(model or os.environ.get('WHISPER_MODEL') or DEFAULT_MODEL).expanduser()
    require(model.is_file(), f'Whisper model not found: {model}. Download one from '
            'https://huggingface.co/ggerganov/whisper.cpp or set WHISPER_MODEL.')
    name = name or ('source-transcript' if start is None else f'range-{start:g}-{end:g}')
    offset = float(start or 0)
    if prompt is None and language == 'zh':
        prompt = ZH_TW_PROMPT
    work = project.path('asr')
    work.mkdir(exist_ok=True)
    wav = work / f'{name}.wav'
    extract_wav(project.source, wav, offset, float(end) if end is not None else None)
    regions = [(a + offset, b + offset) for a, b in speech_regions(wav)]
    segments = []
    if by_pauses:
        for index, (a, b) in enumerate(regions):
            piece = work / f'{name}-region-{index:03d}.wav'
            extract_wav(project.source, piece, a, b)
            tokens = _whisper_tokens(piece, model, language, prompt, work / f'{name}-region-{index:03d}',
                                     work / f'{name}-region-{index:03d}.log')
            segments += sentences(tokens, a, clamp=(a, b))
    else:
        tokens = _whisper_tokens(wav, model, language, prompt, work / f'{name}-raw', work / f'{name}.log')
        segments = sentences(tokens, offset)
    project.write_json(f'{name}.json', {'segments': segments, 'speech_regions': regions})
    project.path(f'{name}.srt').write_text(''.join(
        f"{i}\n{srt_time(s['start'])} --> {srt_time(s['end'])}\n{s['text']}\n\n" for i, s in enumerate(segments, 1)))
    lines = [f"[{s['start']:7.2f} → {s['end']:7.2f}] {s['text']}" + ('  (?)' if s['confidence'] < 0.5 else '')
             for s in segments]
    lines += ['', '# speech regions (silencedetect; exact, use for cut points)']
    lines += [f'{a:7.2f} → {b:7.2f}  ({b - a:.2f}s)' for a, b in regions]
    project.path(f'{name}.txt').write_text('\n'.join(lines) + '\n')
    print(f'{len(segments)} lines, {len(regions)} speech regions → {project.path(name + ".txt")}')
    return segments


def _label_font(size=20):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def contact_sheet(frames, out, columns=4, thumb=480):
    """frames: list of (label, PIL.Image). Writes one labelled grid image."""
    portrait = frames[0][1].height > frames[0][1].width
    cell_w, cell_h = (thumb * 9 // 16, thumb) if portrait else (thumb, thumb * 9 // 16)
    rows = (len(frames) + columns - 1) // columns
    canvas = Image.new('RGB', (columns * cell_w, rows * (cell_h + 30)), (22, 22, 22))
    draw = ImageDraw.Draw(canvas)
    font = _label_font()
    for index, (label, image) in enumerate(frames):
        image = image.convert('RGB')
        image.thumbnail((cell_w, cell_h))
        x, y = index % columns * cell_w, index // columns * (cell_h + 30)
        canvas.paste(image, (x + (cell_w - image.width) // 2, y))
        draw.text((x + 8, y + cell_h + 4), label, fill='white', font=font)
    canvas.save(out, quality=92)
    return out


def grab_frame(video, t, vf=None, cwd=None):
    cmd = [ffmpeg(), '-v', 'error', '-ss', f'{t:.3f}', '-i', str(video)]
    if vf:
        cmd += ['-vf', vf]
    cmd += ['-frames:v', '1', '-f', 'image2pipe', '-vcodec', 'png', '-']
    try:
        return Image.open(BytesIO(subprocess.check_output(cmd, cwd=cwd)))
    except subprocess.CalledProcessError as error:
        raise CaptionCutError(f'Could not grab a frame at {t:.2f}s') from error


def sheet(folder, every=None, times=None, name='source-contact-sheet.jpg'):
    """Thumbnails of the source at fixed intervals or given times, to help pick cuts."""
    project = Project(folder)
    info = probe(project.source)
    duration = float(info['format']['duration'])
    if not times:
        every = every or max(2.0, round(duration / 24, 1))
        times = [round(t * every, 2) for t in range(int(duration // every) + 1) if t * every < duration - 0.05]
    tone = project.get('grade', {})
    vf = (TONEMAP.format(saturation=tone.get('saturation', 0.9), gamma=tone.get('gamma', 1.03))
          if is_hdr(video_stream(info)) else None)
    frames = [(clock(t), grab_frame(project.source, t, vf)) for t in times]
    out = contact_sheet(frames, project.path(name))
    print(f'{len(frames)} frames → {out}')
    return out

#!/usr/bin/env python3
"""Make a synthetic source video plus a filled-in project.json to try captioncut on.

The "speaker" is the macOS `say` voice reading a few lines with long pauses between
them (the dead air the cuts remove). Without `say`, beeps stand in for speech.
Usage: python3 examples/demo/make_demo.py [--portrait] [--hdr] [--out DIR]
"""
import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from captioncut.common import ffmpeg, probe  # noqa: E402

LINES = [
    ('今天來試試看，半夜煮泡麵。', 'dialogue', 1.6),
    ('冰箱裡只剩一顆蛋，要加嗎？', 'question', 2.4),
    ('加！當然要加！', 'joke', 3.0),
    ('結果水又滾出來了。', 'reaction', 2.2),
    ('好啦，下次早點睡。', 'warm', 1.0),
]


def speak(text, path):
    if shutil.which('say'):
        voices = subprocess.run(['say', '-v', '?'], capture_output=True, text=True).stdout
        voice = next((v for v in ('Meijia', 'Tingting') if v in voices), None)
        subprocess.run(['say', *(['-v', voice] if voice else []), '-o', str(path), text], check=True)
        return True
    return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--portrait', action='store_true')
    parser.add_argument('--hdr', action='store_true', help='tag the video as HLG to exercise tone mapping')
    parser.add_argument('--out', default=str(Path(__file__).resolve().parent / 'build'))
    args = parser.parse_args()
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    ff = ffmpeg()
    pieces, cursor, spans = [], 0.6, []
    for index, (text, role, pause) in enumerate(LINES):
        clip = out / f'line-{index}.wav'
        aiff = out / f'line-{index}.aiff'
        if speak(text, aiff):
            subprocess.run([ff, '-v', 'error', '-y', '-i', str(aiff), '-ar', '48000', '-ac', '2', str(clip)], check=True)
            aiff.unlink()
        else:
            subprocess.run([ff, '-v', 'error', '-y', '-f', 'lavfi', '-i', 'sine=frequency=660:duration=1.4',
                            '-ar', '48000', '-ac', '2', str(clip)], check=True)
        length = float(probe(clip)['format']['duration'])
        spans.append((cursor, cursor + length, text, role))
        pieces.append((clip, cursor))
        cursor += length + pause
    total = cursor
    inputs, mix = [], []
    for i, (clip, start) in enumerate(pieces):
        inputs += ['-i', str(clip)]
        ms = round(start * 1000)
        mix.append(f'[{i + 1}:a]adelay={ms}|{ms}[a{i}]')
    mix.append(''.join(f'[a{i}]' for i in range(len(pieces))) + f'amix=inputs={len(pieces)}:normalize=0,'
               f'apad,atrim=duration={total:.3f}[aout]')
    size = '1080x1920' if args.portrait else '1920x1080'
    source = out / ('demo-source-hdr.mov' if args.hdr else 'demo-source.mov')
    # The HLG tag is only a label here (the pixels are SDR), enough to send the clip down the tone-map path.
    tags = ('color_primaries=bt2020:color_trc=arib-std-b67:colorspace=bt2020nc' if args.hdr
            else 'color_primaries=bt709:color_trc=bt709:colorspace=bt709')
    mix.append(f'[0:v]format=yuv420p,setparams={tags}[vout]')
    subprocess.run([ff, '-v', 'error', '-y', '-f', 'lavfi', '-i', f'testsrc2=size={size}:rate=30:duration={total:.3f}',
                    *inputs, '-filter_complex', ';'.join(mix), '-map', '[vout]', '-map', '[aout]',
                    '-c:v', 'libx264', '-crf', '20',
                    '-c:a', 'aac', '-b:a', '160k', '-t', f'{total:.3f}', str(source)], check=True)
    for clip, _ in pieces:
        clip.unlink()
    # Cuts keep each line with a little air around it; captions use source times.
    cuts = [{'start': round(max(0, a - 0.3), 2), 'end': round(b + 0.35, 2), 'purpose': text} for a, b, text, _ in spans]
    captions = [{'start': round(a, 2), 'end': round(b + 0.15, 2), 'text': text, 'role': role}
                for a, b, text, role in spans]
    project_dir = out / f'{source.stem}-edit'
    project_dir.mkdir(exist_ok=True)
    project = {
        'source': f'../{source.name}', 'title': '半夜泡麵 demo', 'output': f'{source.stem}-字幕版.mp4', 'fps': 30,
        'style': 'whatsub-inspired' if args.portrait else 'playful',
        'loudness': {'I': -16, 'TP': -1.5, 'LRA': 11},
        'cuts': cuts, 'captions': captions,
        'notes': [{'start': spans[2][0], 'end': spans[2][1] + 0.2, 'text': '（毫不猶豫）', 'x': 0.74, 'y': 0.62}],
        'titles': [{'clip': 0, 'text': '半夜泡麵挑戰', 'duration': 2.0}],
    }
    if args.portrait:
        for caption in project['captions']:
            caption['role'] = {'joke': 'punchline', 'reaction': 'emphasis'}.get(caption['role'], caption['role'])
    (project_dir / 'project.json').write_text(json.dumps(project, ensure_ascii=False, indent=2) + '\n')
    print(f'source  {source} ({total:.2f}s)')
    print(f'project {project_dir}')


if __name__ == '__main__':
    main()

"""Shared helpers: FFmpeg discovery, probing, project files and time formats."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
HDR_TRANSFERS = {'arib-std-b67', 'smpte2084'}

# iPhone footage is usually HLG. Tone-map to BT.709 SDR, then pull saturation back
# slightly; these values come from the edits this tool was built on.
TONEMAP = ('zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,'
           'tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,format=yuv420p,'
           'eq=saturation={saturation}:gamma={gamma}')
BT709 = 'setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709:range=tv'


class CaptionCutError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise CaptionCutError(message)


def _find_tool(name):
    env = os.environ.get(name.upper())
    if env:
        return env
    # Homebrew's slim `ffmpeg` lacks libass and zimg; prefer `ffmpeg-full` when installed.
    for prefix in ('/opt/homebrew/opt/ffmpeg-full/bin', '/usr/local/opt/ffmpeg-full/bin'):
        candidate = Path(prefix) / name
        if candidate.exists():
            return str(candidate)
    found = shutil.which(name)
    require(found, f'{name} not found. Install FFmpeg with libass and zimg (macOS: brew install ffmpeg-full).')
    return found


def ffmpeg():
    return _find_tool('ffmpeg')


def ffprobe():
    return _find_tool('ffprobe')


def run(cmd, log=None, cwd=None):
    """Run a command, sending output to `log` when given, and raise with its tail on failure."""
    if log is None:
        subprocess.run(cmd, check=True, cwd=cwd)
        return
    log = Path(log)
    with log.open('w') as handle:
        result = subprocess.run(cmd, stdout=handle, stderr=subprocess.STDOUT, cwd=cwd)
    if result.returncode:
        tail = '\n'.join(log.read_text(errors='replace').splitlines()[-15:])
        raise CaptionCutError(f'Command failed ({result.returncode}), see {log}:\n{tail}')


def probe(path):
    return json.loads(subprocess.check_output([
        ffprobe(), '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(path)
    ]))


def video_stream(info):
    return next(s for s in info['streams'] if s['codec_type'] == 'video')


def audio_streams(info):
    return [s for s in info['streams'] if s['codec_type'] == 'audio']


def rotation(stream):
    for side in stream.get('side_data_list', []):
        if 'rotation' in side:
            return int(side['rotation'])
    return int(stream.get('tags', {}).get('rotate', 0))


def display_size(stream):
    """Width and height after FFmpeg's autorotate."""
    w, h = stream['width'], stream['height']
    return (h, w) if abs(rotation(stream)) % 180 == 90 else (w, h)


def is_hdr(stream):
    return stream.get('color_transfer') in HDR_TRANSFERS


def frame_rate(stream):
    return float(Fraction(stream.get('avg_frame_rate') or '0/1') or Fraction(stream.get('r_frame_rate', '30/1')))


def load_loudnorm(log_path):
    text = Path(log_path).read_text(errors='replace')
    start = text.rfind('{')
    return json.loads(text[start:text.index('}', start) + 1])


def ass_time(t):
    cs = round(t * 100)
    return f'{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}'


def srt_time(t):
    ms = round(t * 1000)
    return f'{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}'


def clock(t):
    return f'{int(t // 60):02d}:{t % 60:05.2f}'


class Project:
    """A working folder holding project.json plus every generated file."""

    def __init__(self, folder):
        self.dir = Path(folder).expanduser().resolve()
        self.file = self.dir / 'project.json'
        require(self.file.is_file(), f'No project.json in {self.dir}. Run `captioncut init <video>` first.')
        self.data = json.loads(self.file.read_text())

    def __getitem__(self, key):
        return self.data[key]

    def get(self, key, default=None):
        return self.data.get(key, default)

    def path(self, name):
        return self.dir / name

    @property
    def source(self):
        source = Path(self.data['source']).expanduser()
        source = source if source.is_absolute() else (self.dir / source)
        require(source.is_file(), f'Source video not found: {source}')
        return source.resolve()

    @property
    def fps(self):
        return int(self.data.get('fps', 30))

    @property
    def output(self):
        return self.dir / self.data.get('output', 'final.mp4')

    def read_json(self, name):
        path = self.path(name)
        require(path.is_file(), f'{name} is missing; run the earlier step first.')
        return json.loads(path.read_text())

    def write_json(self, name, value):
        self.path(name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')

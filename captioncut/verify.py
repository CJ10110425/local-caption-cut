"""Technical checks on the finished file; writes delivery.json and a contact sheet."""
from __future__ import annotations

import hashlib
import re
import subprocess
from fractions import Fraction

import numpy as np

from .common import (CaptionCutError, Project, audio_streams, clock, display_size, ffmpeg, load_loudnorm, probe,
                     require, run, video_stream)
from .prepare import contact_sheet, grab_frame

SR = 8000
SEARCH_LAG = 0.100
MAX_LAG = 0.0301
MIN_CORRELATION = 0.90


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def pcm(path):
    return np.frombuffer(subprocess.check_output([
        ffmpeg(), '-v', 'error', '-i', str(path), '-map', '0:a:0', '-ac', '1', '-ar', str(SR), '-f', 'f32le', '-'
    ]), dtype=np.float32).astype(np.float64)


def check_audio(plan, source, output):
    """Find each clip's audio back in the source with normalised cross-correlation.

    Timing drift fails the check. A low score alone is only a warning: loudness
    normalisation can reshape short windows of applause or music enough to lower
    the score without anything being out of sync.
    """
    fps = plan['fps']
    checks, warnings = [], []
    for clip in plan['clips']:
        duration = clip['frames'] / fps
        padding = min(0.20, duration / 8)
        span = min(1.50, duration - 2 * padding)
        if span < 0.15:
            warnings.append(f"clip {clip['index']} too short to check audio")
            continue
        # Use the loudest of three windows, away from quiet lead-ins and the cut points.
        offsets = sorted({padding, (duration - span) / 2, duration - span - padding})
        rms, offset = max((float(np.std(source[round((clip['source_start'] + o) * SR):][:round(span * SR)])), o)
                          for o in offsets)
        if rms < 1e-6:
            warnings.append(f"clip {clip['index']} is silent; audio not checked")
            continue
        expected = clip['source_start'] + offset
        n = round(span * SR)
        actual = output[round((clip['timeline_start'] + offset) * SR):][:n].copy()
        require(len(actual) == n, f"Clip {clip['index']} output audio is truncated")
        actual -= actual.mean()
        low = max(0, round((expected - SEARCH_LAG) * SR))
        reference = source[low:min(len(source), round((expected + span + SEARCH_LAG) * SR))]
        require(len(reference) >= n, f"Clip {clip['index']} source audio is truncated")
        raw = np.correlate(reference, actual, mode='valid')
        sums = np.concatenate(([0.0], np.cumsum(reference)))
        squares = np.concatenate(([0.0], np.cumsum(reference * reference)))
        window = sums[n:] - sums[:-n]
        variance = squares[n:] - squares[:-n] - window ** 2 / n
        scores = raw / (np.linalg.norm(actual) * np.sqrt(np.maximum(variance, 1e-30)))
        best = int(np.argmax(scores))
        result = {'clip': clip['index'], 'sample_source_start': round(expected, 3), 'sample_seconds': round(span, 3),
                  'correlation': round(float(scores[best]), 5), 'offset_seconds': round((low + best) / SR - expected, 5)}
        checks.append(result)
        require(abs(result['offset_seconds']) <= MAX_LAG, f'Audio is out of sync by more than 30 ms: {result}')
        if result['correlation'] < MIN_CORRELATION:
            warnings.append(f"clip {clip['index']} audio correlation {result['correlation']} (< {MIN_CORRELATION}); "
                            'listen to it')
    return checks, warnings


def final_sheet(project, spoken, duration, out):
    count = 12
    mids = [(c['start'] + c['end']) / 2 for c in spoken]
    times = []
    # One frame per twelfth of the edit, preferring moments with a caption on screen.
    for i in range(count):
        left, right = duration * i / count, duration * (i + 1) / count
        inside = [t for t in mids if left <= t < right]
        chosen = min(inside, key=lambda t: abs(t - (left + right) / 2)) if inside else (left + right) / 2
        times.append(round(min(max(chosen, 0.05), duration - 0.05), 3))
    frames = [(clock(t), grab_frame(out, t)) for t in times]
    contact_sheet(frames, project.path('final-contact-sheet.jpg'), columns=3, thumb=640)
    return times


def verify(folder):
    project = Project(folder)
    out = project.output
    require(out.is_file(), f'Final render is missing: {out}')
    plan = project.read_json('edit-plan.json')
    events = project.read_json('caption-plan.json')
    fps = plan['fps']
    source_info, final_info = probe(project.source), probe(out)
    master = video_stream(probe(project.path('picture-master.mov')))
    videos = [s for s in final_info['streams'] if s['codec_type'] == 'video']
    audios = audio_streams(final_info)
    require(len(videos) == len(audios) == 1, 'Expected exactly one video and one audio stream')
    video, audio = videos[0], audios[0]
    require(display_size(video) == display_size(master), 'Final size differs from the picture master')
    require(Fraction(video['avg_frame_rate']) == fps, f'Final video is not {fps} fps')
    require(audio['codec_name'] == 'aac', 'Final audio is not AAC')
    require(all(video.get(k) == 'bt709' for k in ('color_space', 'color_transfer', 'color_primaries')),
            'Final video is not tagged BT.709 SDR')
    require(video.get('pix_fmt') == 'yuv420p', 'Final video is not yuv420p')
    frames = sum(c['frames'] for c in plan['clips'])
    duration = frames / fps
    require(int(video['nb_frames']) == frames, f"Frame count {video['nb_frames']} != planned {frames}")
    require(abs(float(video['duration']) - duration) < 0.001, 'Video duration differs from the cut plan')
    require(abs(float(video['duration']) - float(audio['duration'])) < 0.04, 'Audio and video lengths differ')
    source_duration = float(source_info['format']['duration'])
    cursor = 0.0
    for clip in plan['clips']:
        require(0 <= clip['source_start'] < clip['source_end'] <= source_duration + 1 / fps,
                f"Clip {clip['index']} lies outside the source")
        require(abs(clip['timeline_start'] - cursor) < 0.001, f"Gap or overlap before clip {clip['index']}")
        cursor += clip['frames'] / fps
    for e in events:
        require(0 <= e['start'] < e['end'] <= duration + 0.001, f"Caption outside the edit: {e['text']!r}")
        require(e['text'].strip(), 'Empty caption')
    spoken = [e for e in events if e['kind'] == 'caption']
    for previous, current in zip(spoken, spoken[1:]):
        require(current['start'] >= previous['end'] - 0.001,
                f"Captions overlap: {previous['text']!r} and {current['text']!r}")
    decoded = subprocess.run([ffmpeg(), '-v', 'error', '-xerror', '-i', str(out), '-map', '0:v:0', '-map', '0:a:0',
                              '-progress', 'pipe:1', '-nostats', '-f', 'null', '-'],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if decoded.returncode:
        raise CaptionCutError(f'Full decode failed:\n{decoded.stderr[-2000:]}')
    counts = re.findall(r'^frame=(\d+)\s*$', decoded.stdout, flags=re.MULTILINE)
    require(counts and int(counts[-1]) == frames and 'progress=end' in decoded.stdout,
            'Full decode did not reach every frame')
    target = {'I': -16, 'TP': -1.5, 'LRA': 11, **project.get('loudness', {})}
    log = project.path('final-loudness.log')
    run([ffmpeg(), '-hide_banner', '-v', 'info', '-i', str(out), '-map', '0:a:0', '-vn', '-af',
         f"loudnorm=I={target['I']}:TP={target['TP']}:LRA={target['LRA']}:print_format=json", '-f', 'null', '-'],
        log=log)
    loudness = load_loudnorm(log)
    require(float(loudness['input_tp']) < 0, 'Final audio clips at or above 0 dBTP')
    audio_checks, warnings = check_audio(plan, pcm(project.source), pcm(out))
    times = final_sheet(project, spoken, duration, out)
    delivery = {
        'status': 'passed_with_warnings' if warnings else 'passed',
        'note': 'Technical checks only. Watch the whole video before publishing it.',
        'file': out.name, 'sha256': sha256(out), 'source': project.source.name,
        'duration_seconds': duration, 'frames': frames, 'size': '{}x{}'.format(*display_size(video)),
        'captions': len(spoken), 'notes_and_titles': len(events) - len(spoken),
        'checks': {
            'full_decode': 'passed', 'frame_count': 'passed', 'bt709_sdr': 'passed', 'aac_audio': 'passed',
            'av_durations': 'passed', 'caption_timing': 'passed', 'loudness': loudness,
            'audio_matches_source': audio_checks,
        },
        'warnings': warnings, 'contact_sheet_times': times,
    }
    project.write_json('delivery.json', delivery)
    print(f"{delivery['status']}: {out.name}, {duration:.2f}s, {len(spoken)} captions, "
          f"{loudness['input_i']} LUFS / {loudness['input_tp']} dBTP")
    for warning in warnings:
        print('warning:', warning)
    return delivery

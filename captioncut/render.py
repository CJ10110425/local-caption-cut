"""Preview stills with captions, then the final two-pass-loudness render."""
from __future__ import annotations

import json

from .common import BT709, Project, clock, ffmpeg, load_loudnorm, require, run
from .prepare import contact_sheet, grab_frame


def preview_times(project, events, limit=12):
    wanted = project.get('preview')
    if wanted:
        times = [round((e['start'] + e['end']) / 2, 3) for text in wanted for e in events
                 if e['text'].replace('\\N', '') == text.replace('\n', '')]
        if times:
            return times[:limit]
    # Otherwise every note/title, then spoken captions spread across the edit.
    special = [e for e in events if e['kind'] != 'caption']
    spoken = [e for e in events if e['kind'] == 'caption']
    room = max(0, limit - len(special))
    step = max(1, len(spoken) // room) if room else len(spoken) + 1
    chosen = special[:limit] + spoken[::step][:room]
    times = []
    for t in sorted(round((e['start'] + e['end']) / 2, 3) for e in chosen):
        if not times or t - times[-1] > 0.5:  # a note and its caption often share a moment
            times.append(t)
    return times


def preview(folder):
    """Burn the captions onto single frames of the picture master: fast to check before rendering."""
    project = Project(folder)
    events = project.read_json('caption-plan.json')
    times = preview_times(project, events)
    require(times, 'No captions to preview.')
    frames = []
    for t in times:
        # Input seeking restarts timestamps at 0; shift them back so libass shows the right line.
        # Filter paths stay relative to the project folder: colons and quotes in paths break FFmpeg filters.
        vf = f"setpts=PTS+{t}/TB,ass=filename=captions.ass:fontsdir=selected-fonts"
        frames.append((clock(t), grab_frame('picture-master.mov', t, vf, cwd=project.dir)))
    project.write_json('preview-times.json', times)
    out = contact_sheet(frames, project.path('caption-review.jpg'), columns=3, thumb=640)
    print(f'{len(frames)} caption previews → {out}')
    return out


def loudness_filter(target, measured=None):
    spec = f"loudnorm=I={target['I']}:TP={target['TP']}:LRA={target['LRA']}"
    if measured is None:
        return spec + ':print_format=json'
    return (spec + f":measured_I={measured['input_i']}:measured_TP={measured['input_tp']}"
            f":measured_LRA={measured['input_lra']}:measured_thresh={measured['input_thresh']}"
            f":offset={measured['target_offset']}:linear=false")


def render(folder):
    project = Project(folder)
    plan = project.read_json('edit-plan.json')
    require(project.path('captions.ass').is_file(), 'captions.ass is missing; run `captioncut captions` first.')
    duration = plan['duration']
    target = {'I': -16, 'TP': -1.5, 'LRA': 11, **project.get('loudness', {})}
    measurement = project.path('loudness-measurement.log')
    run([ffmpeg(), '-hide_banner', '-nostdin', '-v', 'info', '-i', 'picture-master.mov', '-map', '0:a:0', '-vn',
         '-af', loudness_filter(target), '-f', 'null', '-'], log=measurement, cwd=project.dir)
    measured = load_loudnorm(measurement)
    fade = max(0.0, duration - 0.18)
    audio = f'{loudness_filter(target, measured)},aresample=48000,afade=t=out:st={fade}:d=0.18'
    video = f'ass=filename=captions.ass:fontsdir=selected-fonts,fade=t=out:st={fade}:d=0.18,{BT709}'
    out = project.output
    cmd = [ffmpeg(), '-hide_banner', '-nostdin', '-y', '-i', 'picture-master.mov', '-map', '0:v:0', '-map', '0:a:0',
           '-vf', video, '-af', audio, '-c:v', 'libx264', '-preset', 'fast', '-crf', '18', '-pix_fmt', 'yuv420p',
           '-r', str(project.fps), '-c:a', 'aac', '-b:a', '192k', '-ar', '48000', '-ac', '2',
           '-color_primaries', 'bt709', '-color_trc', 'bt709', '-colorspace', 'bt709', '-map_metadata', '-1',
           '-metadata', f"title={project.get('title', '')}", '-movflags', '+faststart', '-t', str(duration), str(out)]
    project.path('render-command.json').write_text(json.dumps(cmd, ensure_ascii=False, indent=2) + '\n')
    run(cmd, log=project.path('final-render.log'), cwd=project.dir)
    print(f'rendered {out}')
    return out

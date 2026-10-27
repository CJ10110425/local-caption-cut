"""Cut the source into a frame-exact picture master with the original audio."""
from __future__ import annotations

import concurrent.futures

from .common import BT709, TONEMAP, Project, audio_streams, ffmpeg, is_hdr, probe, require, run, video_stream


def frame_plan(cuts, fps, source_duration=None):
    """Snap each cut to the frame grid and lay the clips end to end on the timeline."""
    clips, total = [], 0
    for index, cut in enumerate(cuts):
        a, b = float(cut['start']), float(cut['end'])
        require(b > a, f'Cut {index} ends before it starts: {cut}')
        if source_duration is not None:
            require(0 <= a and b <= source_duration + 1 / fps, f'Cut {index} is outside the source: {cut}')
        start = round(a * fps) / fps
        frames = round((b - a) * fps)
        require(frames > 0, f'Cut {index} is shorter than one frame: {cut}')
        clips.append({
            'index': index, 'source_start': start, 'source_end': start + frames / fps,
            'start_frame': total, 'frames': frames,
            'timeline_start': total / fps, 'timeline_end': (total + frames) / fps,
            'purpose': cut.get('purpose', ''),
        })
        total += frames
    return clips, total


def video_filter(project, hdr):
    mode = project.get('tonemap', 'auto')
    grade = project.get('grade', {})
    if mode == 'hable' or (mode == 'auto' and hdr):
        tone = TONEMAP.format(saturation=grade.get('saturation', 0.9), gamma=grade.get('gamma', 1.03))
    else:
        tone = 'scale=out_color_matrix=bt709:out_range=tv,format=yuv420p'
    # Encoders take colour tags from the frames, so stamp BT.709 on them explicitly.
    return f'fps={project.fps}:start_time=0,setpts=PTS-STARTPTS,{tone},{BT709},setsar=1'


def cut(folder, workers=2):
    project = Project(folder)
    require(project.get('cuts'), 'project.json has no cuts yet.')
    info = probe(project.source)
    require(audio_streams(info), 'The source has no audio stream.')
    fps = project.fps
    clips, total = frame_plan(project['cuts'], fps, float(info['format']['duration']))
    hdr = is_hdr(video_stream(info))
    plan = {'source': str(project.source), 'method': 'local_ffmpeg', 'fps': fps, 'hdr_source': hdr,
            'duration': total / fps, 'clips': clips}
    project.write_json('edit-plan.json', plan)
    parts = project.path('parts')
    parts.mkdir(exist_ok=True)
    vf = video_filter(project, hdr)

    def render(clip):
        out = parts / f"part-{clip['index']:02d}.mov"
        duration = clip['frames'] / fps
        # Tiny fades on every cut stop clicks at the joins without changing audible speech.
        af = (f'aresample=48000,apad,atrim=duration={duration:.8f},afade=t=in:d=0.008,'
              f'afade=t=out:st={duration - 0.012:.8f}:d=0.012')
        run([ffmpeg(), '-hide_banner', '-nostdin', '-y', '-v', 'warning',
             '-ss', f"{clip['source_start']:.8f}", '-i', str(project.source), '-t', f'{duration:.8f}',
             '-map', '0:v:0', '-map', '0:a:0', '-vf', vf, '-af', af,
             '-c:v', 'libx264', '-crf', '17', '-preset', 'veryfast', '-threads', '4', '-filter_threads', '2',
             '-pix_fmt', 'yuv420p', '-color_primaries', 'bt709', '-color_trc', 'bt709', '-colorspace', 'bt709',
             '-c:a', 'pcm_s16le', '-ar', '48000', '-ac', '2', '-map_metadata', '-1', str(out)],
            log=parts / f"part-{clip['index']:02d}.log")
        print(f"cut {clip['index']:02d} {clip['source_start']:.2f}–{clip['source_end']:.2f}s {clip['purpose']}",
              flush=True)
        return out

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        files = list(pool.map(render, clips))
    listing = project.path('parts.txt')
    # Concat resolves entries relative to the list file, which sits in the project folder.
    listing.write_text(''.join(f"file 'parts/{f.name}'\n" for f in files))
    run([ffmpeg(), '-v', 'warning', '-y', '-f', 'concat', '-safe', '0', '-i', str(listing),
         '-c', 'copy', str(project.path('picture-master.mov'))])
    run([ffmpeg(), '-v', 'error', '-y', '-i', str(project.path('picture-master.mov')), '-vn',
         '-ac', '1', '-ar', '16000', str(project.path('edit-audio.wav'))])
    print(f'picture-master.mov ready: {len(clips)} clips, {total / fps:.2f}s')
    return plan

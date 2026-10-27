"""Turn source-timed captions into a styled ASS file on the edited timeline."""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from PIL import ImageFont

from .common import PACKAGE_DIR, Project, ass_time, display_size, probe, require, srt_time, video_stream
from .fonts import FontBook


def load_style(project):
    name = project.get('style', 'playful')
    path = Path(name).expanduser()
    if path.suffix != '.json':
        path = PACKAGE_DIR / 'styles' / f'{name}.json'
    elif not path.is_absolute():
        path = project.dir / path
    require(path.is_file(), f'Style not found: {path}')
    style = json.loads(path.read_text())
    for role, override in project.get('style_overrides', {}).items():
        style['roles'].setdefault(role, {}).update(override)
    return style


def map_to_timeline(start, end, clips, min_overlap=0.2):
    """Yield (timeline_start, timeline_end, clip_index) for each clip a source range survives in.

    Captions are written against the source, so a line that spans a removed section is split,
    and a fragment shorter than `min_overlap` is dropped.
    """
    for clip in clips:
        a, b = max(start, clip['source_start']), min(end, clip['source_end'])
        if b - a > min_overlap:
            offset = clip['timeline_start'] - clip['source_start']
            yield a + offset, b + offset, clip['index']


TOKEN = re.compile(r"[A-Za-z0-9]+(?:['’][A-Za-z]+)?|\s+|[^A-Za-z0-9\s]")
NO_LINE_START = set('，。、！？：；）」』》…,.!?:;)')


def wrap(text, font, max_width):
    """Break between CJK glyphs or whole Latin words; keep closing punctuation off line starts."""
    lines = []
    for part in text.split('\n'):
        line = ''
        for token in TOKEN.findall(part):
            candidate = line + token
            if line.strip() and font.getlength(candidate) > max_width and token not in NO_LINE_START:
                lines.append(line.strip())
                line = token.lstrip()
            else:
                line = candidate
        if line.strip():
            lines.append(line.strip())
    return lines


def ass_color(hex_color, alpha=0):
    """'#RRGGBB' → ASS '&HAABBGGRR'."""
    h = hex_color.lstrip('#').upper()
    return f'&H{alpha:02X}{h[4:6]}{h[2:4]}{h[0:2]}'


def rounded_box(w, h, r):
    return (f'm {r} 0 l {w - r} 0 b {w} 0 {w} 0 {w} {r} l {w} {h - r} b {w} {h} {w} {h} {w - r} {h} '
            f'l {r} {h} b 0 {h} 0 {h} 0 {h - r} l 0 {r} b 0 0 0 0 {r} 0')


class Typesetter:
    def __init__(self, project, style, width, height):
        self.project, self.style = project, style
        self.width, self.height = width, height
        self.scale = min(width, height) / 1080
        self.portrait = height > width
        self.book = FontBook([project.dir / 'fonts'])
        self.defaults = style.get('defaults', {})
        self.layout = style.get('layout', {})
        self.faces = {}
        self.used = {}

    def role(self, name):
        roles = self.style['roles']
        require(name in roles, f'Unknown caption role {name!r}; style has {sorted(roles)}')
        return {**self.defaults, **roles[name]}

    def face_for(self, role_name, text):
        """Pick the role's font, or the style fallback when the role font lacks a glyph."""
        role = self.role(role_name)
        listed = role.get('font', self.style['font'])
        fallback = self.style.get('fallback', self.style['font'])
        candidates = (listed if isinstance(listed, list) else [listed]) + (
            fallback if isinstance(fallback, list) else [fallback])
        missing = None
        for family in candidates:
            try:
                face = self.book.resolve(family)
            except FileNotFoundError:
                continue
            cmap = self.book.cmap(face)
            missing = sorted({c for c in text if not c.isspace() and ord(c) not in cmap})
            if not missing:
                self.used[face['family']] = face
                return face
        raise ValueError(f'No installed font for role {role_name!r} covers {text!r} '
                         f'(tried {candidates}; missing {missing}). Try `captioncut fonts fetch`.')

    def px(self, value):
        return round(value * self.scale, 2)

    def measure(self, face, text, size, max_width):
        font = ImageFont.truetype(face['path'], round(size), index=face['index'])
        lines = wrap(text, font, max_width)
        return font, lines

    def fit(self, face, text, size, max_width):
        font, lines = self.measure(face, text, size, max_width)
        orphan = len(lines) == 2 and len(lines[-1].strip()) <= 2
        if len(lines) > 2 or orphan:
            size -= self.px(8)
            font, lines = self.measure(face, text, size, max_width)
        if len(lines) > 2:
            size -= self.px(8)
            font, lines = self.measure(face, text, size, max_width)
        require(len(lines) <= 2, f'Caption needs more than two lines; split it: {text!r}')
        return round(size), lines, max(font.getlength(line) for line in lines)

    def anim(self, role, duration, item):
        tags = ''
        pop = item.get('pop', role.get('pop'))
        if pop and duration > role.get('pop_min', 0):
            start, peak, t1, t2 = pop if isinstance(pop, list) else [92, 103, 120, 240]
            tags += f'\\fscx{start}\\fscy{start}\\t(0,{t1},\\fscx{peak}\\fscy{peak})\\t({t1},{t2},\\fscx100\\fscy100)'
        tilt = item.get('tilt', role.get('tilt'))
        if tilt:
            tags += f'\\frz{tilt}'
        return tags

    def event(self, kind, item, role_name, a, b, clip, x, y, align, max_width):
        role = self.role(role_name)
        face = self.face_for(role_name, item['text'])
        size, lines, width = self.fit(face, item['text'], self.px(role['size']), max_width)
        fade = role.get('fade', [30, 60])
        tag = f'\\an{align}\\pos({round(x)},{round(y)})\\fs{size}\\q2\\fad({fade[0]},{fade[1]})'
        if face['family'] != self.family_name(role):
            tag += f"\\fn{face['family']}"
        tag += self.anim(role, b - a, item)
        return {
            'start': round(a, 3), 'end': round(b, 3), 'source_start': item['start'], 'source_end': item['end'],
            'clip': clip, 'kind': kind, 'role': role_name, 'text': '\\N'.join(lines), 'tag': tag,
            'layer': {'caption': 1, 'note': 2, 'title': 2}[kind], 'x': round(x), 'y': round(y), 'align': align,
            'font_family': face['family'], 'font_file': Path(face['path']).name, 'font_size': size,
            'measured_width': round(width, 2), 'line_count': len(lines),
        }

    def family_name(self, role):
        family = role.get('font', self.style['font'])
        try:
            return self.book.resolve(family)['family']
        except FileNotFoundError:
            return family if isinstance(family, str) else family[0]


def build_events(project, setter, clips):
    overlap = setter.layout.get('min_overlap', 0.2)
    max_width = setter.width * setter.layout.get('max_width', 0.82)
    orientation = 'portrait' if setter.portrait else 'landscape'
    caption_y = setter.height * setter.layout.get('caption_y', {}).get(orientation, 0.9)
    events = []
    for item in project.get('captions', []):
        for a, b, clip in map_to_timeline(item['start'], item['end'], clips, overlap):
            events.append(setter.event('caption', item, item.get('role', 'dialogue'), a, b, clip,
                                       setter.width / 2, caption_y, 2, max_width))
    for item in project.get('notes', []):
        for a, b, clip in map_to_timeline(item['start'], item['end'], clips, overlap):
            events.append(setter.event('note', item, item.get('role', 'note'), a, b, clip,
                                       setter.width * item.get('x', 0.75), setter.height * item.get('y', 0.65),
                                       5, setter.width * 0.4))
    title_y = setter.height * setter.layout.get('title_y', {}).get(orientation, 0.1)
    for item in project.get('titles', []):
        if 'clip' in item:
            clip = clips[item['clip']]
            start = clip['source_start'] + item.get('offset', 0)
            item = {**item, 'start': start, 'end': min(start + item.get('duration', 3.0), clip['source_end'])}
        for a, b, clip in map_to_timeline(item['start'], item['end'], clips, overlap):
            events.append(setter.event('title', item, item.get('role', 'title'), a, b, clip,
                                       setter.width / 2, title_y, 8, setter.width * 0.86))
    events.sort(key=lambda e: (e['start'], e['layer']))
    spoken = [e for e in events if e['kind'] == 'caption']
    for previous, current in zip(spoken, spoken[1:]):
        require(current['start'] >= previous['end'] - 0.001,
                f"Captions overlap: {previous['text']!r} ({previous['end']}) and {current['text']!r} ({current['start']})")
    return events


def write_ass(path, title, setter, events):
    style = setter.style
    head = [
        '[Script Info]', f'Title: {title}', 'ScriptType: v4.00+', f'PlayResX: {setter.width}',
        f'PlayResY: {setter.height}', 'ScaledBorderAndShadow: yes', 'WrapStyle: 2', 'YCbCr Matrix: TV.709', '',
        '[V4+ Styles]',
        'Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, '
        'Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, '
        'MarginL, MarginR, MarginV, Encoding',
    ]
    for name in style['roles']:
        role = setter.role(name)
        try:
            face = setter.book.resolve(role.get('font', style['font']))
            family, weight = face['family'], face['weight']
        except FileNotFoundError:
            family, weight = setter.family_name(role), 400
        bold = -1 if role.get('bold', weight >= 600) else 0
        shadow = ass_color(role.get('shadow_color', '#000000'), 0x80)
        head.append(f"Style: {name},{family},{round(setter.px(role['size']))},{ass_color(role['color'])},&H00FFFFFF,"
                    f"{ass_color(role['outline'])},{shadow},{bold},0,0,0,100,100,0,0,1,"
                    f"{setter.px(role.get('border', 3))},{setter.px(role.get('shadow', 1))},2,0,0,0,1")
    head += ['', '[Events]', 'Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text']
    lines = []
    for e in events:
        role = setter.role(e['role'])
        start, end = ass_time(e['start']), ass_time(e['end'])
        if role.get('bg'):
            # Rounded box behind the text, drawn on a lower layer with the same fade.
            pad = setter.px(setter.layout.get('box_padding', 30))
            w = round(e['measured_width'] + 2 * pad)
            h = round(e['font_size'] * e['line_count'] * 1.28 + setter.px(22))
            x = round(e['x'] - w / 2)
            top = {2: e['y'] + setter.px(11) - h, 8: e['y'] - setter.px(11), 5: e['y'] - h / 2}[e['align']]
            y = round(top)
            fade = role.get('fade', [30, 60])
            box = (f"\\an7\\pos({x},{y})\\p1\\bord0\\shad0\\1c{ass_color(role['bg'])}&\\alpha&H08&"
                   f"\\fad({fade[0]},{fade[1]})")
            lines.append(f"Dialogue: 0,{start},{end},{e['role']},,0,0,0,,{{{box}}}{rounded_box(w, h, round(setter.px(18)))}{{\\p0}}")
        lines.append(f"Dialogue: {e['layer']},{start},{end},{e['role']},,0,0,0,,{{{e['tag']}}}{e['text']}")
    path.write_text('\n'.join(head + lines) + '\n', encoding='utf-8-sig')


def author(folder):
    project = Project(folder)
    plan = project.read_json('edit-plan.json')
    master = project.path('picture-master.mov')
    require(master.is_file(), 'picture-master.mov is missing; run `captioncut cut` first.')
    width, height = display_size(video_stream(probe(master)))
    style = load_style(project)
    setter = Typesetter(project, style, width, height)
    events = build_events(project, setter, plan['clips'])
    fonts_dir = project.path('selected-fonts')
    if fonts_dir.exists():
        shutil.rmtree(fonts_dir)
    fonts_dir.mkdir()
    for name in style['roles']:
        try:
            face = setter.book.resolve(setter.role(name).get('font', style['font']))
            setter.used[face['family']] = face
        except FileNotFoundError:
            pass
    for face in setter.used.values():
        shutil.copy2(face['path'], fonts_dir / Path(face['path']).name)
    write_ass(project.path('captions.ass'), project.get('title', ''), setter, events)
    project.write_json('caption-plan.json', events)
    spoken = [e for e in events if e['kind'] == 'caption']
    project.path('captions.srt').write_text(''.join(
        f"{i}\n{srt_time(e['start'])} --> {srt_time(e['end'])}\n{e['text'].replace(chr(92) + 'N', chr(10))}\n\n"
        for i, e in enumerate(spoken, 1)))
    print(f"{len(spoken)} captions, {len(events) - len(spoken)} notes/titles; fonts: {sorted(setter.used)}")
    return events

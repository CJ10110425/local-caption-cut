"""captioncut: cut a phone video and burn in styled Traditional Chinese captions, all locally."""
from __future__ import annotations

import argparse
import sys

from .common import CaptionCutError


def main(argv=None):
    parser = argparse.ArgumentParser(prog='captioncut', description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)

    p = sub.add_parser('init', help='create <video>-edit/project.json and print what the source is')
    p.add_argument('video')
    p.add_argument('--dir', help='project folder (default: next to the video)')
    p.add_argument('--style', default='playful', help='playful, whatsub-inspired, or a path to a style JSON')
    p.add_argument('--force', action='store_true')

    p = sub.add_parser('transcribe', help='whisper.cpp transcript of the source (or of --start/--end)')
    p.add_argument('project')
    p.add_argument('--model', help='ggml model (default: $WHISPER_MODEL or ~/.cache/whisper-cpp/ggml-large-v3-turbo-q5_0.bin)')
    p.add_argument('--language', default='zh')
    p.add_argument('--start', type=float)
    p.add_argument('--end', type=float)
    p.add_argument('--name', help='output base name')
    p.add_argument('--prompt', help='initial prompt; default nudges zh towards Traditional characters')
    p.add_argument('--by-pauses', action='store_true', help='transcribe each speech region separately (tighter times)')

    p = sub.add_parser('sheet', help='contact sheet of the source to help choose cuts')
    p.add_argument('project')
    p.add_argument('--every', type=float, help='seconds between frames')
    p.add_argument('--times', help='comma-separated source times')

    for name, text in [('cut', 'render the picture master from project.json cuts'),
                       ('captions', 'typeset captions.ass / captions.srt on the edited timeline'),
                       ('preview', 'caption-review.jpg: captions burned onto stills'),
                       ('render', 'two-pass loudness and burned-in captions → final MP4'),
                       ('verify', 'technical checks → delivery.json + final-contact-sheet.jpg'),
                       ('all', 'cut, captions, preview, render, verify')]:
        sub.add_parser(name, help=text).add_argument('project')

    p = sub.add_parser('fonts', help='fetch or list the open-licensed caption fonts')
    p.add_argument('action', choices=['fetch', 'list'])
    p.add_argument('families', nargs='*')
    p.add_argument('--force', action='store_true')

    args = parser.parse_args(argv)
    try:
        if args.command == 'init':
            from .prepare import init
            init(args.video, args.dir, args.style, args.force)
        elif args.command == 'transcribe':
            from .prepare import transcribe
            if (args.start is None) != (args.end is None):
                parser.error('--start and --end go together')
            transcribe(args.project, args.model, args.language, args.start, args.end, args.name, args.prompt,
                       args.by_pauses)
        elif args.command == 'sheet':
            from .prepare import sheet
            times = [float(t) for t in args.times.split(',')] if args.times else None
            sheet(args.project, args.every, times)
        elif args.command == 'fonts':
            from . import fonts
            fonts.fetch(args.families, args.force) if args.action == 'fetch' else fonts.list_fonts()
        else:
            from .captions import author
            from .cut import cut
            from .render import preview, render
            from .verify import verify
            steps = {'cut': [cut], 'captions': [author], 'preview': [preview], 'render': [render],
                     'verify': [verify], 'all': [cut, author, preview, render, verify]}[args.command]
            for step in steps:
                step(args.project)
    except (CaptionCutError, FileNotFoundError, ValueError) as error:
        print(f'captioncut: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())

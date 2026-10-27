"""Find caption fonts by family name, and fetch the open-licensed set the styles use."""
from __future__ import annotations

import json
import os
import shutil
import urllib.request
from pathlib import Path

from fontTools.ttLib import TTCollection, TTFont

from .common import PACKAGE_DIR, require

FONT_HOME = Path(os.environ.get('CAPTIONCUT_FONT_DIR', '~/.local/share/captioncut/fonts')).expanduser()
CACHE = Path('~/.cache/captioncut/font-index.json').expanduser()
SYSTEM_DIRS = ['~/Library/Fonts', '/Library/Fonts', '/System/Library/Fonts', '/System/Library/Fonts/Supplemental',
               '~/.local/share/fonts', '~/.fonts', '/usr/share/fonts', '/usr/local/share/fonts']
CATALOG = json.loads((PACKAGE_DIR / 'font-catalog.json').read_text())


def search_dirs(extra=()):
    dirs = [Path(d).expanduser() for d in extra]
    dirs.append(FONT_HOME)
    dirs += [Path(d).expanduser() for d in os.environ.get('CAPTIONCUT_FONT_DIRS', '').split(os.pathsep) if d]
    dirs += [Path(d).expanduser() for d in SYSTEM_DIRS]
    return [d for d in dirs if d.is_dir()]


def _faces(path):
    """Yield (family, weight, face index) for every face in a font file."""
    if path.suffix.lower() == '.ttc':
        fonts = list(enumerate(TTCollection(str(path), lazy=True).fonts))
    else:
        fonts = [(0, TTFont(str(path), lazy=True))]
    for index, font in fonts:
        names = font['name']
        family = names.getDebugName(16) or names.getDebugName(1)
        weight = font['OS/2'].usWeightClass if 'OS/2' in font else 400
        families = {family}
        # CJK fonts often carry a localized family too (e.g. 黑體-繁); index all of them.
        for record in names.names:
            if record.nameID in (1, 16):
                try:
                    families.add(record.toUnicode())
                except UnicodeDecodeError:
                    pass
        for name in families:
            if name:
                yield name, weight, index


def index_fonts(extra=()):
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    fresh, families = {}, {}
    for folder in search_dirs(extra):
        for path in folder.rglob('*'):
            if path.suffix.lower() not in ('.ttf', '.otf', '.ttc'):
                continue
            key = str(path)
            stamp = path.stat().st_mtime
            entry = cache.get(key)
            if not entry or entry['mtime'] != stamp:
                try:
                    entry = {'mtime': stamp, 'faces': [list(face) for face in _faces(path)]}
                except Exception:  # unreadable or unusual font files are simply skipped
                    entry = {'mtime': stamp, 'faces': []}
            fresh[key] = entry
            for family, weight, face in entry['faces']:
                families.setdefault(family, []).append({'path': key, 'weight': weight, 'index': face})
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(fresh))
    return families


class FontBook:
    def __init__(self, extra_dirs=()):
        self.families = index_fonts(extra_dirs)
        self._loaded = {}

    def resolve(self, family, weight=None):
        """Return {'family', 'path', 'index', 'weight'} for the first available family in a list."""
        choices = family if isinstance(family, list) else [family]
        for name in choices:
            faces = self.families.get(name)
            if faces:
                target = weight or 700
                face = min(faces, key=lambda f: (abs(f['weight'] - target), f['path']))
                return {'family': name, **face}
        raise FileNotFoundError(
            f'None of these fonts is installed: {choices}. Run `captioncut fonts fetch` or install one.')

    def cmap(self, face):
        key = (face['path'], face['index'])
        if key not in self._loaded:
            path = Path(face['path'])
            font = (TTCollection(str(path)).fonts[face['index']] if path.suffix.lower() == '.ttc'
                    else TTFont(str(path)))
            self._loaded[key] = set(font.getBestCmap())
        return self._loaded[key]


def fetch(names=None, force=False):
    """Download fonts listed in font-catalog.json from the Google Fonts repository (all SIL OFL 1.1)."""
    from fontTools.varLib.instancer import instantiateVariableFont

    FONT_HOME.mkdir(parents=True, exist_ok=True)
    wanted = [f for f in CATALOG['fonts'] if not names or f['family'] in names]
    require(wanted, f'Unknown font: {names}. Known: {[f["family"] for f in CATALOG["fonts"]]}')
    for entry in wanted:
        folder = FONT_HOME / entry['slug']
        target = folder / entry['file']
        if target.exists() and not force:
            print(f"have {entry['family']}")
            continue
        folder.mkdir(exist_ok=True)
        base = f"https://raw.githubusercontent.com/google/fonts/main/ofl/{entry['slug']}/"
        download = folder / ('download-' + entry['source'].replace('[', '_').replace(']', '_'))
        _get(base + entry['source'].replace('[', '%5B').replace(']', '%5D'), download)
        _get(base + 'OFL.txt', folder / 'OFL.txt')
        if entry.get('instance'):
            # A static instance makes libass and Pillow agree on the weight being drawn.
            font = TTFont(str(download))
            instantiateVariableFont(font, entry['instance'], inplace=True, updateFontNames=True)
            font.save(str(target))
            download.unlink()
        else:
            shutil.move(download, target)
        print(f"fetched {entry['family']} → {target}")
    if CACHE.exists():
        CACHE.unlink()


def _get(url, path):
    request = urllib.request.Request(url, headers={'User-Agent': 'captioncut'})
    with urllib.request.urlopen(request, timeout=120) as response, path.open('wb') as handle:
        shutil.copyfileobj(response, handle)


def list_fonts():
    book = FontBook()
    for entry in CATALOG['fonts']:
        try:
            face = book.resolve(entry['family'])
            status = face['path']
        except FileNotFoundError:
            status = 'missing (captioncut fonts fetch)'
        print(f"{entry['family']:<20} {entry['label']:<8} {status}")

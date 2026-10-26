"""Build offline WOFF2 slices: python3 scripts/subset_fonts.py SOURCE_DIRECTORY.

Sources: google/fonts ofl/blackhansans/BlackHanSans-Regular.ttf and
ofl/gothica1/GothicA1-{Regular,SemiBold}.ttf (SIL OFL 1.1).
Requires fontTools with Brotli. Re-run when adding syllables outside KS X 1001.
"""
import sys
from pathlib import Path
from fontTools import subset
from fontTools.ttLib import TTFont

root = Path(__file__).resolve().parents[1]
source = Path(sys.argv[1])
ks = set()
for first in range(0xB0, 0xC9):
    for second in range(0xA1, 0xFF):
        try:
            ks.update(map(ord, bytes([first, second]).decode('euc_kr')))
        except UnicodeDecodeError:
            pass
corpus = ''.join(p.read_text() for pattern in ('web/**/*.html', 'web/*.js', 'scripts/*.py')
                 for p in root.glob(pattern))
hangul = ks | {ord(c) for c in corpus if 0xAC00 <= ord(c) <= 0xD7A3}
hangul |= set(range(0x1100, 0x1200)) | set(range(0x3000, 0x3040)) | set(range(0x3130, 0x3190))
latin = set(range(0x20, 0x7F)) | set(range(0xA0, 0x100)) | set(range(0x2010, 0x2028)) | {0x20A9}
for name, original in [('BlackHanSans', 'BlackHanSans-Regular.ttf'),
                       ('GothicA1-400', 'GothicA1-Regular.ttf'),
                       ('GothicA1-600', 'GothicA1-SemiBold.ttf')]:
    for part, unicodes in [('latin', latin), ('hangul', hangul)]:
        font = TTFont(source / original)
        options = subset.Options()
        options.flavor = 'woff2'
        sub = subset.Subsetter(options=options)
        sub.populate(unicodes=unicodes)
        sub.subset(font)
        font.flavor = 'woff2'
        target = root / 'web/fonts' / f'{name}-{part}.woff2'
        font.save(target)
        print(target.name, target.stat().st_size)

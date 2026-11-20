"""Freeze Inter 4.1's tnum feature in its Unicode cmap, without a shaping dependency.

Run: uv run python tools/prepare_invoice_fonts.py /path/to/Inter-4.1.zip
"""
from __future__ import annotations
import io
import sys
import zipfile
from pathlib import Path
from fontTools.ttLib import TTFont

root = Path(__file__).resolve().parents[1] / 'src/fiscalm/invoicing/fonts/inter'
with zipfile.ZipFile(sys.argv[1]) as archive:
    (root / 'OFL.txt').write_bytes(archive.read('LICENSE.txt'))
    for weight in ('Regular', 'Medium', 'SemiBold'):
        font = TTFont(io.BytesIO(archive.read(f'extras/ttf/Inter-{weight}.ttf')), recalcTimestamp=False)
        gsub = font['GSUB'].table
        indices = [i for record in gsub.FeatureList.FeatureRecord if record.FeatureTag == 'tnum'
                   for i in record.Feature.LookupListIndex]
        mapping = {a: b for i in indices for table in gsub.LookupList.Lookup[i].SubTable
                   for a, b in table.mapping.items()}
        for table in font['cmap'].tables:
            if table.isUnicode():
                table.cmap = {c: mapping.get(g, g) if 48 <= c <= 57 else g for c, g in table.cmap.items()}
        font.save(root / f'Inter-{weight}.ttf', reorderTables=True)

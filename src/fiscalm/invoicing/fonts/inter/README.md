Inter 4.1, Regular, Medium and SemiBold, from extras/ttf in
https://github.com/rsms/inter/releases/tag/v4.1 (Inter-4.1.zip).
Copyright Rasmus Andersson. SIL Open Font License 1.1: see OFL.txt.

These static TTFs have Inter's tnum substitutions frozen for U+0030–U+0039 (0–9) only in their Unicode cmaps
by tools/prepare_invoice_fonts.py. Thus direct PDF glyph mapping uses tabular
figures without a shaping engine. All other glyphs, including Romanian comma-below
ș/ț and ă/î/â, are preserved. Font timestamps remain the upstream timestamps.
The renderer subsets each weight to used characters with fontTools (MIT), with
layout features dropped and timestamp recalculation disabled. ToUnicode maps
original Unicode text for extraction. No network or system fonts at runtime.

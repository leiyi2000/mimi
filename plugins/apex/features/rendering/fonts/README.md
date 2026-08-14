# Bundled fonts

`NotoSansSC-*.subset.otf` are subsets of Google Noto Sans SC, containing only the
glyphs this plugin renders (map/event names + stats labels + UI text + Latin/digits).
This keeps the plugin self-contained so rendering does not depend on system fonts.

The subset covers every CJK character used across the feature modules and their
Jinja templates. If a new label is added, regenerate both weights so the character
set stays in sync.

## Regenerate

The upstream source is the Google Fonts variable font `NotoSansSC[wght].ttf`.
Instantiate it at weight 400 (Regular) and 700 (Bold), then subset each instance to
the character set the plugin renders:

    # 1. build chars.txt from the union of every CJK char in features/*.py and
    #    features/templates/*.j2 (plus Latin, digits, and punctuation)
    # 2. instantiate the variable font at a fixed weight
    fonttools varLib.instancer 'NotoSansSC[wght].ttf' wght=400 \
        --output NotoSansSC-Regular.instance.ttf
    fonttools varLib.instancer 'NotoSansSC[wght].ttf' wght=700 \
        --output NotoSansSC-Bold.instance.ttf
    # 3. subset each instance
    pyftsubset NotoSansSC-Regular.instance.ttf --text-file=chars.txt \
        --output-file=NotoSansSC-Regular.subset.otf --layout-features='*'
    pyftsubset NotoSansSC-Bold.instance.ttf --text-file=chars.txt \
        --output-file=NotoSansSC-Bold.subset.otf --layout-features='*'

Licensed under the SIL Open Font License 1.1 (see LICENSE).

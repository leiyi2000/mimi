# Bundled fonts

`NotoSansSC-*.subset.otf` are subsets of Google Noto Sans SC, containing only the
glyphs this plugin renders (map/event names + UI labels + Latin/digits). This keeps
the plugin self-contained so rendering does not depend on system fonts.

If a new map or label is added, regenerate the subset from the full Noto Sans SC:

    fonttools subset NotoSansSC-Regular.otf --text-file=chars.txt \
        --output-file=NotoSansSC-Regular.subset.otf --layout-features='*'

Licensed under the SIL Open Font License 1.1 (see LICENSE).

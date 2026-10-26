# Design decisions

## 2026-10-26: site palette and self-hosted type (approved)

Kitchen Genome uses kraft backgrounds (#DFD5C3 light, #1B1915 dark), olive
accents (#4A5A1C light, #C7D77E dark), Black Han Sans 400 headings and numbers,
and Gothic A1 400/600 body text. The shared token block in `web/style.css`
applies across the Korean and English site, including generated pages and login.
Fonts are self-hosted under SIL OFL 1.1; licences live in `web/fonts/`.
Use only 2px ticket, 8px control, and 999px badge radii. Preserve dark mode,
reduced motion, Korean keep-all wrapping, AA contrast, and 44px targets.

Superseded: the original stylesheet's ivory background, terracotta accent,
system-only heading/body stack, and 12px card radius. Those values were the
previous implementation, not the current design contract. No separate old
DESIGN.md, longblack rule document, or decision log existed in this worktree.
The owner's approval replaces any inherited light-only or home-colour-only
restriction with this site-wide background/accent/heading-font decision.

Production subsets retain KS X 1001's 2,350 syllables plus supported Korean
characters in the current page corpus. Latin and Korean slices are separate;
Hanja and unsupported display syllables use the system fallback. Regenerate
with `scripts/subset_fonts.py` from the upstream TTF sources when needed.

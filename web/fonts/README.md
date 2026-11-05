# Display fonts

Both fonts are self-hosted and licensed under the SIL Open Font License 1.1.

## Excalifont Regular

`Excalifont-Regular.woff2` is the unmodified Latin / Latin-1 subset shipped by
[excalidraw/excalidraw](https://github.com/excalidraw/excalidraw/tree/7ded689cad49331167e29fd88b38f38d3736d576/packages/excalidraw/fonts/Excalifont),
renamed from `Excalifont-Regular-a88b72a24fb54c9f94e3b5fdaa7481c9.woff2`.
It includes the accented Latin letters and punctuation used by the site.

SHA-256: `e4423b318e11432aff2e6e865e300b7ca270f92321a3f56632268fede01c1b48`

`OFL.txt` contains the copyright notice and complete OFL text from the official
font's `index.ts` metadata. Excalidraw's application license is separate.

## Gaegu Regular

Source: [Google Fonts, ofl/gaegu](https://github.com/google/fonts/tree/bbda7253fc363238b05d97515ca71f8ae49c4fa9/ofl/gaegu).
`Gaegu-OFL.txt` is the complete, unmodified upstream license.

Source `Gaegu-Regular.ttf` SHA-256:
`aa52c98336f7c62e2896fc8b12b56a75d5b476d88a2f104b0980f4f7ce0adfc3`

Subset and convert with fontTools 4.65.0 and Brotli:

```sh
pyftsubset Gaegu-Regular.ttf \
  --unicodes=U+AC00-D7A3,U+3131-318E \
  --flavor=woff2 --name-IDs='*' --name-languages='*' --name-legacy \
  --output-file=Gaegu-Regular.woff2
```

The subset retains all 2,350 modern Hangul syllables and 51 compatibility jamo
present in the upstream font, with no Latin or punctuation codepoints. CSS uses
`unicode-range:U+AC00-D7A3,U+3131-318E`; characters absent from the source fall
back to the existing system font stack. Latin and punctuation use Excalifont.

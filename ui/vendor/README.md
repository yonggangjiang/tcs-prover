# Vendored libraries

Third-party files the page serves itself (the Content-Security-Policy is `default-src 'self'`, so nothing is
loaded from a CDN). They are used by `ui/markdown.js` to render proofs and memory notes. Every file is
byte-for-byte the file in the published npm package; nothing was edited. `katex.min.css` still names the
`.woff` and `.ttf` fallbacks after each `.woff2`; they are not vendored, and browsers that read `woff2` (every
current one) never request them (checked in Chromium: only `.woff2` requests).

| Library | Version | Licence | Source |
|---|---|---|---|
| markdown-it | 14.1.0 | MIT (`markdown-it/LICENSE`) | https://registry.npmjs.org/markdown-it/-/markdown-it-14.1.0.tgz |
| KaTeX | 0.16.11 | MIT (`katex/LICENSE`) | https://registry.npmjs.org/katex/-/katex-0.16.11.tgz |

Downloaded on 26 Nov 2026. Each tarball matched the `integrity` (SHA-512) the npm registry lists for it, and
`markdown-it.min.js` and `katex.min.js` were also compared with the copies on
`https://cdn.jsdelivr.net/npm/<package>@<version>/dist/` (identical).

| Tarball | SHA-256 | npm integrity |
|---|---|---|
| markdown-it-14.1.0.tgz | `7c4fab4d4d39fe98206a7fbbed4b1e3578f2b70b6b69d309413e33eec140e19f` | `sha512-a54IwgWPaeBCAAsv13YgmALOF1elABB08FxO9i+r4VFk5Vl4pKokRPeX8u5TCgSsPi6ec1otfLjdOpVcgbpshg==` |
| katex-0.16.11.tgz | `8c5494c5f7d8e8bf73ff3fe64d4bd09a340e4878b7eec9bbd9ef8d8f3e24193f` | `sha512-RQrI8rlHY92OLf3rho/Ts8i/XvjgguEjOkO1BEXcU3N8BqPpSzBNwV/G0Ukr+P/l3ivvJUE/Fa/CwbS6HesGNQ==` |

## Files (path in the package → path here, SHA-256)

markdown-it 14.1.0:

| Here | In the package | SHA-256 |
|---|---|---|
| `markdown-it/markdown-it.min.js` | `package/dist/markdown-it.min.js` | `38c70a1e7ca91ab40e2d9e6e60129851a717ed1c7d4acbbdd41bf9503791cf68` |
| `markdown-it/LICENSE` | `package/LICENSE` | `792c48c5a849a15fdf9e37e8bcf9e6d1dd13b32b46c642a748a0a46a9919d473` |

KaTeX 0.16.11 (fonts from `package/dist/fonts/`, `.woff2` only):

| Here | In the package | SHA-256 |
|---|---|---|
| `katex/katex.min.js` | `package/dist/katex.min.js` | `e6bfe5deebd4c7ccd272055bab63bd3ab2c73b907b6e6a22d352740a81381fd4` |
| `katex/katex.min.css` | `package/dist/katex.min.css` | `717bc9ae7853b61f0f76455dddf0ecd4f527a783f42de2ac24684899c1c46258` |
| `katex/LICENSE` | `package/LICENSE` | `766ccc1f306c885aa45542a9846bbd0a505b27a0374f146778171c2254ce18e3` |
| `katex/fonts/KaTeX_AMS-Regular.woff2` | same name | `0cdd387c9590a1a9f9794560022dbb59654a7d86f187aa0c81495ad42d3a7308` |
| `katex/fonts/KaTeX_Caligraphic-Bold.woff2` | same name | `de7701e42cf1f4cf0b766c03fb27977207eee2f4fd5d76fa82188406da43ea4c` |
| `katex/fonts/KaTeX_Caligraphic-Regular.woff2` | same name | `5d53e70ad607c2352162dec9e0923fb54ecdafaccbf604cd8dcf7d00facb989b` |
| `katex/fonts/KaTeX_Fraktur-Bold.woff2` | same name | `74444efd593c005e3f4573b44524704c0af0a937fe911cca9e94068d0d140d3f` |
| `katex/fonts/KaTeX_Fraktur-Regular.woff2` | same name | `51814d270d06ff0255dba0799994fa4d8c84d11f09951d47595f4abb1f3602dc` |
| `katex/fonts/KaTeX_Main-Bold.woff2` | same name | `0f60d1b897938ec918c8ce073092411baf9438f6739465693ff18b0f9d20b021` |
| `katex/fonts/KaTeX_Main-BoldItalic.woff2` | same name | `99cd42a3c072d918f2f44984a807cf7aa16e13545fd0875fc07c6c65f99e715b` |
| `katex/fonts/KaTeX_Main-Italic.woff2` | same name | `97479ca6cce906abc961ecac96faa5f9ca2e61b8e7670d475826bcdee9a7c267` |
| `katex/fonts/KaTeX_Main-Regular.woff2` | same name | `c2342cd8b869e01752a9321dc17213fc40d4d04c79688c1d43f2cf316abd7866` |
| `katex/fonts/KaTeX_Math-BoldItalic.woff2` | same name | `dc47344dbb6cb5b655c8460d561f4df5f501b90c804ad3c6cec65fe322351ab1` |
| `katex/fonts/KaTeX_Math-Italic.woff2` | same name | `7af58c5ec8f132a2ddde9027c6d7814decce4d3b822a11192a42a20e2e973264` |
| `katex/fonts/KaTeX_SansSerif-Bold.woff2` | same name | `e99ae51144bf1232efcc1bfe5add36262c6866b0faab24fa75740e1b98577a62` |
| `katex/fonts/KaTeX_SansSerif-Italic.woff2` | same name | `00b26ac825e2095056396e0553b8ac26d3f8ad158c3826e28b4c45b385c4714a` |
| `katex/fonts/KaTeX_SansSerif-Regular.woff2` | same name | `68e8c73ef42afd3ccec58bf0fba302cce448938e7fc020a5e31f8a952eee1342` |
| `katex/fonts/KaTeX_Script-Regular.woff2` | same name | `036d4e95149b69ff9bcc0cd55771efeb25ffa3947293e69acd78d5ac328c684b` |
| `katex/fonts/KaTeX_Size1-Regular.woff2` | same name | `6b47c40166b6dbe21a5dfca7718413f2147fd2399be1ba605d8ad39cedf25dfe` |
| `katex/fonts/KaTeX_Size2-Regular.woff2` | same name | `d04c54219f9eaec6d4d4fd42dfb28785975a4794d6b2fc71e566b9cd6db842dd` |
| `katex/fonts/KaTeX_Size3-Regular.woff2` | same name | `73d591271b1604960cb10bb90fee021670af7297017e0e98480b332d11f51995` |
| `katex/fonts/KaTeX_Size4-Regular.woff2` | same name | `a4af7d414440a1c1790825cfb700cf9cf43b0f2c4b04f0ebc523011ad9853ec0` |
| `katex/fonts/KaTeX_Typewriter-Regular.woff2` | same name | `71d517d67827787cfabdf186914cc3358eda539e37931941f2b2fd4a21f68c0b` |

To check: `cd ui/vendor && sha256sum -c` against the table, or download the tarballs again and compare.

## Notes for an upgrade

Besides the public `katex.render`, `ui/markdown.js` uses one KaTeX internal, `katex.__domTree.SvgNode`: it
moves the one `style` attribute KaTeX sets with `setAttribute` (on the SVG pieces of tall delimiters and of
`\oiint`, `\oiiint`) to the CSSOM, because the page's policy blocks style attributes. If an upgrade renames
`__domTree`, the formulas still render; only those SVG pieces lose their width and the browser reports a
policy violation. After an upgrade, render a document with `\oiint` and a tall `\left(` matrix and check
the console for policy reports.

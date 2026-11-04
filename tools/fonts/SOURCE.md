# Font sources

The SVG assets are drawn from these subsets. Both families are under the SIL Open Font License 1.1
(see the `OFL-*.txt` files next to them) and declare no reserved font name.

| File | Source | Weight |
|---|---|---|
| `Montserrat-Bold.ttf` | `ofl/montserrat/Montserrat[wght].ttf` | 700 |
| `Montserrat-Medium.ttf` | `ofl/montserrat/Montserrat[wght].ttf` | 500 |
| `JetBrainsMono-Medium.ttf` | `ofl/jetbrainsmono/JetBrainsMono[wght].ttf` | 500 |

Upstream: https://github.com/google/fonts at commit `2fe1e8c6b137fb13041a7cfca6fdcd8a910ea4dc`.

| Upstream file | SHA-256 |
|---|---|
| `Montserrat[wght].ttf` | `0f7b311b2f3279e4eef9b2f968bcdbab6e28f4daeb1f049f4f278a902bcd82f7` |
| `JetBrainsMono[wght].ttf` | `48715a42ec242c21e9f02692891e147d022299a52e48d5e413e1a942193ffeda` |

Regenerate a subset with the pinned `fonttools` from `tools/requirements.txt`:

```sh
fonttools varLib.instancer "Montserrat[wght].ttf" wght=700 --static --update-name-table -o full.ttf
pyftsubset full.ttf --output-file=tools/fonts/Montserrat-Bold.ttf --no-hinting --desubroutinize \
  --layout-features=kern,liga,calt \
  --unicodes=U+0020-007E,U+00A0-00FF,U+00B7,U+2013,U+2022,U+2192,U+2197,U+25CF
```

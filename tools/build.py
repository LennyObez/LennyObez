"""Render the profile's SVG assets in light and dark variants.

Text is converted to glyph outlines from the committed brand fonts, so every viewer sees the same
typography without the SVG loading a font from anywhere.

Usage:
    python tools/build.py static            # header, project chips, stack, discipline tiles
    python tools/build.py stats             # activity card from the GitHub API (needs GH_TOKEN)
    python tools/build.py stats --payload FILE
                                            # also write a createCommitOnBranch file list if it changed
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape

import uharfbuzz as hb
from fontTools.pens.basePen import BasePen
from fontTools.ttLib import TTFont

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
FONTS = ROOT / "tools" / "fonts"
README = ROOT / "README.md"
USER = "LennyObez"
WIDTH = 640

PALETTES = {
    "light": {
        "page": "#ffffff",
        "window": "#f6f8fa",
        "bar": "#eaeef2",
        "border": "#d1d9e0",
        "text": "#1f2328",
        "muted": "#59636e",
        "accent": "#0039cb",
        "accent_text": "#0039cb",
        "on_accent": "#ffffff",
        "ok": "#1a7f37",
        # Each segment reaches 3:1 against the card; the legend names every one with its share.
        "series": ["#0039cb", "#6c8be8", "#132a6b", "#5b7fe8", "#2e5be6", "#7a8491"],
    },
    "dark": {
        "page": "#0d1117",
        "window": "#151b23",
        "bar": "#212830",
        "border": "#3d444d",
        "text": "#f0f6fc",
        "muted": "#9198a1",
        "accent": "#0039cb",
        "accent_text": "#8aa6ff",
        "on_accent": "#ffffff",
        "ok": "#3fb950",
        "series": ["#8aa6ff", "#4f74ee", "#c9d6ff", "#6d8cf0", "#a9bcff", "#9198a1"],
    },
}

# Meaningful non-text graphics (status dot, pictograms, chart series) need 3:1 against their
# background (WCAG 1.4.11); text pairs are recorded per drawing call and checked at 4.5:1.
GRAPHIC_PAIRS = [("ok", "bar"), ("accent_text", "window")] + [(f"series.{i}", "window") for i in range(6)]

STALE_DAYS = 30
STATS_BLOCK = re.compile(r"(<!-- stats:start -->\n)(?:.*?\n)?(<!-- stats:end -->)", re.S)
HACK_EXTENSIONS = (".hh", ".hhi", ".hack")


class Face:
    """One font file, shaped with HarfBuzz so kerning and ligatures match the browser."""

    def __init__(self, key: str, filename: str) -> None:
        self.key = key
        path = FONTS / filename
        self.tt = TTFont(path)
        self.upm = self.tt["head"].unitsPerEm
        self.cap = self.tt["OS/2"].sCapHeight
        self.glyphs = self.tt.getGlyphSet()
        self.order = self.tt.getGlyphOrder()
        self.cmap = self.tt.getBestCmap()
        self.hb_font = hb.Font(hb.Face(hb.Blob.from_file_path(str(path))))

    def shape(self, text: str):
        missing = [c for c in text if ord(c) not in self.cmap]
        if missing:
            raise ValueError(f"{self.key} has no glyph for {missing!r} in {text!r}")
        buf = hb.Buffer()
        buf.add_str(text)
        buf.guess_segment_properties()
        hb.shape(self.hb_font, buf, {"kern": True, "liga": True})
        return list(zip(buf.glyph_infos, buf.glyph_positions))

    def ink(self, runs) -> tuple[int, int]:
        """Highest and lowest ink of a shaped run in font units, descenders included."""
        glyf = self.tt["glyf"]
        tops, bottoms = [0], [0]
        for info, pos in runs:
            g = glyf[self.order[info.codepoint]]
            if g.numberOfContours:
                tops.append(g.yMax + pos.y_offset)
                bottoms.append(g.yMin + pos.y_offset)
        return max(tops), min(bottoms)

    def width(self, text: str, size: float) -> float:
        return sum(p.x_advance for _, p in self.shape(text)) * size / self.upm

    def path(self, gid: int) -> str:
        pen = CompactPen(self.glyphs)
        self.glyphs[self.order[gid]].draw(pen)
        return pen.data()


class CompactPen(BasePen):
    """SVG path data with relative commands on whole font units, smaller than absolute output.

    Points are rounded before the deltas are taken, so rounding never accumulates along a contour.
    """

    def __init__(self, glyph_set) -> None:
        super().__init__(glyph_set)
        self.out: list[str] = []
        self.last_cmd = ""
        self.pos = (0, 0)
        self.start = (0, 0)

    def _emit(self, cmd: str, *points) -> None:
        # Every point of a relative segment, control points included, is measured from its start.
        pts = [(round(x), round(y)) for x, y in points]
        nums = [v for x, y in pts for v in (x - self.pos[0], y - self.pos[1])]
        if cmd == "l" and nums[1] == 0:
            cmd, nums = "h", nums[:1]
        elif cmd == "l" and nums[0] == 0:
            cmd, nums = "v", nums[1:]
        self.pos = pts[-1]
        text = "" if cmd == self.last_cmd and cmd != "m" else cmd
        for n in nums:
            s = str(n)
            prev = text[-1:] if text else (self.out[-1][-1:] if self.out else "")
            if prev.isdigit() and not s.startswith("-"):
                text += " "
            text += s
        self.out.append(text)
        self.last_cmd = cmd

    def _moveTo(self, pt) -> None:
        self._emit("m", pt)
        self.start = self.pos

    def _lineTo(self, pt) -> None:
        self._emit("l", pt)

    def _qCurveToOne(self, p1, p2) -> None:
        self._emit("q", p1, p2)

    def _curveToOne(self, p1, p2, p3) -> None:
        self._emit("c", p1, p2, p3)

    def _closePath(self) -> None:
        self.out.append("z")
        self.last_cmd = "z"
        self.pos = self.start

    def data(self) -> str:
        return "".join(self.out)


BOLD = Face("b", "Montserrat-Bold.ttf")
MEDIUM = Face("m", "Montserrat-Medium.ttf")
MONO = Face("j", "JetBrainsMono-Medium.ttf")


@dataclass
class Doc:
    width: float
    height: float
    title: str
    desc: str
    parts: list[str] = field(default_factory=list)
    glyphs: dict[str, str] = field(default_factory=dict)
    style: str = ""
    # Filled areas (box, corner radius, fill) in drawing order, and each text run's box (advance width,
    # ink height) with the number of areas drawn before it. Raw add() markup (strokes, circles, logo
    # paths, pictograms) is not modelled, so nothing drawn that way may sit under or over text.
    areas: list[tuple[tuple[float, float, float, float], float, str]] = field(default_factory=list)
    texts: list[tuple[str, tuple[float, float, float, float], int]] = field(default_factory=list)
    # (text colour, colour of the area under it) for every run drawn; the tests check each at AA.
    pairs: set[tuple[str, str]] = field(default_factory=set)

    def add(self, markup: str) -> None:
        self.parts.append(markup)

    def area(self, markup: str, box: tuple[float, float, float, float], fill: str, rx: float = 0) -> None:
        self.add(markup)
        self.areas.append((box, rx, fill))

    def rect(self, x, y, w, h, fill, rx=0, stroke=None) -> None:
        s = f' stroke="{stroke}"' if stroke else ""
        self.area(f'<rect x="{x:g}" y="{y:g}" width="{w:g}" height="{h:g}" rx="{rx:g}" fill="{fill}"{s}/>',
                  (x, y, x + w, y + h), fill, rx)

    def under(self, box: tuple[float, float, float, float]) -> str:
        """Colour of the topmost area holding the whole box; any other colour overlapping it is refused."""
        for i in range(len(self.areas) - 1, -1, -1):
            area, rx, fill = self.areas[i]
            if contains(area, rx, box):
                return fill
            if overlaps(area, box):
                raise ValueError(f"an area of {fill} only partly covers text at {box}")
        raise ValueError(f"text at {box} is drawn outside every filled area")

    def text(self, face: Face, text: str, size: float, x: float, y: float, fill: str, on: str,
             anchor: str = "start") -> float:
        """Place outlined text with its baseline at y over background `on`; returns its width.

        The colour beneath the glyphs' ink is found from the areas drawn so far and must equal `on`;
        render() then refuses any area drawn over the text afterwards."""
        runs = face.shape(text)
        width = sum(p.x_advance for _, p in runs) * size / face.upm
        if anchor == "middle":
            x -= width / 2
        elif anchor == "end":
            x -= width
        scale = size / face.upm
        top, bottom = face.ink(runs)
        box = (x, y - top * scale, x + width, y - bottom * scale)
        beneath = self.under(box)
        if beneath != on:
            raise ValueError(f"{text!r} declared on {on} but drawn on {beneath}")
        self.texts.append((text, box, len(self.areas)))
        self.pairs.add((fill, beneath))
        pen_x = 0
        uses = []
        for info, pos in runs:
            gid = f"{face.key}{info.codepoint}"
            if gid not in self.glyphs:
                self.glyphs[gid] = face.path(info.codepoint)
            if self.glyphs[gid]:
                gx = pen_x + pos.x_offset
                gy = pos.y_offset
                coords = f' x="{gx}"' if gx else ""
                coords += f' y="{-gy}"' if gy else ""
                uses.append(f'<use href="#{gid}"{coords}/>')
            pen_x += pos.x_advance
        self.add(f'<g fill="{fill}" transform="translate({x:.1f} {y:.1f}) scale({scale:.5f} {-scale:.5f})">'
                 + "".join(uses) + "</g>")
        return width

    def render(self) -> str:
        for text, box, drawn_before in self.texts:
            for area, _, fill in self.areas[drawn_before:]:
                if overlaps(area, box):
                    raise ValueError(f"an area of {fill} is painted over {text!r}")
        defs ="".join(f'<path id="{k}" d="{v}"/>' for k, v in self.glyphs.items() if v)
        style = f"<style>{self.style}</style>" if self.style else ""
        return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.width:g}" height="{self.height:g}" '
                f'viewBox="0 0 {self.width:g} {self.height:g}" role="img" aria-labelledby="t d">'
                f'<title id="t">{escape(self.title)}</title><desc id="d">{escape(self.desc)}</desc>'
                f"{style}<defs>{defs}</defs>" + "".join(self.parts) + "</svg>\n")


def overlaps(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def contains(area: tuple[float, float, float, float], rx: float, box: tuple[float, float, float, float]) -> bool:
    """Whether a rounded rectangle holds the whole box, cut corners included."""
    x0, y0, x1, y1 = area
    if not (x0 <= box[0] and y0 <= box[1] and box[2] <= x1 and box[3] <= y1):
        return False
    for px in (box[0], box[2]):
        for py in (box[1], box[3]):
            cx = min(max(px, x0 + rx), x1 - rx)
            cy = min(max(py, y0 + rx), y1 - rx)
            if (px - cx) ** 2 + (py - cy) ** 2 > rx ** 2:
                return False
    return True


def baseline(face: Face, size: float, top: float, height: float) -> float:
    """Baseline that centres capital letters vertically in a box."""
    return top + height / 2 + face.cap * size / face.upm / 2


def window(doc: Doc, p: dict, label: str, height: float) -> None:
    doc.rect(0.5, 0.5, doc.width - 1, height - 1, p["window"], rx=12, stroke=p["border"])
    doc.area(f'<path d="M0.5 12.5a12 12 0 0 1 12-12h{doc.width - 25:g}a12 12 0 0 1 12 12v23.5h-{doc.width - 1:g}z" '
             f'fill="{p["bar"]}" stroke="{p["border"]}"/>', (0.5, 0.5, doc.width - 0.5, 36), p["bar"], 12)
    for i in range(3):
        doc.add(f'<circle cx="{22 + i * 18}" cy="18" r="5" fill="{p["border"]}"/>')
    doc.text(MONO, label, 12, doc.width / 2, baseline(MONO, 12, 0, 36), p["muted"], p["bar"], "middle")


HEADER_DESC = ("Lenny Obez, senior full-stack developer, photographer and explorer in Huy, Belgium. "
               "PHP 8.5, Rust, AWS, author of Pulsar Framework. Open to freelance projects. "
               "11+ years building for the web.")

# The development mark from the lennyobez.com press kit, paths and colours unchanged, on its dark
# tile (corners rounded here) so the light "L" stays legible in both themes without recolouring.
LOGO_TILE = "#0D0D1A"
LOGO_RING = ("M12.5 15.5A6.5 6.5 0 1 1 25.5 15.5A6.5 6.5 0 1 1 12.5 15.5Z"
             "M15.7 15.5A3.3 3.3 0 1 1 22.3 15.5A3.3 3.3 0 1 1 15.7 15.5Z")
LOGO_L = "M7 9H10.5V18.5H15V22H7Z"
# The brand mark's own colours, which may not be altered: the only colours outside the palette that
# the tests accept. Like the window outline and dots, they carry no information.
DECORATIVE = {"#0039CB", "#E2E8F0"}


def header(p: dict) -> Doc:
    h = 268
    doc = Doc(WIDTH, h, "Lenny Obez, senior full-stack developer", HEADER_DESC)
    # One short pulse (two 2 s cycles), switched off entirely under reduced motion.
    # Hidden at rest, so the end of the animation looks exactly like reduced motion.
    doc.style = (".pulse{opacity:0;transform-box:fill-box;transform-origin:center;animation:pulse 2s ease-out 2}"
                 "@keyframes pulse{from{transform:scale(1);opacity:.7}to{transform:scale(3);opacity:0}}"
                 "@media (prefers-reduced-motion:reduce){.pulse{animation:none;opacity:0}}")
    window(doc, p, "lennyobez.com", h)
    # The press-kit file draws the mark at scale 30 on a 1024 tile; here the tile is 136 px.
    size, cx, cy = 136, 540, 131
    k = size / 1024
    # A decorative border hints at the tile's edge on the dark window, where both are near black.
    doc.rect(cx - size / 2 + 0.5, cy - size / 2 + 0.5, size - 1, size - 1, LOGO_TILE, rx=18, stroke=p["border"])
    doc.add(f'<g transform="translate({cx} {cy}) scale({30 * k:.4f}) translate(-16.25 -15.5)">'
            f'<path fill="#0039CB" fill-rule="evenodd" d="{LOGO_RING}"/><path fill="#E2E8F0" d="{LOGO_L}"/></g>')
    doc.text(BOLD, "Lenny Obez", 46, 32, 96, p["text"], p["window"])
    doc.text(MEDIUM, "Senior full-stack developer", 22, 32, 130, p["accent_text"], p["window"])
    doc.text(MEDIUM, "Photographer and explorer · Huy, Belgium", 15, 32, 156, p["muted"], p["window"])
    x = 32
    for i, label in enumerate(("PHP 8.5", "Rust", "AWS", "Pulsar Framework")):
        w = MONO.width(label, 13) + 20
        if i == 0:
            doc.rect(x, 174, w, 26, p["accent"], rx=6)
            doc.text(MONO, label, 13, x + 10, baseline(MONO, 13, 174, 26), p["on_accent"], p["accent"])
        else:
            doc.rect(x + 0.5, 174.5, w - 1, 25, p["window"], rx=6, stroke=p["border"])
            doc.text(MONO, label, 13, x + 10, baseline(MONO, 13, 174, 26), p["text"], p["window"])
        x += w + 8
    doc.area(f'<path d="M0.5 226.5h{doc.width - 1:g}v29a12 12 0 0 1-12 12h-{doc.width - 25:g}a12 12 0 0 1-12-12z" '
             f'fill="{p["bar"]}" stroke="{p["border"]}"/>', (0.5, 226.5, doc.width - 0.5, 267.5), p["bar"], 12)
    doc.add(f'<circle class="pulse" cx="38" cy="247" r="4" fill="{p["ok"]}"/>')
    doc.add(f'<circle cx="38" cy="247" r="4" fill="{p["ok"]}"/>')
    doc.text(MONO, "open to freelance projects", 12, 52, baseline(MONO, 12, 227, 40), p["text"], p["bar"])
    doc.text(MONO, "11+ years building for the web", 12, doc.width - 24, baseline(MONO, 12, 227, 40),
             p["muted"], p["bar"], "end")
    return doc


PROJECTS = {
    "pulsar-framework": ["PHP 8.5+", "Apache-2.0", "pre-1.0"],
    "adiungere": ["Rust", "Apache-2.0"],
    "bothdecks": ["PHP", "source-available"],
    "to-spec": ["JavaScript", "Apache-2.0"],
}
# Chips keep their natural size (height="25") only if they fit a phone's content column.
CHIP_MAX_WIDTH = 278  # 360 px screen minus the profile page's own padding and README box border


def chips(labels: list[str], p: dict) -> Doc:
    size, pad, gap, h = 12, 9, 6, 24
    widths = [MONO.width(label, size) + 2 * pad for label in labels]
    text = ", ".join(labels)
    doc = Doc(sum(widths) + gap * (len(labels) - 1) + 1, h + 1, text, text)
    if doc.width > CHIP_MAX_WIDTH:
        raise ValueError(f"chips {text!r} are {doc.width:.0f}px wide and would shrink on a phone")
    x = 0.5
    for label, w in zip(labels, widths):
        doc.rect(x, 0.5, w - 1, h - 1, p["window"], rx=5, stroke=p["border"])
        doc.text(MONO, label, size, x + pad - 0.5, baseline(MONO, size, 0, h), p["text"], p["window"])
        x += w + gap
    return doc


STACK = [
    ("Languages", ["PHP 8.5", "Rust", "TypeScript", "JavaScript", "SQL"]),
    ("Frameworks", ["Pulsar", "Laravel", "Symfony", "Vue.js"]),
    ("Data", ["PostgreSQL", "MariaDB", "MySQL", "SQLite", "Redis"]),
    ("Cloud and operations", ["AWS", "Nginx", "HTTP/3", "Docker", "GitHub Actions", "Linux"]),
    ("Quality", ["PHPUnit", "PHPStan max", "Psalm", "Infection"]),
    ("Security", ["OWASP Top 10", "OAuth2 / OIDC", "CSP", "GDPR by design"]),
]
STACK_DESC = " ".join(f"{label}: {', '.join(items)}." for label, items in STACK)


def stack(p: dict) -> Doc:
    size, pad, gap, ch = 15, 11, 8, 32
    y = 66
    rows = []
    for label, items in STACK:
        rows.append(("label", label, y))
        y += 14
        x = 24
        for item in items:
            w = MONO.width(item, size) + 2 * pad
            if x + w > WIDTH - 24:
                x, y = 24, y + ch + gap
            rows.append(("chip", item, (x, y, w)))
            x += w + gap
        y += ch + 26
    h = y - 4
    doc = Doc(WIDTH, h, "Tech stack", STACK_DESC)
    window(doc, p, "stack", h)
    for kind, value, pos in rows:
        if kind == "label":
            doc.text(BOLD, value, 14, 24, pos, p["muted"], p["window"])
        else:
            x, cy, w = pos
            doc.rect(x + 0.5, cy + 0.5, w - 1, ch - 1, p["page"], rx=6, stroke=p["border"])
            doc.text(MONO, value, size, x + pad, baseline(MONO, size, cy, ch), p["text"], p["page"])
    return doc


DISCIPLINES = {
    "developer": ("Developer", "lennyobez.com/development"),
    "photographer": ("Photographer", "lennyobez.com/photography"),
    "explorer": ("Explorer", "lennyobez.com/explore"),
}

ICONS = {
    # Simple line pictograms drawn on a 32 by 32 grid.
    "developer": "M11 9 4 16l7 7M21 9l7 7-7 7M18 6l-4 20",
    "photographer": "M4 11h6l2-3h8l2 3h6v14H4zM16 13a5 5 0 1 0 0 10 5 5 0 0 0 0-10",
    "explorer": "M3 26 12 12l5 7 4-5 8 12zM12 12V5l6 2.5-6 2.5",
}


def tile(key: str, p: dict) -> Doc:
    # Only a pictogram and one word: the tiles shrink to a third of a phone screen.
    label, url = DISCIPLINES[key]
    w, h = 200, 104
    doc = Doc(w, h, label, f"{label}, on {url}")
    doc.rect(0.5, 0.5, w - 1, h - 1, p["window"], rx=12, stroke=p["border"])
    doc.add(f'<path d="{ICONS[key]}" transform="translate(18 16)" fill="none" stroke="{p["accent_text"]}" '
            'stroke-width="2.2" stroke-linejoin="round" stroke-linecap="round"/>')
    doc.text(MONO, "↗", 16, w - 18, 36, p["muted"], p["window"], "end")
    if BOLD.width(label, 22) > w - 36:
        raise ValueError(f"{label!r} overflows its tile")
    doc.text(BOLD, label, 22, 18, 84, p["text"], p["window"])
    return doc


def api(path: str, token: str, body: dict | None = None):
    req = urllib.request.Request(
        "https://api.github.com/" + path,
        data=json.dumps(body).encode() if body else None,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "User-Agent": "profile-stats-renderer", "X-GitHub-Api-Version": "2022-11-28"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def has_hack_files(repo: dict, token: str, fetch=api) -> bool:
    tree = fetch(f"repos/{USER}/{repo['name']}/git/trees/{repo['default_branch']}?recursive=1", token)
    if tree.get("truncated"):
        return True  # cannot prove the absence, so keep GitHub's figure
    return any(e["path"].lower().endswith(HACK_EXTENSIONS) for e in tree["tree"] if e["type"] == "blob")


def collect(token: str, fetch=api) -> dict:
    user = fetch(f"users/{USER}", token)
    repos, page = [], 1
    while True:
        batch = fetch(f"users/{USER}/repos?type=owner&per_page=100&page={page}", token)
        repos += batch
        if len(batch) < 100:
            break
        page += 1
    public = [r for r in repos if not r["fork"] and not r["private"]]
    own = [r for r in public if not r["archived"] and r["name"].lower() != USER.lower()]
    languages: dict[str, int] = {}
    for repo in own:
        sizes = fetch(f"repos/{USER}/{repo['name']}/languages", token)
        # GitHub's classifier labels some .php files as Hack; with no Hack file in the tree, they are PHP.
        # The README states this adjustment next to the card.
        if "Hack" in sizes and not has_hack_files(repo, token, fetch):
            sizes["PHP"] = sizes.get("PHP", 0) + sizes.pop("Hack")
        for lang, size in sizes.items():
            languages[lang] = languages.get(lang, 0) + size
    query = ("query($login:String!){user(login:$login){contributionsCollection"
             "{contributionCalendar{totalContributions}}}}")
    graph = fetch("graphql", token, {"query": query, "variables": {"login": USER}})
    if graph.get("errors"):
        raise RuntimeError(graph["errors"])
    return {
        "contributions": graph["data"]["user"]["contributionsCollection"]["contributionCalendar"]
        ["totalContributions"],
        "repositories": user["public_repos"],
        "stars": sum(r["stargazers_count"] for r in public),
        "followers": user["followers"],
        "languages": dict(sorted(languages.items(), key=lambda kv: -kv[1])),
    }


def share(languages: dict[str, int], top: int = 5) -> list[tuple[str, float]]:
    total = sum(languages.values()) or 1
    items = list(languages.items())
    head = [(k, v / total * 100) for k, v in items[:top]]
    rest = sum(v for _, v in items[top:]) / total * 100
    return head + ([("Other", rest)] if rest > 0 else [])


def stats_sentence(data: dict, day: str) -> str:
    lang_text = ", ".join(f"{k} {v:.1f}%" for k, v in share(data["languages"]))
    return (f"{data['contributions']:,} contributions in the last 12 months, "
            f"{data['repositories']} public repositories, {data['stars']} stars, {data['followers']} followers. "
            f"Languages by bytes across own public repositories: {lang_text}. Data as of {day}.")


def stats_card(data: dict, day: str, p: dict) -> Doc:
    parts = share(data["languages"])
    rows = (len(parts) + 2) // 3
    h = 236 + rows * 28
    metrics = [(f"{data['contributions']:,}", "contributions", "last 12 months"),
               (str(data["repositories"]), "repositories", "public"),
               (str(data["stars"]), "stars", "own public repos"),
               (str(data["followers"]), "followers", "on GitHub")]
    doc = Doc(WIDTH, h, "GitHub activity", stats_sentence(data, day))
    window(doc, p, f"github activity · data as of {day}", h)
    cw = (WIDTH - 48 - 3 * 10) / 4
    for i, (value, label, sub) in enumerate(metrics):
        x = 24 + i * (cw + 10)
        for face, text, size in ((BOLD, value, 28), (MEDIUM, label, 14), (MEDIUM, sub, 13)):
            if face.width(text, size) > cw - 24:
                raise ValueError(f"{text!r} overflows its metric box")
        doc.rect(x + 0.5, 52.5, cw - 1, 90, p["page"], rx=8, stroke=p["border"])
        doc.text(BOLD, value, 28, x + 12, 88, p["text"], p["page"])
        doc.text(MEDIUM, label, 14, x + 12, 111, p["text"], p["page"])
        doc.text(MEDIUM, sub, 13, x + 12, 129, p["muted"], p["page"])
    doc.text(BOLD, "Languages by bytes, own public repositories", 14, 24, 172, p["muted"], p["window"])
    x, bw = 24, WIDTH - 48
    for i, (_, pct) in enumerate(parts):
        seg = max(bw * pct / 100 - 2, 1.5)
        doc.rect(x, 184, seg, 12, p["series"][i], rx=2)
        x += seg + 2
    for i, (name, pct) in enumerate(parts):
        col, row = i % 3, i // 3
        lx, ly = 24 + col * 200, 226 + row * 28
        if MEDIUM.width(name, 15) + MONO.width(f"{pct:.1f}%", 14) > 180 - 21 - 8:
            raise ValueError(f"legend entry {name!r} would run into its percentage")
        doc.rect(lx, ly - 11, 13, 13, p["series"][i], rx=3)
        doc.text(MEDIUM, name, 15, lx + 21, ly + 2, p["text"], p["window"])
        doc.text(MONO, f"{pct:.1f}%", 14, lx + 180, ly + 2, p["muted"], p["window"], "end")
    return doc


def static_docs() -> dict[str, Doc]:
    """Every static asset, keyed by its path under assets/, rendered in memory."""
    docs = {}
    for theme, p in PALETTES.items():
        docs[f"header-{theme}.svg"] = header(p)
        docs[f"stack-{theme}.svg"] = stack(p)
        for name, labels in PROJECTS.items():
            docs[f"projects/{name}-{theme}.svg"] = chips(labels, p)
        for key in DISCIPLINES:
            docs[f"disciplines/{key}-{theme}.svg"] = tile(key, p)
    return docs


def stats_docs(record: dict) -> dict[str, Doc]:
    return {f"stats-{t}.svg": stats_card(record["data"], record["date"], p) for t, p in PALETTES.items()}


def write(path: Path, content: str) -> bool:
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    return True


def build_static() -> list[Path]:
    out = []
    for rel, doc in static_docs().items():
        write(ASSETS / rel, doc.render())
        out.append(ASSETS / rel)
    return out


def refresh_stats(data: dict, root: Path, today: str) -> list[Path]:
    """Write the card, README block and stats.json under `root` for `data`; return the changed files."""
    assets, readme = root / "assets", root / "README.md"
    stamp = assets / "stats.json"
    # The card carries a date: keep the previous one only when the stored figures are the same, the cards
    # would render identically and the date is under STALE_DAYS old. With the weekly cron a commit lands
    # within STALE_DAYS + 7 days, well inside the 60 idle days after which GitHub disables the schedule.
    record = {"date": today, "data": data}
    stored = json.loads(stamp.read_text(encoding="utf-8")) if stamp.exists() else None
    if (stored and stored["data"] == data and (datetime.fromisoformat(today) - datetime.fromisoformat(stored["date"])).days
            < STALE_DAYS):
        previous = {"date": stored["date"], "data": data}
        if all((assets / rel).exists() and (assets / rel).read_text(encoding="utf-8") == doc.render()
               for rel, doc in stats_docs(previous).items()):
            record = previous
    # The figures are also written out as text in the README, where every reader can reach them.
    # Checked before anything is written, so a malformed README never leaves a half-updated card.
    sentence = stats_sentence(data, record["date"])
    updated, count = STATS_BLOCK.subn(lambda m: m.group(1) + sentence + "\n" + m.group(2),
                                      readme.read_text(encoding="utf-8"))
    if count != 1:
        raise ValueError(f"expected one stats block in README.md, found {count}")
    changed = [assets / rel for rel, doc in stats_docs(record).items() if write(assets / rel, doc.render())]
    if write(readme, updated):
        changed.append(readme)
    if write(stamp, json.dumps(record, indent=2) + "\n"):
        changed.append(stamp)
    return changed


def commit_payload(changed: list[Path], root: Path) -> str:
    return json.dumps([{"path": f.relative_to(root).as_posix(),
                        "contents": base64.b64encode(f.read_bytes()).decode()} for f in changed])


def build_stats(payload: Path | None) -> None:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        sys.exit("GH_TOKEN is required to read the GitHub API")
    changed = refresh_stats(collect(token), ROOT, datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    if not changed:
        print("stats unchanged")
        return
    print("stats updated:", ", ".join(f.relative_to(ROOT).as_posix() for f in changed))
    if payload:
        payload.write_text(commit_payload(changed, ROOT), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("target", choices=["static", "stats"])
    parser.add_argument("--payload", type=Path)
    args = parser.parse_args()
    if args.target == "static":
        for path in build_static():
            print(path.relative_to(ROOT).as_posix(), path.stat().st_size)
    else:
        build_stats(args.payload)


if __name__ == "__main__":
    main()

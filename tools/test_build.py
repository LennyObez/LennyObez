"""Guards for the profile: run with `python -B -m unittest discover -s tools`.

Run that way, the tests never write into the repository; stats refreshes run in a temporary copy."""

from __future__ import annotations

import base64
import json
import re
import shutil
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from html.parser import HTMLParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import build  # noqa: E402
from fontTools.pens.basePen import BasePen  # noqa: E402

ROOT = build.ROOT
README = build.README.read_text(encoding="utf-8")
STATS = json.loads((build.ASSETS / "stats.json").read_text(encoding="utf-8"))
SVG_NS = "{http://www.w3.org/2000/svg}"
FILE_BUDGET = 40_000
THEME_BUDGET = 150_000


def luminance(hex_colour: str) -> float:
    channels = [int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(a: str, b: str) -> float:
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def all_docs() -> dict[str, build.Doc]:
    return {**build.static_docs(), **build.stats_docs(STATS)}


def committed_bytes() -> dict[str, bytes]:
    return {p.relative_to(build.ASSETS).as_posix(): p.read_bytes() for p in sorted(build.ASSETS.rglob("*.svg"))}


def committed_svgs() -> dict[str, str]:
    return {p.relative_to(build.ASSETS).as_posix(): p.read_text(encoding="utf-8")
            for p in sorted(build.ASSETS.rglob("*.svg"))}


class Palette(unittest.TestCase):
    def test_every_text_drawn_reaches_wcag_aa_on_its_background(self):
        for rel, doc in all_docs().items():
            for fg, bg in doc.pairs:
                with self.subTest(asset=rel, fg=fg, bg=bg):
                    self.assertGreaterEqual(contrast(fg, bg), 4.5)

    def test_every_colour_drawn_comes_from_the_palette_or_is_declared_decorative(self):
        # Parsed, not pattern-matched: only known elements, colours only in fill and stroke attributes,
        # each a palette, series or declared brand colour; the style sheet may set no colour at all.
        allowed_tags = {"svg", "title", "desc", "style", "defs", "path", "g", "use", "rect", "circle"}
        for rel, doc in all_docs().items():
            p = build.PALETTES["dark" if "-dark" in rel else "light"]
            named = {v for v in p.values() if isinstance(v, str)} | set(p["series"])
            known = {c.lower() for c in named | build.DECORATIVE | {build.LOGO_TILE}}
            svg = doc.render()
            self.assertNotIn("<![CDATA[", svg, rel)
            for el in ET.fromstring(svg).iter():
                tag = el.tag.split("}")[-1]
                with self.subTest(asset=rel, tag=tag):
                    self.assertIn(tag, allowed_tags)
                    if tag == "style":
                        self.assertEqual(el.attrib, {})
                        css = re.sub(r"/\*.*?\*/", "", el.text or "", flags=re.S).lower()
                        props = re.findall(r"([a-z-]+)\s*:", css)
                        self.assertFalse([pr for pr in props if pr.endswith(("color", "fill", "stroke"))], props)
                    for name, value in el.attrib.items():
                        name = name.split("}")[-1].lower()
                        self.assertNotEqual(name, "style")
                        self.assertFalse(name.endswith("color"), name)
                        if name in ("fill", "stroke"):
                            self.assertTrue(value == "none" or value.lower() in known, value)

    def test_meaningful_graphics_reach_three_to_one(self):
        for theme, p in build.PALETTES.items():
            for fg, bg in build.GRAPHIC_PAIRS:
                colour = p["series"][int(fg.split(".")[1])] if fg.startswith("series.") else p[fg]
                with self.subTest(theme=theme, fg=fg, bg=bg):
                    self.assertGreaterEqual(contrast(colour, p[bg]), 3)


class Absolute(BasePen):
    """Reference outline: absolute points rounded to whole units, as CompactPen rounds them."""

    def __init__(self, glyph_set):
        super().__init__(glyph_set)
        self.points: list[tuple[str, tuple[int, int]]] = []

    def _add(self, kind, *pts):
        for pt in pts:
            self.points.append((kind, (round(pt[0]), round(pt[1]))))

    def _moveTo(self, pt):
        self._add("m", pt)

    def _lineTo(self, pt):
        self._add("l", pt)

    def _qCurveToOne(self, p1, p2):
        self._add("q", p1, p2)

    def _curveToOne(self, p1, p2, p3):
        self._add("c", p1, p2, p3)

    def _closePath(self):
        self.points.append(("z", (0, 0)))


def parse_relative(d: str) -> list[tuple[str, tuple[int, int]]]:
    """Resolve CompactPen output back to absolute points, following the SVG path grammar."""
    tokens = re.findall(r"[mlhvqcz]|-?\d+", d)
    out, pos, start, cmd, i = [], (0, 0), (0, 0), None, 0
    arity = {"m": 2, "l": 2, "h": 1, "v": 1, "q": 4, "c": 6, "z": 0}
    while i < len(tokens):
        if tokens[i].isalpha():
            cmd = tokens[i]
            i += 1
            if cmd == "z":
                out.append(("z", (0, 0)))
                pos = start
                continue
        nums = [int(t) for t in tokens[i:i + arity[cmd]]]
        i += arity[cmd]
        if cmd == "m":
            pos = (pos[0] + nums[0], pos[1] + nums[1])
            start = pos
            out.append(("m", pos))
            cmd = "l"  # implicit repeats after a moveto are linetos
        elif cmd in "lhv":
            dx, dy = (nums[0], nums[1]) if cmd == "l" else ((nums[0], 0) if cmd == "h" else (0, nums[0]))
            pos = (pos[0] + dx, pos[1] + dy)
            out.append(("l", pos))
        else:
            pts = [(pos[0] + nums[k], pos[1] + nums[k + 1]) for k in range(0, len(nums), 2)]
            out += [(cmd, p) for p in pts]
            pos = pts[-1]
    return out


class Guards(unittest.TestCase):
    """Each generator guard must refuse the mistake it exists for."""

    def doc(self):
        d = build.Doc(200, 100, "t", "d")
        d.rect(0, 0, 200, 100, "#ffffff", rx=12)
        return d

    def test_text_on_a_declared_colour_that_is_not_beneath_it_is_refused(self):
        with self.assertRaises(ValueError):
            self.doc().text(build.MEDIUM, "Text", 14, 40, 50, "#1f2328", "#000000")

    def test_an_area_painted_over_text_afterwards_is_refused(self):
        d = self.doc()
        d.text(build.MEDIUM, "Text", 14, 40, 50, "#1f2328", "#ffffff")
        d.rect(30, 30, 100, 30, "#0039cb")
        with self.assertRaises(ValueError):
            d.render()

    def test_an_area_that_only_partly_covers_text_is_refused(self):
        d = self.doc()
        d.rect(30, 30, 25, 30, "#0039cb")
        with self.assertRaises(ValueError):
            d.text(build.MEDIUM, "Text", 14, 40, 50, "#1f2328", "#ffffff")

    def test_text_in_a_cut_corner_or_with_a_descender_outside_is_refused(self):
        # The box's top-left corner sits 1 px outside the 12 px corner arc, inside the square bounds.
        d = self.doc()
        ink_top, _ = build.MEDIUM.ink(build.MEDIUM.shape("Text"))
        top = 12 - (12 + 1) / 2 ** 0.5
        with self.assertRaises(ValueError):
            d.text(build.MEDIUM, "Text", 14, top, top + ink_top * 14 / build.MEDIUM.upm, "#1f2328", "#ffffff")
        d = self.doc()
        d.rect(20, 40, 160, 12, "#f6f8fa")  # baseline at its bottom edge: the "y" tail leaves it
        with self.assertRaises(ValueError):
            d.text(build.MEDIUM, "Typ", 14, 30, 52, "#1f2328", "#f6f8fa")

    def test_the_pulse_is_hidden_at_rest_like_under_reduced_motion(self):
        style = build.header(build.PALETTES["light"]).style
        self.assertRegex(style, r"\.pulse\{opacity:0;")
        self.assertRegex(style, r"prefers-reduced-motion:reduce\)\{\.pulse\{animation:none;opacity:0\}")

    def test_the_logo_tile_is_a_registered_area(self):
        # Registered areas are what the text checks see; a tile drawn through add() would be invisible.
        for p in build.PALETTES.values():
            self.assertIn(((472.5, 63.5, 607.5, 198.5), 18, build.LOGO_TILE), build.header(p).areas)

    def test_the_header_description_covers_every_line_it_draws(self):
        for p in build.PALETTES.values():
            doc = build.header(p)
            for text, _, _ in doc.texts[1:]:  # the first run is the window's address label
                for piece in text.split(" · "):
                    with self.subTest(piece=piece):
                        self.assertIn(piece.lower(), doc.desc.lower())

    def test_chips_too_wide_for_a_phone_are_refused(self):
        with self.assertRaises(ValueError):
            build.chips(["a very long label", "another long label", "and a third one"], build.PALETTES["light"])

    def test_a_legend_entry_that_would_hit_its_percentage_is_refused(self):
        data = {**STATS["data"], "languages": {"Visual Basic .NET": 10, "PHP": 5}}
        with self.assertRaises(ValueError):
            build.stats_card(data, "2999-01-01", build.PALETTES["light"])

    def test_a_character_missing_from_the_font_is_refused(self):
        with self.assertRaises(ValueError):
            build.MEDIUM.shape("中")


class Outlines(unittest.TestCase):
    def test_compact_paths_match_the_font_outlines(self):
        for face in (build.BOLD, build.MEDIUM, build.MONO):
            for gid, name in enumerate(face.order):
                ref = Absolute(face.glyphs)
                face.glyphs[name].draw(ref)
                with self.subTest(font=face.key, glyph=name):
                    self.assertEqual(parse_relative(face.path(gid)), ref.points)


class Assets(unittest.TestCase):
    def test_committed_svgs_are_exactly_the_generator_output(self):
        expected = {rel: doc.render().encode("utf-8") for rel, doc in all_docs().items()}
        committed = committed_bytes()
        self.assertEqual(sorted(committed), sorted(expected), "unexpected or missing SVG files")
        for rel, content in expected.items():
            with self.subTest(asset=rel):
                self.assertEqual(committed[rel], content, "run tools/build.py to regenerate")

    def test_svgs_are_self_contained_and_labelled(self):
        for rel, text in committed_svgs().items():
            with self.subTest(asset=rel):
                root = ET.fromstring(text)
                self.assertEqual(root.get("role"), "img")
                title = root.find(f"{SVG_NS}title")
                desc = root.find(f"{SVG_NS}desc")
                self.assertTrue(title is not None and title.text and title.text.strip())
                self.assertTrue(desc is not None and desc.text and desc.text.strip())
                self.assertNotRegex(text, r"https?://(?!www\.w3\.org/2000/svg)")
                self.assertNotRegex(text, r"<script|<image|<foreignObject|@import|@font-face|url\(")

    def test_motion_is_short_and_honours_reduced_motion(self):
        for rel, text in committed_svgs().items():
            with self.subTest(asset=rel):
                self.assertNotRegex(text, r"<(animate|animateMotion|animateTransform|set)\b")
                declarations = re.findall(r"animation(?:-[a-z-]+)?\s*:\s*([^;}]+)", text)
                running = [d for d in declarations if d.strip() != "none"]
                for d in running:
                    m = re.fullmatch(r"[\w-]+ ([\d.]+)s [\w-]+ (\d+)", d.strip())
                    self.assertIsNotNone(m, f"animation {d!r} is not in the checked form")
                    self.assertLessEqual(float(m.group(1)) * int(m.group(2)), 5)
                if running:
                    self.assertRegex(text, r"@media \(prefers-reduced-motion:reduce\)\{[^}]*animation:none")

    def test_weight_budget(self):
        sizes = {p.relative_to(build.ASSETS).as_posix(): p.stat().st_size for p in build.ASSETS.rglob("*.svg")}
        for rel, size in sizes.items():
            with self.subTest(asset=rel):
                self.assertLessEqual(size, FILE_BUDGET)
        for theme in build.PALETTES:
            total = sum(s for rel, s in sizes.items() if rel.endswith(f"-{theme}.svg"))
            with self.subTest(theme=theme):
                self.assertLessEqual(total, THEME_BUDGET)


class Markup(HTMLParser):
    def __init__(self):
        super().__init__()
        self.imgs, self.links, self.pictures = [], [], []
        self._picture = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "picture":
            self._picture = {"sources": [], "img": None}
        elif tag == "source" and self._picture is not None:
            self._picture["sources"].append(a)
        elif tag == "img":
            self.imgs.append(a)
            if self._picture is not None:
                self._picture["img"] = a
        elif tag == "a":
            self.links.append(a.get("href", ""))

    def handle_endtag(self, tag):
        if tag == "picture":
            self.pictures.append(self._picture)
            self._picture = None


class Readme(unittest.TestCase):
    parsed = Markup()
    parsed.feed(README)

    def test_every_image_sits_in_a_themed_picture(self):
        self.assertEqual(len(self.parsed.pictures), len(self.parsed.imgs))
        for pic in self.parsed.pictures:
            media = {s["media"]: s["srcset"] for s in pic["sources"]}
            with self.subTest(img=pic["img"]["src"]):
                self.assertEqual(set(media), {"(prefers-color-scheme: dark)", "(prefers-color-scheme: light)"})
                self.assertEqual(media["(prefers-color-scheme: light)"], pic["img"]["src"])
                self.assertEqual(media["(prefers-color-scheme: dark)"],
                                 pic["img"]["src"].replace("-light.svg", "-dark.svg"))
                for src in media.values():
                    self.assertTrue((ROOT / src).is_file(), src)

    def test_alt_text_says_what_the_image_says(self):
        for img in self.parsed.imgs:
            if img["src"] == "assets/stats-light.svg":
                # Its figures change weekly and are written out in the stats block checked below.
                self.assertEqual(img.get("alt"), STATS_ALT)
                continue
            desc = ET.fromstring((ROOT / img["src"]).read_text(encoding="utf-8")).find(f"{SVG_NS}desc").text
            with self.subTest(src=img["src"]):
                self.assertEqual(img.get("alt"), desc)

    def visible(self, markdown: str) -> str:
        """README text a reader sees as text: tags (and so alt attributes) removed."""
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", markdown)).lower()

    def test_every_line_drawn_in_the_header_is_also_readable_text(self):
        # Search engines and Ctrl+F see no text inside the SVG outlines; About and Contact restate it.
        sections = re.findall(r"^## (?:About|Contact)\n(.*?)(?=^## |\Z)", README, re.S | re.M)
        self.assertEqual(len(sections), 2)
        text = self.visible(" ".join(sections))
        for drawn, _, _ in build.header(build.PALETTES["light"]).texts[1:]:
            for piece in drawn.split(" · "):
                with self.subTest(piece=piece):
                    self.assertIn(piece.lower(), text)

    def test_each_project_states_its_chips_in_text(self):
        for name, labels in build.PROJECTS.items():
            section = re.search(rf"^### \[[^\]]+\]\(https://github\.com/LennyObez/{re.escape(name)}\)\n(.*?)(?=^##)",
                                README, re.S | re.M)
            self.assertIsNotNone(section, name)
            # The paragraph's last sentence restates the chips, so they never exist only as an image.
            paragraphs = [p for p in section.group(1).split("\n\n") if p.strip() and not p.lstrip().startswith("<")]
            last = re.split(r"(?<=\.) ", paragraphs[0].strip())[-1]
            with self.subTest(project=name):
                self.assertEqual(last.replace(" licence", ""), ", ".join(labels) + ".")

    def test_the_stack_as_text_matches_the_stack_image(self):
        block = re.search(r"<summary>The stack as text</summary>\n\n(.*?)\n\n</details>", README, re.S)
        self.assertIsNotNone(block)
        self.assertEqual(block.group(1).splitlines(),
                         [f"- {label}: {', '.join(items)}" for label, items in build.STACK])

    def test_stats_block_matches_the_card(self):
        block = re.search(r"<!-- stats:start -->\n(.*?)\n<!-- stats:end -->", README, re.S)
        self.assertIsNotNone(block)
        self.assertEqual(block.group(1), build.stats_sentence(STATS["data"], STATS["date"]))

    def test_links_are_absolute(self):
        # The profile page and the repository page resolve relative links differently.
        md_links = re.findall(r"\]\(([^)]+)\)", README)
        for href in self.parsed.links + md_links:
            with self.subTest(href=href):
                self.assertRegex(href, r"^(https://|mailto:)")

    def test_heading_levels_do_not_skip(self):
        # The profile page supplies the h1 (the account name), so the README starts at h2.
        levels = [len(m) for m in re.findall(r"^(#+) ", README, flags=re.M)]
        self.assertEqual(levels[0], 2)
        for prev, cur in zip(levels, levels[1:]):
            self.assertLessEqual(cur - prev, 1)

    def test_section_headings_are_in_sentence_case(self):
        # Level-3 headings are project names and keep their own capitalisation.
        for heading in re.findall(r"^## (.+)$", README, flags=re.M):
            with self.subTest(heading=heading):
                for word in heading.split()[1:]:
                    self.assertTrue(word in PROPER_NOUNS or word == word.lower(), word)


STATS_ALT = "GitHub activity card. The same figures are written out below it."
PROPER_NOUNS = {"GitHub"}
DASHES = [chr(c) for c in range(0x2012, 0x2016)]
BANNED = ["designed to", "seamless", "leverag", "furthermore", "in today's",
          "it's worth noting", "at the end of the day", "whether you're"]


class Writing(unittest.TestCase):
    def published_text(self) -> dict[str, str]:
        files = [ROOT / "README.md", ROOT / ".gitignore", ROOT / ".gitattributes", ROOT / "tools/fonts/SOURCE.md",
                 *ROOT.glob("tools/*.py"), *ROOT.glob("tools/*.txt"), *ROOT.glob(".github/workflows/*.yml")]
        texts = {p.relative_to(ROOT).as_posix(): p.read_text(encoding="utf-8") for p in files}
        for rel, svg in committed_svgs().items():
            root = ET.fromstring(svg)
            texts[rel] = f"{root.find(f'{SVG_NS}title').text} {root.find(f'{SVG_NS}desc').text}"
        return texts

    def test_no_dashes_used_as_punctuation(self):
        for rel, text in self.published_text().items():
            for dash in DASHES:
                with self.subTest(file=rel, dash=hex(ord(dash))):
                    self.assertNotIn(dash, text)

    def test_no_banned_phrasing(self):
        for rel, text in self.published_text().items():
            if rel == "tools/test_build.py":
                continue  # holds the list itself
            plain = text.lower().replace(chr(0x2019), "'")
            for phrase in BANNED:
                with self.subTest(file=rel, phrase=phrase):
                    self.assertNotIn(phrase, plain)


def fake_api(languages: dict[str, dict[str, int]], trees: dict[str, list[str]], truncated=(), errors=None,
             filler=0):
    """A stand-in for build.api answering the handful of endpoints collect() calls."""
    padding = [{"name": f"r{i}", "fork": True, "private": False, "archived": False, "stargazers_count": 0,
                "default_branch": "main"} for i in range(filler)]
    repos = [
        {"name": "app", "fork": False, "private": False, "archived": False, "stargazers_count": 3, "default_branch": "main"},
        {"name": "old", "fork": False, "private": False, "archived": True, "stargazers_count": 2, "default_branch": "main"},
        {"name": "copy", "fork": True, "private": False, "archived": False, "stargazers_count": 50, "default_branch": "main"},
        {"name": "secret", "fork": False, "private": True, "archived": False, "stargazers_count": 70, "default_branch": "main"},
        {"name": build.USER, "fork": False, "private": False, "archived": False, "stargazers_count": 1, "default_branch": "main"},
    ]

    def fetch(path, token, body=None):
        if path == f"users/{build.USER}":
            return {"public_repos": 9, "followers": 7}  # differs from the listing: the count is GitHub's
        if path.startswith(f"users/{build.USER}/repos"):
            listing = padding + repos
            page = int(re.search(r"[?&]page=(\d+)", path).group(1))
            return listing[(page - 1) * 100:page * 100]
        if path == "graphql":
            if errors:
                return {"errors": errors}
            return {"data": {"user": {"contributionsCollection": {"contributionCalendar": {"totalContributions": 42}}}}}
        name = path.split("/")[2]
        if path.endswith("/languages"):
            return dict(languages.get(name, {}))
        if "/git/trees/" in path:
            return {"truncated": name in truncated,
                    "tree": [{"path": p, "type": "blob"} for p in trees.get(name, [])]}
        raise AssertionError(f"unexpected call {path}")
    return fetch


class Collect(unittest.TestCase):
    def test_counts_only_own_public_unarchived_repositories(self):
        data = build.collect("t", fake_api({"app": {"PHP": 10}, "old": {"Go": 5}, "copy": {"C": 9}, "secret": {"Ada": 3}}, {}))
        self.assertEqual(data["languages"], {"PHP": 10})
        self.assertEqual(data["stars"], 6)  # forks and private excluded, archived and profile repositories included
        self.assertEqual((data["contributions"], data["followers"], data["repositories"]), (42, 7, 9))

    def test_every_page_of_repositories_is_read(self):
        data = build.collect("t", fake_api({"app": {"PHP": 10}}, {}, filler=150))
        self.assertEqual(data["languages"], {"PHP": 10})

    def test_graphql_errors_stop_the_run(self):
        with self.assertRaises(RuntimeError):
            build.collect("t", fake_api({}, {}, errors=[{"message": "denied"}]))

    def test_hack_folds_into_php_only_without_hack_files(self):
        langs = {"app": {"PHP": 10, "Hack": 4}}
        self.assertEqual(build.collect("t", fake_api(langs, {"app": ["a.php"]}))["languages"], {"PHP": 14})
        self.assertEqual(build.collect("t", fake_api(langs, {"app": ["a.hack"]}))["languages"], {"PHP": 10, "Hack": 4})
        self.assertEqual(build.collect("t", fake_api(langs, {"app": ["A.HH"]}))["languages"], {"PHP": 10, "Hack": 4})
        self.assertEqual(build.collect("t", fake_api(langs, {"app": []}, truncated=("app",)))["languages"],
                         {"PHP": 10, "Hack": 4})


class RefreshStats(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        (self.root / "assets").mkdir()
        shutil.copy(build.README, self.root / "README.md")
        for name in ("stats.json", "stats-light.svg", "stats-dark.svg"):
            shutil.copy(build.ASSETS / name, self.root / "assets" / name)

    def day(self, offset: int) -> str:
        return (date.fromisoformat(STATS["date"]) + timedelta(days=offset)).isoformat()

    def test_identical_figures_change_nothing_and_keep_the_date(self):
        self.assertEqual(build.refresh_stats(STATS["data"], self.root, self.day(build.STALE_DAYS - 1)), [])

    def test_identical_figures_are_re_dated_once_the_card_is_stale(self):
        changed = build.refresh_stats(STATS["data"], self.root, self.day(build.STALE_DAYS))
        self.assertEqual(len(changed), 4)
        self.assertEqual(json.loads((self.root / "assets/stats.json").read_text(encoding="utf-8"))["date"],
                         self.day(build.STALE_DAYS))

    def test_a_commit_lands_well_inside_githubs_sixty_idle_days(self):
        # Weekly cron: the longest gap is STALE_DAYS plus one week, plus one more week if a run fails.
        cron = re.search(r'cron: "([^"]+)"', (ROOT / ".github/workflows/stats.yml").read_text(encoding="utf-8"))
        minute, hour, dom, month, dow = cron.group(1).split()
        self.assertEqual((dom, month), ("*", "*"))
        self.assertRegex(dow, r"^[0-6]$", "the refresh must run every week")
        self.assertLess(build.STALE_DAYS + 7 + 7, 60)

    def test_new_figures_rewrite_card_block_and_record_with_todays_date(self):
        data = {**STATS["data"], "contributions": STATS["data"]["contributions"] + 7}
        changed = build.refresh_stats(data, self.root, "2999-01-01")
        self.assertEqual(sorted(p.relative_to(self.root).as_posix() for p in changed),
                         ["README.md", "assets/stats-dark.svg", "assets/stats-light.svg", "assets/stats.json"])
        self.assertIn(build.stats_sentence(data, "2999-01-01"), (self.root / "README.md").read_text(encoding="utf-8"))
        payload = json.loads(build.commit_payload(changed, self.root))
        self.assertEqual({a["path"]: base64.b64decode(a["contents"]) for a in payload},
                         {p.relative_to(self.root).as_posix(): p.read_bytes() for p in changed})

    def test_figures_that_change_without_changing_the_card_still_move_the_date(self):
        langs = STATS["data"]["languages"]
        data = {**STATS["data"], "languages": {**langs, "HTML": langs["HTML"] - 100, "Shell": langs["Shell"] + 100}}
        changed = build.refresh_stats(data, self.root, self.day(1))
        self.assertEqual(len(changed), 4)
        self.assertEqual(json.loads((self.root / "assets/stats.json").read_text(encoding="utf-8"))["date"], self.day(1))

    def test_a_card_that_differs_from_its_render_gets_todays_date(self):
        card = self.root / "assets/stats-dark.svg"
        card.write_text(card.read_text(encoding="utf-8").replace("</svg>", "<g/></svg>"), encoding="utf-8")
        build.refresh_stats(STATS["data"], self.root, self.day(1))
        self.assertEqual(json.loads((self.root / "assets/stats.json").read_text(encoding="utf-8"))["date"], self.day(1))

    def test_a_missing_card_is_rendered_again_with_todays_date(self):
        (self.root / "assets/stats-dark.svg").unlink()
        build.refresh_stats(STATS["data"], self.root, self.day(1))
        self.assertEqual(json.loads((self.root / "assets/stats.json").read_text(encoding="utf-8"))["date"], self.day(1))
        self.assertTrue((self.root / "assets/stats-dark.svg").exists())

    def test_a_readme_without_the_block_writes_nothing(self):
        (self.root / "README.md").write_text("no markers\n", encoding="utf-8")
        before = {p: p.read_bytes() for p in (self.root / "assets").iterdir()}
        data = {**STATS["data"], "followers": STATS["data"]["followers"] + 1}
        with self.assertRaises(ValueError):
            build.refresh_stats(data, self.root, "2999-01-01")
        self.assertEqual(before, {p: p.read_bytes() for p in (self.root / "assets").iterdir()})


if __name__ == "__main__":
    unittest.main()

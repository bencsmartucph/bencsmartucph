# /// script
# requires-python = "==3.12.*"
# dependencies = ["pillow==12.3.0"]
# ///
"""Draw the neofetch-style profile card as dark_mode.svg and light_mode.svg.

    uv run make_card.py                 # redraw with live stats (nightly job)
    uv run make_card.py --refresh-art   # rebuild art/*.txt from the current GitHub avatar
    uv run make_card.py --preview       # both cards into preview/ plus preview.html

Stats are public profile data, so any token reads them: GITHUB_TOKEN in Actions,
`gh auth token` locally, else placeholders (never in CI, where a failure is fatal).
"""

import argparse
import datetime as dt
import io
import json
import math
import os
import subprocess
import sys
import urllib.request
from html import escape
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageOps

# ---------------------------------------------------------------- data block
USER = "bencsmartucph"
HEADER = ("ben", "copenhagen")
CAREER_START = 2015
YEAR = dt.date.today().year

ROWS = [
    ("kv", "OS", "Windows 11 + AutoHotkey"),
    ("kv", "Uptime", f"{YEAR - CAREER_START} years (economist since {CAREER_START})"),
    ("kv", "Host", "University of Copenhagen"),
    ("kv", "Kernel", "MSc Economics (finishing early 2027)"),
    ("kv", "IDE", "VS Code, Claude Code"),
    ("blank",),
    ("kv", "Languages.Programming", "Python, R, SQL, C#, TypeScript"),
    ("kv", "Languages.Computer", "Markdown, LaTeX, AutoHotkey"),
    ("kv", "Languages.Real", "English, Danish"),
    ("blank",),
    ("kv", "Built", "ben-mods, QuickEdit, StickyPosition, claimpin, chess-coach"),
    ("kv", "Hobbies", "chess, piano"),
    ("kv", "Open to", "graduate and analyst roles, from March 2027"),
    ("blank",),
    ("section", "Contact"),
    ("kv", "Email", "bencsmart@gmail.com"),
    ("kv", "LinkedIn", "bencsmart"),
    ("blank",),
    ("section", "GitHub Stats"),
    ("stats", ("Repos", "repos"), ("Stars", "stars")),
    ("stats", ("Contributions (12 mo)", "contributions"), ("Followers", "followers")),
]

# GitHub Primer colours: card background, border, keys, values, leaders, text, art
THEMES = {
    "dark": dict(bg="#161b22", border="#30363d", key="#ffa657", value="#a5d6ff",
                 dots="#6e7681", text="#e6edf3", art="#c9d1d9"),
    "light": dict(bg="#f6f8fa", border="#d0d7de", key="#953800", value="#0a3069",
                  dots="#8c959f", text="#1f2328", art="#24292f"),
}

# ---------------------------------------------------------------- layout
WIDTH = 60  # characters per info line
ART_ROWS = 40
ART_FS, ART_LH = 10, 11  # art font size and line height, px
TXT_FS = 14
EM = 0.6  # monospace advance as a fraction of font size (Menlo, DejaVu; Consolas is 0.55)
PAD, GAP = 30, 30
FONTS = "Consolas, Menlo, 'DejaVu Sans Mono', 'Courier New', monospace"

HERE = Path(__file__).resolve().parent
ART_DIR = HERE / "art"
RAMP = ".,:;i1tfLCG08@"  # sparse to dense; space is reserved for background
AVATAR_CROP = (0.16, 0.03, 0.84, 0.80)  # left, top, right, bottom as fractions


# ---------------------------------------------------------------- stats
QUERY = """
query($login: String!, $after: String) {
  user(login: $login) {
    followers { totalCount }
    contributionsCollection { contributionCalendar { totalContributions } }
    repositories(privacy: PUBLIC, ownerAffiliations: OWNER, first: 100, after: $after) {
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes { stargazerCount }
    }
  }
}
"""


def get_token():
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        return token
    try:
        out = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=15)
        return out.stdout.strip() or None
    except (OSError, subprocess.TimeoutExpired):
        return None


def graphql(token, variables):
    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=json.dumps({"query": QUERY, "variables": variables}).encode(),
        headers={"Authorization": f"bearer {token}", "User-Agent": f"{USER}-profile-card"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.load(resp)
    if body.get("errors"):
        raise RuntimeError(body["errors"][0].get("message", "GraphQL error"))
    return body["data"]["user"]


def fetch_stats():
    token = get_token()
    if not token:
        raise RuntimeError("no token (set GITHUB_TOKEN or run gh auth login)")
    variables = {"login": USER, "after": None}
    user = graphql(token, variables)
    repos = user["repositories"]
    stars = sum(n["stargazerCount"] for n in repos["nodes"])
    while repos["pageInfo"]["hasNextPage"]:
        variables["after"] = repos["pageInfo"]["endCursor"]
        repos = graphql(token, variables)["repositories"]
        stars += sum(n["stargazerCount"] for n in repos["nodes"])
    # the calendar total is the "contributions in the last year" figure on the profile
    return {
        "repos": user["repositories"]["totalCount"],
        "stars": stars,
        "contributions": user["contributionsCollection"]["contributionCalendar"]["totalContributions"],
        "followers": user["followers"]["totalCount"],
    }


def stats_or_placeholders():
    try:
        return {k: f"{v:,}" for k, v in fetch_stats().items()}
    except Exception as exc:  # noqa: BLE001
        if os.environ.get("GITHUB_ACTIONS"):
            sys.exit(f"stats failed in CI, not writing placeholders: {exc}")
        print(f"stats unavailable, using placeholders: {exc}", file=sys.stderr)
        return dict.fromkeys(("repos", "stars", "contributions", "followers"), "--")


# ---------------------------------------------------------------- art
def to_ascii(gray, mask, cols, rows, dense_is_bright):
    """Map a greyscale image to ramp characters; masked-out cells become spaces."""
    gray = gray.resize((cols, rows), Image.Resampling.BOX)
    mask = mask.resize((cols, rows), Image.Resampling.BOX)
    top = len(RAMP) - 1
    lines = []
    for y in range(rows):
        line = ""
        for x in range(cols):
            if mask.getpixel((x, y)) < 128:
                line += " "
                continue
            lum = gray.getpixel((x, y)) / 255
            level = lum if dense_is_bright else 1 - lum
            line += RAMP[round(level * top)]
        lines.append(line.rstrip())
    return lines


def cols_for(width, height, rows=ART_ROWS):
    # one character cell is EM*ART_FS wide and ART_LH tall
    return round(rows * (width / height) * ART_LH / (EM * ART_FS))


def avatar_art():
    with urllib.request.urlopen(f"https://github.com/{USER}.png", timeout=30) as resp:
        img = Image.open(io.BytesIO(resp.read())).convert("RGB")
    w, h = img.size
    img = img.crop(tuple(round(f * s) for f, s in zip(AVATAR_CROP, (w, h, w, h))))
    w, h = img.size
    # background: flood-fill from the border with a sentinel colour, then mask it
    sentinel = (255, 0, 254)
    filled = img.copy()
    for xy in [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1), (w // 2, 0)]:
        if filled.getpixel(xy) != sentinel:
            ImageDraw.floodfill(filled, xy, sentinel, thresh=38)
    mask = Image.new("L", img.size, 0)
    mask.putdata([0 if p == sentinel else 255 for p in filled.get_flattened_data()])
    # a wide unsharp mask lifts eyes, brows and mouth out of the broad lighting
    gray = ImageOps.grayscale(img).filter(ImageFilter.MedianFilter(3))
    gray = gray.filter(ImageFilter.UnsharpMask(round(20 * w / 195), 180, 0))
    gray = ImageOps.autocontrast(gray, cutoff=2, mask=mask)
    gray = gray.point(lambda v: round(255 * (v / 255) ** 0.7))
    cols = cols_for(w, h)
    return {
        "avatar_dark": to_ascii(gray, mask, cols, ART_ROWS, dense_is_bright=True),
        "avatar_light": to_ascii(gray, mask, cols, ART_ROWS, dense_is_bright=False),
    }


def refresh_art():
    ART_DIR.mkdir(exist_ok=True)
    for name, lines in avatar_art().items():
        (ART_DIR / f"{name}.txt").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        print(f"art/{name}.txt  {max(map(len, lines))}x{len(lines)}")


def load_art(mode):
    path = ART_DIR / f"avatar_{mode}.txt"
    return path.read_text(encoding="utf-8").rstrip("\n").split("\n")


# ---------------------------------------------------------------- info lines
def wrap(value, room):
    """Split a comma list into balanced lines no wider than room."""
    items = value.split(", ")

    def greedy(limit):
        lines, cur = [], ""
        for item in items:
            nxt = f"{cur}, {item}" if cur else item
            if cur and len(nxt) + 1 > limit:
                lines.append(cur + ",")
                cur = item
            else:
                cur = nxt
        return lines + [cur]

    n = len(greedy(room))
    # narrowest limit that still needs only n lines
    for limit in range(math.ceil(len(value) / n), room + 1):
        if len(greedy(limit)) <= n:
            return greedy(limit)
    return greedy(room)


def kv(key, value, width):
    """Spans for 'key: ..... value', plus continuation lines when value wraps."""
    head = len(key) + 2
    room = width - head - 3
    parts = wrap(value, room) if len(value) > room else [value]
    dots = max(1, width - head - 2 - len(parts[0]))
    lines = [[("key", key), ("text", ": "), ("dots", "." * dots), ("text", " "), ("value", parts[0])]]
    for part in parts[1:]:
        lines.append([("text", " " * (width - len(part))), ("value", part)])
    return lines


def info_lines(stats):
    user, host = HEADER
    rule = "-" * (WIDTH - len(user) - len(host) - 2)
    lines = [[("key", user), ("text", "@"), ("key", host), ("text", " " + rule)]]
    half = 32  # left stat column; wide enough for "Contributions (12 mo)" plus five digits
    for row in ROWS:
        kind = row[0]
        if kind == "blank":
            lines.append([])
        elif kind == "section":
            lines.append([("text", f"- {row[1]} " + "-" * (WIDTH - len(row[1]) - 3))])
        elif kind == "kv":
            lines += kv(row[1], row[2], WIDTH)
        elif kind == "stats":
            (lk, lkey), (rk, rkey) = row[1], row[2]
            left = kv(lk, stats[lkey], half)[0]
            right = kv(rk, stats[rkey], WIDTH - half - 3)[0]
            lines.append(left + [("text", " | ")] + right)
    return lines


# ---------------------------------------------------------------- svg
def svg(art_lines, info, theme):
    c = THEMES[theme]
    art_w = max(map(len, art_lines)) * EM * ART_FS
    art_h = len(art_lines) * ART_LH
    info_lh = max(TXT_FS * 1.3, art_h / len(info))
    info_h = len(info) * info_lh
    info_x = PAD + art_w + GAP
    width = round(info_x + WIDTH * EM * TXT_FS + PAD)
    height = round(max(art_h, info_h) + 2 * PAD)

    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" font-family="{FONTS}">',
        "<style>"
        f".key{{fill:{c['key']}}}.value{{fill:{c['value']}}}.dots{{fill:{c['dots']}}}"
        f".text{{fill:{c['text']}}}.art{{fill:{c['art']}}}"
        "</style>",
        f'<rect x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" rx="15" '
        f'fill="{c["bg"]}" stroke="{c["border"]}"/>',
        f'<text class="art" font-size="{ART_FS}" xml:space="preserve">',
    ]
    art_top = PAD + (max(art_h, info_h) - art_h) / 2
    for i, line in enumerate(art_lines):
        y = art_top + (i + 0.8) * ART_LH
        out.append(f'<tspan x="{PAD}" y="{y:.1f}">{escape(line)}</tspan>')
    out.append("</text>")
    out.append(f'<text font-size="{TXT_FS}" xml:space="preserve">')
    for i, spans in enumerate(info):
        if not spans:
            continue
        y = PAD + (i + 0.75) * info_lh
        inner = "".join(f'<tspan class="{cls}">{escape(txt)}</tspan>' for cls, txt in spans)
        out.append(f'<tspan x="{info_x:.1f}" y="{y:.1f}">{inner}</tspan>')
    out.append("</text>")
    out.append("</svg>")
    return "\n".join(out) + "\n"


def render(outdir, stats):
    outdir.mkdir(parents=True, exist_ok=True)
    info = info_lines(stats)
    for mode in THEMES:
        (outdir / f"{mode}_mode.svg").write_text(svg(load_art(mode), info, mode), encoding="utf-8", newline="\n")


PREVIEW = """<!doctype html>
<meta charset="utf-8"><title>Profile card preview</title>
<style>
body{{margin:0;font:15px system-ui,sans-serif}}
section{{padding:24px 16px}}section.dark{{background:#0d1117;color:#e6edf3}}
section.light{{background:#ffffff;color:#1f2328}}
img{{display:block;max-width:100%;height:auto;margin-top:8px}}
.gh{{max-width:830px}}
</style>
{body}
"""


def preview(stats):
    sections = []
    render(HERE / "preview", stats)
    for mode in THEMES:
        src = f"preview/{mode}_mode.svg"
        sections.append(
            f'<section class="{mode}"><b>{mode}</b>'
            f'<img src="{src}"><small>At GitHub README width (830px):</small>'
            f'<img class="gh" src="{src}"></section>'
        )
    (HERE / "preview.html").write_text(PREVIEW.format(body="\n".join(sections)), encoding="utf-8", newline="\n")
    print(HERE / "preview.html")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh-art", action="store_true")
    ap.add_argument("--preview", action="store_true")
    args = ap.parse_args()
    if args.refresh_art:
        refresh_art()
    stats = stats_or_placeholders()
    if args.preview:
        preview(stats)
    else:
        render(HERE, stats)


if __name__ == "__main__":
    main()

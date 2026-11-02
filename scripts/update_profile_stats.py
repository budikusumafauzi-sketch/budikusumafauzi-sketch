#!/usr/bin/env python3
"""Generate the SVG cards used by the GitHub profile README.

Outputs (written atomically, only if every step succeeded):
    assets/github-stats.svg    - public repositories, followers, following
    assets/top-languages.svg   - language composition of owned, non-fork public repos

Data comes from the GitHub REST API at runtime. Nothing is hardcoded.
Only the Python standard library is used.

Environment:
    GITHUB_TOKEN      optional; raises the API rate limit. Never printed.
    PROFILE_USERNAME  optional; defaults to DEFAULT_USERNAME.

Exit status is non-zero on any failure and existing SVGs are left untouched.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from xml.sax.saxutils import escape

DEFAULT_USERNAME = "budikusumafauzi-sketch"
API_ROOT = "https://api.github.com"
API_VERSION = "2022-11-28"
PER_PAGE = 100
MAX_ATTEMPTS = 5
BACKOFF_BASE = 1.0          # seconds: 1, 2, 4, 8 ...
MAX_RATE_LIMIT_WAIT = 120   # never sleep longer than this for a rate-limit reset
REQUEST_TIMEOUT = 20
TOP_N = 5
CARD_WIDTH = 640

FONT = "'Segoe UI', 'Helvetica Neue', Arial, sans-serif"
BG, SURFACE, BORDER = "#0A0A0C", "#16161A", "#2A2A30"
TEXT, MUTED, ACCENT, ACCENT_DARK = "#F5F5F7", "#9A9AA3", "#E01B3C", "#7A0B1D"
# Crimson tonal scale by rank, neutral gray for "Others".
RANK_COLORS = ["#E01B3C", "#D01A38", "#BE1833", "#AB162E", "#981429"]
OTHERS_COLOR = "#5A5A63"


class ApiError(RuntimeError):
    """Raised when the GitHub API cannot give us trustworthy data."""


# --------------------------------------------------------------------------- API

def api_get(path: str, token: str | None = None):
    """GET a GitHub API path and return decoded JSON. Retries transient errors."""
    url = path if path.startswith("http") else API_ROOT + path
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": API_VERSION,
        "User-Agent": "profile-stats-generator",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    last_error = "unknown error"
    for attempt in range(MAX_ATTEMPTS):
        delay = BACKOFF_BASE * (2 ** attempt)
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            last_error = f"HTTP {err.code} for {path}"
            remaining = err.headers.get("x-ratelimit-remaining")
            retry_after = err.headers.get("retry-after")
            if err.code in (403, 429) and (remaining == "0" or retry_after):
                if retry_after and retry_after.isdigit():
                    wait = int(retry_after)
                else:
                    reset = err.headers.get("x-ratelimit-reset", "0")
                    wait = max(int(reset) - int(time.time()), 0) + 1 if reset.isdigit() else 0
                if wait > MAX_RATE_LIMIT_WAIT:
                    raise ApiError(f"Rate limit exhausted; reset in {wait}s (too long to wait)") from err
                delay = max(wait, delay)
            elif err.code >= 500:
                pass  # transient server error: back off and retry
            else:
                raise ApiError(last_error) from err  # 4xx other than rate limit: not retryable
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as err:
            last_error = f"{type(err).__name__} for {path}"
        if attempt < MAX_ATTEMPTS - 1:
            time.sleep(delay)
    raise ApiError(f"Giving up after {MAX_ATTEMPTS} attempts: {last_error}")


def fetch_user(username: str, token: str | None) -> dict:
    data = api_get(f"/users/{username}", token)
    for key in ("followers", "following"):
        if not isinstance(data.get(key), int):
            raise ApiError(f"User response is missing '{key}'")
    return data


def fetch_repos(username: str, token: str | None) -> list[dict]:
    """All owned public non-fork repositories, following pagination."""
    repos: list[dict] = []
    page = 1
    while True:
        batch = api_get(
            f"/users/{username}/repos?type=owner&per_page={PER_PAGE}&page={page}", token
        )
        if not isinstance(batch, list):
            raise ApiError("Unexpected repository list response")
        repos.extend(batch)
        if len(batch) < PER_PAGE:
            break
        page += 1
    return [
        r for r in repos
        if not r.get("fork") and not r.get("private") and r.get("owner", {}).get("login", "").lower() == username.lower()
    ]


def fetch_languages(repos: list[dict], token: str | None) -> Counter:
    """Sum language bytes across repositories (one request per repository)."""
    totals: Counter = Counter()
    seen: set[str] = set()
    for repo in repos:
        url = repo.get("languages_url")
        if not url or url in seen:
            continue
        seen.add(url)
        langs = api_get(url, token)
        if not isinstance(langs, dict):
            raise ApiError(f"Unexpected language response for {repo.get('name')}")
        for name, nbytes in langs.items():
            if isinstance(nbytes, int) and nbytes > 0:
                totals[name] += nbytes
    return totals


# ------------------------------------------------------------------- Computation

def language_breakdown(totals: Counter, top_n: int = TOP_N) -> list[tuple[str, int, float]]:
    """Return [(name, bytes, percent)] sorted high->low, 'Others' last.

    Percentages are computed from raw byte counts and rounded to 0.1 using the
    largest-remainder method so the displayed values always add up to 100.0.
    """
    items = sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))
    if not items:
        return []
    if len(items) > top_n + 1:
        rest = sum(b for _, b in items[top_n:])
        items = items[:top_n] + [("Others", rest)]
    total = sum(b for _, b in items)

    scaled = [b * 1000 / total for _, b in items]   # tenths of a percent
    floors = [int(s) for s in scaled]
    shortfall = 1000 - sum(floors)
    order = sorted(range(len(items)), key=lambda i: (scaled[i] - floors[i]), reverse=True)
    for i in order[:shortfall]:
        floors[i] += 1
    return [(name, nbytes, floors[i] / 10) for i, (name, nbytes) in enumerate(items)]


# --------------------------------------------------------------------- Rendering

def _svg_open(width: int, height: int, title: str, desc: str, uid: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-labelledby="{uid}-t {uid}-d">\n'
        f'  <title id="{uid}-t">{escape(title)}</title>\n'
        f'  <desc id="{uid}-d">{escape(desc)}</desc>\n'
        f'  <defs>\n'
        f'    <linearGradient id="{uid}-g" x1="0" y1="0" x2="1" y2="0">'
        f'<stop offset="0" stop-color="{ACCENT}"/><stop offset="1" stop-color="{ACCENT_DARK}"/></linearGradient>\n'
        f'    <clipPath id="{uid}-c"><rect x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" rx="12"/></clipPath>\n'
        f'  </defs>\n'
        f'  <rect x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" rx="12" fill="{BG}" stroke="{BORDER}"/>\n'
        f'  <rect x="0" y="0" width="{width}" height="3" fill="url(#{uid}-g)" clip-path="url(#{uid}-c)"/>\n'
    )


def _text(x, y, content, size, fill, weight="400", anchor="start", extra="") -> str:
    return (
        f'  <text x="{x}" y="{y}" font-family="{FONT}" font-size="{size}" font-weight="{weight}" '
        f'fill="{fill}" text-anchor="{anchor}"{extra}>{escape(str(content))}</text>\n'
    )


def render_stats_svg(repo_count: int, followers: int, following: int, updated: str) -> str:
    height = 148
    desc = (f"{repo_count} public repositories, {followers} followers, {following} following. "
            f"Updated {updated}.")
    out = _svg_open(CARD_WIDTH, height, "GitHub Statistics", desc, "st")
    metrics = [("Public Repositories", repo_count), ("Followers", followers), ("Following", following)]
    gap, pad = 16, 28
    pw = (CARD_WIDTH - 2 * pad - 2 * gap) / 3
    for i, (label, value) in enumerate(metrics):
        x = pad + i * (pw + gap)
        out += (f'  <rect x="{x:.1f}" y="26" width="{pw:.1f}" height="76" rx="8" fill="{SURFACE}" stroke="{BORDER}"/>\n'
                f'  <rect x="{x:.1f}" y="38" width="3" height="52" rx="1.5" fill="{ACCENT}"/>\n')
        out += _text(f"{x + 20:.1f}", 64, value, 30, TEXT, "700")
        out += _text(f"{x + 20:.1f}", 88, label, 12, MUTED)
    out += _text(28, 128, f"Owned public repositories, forks excluded  ·  Updated {updated}", 10.5, MUTED)
    return out + "</svg>\n"


def render_languages_svg(rows: list[tuple[str, int, float]], repo_count: int, updated: str) -> str:
    row_h = 34
    body_h = max(len(rows), 1) * row_h
    height = 30 + body_h + 46
    if rows:
        desc = "; ".join(f"{n} {p:.1f}%" for n, _, p in rows) + f". Updated {updated}."
    else:
        desc = f"No language data available. Updated {updated}."
    out = _svg_open(CARD_WIDTH, height, "Top Languages - Based on Public Repository Code", desc, "tl")

    bar_x, bar_w = 190, 342
    if not rows:
        out += _text(28, 56, "No language data available.", 13, MUTED)
    for i, (name, _bytes, pct) in enumerate(rows):
        y = 30 + i * row_h
        color = OTHERS_COLOR if name == "Others" and i == len(rows) - 1 and len(rows) > 1 else RANK_COLORS[min(i, len(RANK_COLORS) - 1)]
        label = name if len(name) <= 20 else name[:19] + "…"
        out += _text(28, y + 18, label, 13, TEXT)
        out += f'  <rect x="{bar_x}" y="{y + 10}" width="{bar_w}" height="8" rx="4" fill="{SURFACE}"/>\n'
        fill_w = max(pct / 100 * bar_w, 3) if pct > 0 else 0
        out += f'  <rect x="{bar_x}" y="{y + 10}" width="{fill_w:.1f}" height="8" rx="4" fill="{color}"/>\n'
        out += _text(CARD_WIDTH - 28, y + 18, f"{pct:.1f}%", 13, MUTED, anchor="end")
    foot = f"Code composition, not skill level  ·  {repo_count} repositories analyzed  ·  Updated {updated}"
    out += _text(28, height - 18, foot, 10.5, MUTED)
    return out + "</svg>\n"


# ---------------------------------------------------------------------- File I/O

def validate_svg(text: str) -> None:
    ET.fromstring(text)  # raises ParseError if not well-formed


def atomic_write(path: Path, text: str) -> bool:
    """Write text to path via temp file + replace. Return True if content changed."""
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)
    return True


# ------------------------------------------------------------------------- Main

def build(username: str, token: str | None, today: str):
    user = fetch_user(username, token)
    repos = fetch_repos(username, token)
    totals = fetch_languages(repos, token)
    rows = language_breakdown(totals)
    stats_svg = render_stats_svg(len(repos), user["followers"], user["following"], today)
    lang_svg = render_languages_svg(rows, len(repos), today)
    validate_svg(stats_svg)
    validate_svg(lang_svg)
    return stats_svg, lang_svg, rows, len(repos)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--username", default=os.environ.get("PROFILE_USERNAME", DEFAULT_USERNAME))
    parser.add_argument("--out-dir", default=str(Path(__file__).resolve().parent.parent / "assets"))
    parser.add_argument("--dry-run", action="store_true", help="fetch and render but do not write files")
    args = parser.parse_args(argv)

    token = os.environ.get("GITHUB_TOKEN") or None
    today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    try:
        stats_svg, lang_svg, rows, repo_count = build(args.username, token, today)
    except (ApiError, ET.ParseError) as err:
        print(f"ERROR: {err}. Existing SVG files were left unchanged.", file=sys.stderr)
        return 1

    print(f"{args.username}: {repo_count} qualifying repositories")
    for name, nbytes, pct in rows:
        print(f"  {name:<20} {nbytes:>10} bytes  {pct:5.1f}%")
    if args.dry_run:
        print("Dry run: no files written.")
        return 0

    out_dir = Path(args.out_dir)
    changed = [
        atomic_write(out_dir / "github-stats.svg", stats_svg),
        atomic_write(out_dir / "top-languages.svg", lang_svg),
    ]
    print("Files updated." if any(changed) else "No changes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

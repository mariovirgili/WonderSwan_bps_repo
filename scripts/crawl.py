#!/usr/bin/env python3
"""Mirror WonderSwan .bps/.ips translation patches from GitHub sources.

For every source in sources.json the crawler looks at
  * the assets of every published release, and
  * the files committed on the default branch,
takes .bps/.ips files directly and pulls them out of .zip/.7z archives, then
stores them under patches/ and writes index.json, index.csv and PATCHES.md.

What is recorded about the ROM a patch needs (and the ROM it produces):
  * BPS patches carry the original and patched ROM CRC32 and sizes in the
    patch itself - these are authoritative;
  * release notes and the README files next to committed patches are scanned
    for CRC32 / MD5 / SHA-1 / SHA-256 values and No-Intro style ROM names,
    which fill the gaps (IPS patches carry no checksums at all) and add the
    stronger hashes;
  * overrides.json can set anything by hand.

The run is incremental: a release asset (id + updated_at + size) or a git blob
(sha) that was already processed is not downloaded again; descriptive fields
are recomputed every run, so improving the heuristics updates old entries.
Patches that disappear upstream are removed here too, but only when their
source was crawled successfully, so a temporary API error never deletes
anything.

Environment: GITHUB_TOKEN (recommended), GITHUB_REPOSITORY (this repo),
TARGET_BRANCH (default main). Only the standard library is required; .7z
archives are opened when py7zr is installed.
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATCH_DIR = ROOT / "patches"
INDEX_JSON = ROOT / "index.json"
INDEX_CSV = ROOT / "index.csv"
PATCHES_MD = ROOT / "PATCHES.md"
STATE_JSON = ROOT / "state.json"
SOURCES_JSON = ROOT / "sources.json"
OVERRIDES_JSON = ROOT / "overrides.json"
ROMS_JSON = ROOT / "roms.json"
LOOKUP_DIR = ROOT / "lookup"

API = "https://api.github.com"
TOKEN = os.environ.get("GITHUB_TOKEN", "")
SELF_REPO = os.environ.get("GITHUB_REPOSITORY", "mariovirgili/WonderSwan_bps_repo")
BRANCH = os.environ.get("TARGET_BRANCH", "main")
PATCH_EXT = (".bps", ".ips")
ARCHIVE_EXT = (".zip", ".7z")
MAX_DOWNLOAD = 100 * 1024 * 1024
MAX_PATCH = 50 * 1024 * 1024
MAX_WS_ROM = 16 * 1024 * 1024
SCHEMA = 2
DEFAULT_PLATFORMS = ["wonderswan", "wonderswan_color", None]
ALLOWED: list = []  # platforms kept, set from sources.json in main()
DEFAULT_EXCLUDE_PATHS = r"(^|/)(vendor|third[_-]?party|fixtures?|tests?|node_modules|\.github)(/|$)"

try:  # optional
    import py7zr  # type: ignore
except ImportError:  # pragma: no cover
    py7zr = None


def log(*a):
    print(*a, flush=True)


# --------------------------------------------------------------------------- HTTP

def http_get(url: str, accept: str = "application/vnd.github+json", auth: bool = True) -> tuple[bytes, dict]:
    headers = {"Accept": accept, "User-Agent": "WonderSwan-bps-crawler", "X-GitHub-Api-Version": "2022-11-28"}
    if TOKEN and auth:
        headers["Authorization"] = f"Bearer {TOKEN}"
    for attempt in range(5):
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                if int(r.headers.get("Content-Length") or 0) > MAX_DOWNLOAD:
                    raise ValueError("too large")
                data = r.read(MAX_DOWNLOAD + 1)
                if len(data) > MAX_DOWNLOAD:
                    raise ValueError("too large")
                return data, dict(r.headers)
        except urllib.error.HTTPError as e:
            retry = e.code >= 500 or e.code == 429 or (e.code == 403 and e.headers.get("X-RateLimit-Remaining") == "0")
            if not retry or attempt == 4:
                raise
            reset = int(e.headers.get("X-RateLimit-Reset") or 0)
            wait = min(max(reset - time.time(), 5), 300) if reset else 5 * (attempt + 1)
            log(f"  HTTP {e.code} on {url}; retrying in {wait:.0f}s")
            time.sleep(wait)
        except urllib.error.URLError:
            if attempt == 4:
                raise
            time.sleep(5 * (attempt + 1))
    raise RuntimeError("unreachable")


def api(path: str):
    data, _ = http_get(API + path)
    return json.loads(data)


def api_list(path: str) -> list:
    out, url = [], API + path + ("&" if "?" in path else "?") + "per_page=100"
    while url:
        data, headers = http_get(url)
        out.extend(json.loads(data))
        m = re.search(r'<([^>]+)>;\s*rel="next"', headers.get("Link", "") or headers.get("link", ""))
        url = m.group(1) if m else None
    return out


# ------------------------------------------------------------------ patch parsing

def crc32(b: bytes) -> str:
    return f"{zlib.crc32(b) & 0xFFFFFFFF:08X}"


def bps_number(d: bytes, i: int) -> tuple[int, int]:
    value, shift = 0, 1
    while True:
        x = d[i]
        i += 1
        value += (x & 0x7F) * shift
        if x & 0x80:
            return value, i
        shift <<= 7
        value += shift


def parse_patch(d: bytes) -> dict | None:
    """Format-specific info, or None if the bytes are not a valid patch."""
    if d[:4] == b"BPS1" and len(d) >= 4 + 3 + 12:
        try:
            src_size, i = bps_number(d, 4)
            dst_size, i = bps_number(d, i)
            meta_size, i = bps_number(d, i)
            meta = d[i:i + meta_size].decode("utf-8", "replace") if meta_size else ""
        except IndexError:
            return None
        n = len(d)
        src_crc, dst_crc, patch_crc = (int.from_bytes(d[n + k:n + k + 4], "little") for k in (-12, -8, -4))
        return {
            "format": "bps",
            "source_size": src_size, "target_size": dst_size,
            "source_crc32": f"{src_crc:08X}", "target_crc32": f"{dst_crc:08X}",
            "patch_crc32_footer": f"{patch_crc:08X}",
            "patch_checksum_ok": (zlib.crc32(d[:-4]) & 0xFFFFFFFF) == patch_crc,
            "bps_metadata": meta or None,
        }
    if d[:5] == b"PATCH":
        i, records, max_end = 5, 0, 0
        try:
            while d[i:i + 3] != b"EOF":
                off = int.from_bytes(d[i:i + 3], "big")
                size = int.from_bytes(d[i + 3:i + 5], "big")
                i += 5
                if size == 0:  # RLE record
                    size = int.from_bytes(d[i:i + 2], "big")
                    i += 3
                else:
                    i += size
                if i > len(d):
                    return None
                records += 1
                max_end = max(max_end, off + size)
            i += 3
        except IndexError:
            return None
        truncate = int.from_bytes(d[i:i + 3], "big") if len(d) >= i + 3 else None
        return {
            "format": "ips", "ips_records": records, "ips_min_target_size": max_end,
            "source_size": None, "target_size": truncate, "source_crc32": None, "target_crc32": None,
        }
    return None


def extract_patches(name: str, data: bytes, depth: int = 0) -> list[tuple[str, str | None, bytes]]:
    """(file name, member path inside archive or None, bytes) for every patch found."""
    low = name.lower()
    if low.endswith(PATCH_EXT):
        return [(Path(name).name, None, data)]
    out = []
    if low.endswith(".zip") and depth < 2:
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                for info in z.infolist():
                    if info.is_dir() or info.file_size > MAX_DOWNLOAD:
                        continue
                    m = info.filename
                    if m.lower().endswith(PATCH_EXT) and info.file_size <= MAX_PATCH:
                        out.append((Path(m).name, m, z.read(info)))
                    elif m.lower().endswith(ARCHIVE_EXT):
                        for n, sub, b in extract_patches(m, z.read(info), depth + 1):
                            out.append((n, f"{m}/{sub or n}", b))
        except zipfile.BadZipFile:
            log(f"  ! bad zip: {name}")
    elif low.endswith(".7z") and depth < 2:
        if py7zr is None:
            log(f"  ! skipping {name}: py7zr not installed")
            return []
        try:
            with tempfile.TemporaryDirectory() as tmp, py7zr.SevenZipFile(io.BytesIO(data)) as z:
                wanted = [f.filename for f in z.list() if not f.is_directory
                          and f.filename.lower().endswith(PATCH_EXT + ARCHIVE_EXT)
                          and (f.uncompressed or 0) <= MAX_DOWNLOAD]
                if wanted:
                    z.extract(path=tmp, targets=wanted)
                for m in wanted:
                    f = Path(tmp, m)
                    if not f.is_file():
                        continue
                    b = f.read_bytes()
                    if m.lower().endswith(PATCH_EXT):
                        if len(b) <= MAX_PATCH:
                            out.append((Path(m).name, m, b))
                    else:
                        for n, sub, bb in extract_patches(m, b, depth + 1):
                            out.append((n, f"{m}/{sub or n}", bb))
        except Exception as e:  # noqa: BLE001 - corrupt archives must not stop the crawl
            log(f"  ! bad 7z {name}: {e}")
    return out


# ------------------------------------------------------- hashes in notes / READMEs

HASH_RE = re.compile(r"(?<![0-9A-Za-z])([0-9A-Fa-f]{64}|[0-9A-Fa-f]{40}|[0-9A-Fa-f]{32}|[0-9A-Fa-f]{8})(?![0-9A-Za-z])")
HASH_KIND = {8: "crc32", 32: "md5", 40: "sha1", 64: "sha256"}
SRC_WORDS = r"unpatched|original|source|clean|required|requires|input|base|vanilla|untouched|before|japan(?:ese)?|\bjp\b|\bjpn\b|from"
DST_WORDS = r"(?<!un)patched|output|result|target|translated|english|after|resulting|\bto\b"
ROM_NAME_RE = re.compile(r"([A-Za-z0-9][^\n`|*\"<>\[\]]{1,120}?\((?:Japan|J|Asia)\)(?:\s*\([^()\n]{1,30}\))*(?:\.(?:wsc|ws|pc2))?)")


def _label_kind(label: str, length: int) -> bool:
    """Does the text label allow a hex token of this length to be read as a hash?"""
    lab = label.lower()
    if length == 8:
        return "crc" in lab
    if length == 32:
        return "md5" in lab
    if length == 40:
        return bool(re.search(r"sha-?1(?!\d)", lab))
    return bool(re.search(r"sha-?256", lab))


def _role(context: str, pos: int) -> str | None:
    """'source' / 'target' from the keyword nearest before `pos` (else anywhere)."""
    ctx = context.lower()
    best = None
    for role, rx in (("source", SRC_WORDS), ("target", DST_WORDS)):
        for m in re.finditer(rx, ctx):
            dist = pos - m.start() if m.start() <= pos else 10_000 + m.start() - pos
            if best is None or dist < best[0]:
                best = (dist, role)
    return best[1] if best else None


def find_hashes(text: str) -> list[dict]:
    """Every hash in a README / release note: {kind, value, role, line}."""
    out = []
    lines = text.splitlines()
    header: list[str] | None = None
    for ln, line in enumerate(lines):
        stripped = line.strip()
        cells = [c.strip() for c in stripped.strip("|").split("|")] if stripped.startswith("|") else None
        if cells and all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
            continue  # table separator row
        if cells and not HASH_RE.search(stripped):
            header = cells  # a table header (or a row without hashes)
        elif not cells:
            header = None
        for m in HASH_RE.finditer(line):
            tok = m.group(1)
            kind = HASH_KIND[len(tok)]
            label, role = "", None
            if cells and header:
                # Which column is this token in?
                col = line[:m.start()].count("|") - (1 if stripped.startswith("|") else 0)
                if 0 <= col < len(header):
                    label = header[col]
                    role = _role(label, len(label))
            near = " ".join(lines[max(0, ln - 1):ln + 1])
            if not _label_kind(label + " " + line + " " + near, len(tok)):
                if not (kind == "crc32" and line[max(0, m.start() - 1):m.start()] == "`"
                        and re.search(r"[A-Fa-f]", tok) and re.search(r"\d", tok)):
                    continue  # unlabeled hex (dates, commit ids, ...)
            if role is None:
                role = _role(line, m.start()) or _role(near, len(near))
            out.append({"kind": kind, "value": tok.upper() if kind == "crc32" else tok.lower(), "role": role, "line": ln})
    return out


def rom_info_from_notes(text: str, src_crc: str | None, dst_crc: str | None) -> dict:
    """Hashes / name of the required (source) and resulting (target) ROM found in text."""
    hs = find_hashes(text)
    lines = text.splitlines()
    res: dict = {"source": {}, "target": {}}
    # Anchor on CRCs we already trust (from the BPS): other hashes on the same line belong to the same ROM.
    anchors = {}
    for h in hs:
        if h["kind"] == "crc32" and h["value"] in (src_crc, dst_crc):
            anchors[h["line"]] = "source" if h["value"] == src_crc else "target"
    if src_crc or dst_crc:
        # Only trust notes about this patch: drop hashes for other ROMs listed in the same file.
        known = {c for c in (src_crc, dst_crc) if c}
        if not anchors and any(h["kind"] == "crc32" for h in hs) and not any(h["value"] in known for h in hs):
            return res | {"mismatch": sorted({h["value"] for h in hs if h["kind"] == "crc32"})}
    known = {c for c in (src_crc, dst_crc) if c}
    foreign = {h["line"] for h in hs if known and h["kind"] == "crc32" and h["value"] not in known}
    for h in hs:
        role = h["role"]
        if h["line"] in foreign and h["line"] not in anchors:
            continue  # this line is about a ROM with another CRC32
        if anchors:
            dist, near_role = min((abs(h["line"] - ln), r) for ln, r in anchors.items())
            if h["line"] in anchors:
                role = anchors[h["line"]] if h["kind"] == "crc32" else (role or anchors[h["line"]])
            elif dist <= 3:
                role = role or near_role
            else:
                continue  # far from every line about this patch's ROMs: some other ROM
        if role in ("source", "target"):
            res[role].setdefault(h["kind"], set()).add(h["value"])
    out: dict = {"source": {}, "target": {}}
    for role in ("source", "target"):
        other = {h["value"] for h in hs if h["line"] in foreign and h["line"] not in anchors and h["kind"] == "crc32"
                 and h["role"] == role and h["value"] not in known}
        if len(other) == 1:
            out[role]["crc32_other"] = next(iter(other))
        for kind, vals in res[role].items():
            if len(vals) == 1:  # ambiguous values are dropped
                out[role][kind] = next(iter(vals))
    # A No-Intro style name of the original ROM, preferably on/near an anchored line.
    name_lines = [ln for ln, r in anchors.items() if r == "source"] or range(len(lines))
    for ln in name_lines:
        for k in (ln, ln - 1, ln + 1, ln - 2, ln + 2, ln - 3, ln + 3) if anchors else (ln,):
            if 0 <= k < len(lines) and (k not in foreign or k in anchors):
                m = ROM_NAME_RE.search(lines[k])
                if m:
                    name = re.sub(r"^[\s:>\-–*#]+", "", m.group(1)).strip()
                    name = re.sub(r"^(?:[A-Za-z]+\s+){0,3}?(?:rom|file|game|name|cartridge)\s*(?:name)?\s*[:=]\s*", "", name, flags=re.I)
                    out["source"]["name"] = re.split(r"[\\/]", name)[-1]
                    break
        if "name" in out["source"]:
            break
    return out


# --------------------------------------------------------------------- heuristics

LANG_WORDS = {
    "en": ["english", "inglese", "anglais", "ingles", "inglés"],
    "it": ["italian", "italiano", "italiana"],
    "es": ["spanish", "español", "espanol", "castellano"],
    "fr": ["french", "français", "francais"],
    "de": ["german", "deutsch"],
    "pt": ["portuguese", "português", "portugues", "pt-br"],
    "zh": ["chinese", "中文"],
    "ko": ["korean"],
    "ru": ["russian"],
}
LANG_CODE = {"en": "en", "eng": "en", "it": "it", "ita": "it", "es": "es", "esp": "es", "spa": "es", "fr": "fr",
             "fra": "fr", "fre": "fr", "de": "de", "deu": "de", "ger": "de", "pt": "pt", "por": "pt", "ptbr": "pt",
             "ru": "ru", "rus": "ru", "ko": "ko", "kor": "ko", "zh": "zh", "chs": "zh", "cht": "zh"}


def guess_language(texts: list[str], default: str | None) -> str | None:
    raw = " ".join(t for t in texts if t)
    blob = raw.lower()
    # 1. Translation tags: "T-En", "[T+Ita]", "T-Es v0.2".
    m = re.search(r"(?<![a-z])t[-+](" + "|".join(LANG_CODE) + r")(?![a-z])", blob)
    if m:
        return LANG_CODE[m.group(1)]
    # 2. Hyphenated lower-case file stems such as "lode-runner-se-ja-1.0": the last code wins.
    stem = strip_ext(Path(texts[0]).name) if texts and texts[0] else ""
    if re.fullmatch(r"[a-z0-9.\-]+", stem):
        codes = re.findall(r"(?<=-)(en|ja|it|es|fr|de|pt)(?=-|$)", stem)
        if codes:
            return codes[-1]
    # 3. Language words.
    for code, words in LANG_WORDS.items():
        if any(re.search(rf"(?<![a-z]){re.escape(w)}(?![a-z])", blob) for w in words):
            return code
    # 4. Upper-case codes set off by separators: "Chocobo_EN_v1.1", "Game-ITA.ips", "(ES)".
    #    Case-sensitive on purpose: lower-case "de"/"it" are ordinary words in titles.
    m = re.search(r"(?:^|[_\-.\s\[(])(EN|ENG|IT|ITA|ES|ESP|FR|FRA|DE|GER|PT|PTBR|RU)(?=$|[_\-.\s\])])", raw)
    if m:
        return LANG_CODE[m.group(1).lower()]
    return default


VERSION_RE = re.compile(r"(?<![a-z0-9])v(?:er(?:sion)?)?[ ._-]?(\d+(?:\.\d+){0,3}[a-z]?)(?!\d|\.\d)", re.I)
VERSION_RE2 = re.compile(r"(?<![\d.])(\d+\.\d+(?:\.\d+){0,2})(?!\d|\.\d)")


def strip_ext(t: str) -> str:
    return re.sub(r"\.(bps|ips|zip|7z)$", "", t, flags=re.I)


def guess_version(texts: list[str | None]) -> str | None:
    for rx in (VERSION_RE, VERSION_RE2):
        for t in texts:
            if t:
                m = rx.search(strip_ext(t))
                if m:
                    return m.group(1)
    return None


def version_key(v: str | None) -> tuple:
    return tuple(int(x) if x.isdigit() else x for x in re.findall(r"\d+|[a-z]+", (v or "").lower()))


def natural(s: str) -> tuple:
    return tuple((0, int(x)) if x.isdigit() else (1, x) for x in re.findall(r"\d+|\D+", s.lower()))


PLATFORMS = [
    ("saturn", r"saturn"), ("psx", r"\bps1\b|\bpsx\b|playstation"), ("nds", r"nintendo ds|\bnds\b|^ds$"),
    ("gba", r"\bgba\b|game boy advance"), ("pce", r"pc[- ]?engine|\bpce\b|turbografx"),
    ("wonderswan_color", r"wonderswan colou?r|\bwsc\b|swancrystal"), ("wonderswan", r"wonderswan|\bws\b"),
]


def platform_from_text(t: str | None) -> str | None:
    if not t:
        return None
    low = t.lower()
    for name, rx in PLATFORMS:
        if re.search(rx, low):
            return name
    return None


GENERIC_TITLE = re.compile(r"^\s*(test(ing)?|latest|release|patch(es)?|beta|alpha|builds?|files?|v?\d[\d.]*)\b", re.I)


def parse_release_name(name: str | None) -> tuple[str | None, str | None, bool]:
    """'[Release] Title (Platform) - … v1.0 (superseded by …)' -> (title, platform, superseded)."""
    if not name:
        return None, None, False
    s = name.strip()
    tag = None
    m = re.match(r"^\[([^\]]*)\]\s*", s)
    if m:
        tag, s = m.group(1), s[m.end():]
    superseded = bool(re.search(r"\(superseded\b", s, re.I))
    title = s
    for m in re.finditer(r"\s+[-–—]\s+", s):
        segment = re.split(r"\s+[-–—]\s+", s[m.end():], maxsplit=1)[0]
        if re.search(r"translat|patch|tradu|english|spanish|italian|french|german|version|\bv\d", segment, re.I):
            title = s[:m.start()]
            break
    title = title.strip()
    plat = platform_from_text(tag)
    pm = re.search(r"\(([^()]*)\)\s*$", title)
    if pm and platform_from_text(pm.group(1)):
        plat = plat or platform_from_text(pm.group(1))
        title = title[:pm.start()].strip()
    plat = plat or platform_from_text(s)
    title = VERSION_RE.sub("", title).strip(" -–—:")
    if not title or GENERIC_TITLE.match(title):
        title = None
    return title, plat, superseded


NOISE_WORDS = (r"wonderswan|colou?r|english|translation|translated|patch(es)?|ai|gpt|rom|fix|ws|wsc|cumulative|"
               r"incremental|from|jp|en|test|release|final|bps|ips|t[-+]\w+")


def clean_name(text: str | None) -> str | None:
    """A readable game name from a file or repository name."""
    if not text:
        return None
    t = strip_ext(text)
    t = re.sub(r"\[[^\]]*\]|\((?:Japan|J|Rev[^)]*|En|debug)\)", " ", t, flags=re.I)
    t = VERSION_RE.sub(" ", t)
    t = re.sub(r"(?<![A-Za-z])T[-+][A-Za-z]{2,3}(?![A-Za-z])", " ", t)
    t = re.sub(r"[._]+", " ", t)
    t = re.sub(r"(?<=[A-Za-z0-9])-(?=[A-Za-z0-9])", " ", t)
    t = re.sub(rf"(?<![A-Za-z])({NOISE_WORDS})(?![A-Za-z])", " ", t, flags=re.I)
    t = re.sub(r"\b(HF|QA|RC|MANT|S|Phase)\d+\w*", " ", t)
    t = re.sub(r"\s{2,}", " ", t).strip(" -–—:")
    return t or None


def describe(e: dict, notes: str, default_lang: str | None) -> None:
    """Fill the descriptive fields of an entry from its file, origin and notes."""
    o = e["origin"]
    fname = e["file_name"]
    if e["format"] == "ips":  # IPS has no checksums: anything here came from notes/overrides last time
        e["source_crc32"] = e["target_crc32"] = None
    title, plat, superseded = parse_release_name(o.get("release_name"))
    e["_game_from"] = 3 if title else 0
    repo_name = o["source"].split("/", 1)[1]
    e["variant"] = Path(fname).stem
    if o["kind"] == "release_asset":
        vtexts = [o.get("asset_name"), fname, o.get("archive_member"), o.get("release_name"), o.get("release_tag")]
    else:
        vtexts = [fname, o.get("repo_path")]
    e["version"] = guess_version(vtexts)
    e["language"] = guess_language([fname, o.get("archive_member"), o.get("asset_name"), o.get("release_name"),
                                    o.get("release_tag"), o.get("repo_path"), repo_name], default_lang)
    e["superseded"] = superseded

    # Platform: the release name first, then any other name, then the ROM size.
    plat = plat or platform_from_text(" ".join(filter(None, [o.get("asset_name"), fname, repo_name])))
    size = e.get("source_size") or e.get("target_size")
    if size and size > MAX_WS_ROM:
        plat = plat if plat and not plat.startswith("wonderswan") else "other"
    elif plat is None and size and size & (size - 1) == 0 and size >= 128 * 1024:
        plat = "wonderswan"
    e["platform"] = plat

    # Required / produced ROM.
    info = rom_info_from_notes(notes, e.get("source_crc32"), e.get("target_crc32")) if notes else {"source": {}, "target": {}}
    src = {"crc32": e.get("source_crc32"), "size": e.get("source_size"), "crc32_from": "bps" if e.get("source_crc32") else None}
    dst = {"crc32": e.get("target_crc32"), "size": e.get("target_size"), "crc32_from": "bps" if e.get("target_crc32") else None}
    for rom, found in ((src, info["source"]), (dst, info["target"])):
        if not rom["crc32"] and found.get("crc32"):
            rom["crc32"], rom["crc32_from"] = found["crc32"], "notes"
        if rom["crc32"] and not found.get("crc32") and found.get("crc32_other"):
            rom["notes_crc32_mismatch"] = found["crc32_other"]  # notes name another ROM for this role
        elif rom["crc32"] and found.get("crc32") and found["crc32"] != rom["crc32"]:
            rom["notes_crc32_mismatch"] = found["crc32"]  # notes describe another revision: ignore them
            continue
        for k in ("md5", "sha1", "sha256", "name"):
            if found.get(k):
                rom[k] = found[k]
    if info.get("mismatch"):
        src["notes_other_crc32"] = info["mismatch"]
    if not title:
        # No release title: the No-Intro name in the notes, else the repository / file name.
        rom_name = re.sub(r"\s*\((?:Japan|J|Asia|Rev[^)]*|En|[A-Z][a-z]+(?:,\s*[A-Z][a-z]+)*)\)|\.(?:wsc|ws|pc2)$", "",
                          src.get("name") or "").strip() or None
        if o["kind"] == "repo_file":
            title = rom_name or clean_name(repo_name) or clean_name(fname)
            e["_game_from"] = 2 if rom_name else 1
        else:
            title = rom_name or clean_name(fname)
            e["_game_from"] = 2 if rom_name else 0
    e["game"] = title
    e["source_rom"] = {k: v for k, v in src.items() if v is not None}
    e["target_rom"] = {k: v for k, v in dst.items() if v is not None}
    e["source_crc32"] = src["crc32"]
    e["target_crc32"] = dst["crc32"]


# ------------------------------------------------------------------------ storage

SAFE = re.compile(r"[^A-Za-z0-9._ ()\[\]&+,!'-]")


def safe(part: str) -> str:
    return (SAFE.sub("_", part).strip(" .") or "_")[:120]


def raw_url(rel: str) -> str:
    return f"https://raw.githubusercontent.com/{SELF_REPO}/{BRANCH}/" + urllib.parse.quote(rel)


def store(rel_dir: Path, name: str, data: bytes, sha: str, taken: dict) -> str:
    path = PATCH_DIR / rel_dir / safe(name)
    rel = path.relative_to(ROOT).as_posix()
    if taken.get(rel, sha) != sha:  # same name, different content in the same place
        stem, ext = os.path.splitext(safe(name))
        path = path.with_name(f"{stem}.{sha[:8]}{ext}")
        rel = path.relative_to(ROOT).as_posix()
    taken[rel] = sha
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != sha:
        path.write_bytes(data)
    return rel


# -------------------------------------------------------------------------- crawl

def load_json(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def make_entries(found, origin: dict, rel_dir: Path, taken: dict, seen_sha: set) -> list[dict]:
    entries = []
    for fname, member, data in found:
        info = parse_patch(data)
        if not info:
            log(f"  ! not a valid BPS/IPS: {fname}")
            continue
        sha = hashlib.sha256(data).hexdigest()
        if sha in seen_sha:  # same patch twice in one release (raw + inside zip)
            continue
        seen_sha.add(sha)
        rel = store(rel_dir, fname, data, sha, taken)
        entries.append({
            "id": sha[:16], "file_name": fname, "path": rel, "url": raw_url(rel),
            "size": len(data), "sha256": sha, "patch_crc32": crc32(data),
            **info, "origin": {**origin, "archive_member": member},
        })
    return entries


def readme_notes(full: str, tree_nodes: list[dict], cache: dict) -> callable:
    """Return f(repo_path) -> text of the README files in that folder and its parents (closest first)."""
    readmes = {}
    for n in tree_nodes:
        p = n.get("path", "")
        if n.get("type") == "blob" and re.fullmatch(r"(?:.*/)?readme(?:\.[a-z]+)?", p, re.I) and n.get("size", 0) < 512 * 1024:
            readmes.setdefault(str(Path(p).parent), []).append(n)

    def text(d: str) -> str:
        parts = []
        for n in readmes.get(d, []):
            if n["sha"] not in cache:
                try:
                    cache[n["sha"]] = http_get(f"{API}/repos/{full}/git/blobs/{n['sha']}",
                                               accept="application/vnd.github.raw")[0].decode("utf-8", "replace")
                except Exception as e:  # noqa: BLE001
                    log(f"  ! README {n['path']}: {e}")
                    cache[n["sha"]] = ""
            parts.append(cache[n["sha"]])
        return "\n".join(parts)

    def for_path(p: str) -> str:
        d, out = Path(p).parent, []
        while True:
            out.append(text(str(d)))
            if str(d) == ".":
                break
            d = d.parent
        return "\n\n".join(x for x in out if x)

    return for_path


def crawl_repo(full: str, src: dict, state_items: dict, old_by_key: dict, taken: dict, notes: dict) -> tuple[list[dict], dict]:
    """Return (entries, new state items) for one repository. Raises on API failure."""
    meta = api(f"/repos/{full}")
    owner, repo = full.split("/")
    branch = meta.get("default_branch") or "main"
    exclude = re.compile(src.get("exclude_paths", DEFAULT_EXCLUDE_PATHS), re.I)
    entries, items = [], {}

    def reuse_or(key, build, note_text):
        cached = old_by_key.get(key, [])  # [] = processed before, held no valid patch
        if key in state_items and all(e.get("path") is None or (ROOT / e["path"]).exists() for e in cached) \
                and not any(e.get("path") is None and e.get("platform") in ALLOWED for e in cached):
            got = [json.loads(json.dumps(e)) for e in cached]
            for e in got:
                if e.get("path"):
                    taken[e["path"]] = e["sha256"]
            items[key] = state_items[key]
        else:
            got = build()
            items[key] = {"entries": [e["sha256"] for e in got]}
            for e in got:
                e["origin"]["key"] = key
        for e in got:
            notes[id(e)] = note_text
        entries.extend(got)

    for rel in api_list(f"/repos/{full}/releases"):
        if rel.get("draft"):
            continue
        tag = rel.get("tag_name") or str(rel["id"])
        seen_sha: set = set()
        body = rel.get("body") or ""
        for asset in sorted(rel.get("assets", []), key=lambda a: not a["name"].lower().endswith(PATCH_EXT)):
            if not asset["name"].lower().endswith(PATCH_EXT + ARCHIVE_EXT):
                continue
            if asset.get("size", 0) > MAX_DOWNLOAD:
                log(f"  ! {full} {tag}: {asset['name']} too large, skipped")
                continue
            key = f"asset:{full}:{asset['id']}:{asset.get('updated_at')}:{asset.get('size')}"
            origin = {
                "source": full, "source_url": meta["html_url"], "kind": "release_asset",
                "release_tag": tag, "release_name": rel.get("name") or tag,
                "release_url": rel.get("html_url"), "published_at": rel.get("published_at"),
                "prerelease": bool(rel.get("prerelease")), "asset_name": asset["name"],
                "download_url": asset.get("browser_download_url"),
            }

            def build(asset=asset, origin=origin, tag=tag, seen_sha=seen_sha):
                log(f"  ↓ {full} {tag}: {asset['name']}")
                data, _ = http_get(f"{API}/repos/{full}/releases/assets/{asset['id']}", accept="application/octet-stream")
                found = extract_patches(asset["name"], data)
                if not found:
                    log("    (no .bps/.ips inside)")
                return make_entries(found, origin, Path(safe(owner)) / safe(repo) / safe(tag), taken, seen_sha)

            reuse_or(key, build, body)

    tree = api(f"/repos/{full}/git/trees/{urllib.parse.quote(branch)}?recursive=1")
    if tree.get("truncated"):
        log(f"  ! {full}: file tree truncated by the API; some committed files may be missed")
    nodes = tree.get("tree", [])
    patch_nodes = [n for n in nodes if n.get("type") == "blob"
                   and n.get("path", "").lower().endswith(PATCH_EXT + ARCHIVE_EXT)
                   and n.get("size", 0) <= MAX_DOWNLOAD and not exclude.search(n["path"])]
    readme_for = readme_notes(full, nodes, {}) if patch_nodes else None
    for node in patch_nodes:
        p = node["path"]
        key = f"blob:{full}:{node['sha']}:{p}"
        origin = {
            "source": full, "source_url": meta["html_url"], "kind": "repo_file", "branch": branch,
            "repo_path": p, "file_url": f"{meta['html_url']}/blob/{branch}/{urllib.parse.quote(p)}",
            "release_tag": None, "release_name": None,
        }

        def build(node=node, origin=origin, p=p):
            log(f"  ↓ {full} (repo) {p}")
            data, _ = http_get(f"{API}/repos/{full}/git/blobs/{node['sha']}", accept="application/vnd.github.raw")
            parent = Path(p).parent
            return make_entries(extract_patches(p, data), origin,
                                Path(safe(owner)) / safe(repo) / "_files" / Path(*map(safe, parent.parts)), taken, set())

        reuse_or(key, build, readme_for(p))
    return entries, items


def expand_sources(sources: list[dict]) -> list[tuple[str, dict]]:
    repos = []
    for s in sources:
        if s["type"] == "repo":
            repos.append((s["repo"], s))
        elif s["type"] == "user":
            for r in api_list(f"/users/{s['user']}/repos?type=owner&sort=full_name"):
                if not r.get("fork"):
                    repos.append((r["full_name"], s))
    return repos


def finalize(entries: list[dict], platforms: list) -> tuple[list[dict], list[dict]]:
    """Split by platform, flag duplicates and the latest version of each game."""
    kept, excluded = [], []
    for e in entries:
        if e.get("platform") in platforms:
            e.pop("excluded", None)
            kept.append(e)
        else:
            e["excluded"] = f"platform {e.get('platform')}"
            e["path"] = e["url"] = None
            excluded.append(e)

    # IPS published next to a BPS of the same name (same folder / same release) was made from the same ROMs.
    bps_by_stem = {}
    for e in kept:
        if e["format"] == "bps":
            o = e["origin"]
            where = o.get("release_tag") or str(Path(o.get("repo_path") or "").parent)
            bps_by_stem[(o["source"], where, Path(e["file_name"]).stem.lower())] = e
    for e in kept:
        if e["format"] == "ips":
            o = e["origin"]
            where = o.get("release_tag") or str(Path(o.get("repo_path") or "").parent)
            twin = bps_by_stem.get((o["source"], where, Path(e["file_name"]).stem.lower()))
            if twin:
                for side in ("source", "target"):
                    rom = e.setdefault(f"{side}_rom", {})
                    tc = twin.get(f"{side}_crc32")
                    if tc and rom.get("crc32_from") in (None, "notes"):
                        if rom.get("crc32") and rom["crc32"] != tc:  # the notes were about another revision
                            mism = rom["crc32"]
                            rom.clear()
                            rom["notes_crc32_mismatch"] = mism
                            if side == "source":  # so was any name taken from them
                                e["game"], e["_game_from"] = twin.get("game"), twin.get("_game_from", 0)
                        rom.update(crc32=tc, crc32_from="bps_twin", size=twin.get(f"{side}_size"))
                        for k in ("md5", "sha1", "sha256", "name"):
                            if k not in rom and twin.get(f"{side}_rom", {}).get(k):
                                rom[k] = twin[f"{side}_rom"][k]
                        e[f"{side}_crc32"] = tc
                e["bps_twin"] = twin["id"]

    # Patches whose input is the output of another patch (incremental updates, add-ons).
    produced_by: dict = {}
    for e in kept:
        if e.get("target_crc32"):
            produced_by.setdefault(e["target_crc32"], []).append(e["id"])
    for e in kept:
        prev = [i for i in produced_by.get(e.get("source_crc32") or "", []) if i != e["id"]]
        e["requires_patched_rom"] = bool(prev)
        e["requires_patch_ids"] = sorted(set(prev)) or None
        if prev:  # notes about the original Japanese ROM are expected to disagree here
            e.get("source_rom", {}).pop("notes_crc32_mismatch", None)

    # One name per original ROM: the best-sourced name wins (release title > ROM name in notes > repo/file name).
    best_name: dict = {}
    for e in kept:
        c = e.get("source_crc32")
        if c and e.get("game") and not e["requires_patched_rom"]:
            if c not in best_name or e.get("_game_from", 0) > best_name[c][0]:
                best_name[c] = (e.get("_game_from", 0), e["game"])
    for e in kept:
        b = best_name.get(e.get("source_crc32") or "")
        if b and b[0] > e.get("_game_from", 0):
            e["game"] = b[1]
    for e in entries:
        e.pop("_game_from", None)

    # Identical files in one source: keep the release asset / the shortest path, mark the others.
    by_sha: dict = {}
    for e in kept:
        by_sha.setdefault((e["origin"]["source"], e["sha256"]), []).append(e)
    for group in by_sha.values():
        group.sort(key=lambda e: (e["origin"]["kind"] != "release_asset", len(e["path"]), e["path"]))
        for e in group:
            e["duplicate_of"] = None
        for e in group[1:]:
            e["duplicate_of"] = group[0]["id"]

    # Latest: per source and original ROM, highest version that is not superseded.
    groups: dict = {}
    for e in kept:
        if e.get("duplicate_of"):
            e["latest"] = False
            continue
        groups.setdefault((e["origin"]["source"], e.get("source_crc32") or e.get("game")), []).append(e)

    def rank(e):
        o = e["origin"]
        return (not e.get("superseded"), not o.get("prerelease"), version_key(e.get("version")), o.get("published_at") or "",
                natural(e.get("variant") or "") if o["kind"] == "repo_file" else ())

    for group in groups.values():
        best = max(rank(e) for e in group)
        for e in group:
            e["latest"] = rank(e) == best
    return kept, excluded


ENTRY_ORDER = ["id", "game", "variant", "version", "language", "platform", "latest", "superseded", "duplicate_of",
               "requires_patched_rom", "requires_patch_ids", "bps_twin", "format", "source_crc32", "target_crc32", "source_rom", "target_rom", "file_name", "path", "url",
               "size", "sha256", "patch_crc32"]


def ordered(e: dict) -> dict:
    return {**{k: e[k] for k in ENTRY_ORDER if k in e}, **{k: v for k, v in e.items() if k not in ENTRY_ORDER}}


def main() -> int:
    cfg = load_json(SOURCES_JSON, {})
    sources = cfg.get("sources", [])
    platforms = cfg.get("platforms", DEFAULT_PLATFORMS)
    ALLOWED[:] = platforms
    overrides = {k: v for k, v in load_json(OVERRIDES_JSON, {}).items() if not k.startswith("_")}
    state = load_json(STATE_JSON, {"items": {}})
    old_index = load_json(INDEX_JSON, {"entries": []})
    old_all = old_index.get("entries", []) + old_index.get("excluded", [])
    old_by_key: dict[str, list] = {}
    for e in old_all:
        old_by_key.setdefault(e["origin"].get("key", ""), []).append(e)
    first_seen = {(e["sha256"], e["origin"].get("key")): e.get("first_seen") for e in old_all}

    taken: dict = {}
    notes: dict = {}
    frozen: set = set()  # entries of sources that failed this run: keep their old description
    entries, items, failed, crawled = [], {}, [], []
    try:
        repos = expand_sources(sources)
    except Exception as e:  # noqa: BLE001
        log(f"! cannot list sources: {e}")
        return 1
    src_of = {}
    for full, src in repos:
        log(f"== {full}")
        src_of[full] = src
        prev_items = {k: v for k, v in state.get("items", {}).items() if k.split(":")[1] == full}
        try:
            got, its = crawl_repo(full, src, prev_items, old_by_key, taken, notes)
        except Exception as e:  # noqa: BLE001 - keep the previous data for this source
            log(f"! {full} failed: {e}; keeping previous data")
            failed.append(full)
            prev = [json.loads(json.dumps(x)) for x in old_all if x["origin"]["source"] == full]
            for x in prev:
                frozen.add(id(x))
                if x.get("path"):
                    taken[x["path"]] = x["sha256"]
            entries.extend(prev)
            items.update(prev_items)
            continue
        crawled.append(full)
        log(f"   {len(got)} patch(es)")
        entries.extend(got)
        items.update(its)

    today = dt.date.today().isoformat()
    for e in entries:
        if id(e) not in frozen:
            describe(e, notes.get(id(e), ""), src_of.get(e["origin"]["source"], {}).get("default_language"))
        e["first_seen"] = first_seen.get((e["sha256"], e["origin"].get("key"))) or e.get("first_seen") or today
        if e["sha256"] in overrides:
            e.update({k: v for k, v in overrides[e["sha256"]].items() if k not in ("sha256", "path", "url")})
    kept, excluded = finalize(entries, platforms)
    sort_key = lambda e: ((e.get("game") or "").lower(), e["origin"]["source"].lower(),  # noqa: E731
                          version_key(e.get("version")), natural(e["file_name"]))
    kept = [ordered(e) for e in sorted(kept, key=sort_key)]
    excluded = [ordered(e) for e in sorted(excluded, key=sort_key)]
    for e in excluded:
        log(f"  · excluded ({e['excluded']}): {e['origin']['source']} {e['file_name']}")

    # Remove files no longer referenced (only under patches/).
    keep = {e["path"] for e in kept}
    for f in sorted(PATCH_DIR.rglob("*"), reverse=True) if PATCH_DIR.exists() else []:
        rel = f.relative_to(ROOT).as_posix()
        if f.is_file() and rel not in keep:
            log(f"- removing {rel}")
            f.unlink()
        elif f.is_dir() and not any(f.iterdir()):
            f.rmdir()

    old_cmp = old_index.get("entries", []), old_index.get("excluded", [])
    changed = json.dumps([kept, excluded], sort_keys=True) != json.dumps(list(old_cmp), sort_keys=True) \
        or old_index.get("schema") != SCHEMA
    index = {
        "schema": SCHEMA,
        "repository": SELF_REPO,
        "generated_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
        if changed or "generated_at" not in old_index else old_index["generated_at"],
        "sources": [s for s, _ in repos],
        "platforms": platforms,
        "count": len(kept),
        "entries": kept,
        "excluded": excluded,
    }
    INDEX_JSON.write_text(json.dumps(index, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    STATE_JSON.write_text(json.dumps({"items": dict(sorted(items.items()))}, indent=1) + "\n", encoding="utf-8")
    write_csv(kept)
    write_md(index)
    write_lookup(kept)

    summary = (f"Sources: {len(repos)} repos ({len(crawled)} ok, {len(failed)} failed). "
               f"Patches: {len(kept)} ({sum(1 for e in kept if e.get('latest'))} latest, "
               f"{sum(1 for e in kept if e.get('duplicate_of'))} duplicates), {len(excluded)} excluded. Changed: {changed}.")
    log(summary)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
            fh.write(f"### Patch crawl\n\n{summary}\n\n" + (f"Failed: {', '.join(failed)}\n" if failed else ""))
    return 0 if not failed or crawled else 1


HASH_KINDS = ("crc32", "md5", "sha1", "sha256")


def write_lookup(entries: list[dict]) -> None:
    """roms.json + lookup/<kind>/<hash>.json: from any hash of a ROM to the patches that take it as input.

    Keyed by the ROM a patch *needs* (source_rom). Each file also lists the patches that *produce* that ROM, so an
    already-patched ROM is recognised too (and incremental patches are found from it).
    """
    roms: dict[str, dict] = {}

    def rom(crc: str) -> dict:
        return roms.setdefault(crc, {"crc32": crc, "size": None, "md5": set(), "sha1": set(), "sha256": set(),
                                     "names": set(), "patches": [], "produced_by": []})

    def brief(e: dict) -> dict:
        o = e["origin"]
        return {k: v for k, v in {
            "id": e["id"], "game": e.get("game"), "variant": e.get("variant"), "version": e.get("version"),
            "language": e.get("language"), "platform": e.get("platform"), "latest": e.get("latest"),
            "superseded": e.get("superseded"), "format": e["format"],
            "requires_patched_rom": e.get("requires_patched_rom") or None,
            "target_crc32": e.get("target_crc32"), "target_size": e.get("target_size"),
            "url": e["url"], "sha256": e["sha256"], "size": e["size"], "source": o["source"],
            "page": o.get("release_url") or o.get("file_url"),
        }.items() if v is not None}

    for e in entries:
        if e.get("duplicate_of"):
            continue
        for side, bucket in (("source", "patches"), ("target", "produced_by")):
            info = e.get(f"{side}_rom") or {}
            crc = info.get("crc32")
            if not crc:
                continue
            r = rom(crc)
            r["size"] = r["size"] or info.get("size")
            for k in ("md5", "sha1", "sha256"):
                if info.get(k):
                    r[k].add(info[k])
            if info.get("name"):
                r["names"].add(info["name"])
            r[bucket].append(brief(e))

    order = lambda b: (not b.get("latest"), b.get("superseded", False), [-x if isinstance(x, int) else 0  # noqa: E731
                       for x in version_key(b.get("version"))], b.get("game") or "", b["variant"] or "")
    by_kind: dict[str, dict] = {k: {} for k in HASH_KINDS}
    out_roms = {}
    for crc in sorted(roms):
        r = roms[crc]
        clean = {"crc32": crc, "size": r["size"]}
        for k in ("md5", "sha1", "sha256"):
            if len(r[k]) == 1:  # conflicting values from different notes are left out
                clean[k] = next(iter(r[k]))
        clean["names"] = sorted(r["names"])
        clean["patches"] = sorted(r["patches"], key=order)
        clean["produced_by"] = sorted(r["produced_by"], key=order)
        out_roms[crc] = clean
        for k in HASH_KINDS:
            if clean.get(k):
                by_kind[k][clean[k]] = crc

    ROMS_JSON.write_text(json.dumps({
        "schema": 1,
        "about": "ROM hash -> patches. by_crc32 is keyed by the CRC32 of the ROM a patch needs (patches) or produces "
                 "(produced_by). by_md5 / by_sha1 / by_sha256 map those hashes to the CRC32 key.",
        "by_crc32": out_roms,
        **{f"by_{k}": dict(sorted(v.items())) for k, v in by_kind.items() if k != "crc32"},
    }, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")

    # One small file per hash, so a client fetches only what it needs (404 = no patch for that ROM).
    wanted = {}
    for crc, r in out_roms.items():
        for k in HASH_KINDS:
            if r.get(k):
                key = r[k].upper() if k == "crc32" else r[k].lower()
                wanted[LOOKUP_DIR / k / f"{key}.json"] = json.dumps({"schema": 1, "query": {k: key}, **r},
                                                                     indent=1, ensure_ascii=False) + "\n"
    for f in list(LOOKUP_DIR.rglob("*.json")) if LOOKUP_DIR.exists() else []:
        if f not in wanted:
            f.unlink()
    for f, text in wanted.items():
        f.parent.mkdir(parents=True, exist_ok=True)
        if not f.exists() or f.read_text(encoding="utf-8") != text:
            f.write_text(text, encoding="utf-8")


CSV_COLS = ["id", "game", "variant", "version", "language", "platform", "latest", "superseded", "duplicate_of",
            "format", "source_crc32", "source_md5", "source_sha1", "source_sha256", "source_rom_name", "source_size",
            "target_crc32", "target_sha1", "target_size", "patch_crc32", "sha256", "file_name", "path", "url",
            "source", "kind", "release_tag", "release_name", "asset_name", "archive_member", "repo_path",
            "release_url", "file_url", "first_seen"]


def write_csv(entries):
    with INDEX_CSV.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(CSV_COLS)
        for e in entries:
            o, s, t = e["origin"], e.get("source_rom", {}), e.get("target_rom", {})
            row = {**o, **e, "source_md5": s.get("md5"), "source_sha1": s.get("sha1"), "source_sha256": s.get("sha256"),
                   "source_rom_name": s.get("name"), "target_sha1": t.get("sha1")}
            w.writerow([row.get(c) for c in CSV_COLS])


def write_md(index):
    cell = lambda v: (str(v) if v not in (None, "") else "—").replace("|", "\\|")  # noqa: E731
    rows = ["| Game | Variant | Ver. | Lang | Fmt | Original ROM CRC32 | Patched CRC32 | Source |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    shown = [e for e in index["entries"] if not e.get("duplicate_of")]
    for e in shown:
        o = e["origin"]
        where = o.get("release_url") or o.get("file_url") or o["source_url"]
        star = " ★" if e.get("latest") else ""
        note = "" if e.get("source_rom", {}).get("crc32_from") != "notes" else " (notes)"
        rows.append(f"| {cell(e.get('game'))} | [{cell(e['variant'])}]({urllib.parse.quote(e['path'])}){star} | "
                    f"{cell(e.get('version'))} | {cell(e.get('language'))} | {e['format'].upper()} | "
                    f"`{cell(e.get('source_crc32'))}`{note} | `{cell(e.get('target_crc32'))}` | "
                    f"[{cell(o['source'])}]({where}) |")
    PATCHES_MD.write_text(
        "# Patch catalog\n\n"
        f"Generated by `scripts/crawl.py` — {len(shown)} patches ({index['count']} files incl. duplicates), "
        f"last change {index['generated_at']}. ★ = latest version from that source.\n"
        "Machine-readable versions: [`index.json`](index.json), [`index.csv`](index.csv).\n\n"
        + "\n".join(rows) + "\n", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())

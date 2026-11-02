#!/usr/bin/env python3
"""Mirror WonderSwan .bps/.ips translation patches from GitHub sources.

For every source in sources.json the crawler looks at
  * the assets of every published release, and
  * the files committed on the default branch,
takes .bps/.ips files directly and pulls them out of .zip/.7z archives, then
stores them under patches/ and writes index.json, index.csv and PATCHES.md.

BPS patches carry the CRC32 of the original ROM and of the patched ROM in
their footer, so both are recorded; IPS patches carry no checksums, so those
fields stay empty unless overrides.json supplies them.

The run is incremental: a release asset (id + updated_at + size) or a git blob
(sha) that was already processed is not downloaded again. Patches that
disappear upstream are removed here too, but only when their source was
crawled successfully, so a temporary API error never deletes anything.

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

API = "https://api.github.com"
TOKEN = os.environ.get("GITHUB_TOKEN", "")
SELF_REPO = os.environ.get("GITHUB_REPOSITORY", "mariovirgili/WonderSwan_bps_repo")
BRANCH = os.environ.get("TARGET_BRANCH", "main")
PATCH_EXT = (".bps", ".ips")
ARCHIVE_EXT = (".zip", ".7z")
MAX_DOWNLOAD = 100 * 1024 * 1024
MAX_PATCH = 50 * 1024 * 1024
SCHEMA = 1

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
                length = int(r.headers.get("Content-Length") or 0)
                if length > MAX_DOWNLOAD:
                    raise ValueError(f"too large ({length} bytes)")
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


# ----------------------------------------------------------------------- parsing

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


def parse_patch(name: str, d: bytes) -> dict | None:
    """Return format-specific info, or None if the bytes are not a valid patch."""
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
            "source_size": src_size,
            "target_size": dst_size,
            "source_crc32": f"{src_crc:08X}",
            "target_crc32": f"{dst_crc:08X}",
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
            "format": "ips",
            "ips_records": records,
            "ips_min_target_size": max_end,
            "ips_truncate_size": truncate,
            "source_size": None,
            "target_size": truncate,
            "source_crc32": None,
            "target_crc32": None,
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
        import tempfile
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


# --------------------------------------------------------------------- heuristics

LANG_WORDS = {
    "en": ["english", "inglese", "anglais", "ingles", "inglés"],
    "it": ["italian", "italiano", "italiana"],
    "es": ["spanish", "español", "espanol", "castellano"],
    "fr": ["french", "français", "francais"],
    "de": ["german", "deutsch"],
    "pt": ["portuguese", "português", "portugues", "pt-br", "ptbr"],
    "zh": ["chinese", "中文"],
    "ko": ["korean"],
    "ru": ["russian"],
}
LANG_CODES = {"en": "en|eng", "it": "it|ita", "es": "es|esp|spa", "fr": "fr|fra|fre", "de": "de|deu|ger",
              "pt": "pt|por|ptbr|pt-br", "zh": "zh|chi|chs|cht", "ko": "ko|kor", "ru": "ru|rus"}
LANG_3 = {"eng": "en", "ita": "it", "esp": "es", "spa": "es", "fra": "fr", "fre": "fr", "deu": "de", "ger": "de",
          "por": "pt", "ptbr": "pt", "rus": "ru", "kor": "ko", "chs": "zh", "cht": "zh"}


def guess_language(texts: list[str], default: str | None) -> str | None:
    blob = " ".join(t for t in texts if t).lower()
    for code, words in LANG_WORDS.items():
        if any(re.search(rf"(?<![a-z]){re.escape(w)}(?![a-z])", blob) for w in words):
            return code
    # Bracketed tags: "[T-En]", "(ITA)", "[T+Eng v1.2]". Bare two-letter words are
    # ignored because they occur in game titles ("Densha de Go!").
    for code, alts in LANG_CODES.items():
        if re.search(rf"[\[(](?:t[-+])?(?:{alts})(?![a-z])[^\])]*[\])]", blob):
            return code
    # Three-letter codes set off by separators: "Game_ITA.bps", "Game - ESP.ips".
    m = re.search(r"(?:^|[_\-\s.])(" + "|".join(LANG_3) + r")(?=$|[_\-\s.)\]])", blob)
    if m:
        return LANG_3[m.group(1)]
    return default


VERSION_RES = [
    re.compile(r"(?<![a-z0-9])v(?:er(?:sion)?)?[ ._-]?(\d+(?:\.\d+){0,3}[a-z]?)(?![\d.])", re.I),
    re.compile(r"(?<![\d.])(\d+\.\d+(?:\.\d+){0,2})(?![\d.])"),
]


def guess_version(texts: list[str]) -> str | None:
    for rx in VERSION_RES:
        for t in texts:
            if t:
                m = rx.search(t)
                if m:
                    return m.group(1)
    return None


def clean_title(text: str | None) -> str | None:
    if not text:
        return None
    t = re.sub(r"\.(bps|ips|zip|7z)$", "", text, flags=re.I)
    t = VERSION_RES[0].sub("", t)
    t = re.sub(r"[\[(][^\])]*(?:t[-+]|eng|english|patch|translation)[^\])]*[\])]", "", t, flags=re.I)
    t = re.sub(r"\b(english|translation|patch|release|ai|bps|ips)\b", "", t, flags=re.I)
    t = re.sub(r"[\s_\-–—:|]+$", "", re.sub(r"^[\s_\-–—:|]+", "", re.sub(r"\s{2,}", " ", t)))
    return t.strip() or None


# ------------------------------------------------------------------------ storage

SAFE = re.compile(r"[^A-Za-z0-9._ ()\[\]&+,!'-]")


def safe(part: str) -> str:
    part = SAFE.sub("_", part).strip(" .") or "_"
    return part[:120]


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


def make_entries(found, origin: dict, texts: list[str], default_lang, rel_dir: Path, taken: dict, seen_sha: set) -> list[dict]:
    entries = []
    for fname, member, data in found:
        info = parse_patch(fname, data)
        if not info:
            log(f"  ! not a valid BPS/IPS: {fname}")
            continue
        sha = hashlib.sha256(data).hexdigest()
        if sha in seen_sha:  # same patch twice in one release (raw + inside zip)
            continue
        seen_sha.add(sha)
        rel = store(rel_dir, fname, data, sha, taken)
        name_texts = [fname, member or "", *texts]
        entries.append({
            "id": sha[:16],
            "file_name": fname,
            "path": rel,
            "url": raw_url(rel),
            "size": len(data),
            "sha256": sha,
            "patch_crc32": crc32(data),
            "game": clean_title(origin.get("release_name")) or clean_title(origin.get("release_tag"))
                    or clean_title(Path(fname).stem),
            "variant": Path(fname).stem,
            "version": guess_version([origin.get("release_name"), origin.get("release_tag"), fname, member]),
            "language": guess_language(name_texts, default_lang),
            **info,
            "origin": {**origin, "archive_member": member},
        })
    return entries


def crawl_repo(full: str, default_lang, state_items: dict, old_by_key: dict, taken: dict) -> tuple[list[dict], dict]:
    """Return (entries, new state items) for one repository. Raises on API failure."""
    meta = api(f"/repos/{full}")
    owner, repo = full.split("/")
    branch = meta.get("default_branch") or "main"
    entries, items = [], {}

    def reuse_or(key, build):
        cached = old_by_key.get(key, [])  # [] = processed before, held no valid patch
        if key in state_items and all((ROOT / e["path"]).exists() for e in cached):
            for e in cached:
                taken[e["path"]] = e["sha256"]
            items[key] = state_items[key]
            entries.extend(cached)
            return
        new = build()
        items[key] = {"entries": [e["sha256"] for e in new]}
        for e in new:
            e["origin"]["key"] = key
        entries.extend(new)

    for rel in api_list(f"/repos/{full}/releases"):
        if rel.get("draft"):
            continue
        tag = rel.get("tag_name") or str(rel["id"])
        seen_sha: set = set()
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
                return make_entries(found, origin, [rel.get("name"), tag, asset["name"]], default_lang,
                                    Path(safe(owner)) / safe(repo) / safe(tag), taken, seen_sha)

            reuse_or(key, build)

    tree = api(f"/repos/{full}/git/trees/{urllib.parse.quote(branch)}?recursive=1")
    if tree.get("truncated"):
        log(f"  ! {full}: file tree truncated by the API; some committed files may be missed")
    commit_sha = tree.get("sha")
    for node in tree.get("tree", []):
        p = node.get("path", "")
        if node.get("type") != "blob" or not p.lower().endswith(PATCH_EXT + ARCHIVE_EXT):
            continue
        if node.get("size", 0) > MAX_DOWNLOAD:
            continue
        key = f"blob:{full}:{node['sha']}:{p}"
        origin = {
            "source": full, "source_url": meta["html_url"], "kind": "repo_file", "branch": branch,
            "repo_path": p, "file_url": f"{meta['html_url']}/blob/{branch}/{urllib.parse.quote(p)}",
            "release_tag": None, "release_name": None, "tree_sha": commit_sha,
        }

        def build(node=node, origin=origin, p=p):
            log(f"  ↓ {full} (repo) {p}")
            data, _ = http_get(f"{API}/repos/{full}/git/blobs/{node['sha']}", accept="application/vnd.github.raw")
            found = extract_patches(p, data)
            parent = Path(p).parent
            texts = [p, *parent.parts]
            for e in (made := make_entries(found, origin, texts, default_lang,
                                           Path(safe(owner)) / safe(repo) / "_files" / Path(*map(safe, parent.parts)),
                                           taken, set())):
                if not e["game"] or e["game"] == clean_title(e["variant"]):
                    e["game"] = clean_title(parent.name) or e["game"]
            return made

        reuse_or(key, build)
    return entries, items


def expand_sources(sources: list[dict]) -> list[tuple[str, dict]]:
    repos = []
    for s in sources:
        if s["type"] == "repo":
            repos.append((s["repo"], s))
        elif s["type"] == "user":
            for r in api_list(f"/users/{s['user']}/repos?type=owner&sort=full_name"):
                if r.get("fork"):
                    continue
                repos.append((r["full_name"], s))
    return repos


def main() -> int:
    sources = load_json(SOURCES_JSON, {}).get("sources", [])
    overrides = {k: v for k, v in load_json(OVERRIDES_JSON, {}).items() if not k.startswith("_")}
    state = load_json(STATE_JSON, {"items": {}})
    old_index = load_json(INDEX_JSON, {"entries": []})
    old_entries = old_index.get("entries", [])
    old_by_key: dict[str, list] = {}
    for e in old_entries:
        old_by_key.setdefault(e["origin"].get("key", ""), []).append(e)
    first_seen = {(e["sha256"], e["origin"].get("key")): e.get("first_seen") for e in old_entries}

    taken: dict = {}
    entries, items, failed, crawled = [], {}, [], []
    try:
        repos = expand_sources(sources)
    except Exception as e:  # noqa: BLE001
        log(f"! cannot list sources: {e}")
        return 1
    for full, src in repos:
        log(f"== {full}")
        prev_items = {k: v for k, v in state.get("items", {}).items() if k.split(":")[1] == full}
        try:
            got, its = crawl_repo(full, src.get("default_language"), prev_items, old_by_key, taken)
        except Exception as e:  # noqa: BLE001 - keep the previous data for this source
            log(f"! {full} failed: {e}; keeping previous data")
            failed.append(full)
            entries.extend(x for x in old_entries if x["origin"]["source"] == full)
            items.update(prev_items)
            for x in old_entries:
                if x["origin"]["source"] == full:
                    taken[x["path"]] = x["sha256"]
            continue
        crawled.append(full)
        log(f"   {len(got)} patch(es)")
        entries.extend(got)
        items.update(its)

    # Sources removed from sources.json are dropped on purpose.
    today = dt.date.today().isoformat()
    for e in entries:
        e["first_seen"] = first_seen.get((e["sha256"], e["origin"].get("key"))) or e.get("first_seen") or today
        if e["sha256"] in overrides:
            e.update({k: v for k, v in overrides[e["sha256"]].items() if k not in ("sha256", "path", "url")})
    entries.sort(key=lambda e: ((e.get("game") or "").lower(), e["origin"]["source"].lower(),
                                e.get("version") or "", e["file_name"].lower()))

    # Remove files no longer referenced (only under patches/).
    keep = {e["path"] for e in entries}
    for f in sorted(PATCH_DIR.rglob("*"), reverse=True) if PATCH_DIR.exists() else []:
        rel = f.relative_to(ROOT).as_posix()
        if f.is_file() and rel not in keep:
            log(f"- removing {rel} (gone upstream)")
            f.unlink()
        elif f.is_dir() and not any(f.iterdir()):
            f.rmdir()

    changed = json.dumps(entries, sort_keys=True) != json.dumps(old_entries, sort_keys=True)
    index = {
        "schema": SCHEMA,
        "repository": SELF_REPO,
        "generated_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
        if changed or "generated_at" not in old_index else old_index["generated_at"],
        "sources": [s for s, _ in repos],
        "count": len(entries),
        "entries": entries,
    }
    INDEX_JSON.write_text(json.dumps(index, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    STATE_JSON.write_text(json.dumps({"items": dict(sorted(items.items()))}, indent=1) + "\n", encoding="utf-8")
    write_csv(entries)
    write_md(index)

    summary = (f"Sources: {len(repos)} repos ({len(crawled)} ok, {len(failed)} failed). "
               f"Patches: {len(entries)}. Changed: {changed}.")
    log(summary)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
            fh.write(f"### Patch crawl\n\n{summary}\n\n" + (f"Failed: {', '.join(failed)}\n" if failed else ""))
    return 0 if not failed or crawled else 1


CSV_COLS = ["game", "variant", "version", "language", "format", "source_crc32", "target_crc32", "source_size",
            "target_size", "patch_crc32", "sha256", "file_name", "path", "url", "source", "kind", "release_tag",
            "release_name", "asset_name", "archive_member", "repo_path", "release_url", "first_seen"]


def write_csv(entries):
    with INDEX_CSV.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(CSV_COLS)
        for e in entries:
            o = e["origin"]
            w.writerow([e.get(c) if c in e else o.get(c) for c in CSV_COLS])


def write_md(index):
    rows = ["| Game | Variant | Ver. | Lang | Fmt | Original CRC32 | Patched CRC32 | Source |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for e in index["entries"]:
        o = e["origin"]
        where = o.get("release_url") or o.get("file_url") or o["source_url"]
        cell = lambda v: (str(v) if v not in (None, "") else "—").replace("|", "\\|")  # noqa: E731
        rows.append(f"| {cell(e.get('game'))} | [{cell(e['variant'])}]({urllib.parse.quote(e['path'])}) | "
                    f"{cell(e.get('version'))} | {cell(e.get('language'))} | {e['format'].upper()} | "
                    f"`{cell(e.get('source_crc32'))}` | `{cell(e.get('target_crc32'))}` | "
                    f"[{cell(o['source'])}]({where}) |")
    PATCHES_MD.write_text(
        "# Patch catalog\n\n"
        f"Generated by `scripts/crawl.py` — {index['count']} patches, last change {index['generated_at']}.\n"
        "Machine-readable versions: [`index.json`](index.json), [`index.csv`](index.csv).\n\n"
        + "\n".join(rows) + "\n", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())

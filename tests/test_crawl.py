# Offline test of scripts/crawl.py against a mocked GitHub API (needs py7zr). Run: python tests/test_crawl.py
import importlib.util, io, json, shutil, sys, tempfile, zipfile, zlib
from pathlib import Path
import py7zr

SRC = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]
work = Path(tempfile.mkdtemp())
(work / "scripts").mkdir()
shutil.copy(SRC / "scripts/crawl.py", work / "scripts/crawl.py")
(work / "sources.json").write_text(json.dumps({"sources": [
    {"type": "repo", "repo": "mariovirgili/WonderSwanColor-AI_Translation", "default_language": "en"},
    {"type": "repo", "repo": "PokemonGoDB/wonderswan-english-patches", "default_language": "en"},
    {"type": "user", "user": "Gand46", "default_language": None}]}))


def num(n):
    out = bytearray()
    while True:
        x = n & 0x7F
        n >>= 7
        if n == 0:
            out.append(0x80 | x)
            return bytes(out)
        out.append(x)
        n -= 1


def bps(src_crc, dst_crc, src=4194304, dst=4194304, meta=b"", salt=0):
    body = b"BPS1" + num(src) + num(dst) + num(len(meta)) + meta + num(5 + salt)
    body += src_crc.to_bytes(4, "little") + dst_crc.to_bytes(4, "little")
    return body + (zlib.crc32(body) & 0xFFFFFFFF).to_bytes(4, "little")


def ips(salt=0):
    return (b"PATCH" + (0x100 + salt).to_bytes(3, "big") + (3).to_bytes(2, "big") + b"abc"
            + (0x200).to_bytes(3, "big") + b"\0\0" + (16).to_bytes(2, "big") + b"Z" + b"EOF")


def zipb(files):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for n, d in files.items():
            z.writestr(n, d)
    return b.getvalue()


def sevenz(files):
    b = io.BytesIO()
    with py7zr.SevenZipFile(b, "w") as z:
        for n, d in files.items():
            z.writestr(d, n)
    return b.getvalue()


P1 = bps(0xF8A1DD2B, 0xF3DB0917, meta=b"One Piece GB")
P1old = bps(0xF8A1DD2B, 0x11112222, salt=1)
P2, P2s = bps(0x14ADBD4B, 0x3E655169), bps(0x14ADBD4B, 0xBC3C951B, salt=2)
P3 = bps(0x11111111, 0x22222222)
PSX = bps(0x55555555, 0x66666666, src=238495152, dst=238495152)
IPS1, IPS2 = ips(), ips(1)
assets = {
    1: P1, 6: P1old,
    2: zipb({"Ring Infinity v1.1/ring-infinity.bps": P2, "Ring Infinity v1.1/ring-infinity-special.bps": P2s, "README.txt": b"x"}),
    3: zipb({"FF4 [T-En v0.9].bps": P3}), 4: sevenz({"Gioco (ITA)/patch_ITA.ips": IPS1}), 5: b"not a patch at all", 7: PSX,
}
ROMNOTE = """Apply to **Gioco Bello (Japan).wsc**

| | CRC32 | SHA-1 |
| --- | --- | --- |
| Original ROM | `ABCD1234` | da39a3ee5e6b4b0d3255bfef95601890afd80709 |
| Patched ROM | `9876FEDC` | 2fd4e1c67a2d28fced849ee1bb76e7391b93eb12 |

Built from commit 0123456789abcdef0123456789abcdef01234567 on 20261102.
"""
releases = {
    "mariovirgili/WonderSwanColor-AI_Translation": [
        {"id": 10, "tag_name": "FFIV", "name": "[Release] Final Fantasy IV (WonderSwan Color) - AI English translation patch",
         "html_url": "https://x/r/ff4", "published_at": "2026-10-01T00:00:00Z",
         "body": "Original ROM: Final Fantasy IV (Japan).wsc\nCRC32: 11111111\nSHA-1: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n",
         "assets": [{"id": 3, "name": "Final.Fantasy.IV.T-En.v0.9.zip", "size": len(assets[3]), "updated_at": "t1"},
                    {"id": 5, "name": "broken.bps", "size": 18, "updated_at": "t1"}]},
        {"id": 11, "tag_name": "PS1-DigiPocket2", "name": "[Release] Pocket Digimon World: Wind Battle Disc (PS1) – English translation patch v0.9",
         "html_url": "https://x/r/psx", "assets": [{"id": 7, "name": "pdw.bps", "size": len(PSX), "updated_at": "t1"}]}],
    "PokemonGoDB/wonderswan-english-patches": [
        {"id": 20, "tag_name": "grand-battle-v1.3", "name": "[Release] One Piece: Grand Battle Swan Colosseum (WonderSwan Color) - AI English translation patch v1.3",
         "html_url": "https://x/r/gb", "published_at": "2026-10-05T00:00:00Z", "body": "",
         "assets": [{"id": 1, "name": "grand-battle-v1.3.bps", "size": len(P1), "updated_at": "t1"}]},
        {"id": 22, "tag_name": "grand-battle-v1.2", "name": "[Release] One Piece: Grand Battle Swan Colosseum (WonderSwan Color) - AI English translation patch v1.2 (superseded by v1.3)",
         "html_url": "https://x/r/gb2", "published_at": "2026-09-05T00:00:00Z",
         "assets": [{"id": 6, "name": "grand-battle-v1.2.bps", "size": len(P1old), "updated_at": "t1"}]},
        {"id": 21, "tag_name": "ring-infinity-v1.1", "name": "[Release] Ring Infinity (WonderSwan) - AI English translation patch v1.1 (+ special edition)",
         "html_url": "https://x/r/ri", "published_at": "2026-10-06T00:00:00Z",
         "assets": [{"id": 2, "name": "ring-infinity-v1.1.zip", "size": len(assets[2]), "updated_at": "t1"}]}],
    "Gand46/traduzioni": [{"id": 30, "tag_name": "v1", "name": "Gioco in italiano", "html_url": "https://x/r/g", "body": ROMNOTE,
                           "assets": [{"id": 4, "name": "gioco_ITA.7z", "size": len(assets[4]), "updated_at": "t1"}]}],
}
blobs = {"b1": bps(0x33333333, 0x44444444), "b2": blobs_b2 if False else None, "v1": b"PATCH" + b"EOF",
         "rd": b"# Densha\n\nNeeds `Densha de Go! 2 (Japan).ws` - CRC32 AF9D8F42, MD5 0cc175b9c0f1b6a831c399e269772661\n"
               b"Result CRC32: 44444444\n",
         "ri": b"Required ROM: Hanafuda (Japan).ws\nCRC32: 0BADF00D\n"}
blobs["b2"] = blobs["b1"]  # duplicate copy in another folder
blobs["ip"] = IPS2
blobs["tw"] = ips(7)
blobs["inc"] = bps(0x44444444, 0x77777777, salt=3)
trees = {"Gand46/Densha-de-Go-2-Wonderswan-English-Translation-AI_GPT": [
    {"path": "patches/Densha_de_Go_2_EN_v0.4.20_CUMULATIVE.bps", "type": "blob", "sha": "b1", "size": 30},
    {"path": "patches/README.md", "type": "blob", "sha": "rd", "size": 100},
    {"path": "history/old/Densha_de_Go_2_EN_v0.4.20_CUMULATIVE.bps", "type": "blob", "sha": "b2", "size": 30},
    {"path": "components/vendor/someone_else.ips", "type": "blob", "sha": "v1", "size": 8},
    {"path": "hanafuda/Hanafuda_EN.ips", "type": "blob", "sha": "ip", "size": 20},
    {"path": "patches/Densha_de_Go_2_EN_v0.4.20_CUMULATIVE.ips", "type": "blob", "sha": "tw", "size": 20},
    {"path": "patches/Densha_de_Go_2_EN_v0.4.21_from_v0.4.20.bps", "type": "blob", "sha": "inc", "size": 30},
    {"path": "hanafuda/readme.txt", "type": "blob", "sha": "ri", "size": 40}]}
spec = importlib.util.spec_from_file_location("crawl", work / "scripts/crawl.py")
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)
downloads = []


def api(path):
    full = "/".join(path.split("/")[2:4])
    if "/git/trees/" in path:
        return {"sha": "c0ffee", "tree": trees.get(full, []), "truncated": False}
    return {"html_url": f"https://github.com/{full}", "default_branch": "main"}


def api_list(path):
    if path.startswith("/users/Gand46/repos"):
        return [{"full_name": "Gand46/traduzioni", "fork": False},
                {"full_name": "Gand46/Densha-de-Go-2-Wonderswan-English-Translation-AI_GPT", "fork": False},
                {"full_name": "Gand46/forked", "fork": True}]
    return releases.get("/".join(path.split("/")[2:4]), [])


def http_get(url, accept=None, auth=True):
    downloads.append(url)
    if "/releases/assets/" in url:
        return assets[int(url.rsplit("/", 1)[1])], {}
    if "/git/blobs/" in url:
        return blobs[url.rsplit("/", 1)[1]], {}
    raise AssertionError(url)


c.api, c.api_list, c.http_get = api, api_list, http_get


def run():
    assert c.main() == 0
    return json.loads((work / "index.json").read_text())


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("ok -", msg)


idx = run()
E = {e["variant"]: e for e in idx["entries"]}
for e in idx["entries"]:
    print(f"   {e['game']!s:38} {e['variant'][:34]:34} v={e['version']!s:7} {e['language']!s:4} {e['platform']!s:16} "
          f"latest={e['latest']!s:5} dup={bool(e['duplicate_of'])!s:5} src={e['source_rom']}")
check(all(e["patch_checksum_ok"] for e in idx["entries"] if e["format"] == "bps"), "BPS checksums verified")
check(E["grand-battle-v1.3"]["source_crc32"] == "F8A1DD2B" and E["grand-battle-v1.3"]["target_crc32"] == "F3DB0917", "BPS CRC32s read")
check(E["grand-battle-v1.3"]["latest"] and not E["grand-battle-v1.2"]["latest"] and E["grand-battle-v1.2"]["superseded"], "superseded/latest")
check(E["ring-infinity"]["latest"] and E["ring-infinity-special"]["latest"], "editions of the latest version are all latest")
check(E["grand-battle-v1.3"]["game"] == "One Piece: Grand Battle Swan Colosseum" and E["grand-battle-v1.3"]["platform"] == "wonderswan_color", "title/platform from release name")
check(E["FF4 [T-En v0.9]"]["version"] == "0.9" and E["FF4 [T-En v0.9]"]["source_rom"].get("sha1") == "a" * 40
      and E["FF4 [T-En v0.9]"]["source_rom"].get("name") == "Final Fantasy IV (Japan).wsc", "SHA-1 + ROM name from release notes")
ips_e = E["patch_ITA"]
check(ips_e["language"] == "it" and ips_e["source_crc32"] == "ABCD1234" and ips_e["source_rom"]["crc32_from"] == "notes"
      and ips_e["source_rom"]["sha1"] == "da39a3ee5e6b4b0d3255bfef95601890afd80709" and ips_e["target_crc32"] == "9876FEDC"
      and ips_e["source_rom"]["name"] == "Gioco Bello (Japan).wsc", "IPS: CRC32/SHA-1/name from a release-notes table, commit id ignored")
d = [e for e in idx["entries"] if e["file_name"] == "Densha_de_Go_2_EN_v0.4.20_CUMULATIVE.bps"]
check(len(d) == 2 and sum(1 for e in d if e["duplicate_of"]) == 1, "identical files in one repo flagged as duplicate")
dk = next(e for e in d if not e["duplicate_of"])
check(dk["game"] == "Densha de Go 2" and dk["language"] == "en" and dk["version"] == "0.4.20", "repo-file name/lang/version")
check(dk["source_crc32"] == "33333333" and dk["source_rom"]["notes_crc32_mismatch"] == "AF9D8F42"
      and "md5" not in dk["source_rom"], "README about another ROM revision: BPS wins, mismatch flagged, its hashes ignored")
h = E["Hanafuda_EN"]
check(h["source_crc32"] == "0BADF00D" and h["source_rom"]["name"] == "Hanafuda (Japan).ws", "IPS: CRC32 + name from README next to the file")
check(not any("someone_else" in e["file_name"] for e in idx["entries"] + idx["excluded"]), "vendor/ folders skipped")
check([x["file_name"] for x in idx["excluded"]] == ["pdw.bps"] and idx["excluded"][0]["path"] is None, "PS1 patch excluded, not stored")
check(not (work / "patches/mariovirgili/WonderSwanColor-AI_Translation/PS1-DigiPocket2").exists(), "excluded file removed from disk")
tw = E["Densha_de_Go_2_EN_v0.4.20_CUMULATIVE"] if E["Densha_de_Go_2_EN_v0.4.20_CUMULATIVE"]["format"] == "ips" else \
    next(e for e in idx["entries"] if e["file_name"].endswith("CUMULATIVE.ips"))
check(tw["source_crc32"] == "33333333" and tw["source_rom"]["crc32_from"] == "bps_twin" and tw["target_crc32"] == "44444444",
      "IPS inherits CRC32s from its BPS twin")
inc = E["Densha_de_Go_2_EN_v0.4.21_from_v0.4.20"]
check(inc["requires_patched_rom"] and inc["requires_patch_ids"] and not dk["requires_patched_rom"], "incremental patch detected")
check("md5" not in inc["source_rom"] and "name" not in inc["source_rom"], "hashes on a line about another CRC32 are not attributed")
check(tw["source_rom"].get("notes_crc32_mismatch") == "AF9D8F42" and "md5" not in tw["source_rom"], "twin BPS beats disagreeing notes")
check(c.parse_release_name("[Release] X - Card of Fate (WonderSwan Color) - English translation patch v0.9")[:2]
      == ("X - Card of Fate", "wonderswan_color"), "title with ' - ' inside")
check(c.guess_language(["lode-runner-se-ja-1.0.bps", None, "x.zip", "[Release] Lode Runner - AI English translation"], "en") == "ja"
      and c.guess_language(["densha-de-go-en-1.0.bps"], None) == "en", "edition language from hyphenated stems")
n1 = len(downloads)

downloads.clear()
idx2 = run()
check(json.dumps(idx2["entries"]) == json.dumps(idx["entries"]) and idx2["generated_at"] == idx["generated_at"], "second run: identical index")
check(all("/releases/assets/" not in u and not u.endswith(("/b1", "/b2", "/ip")) for u in downloads), f"second run: no patch re-downloaded ({len(downloads)} README reads)")

assets[2] = zipb({"Ring Infinity v1.1/ring-infinity.bps": P2})
releases["PokemonGoDB/wonderswan-english-patches"][2]["assets"][0].update(updated_at="t2", size=len(assets[2]))
orig = api_list
c.api_list = lambda path: (_ for _ in ()).throw(OSError("simulated outage")) if "Gand46/traduzioni/releases" in path else orig(path)
idx3 = run()
check(not any(e["variant"] == "ring-infinity-special" for e in idx3["entries"]), "patch removed upstream is removed")
check(any(e["variant"] == "patch_ITA" and e["source_crc32"] == "ABCD1234" for e in idx3["entries"]), "failed source keeps its data and metadata")
print("all tests passed")

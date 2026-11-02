# WonderSwan_bps_repo

Mirror of WonderSwan / WonderSwan Color translation patches (`.bps` and `.ips`, English and other languages),
collected once a day from:

- [mariovirgili/WonderSwanColor-AI_Translation](https://github.com/mariovirgili/WonderSwanColor-AI_Translation)
- [PokemonGoDB/wonderswan-english-patches](https://github.com/PokemonGoDB/wonderswan-english-patches)
- every public repository of [Gand46](https://github.com/Gand46)

The list lives in [`sources.json`](sources.json). The catalog is in [`PATCHES.md`](PATCHES.md); the data for programs
(for example the WonderSwan web emulator) is in [`index.json`](index.json) and [`index.csv`](index.csv).
**No ROMs are stored here**, only patches.

## How it works

[`scripts/crawl.py`](scripts/crawl.py) runs every day at 04:17 UTC through
[GitHub Actions](.github/workflows/crawl.yml) (and on demand from the Actions tab). For each source repository it:

1. reads every published release and downloads the `.bps` / `.ips` assets, opening `.zip` and `.7z` archives to find
   the patches inside them;
2. scans the files committed on the default branch for the same file types;
3. stores the patches under `patches/<owner>/<repo>/<release tag>/` (or `_files/<path>` for committed files);
4. writes the catalog and commits only if something changed.

Already-processed assets and files are not downloaded again. A patch removed or replaced upstream is removed here too,
but only after its source was read successfully: an API error never deletes anything.

## `index.json`

```jsonc
{
  "schema": 1,
  "count": 42,
  "entries": [{
    "id": "3f2a…",                // first 16 hex digits of the sha256
    "game": "Ring Infinity",      // from the release name (best effort)
    "variant": "ring-infinity-special",
    "version": "1.1",             // from release name / tag / file name
    "language": "en",             // ISO 639-1, best effort; null if unknown
    "format": "bps",              // or "ips"
    "source_crc32": "14ADBD4B",   // CRC32 of the original ROM (BPS only)
    "target_crc32": "BC3C951B",   // CRC32 of the patched ROM (BPS only)
    "source_size": 4194304, "target_size": 4194304,
    "patch_crc32": "…", "sha256": "…", "size": 1234,
    "file_name": "ring-infinity-special.bps",
    "path": "patches/PokemonGoDB/…/ring-infinity-special.bps",
    "url": "https://raw.githubusercontent.com/mariovirgili/WonderSwan_bps_repo/main/patches/…",
    "origin": {
      "source": "PokemonGoDB/wonderswan-english-patches",
      "kind": "release_asset",    // or "repo_file"
      "release_tag": "ring-infinity-v1.1", "release_name": "…", "release_url": "…",
      "asset_name": "ring-infinity-v1.1.zip", "archive_member": "…/ring-infinity-special.bps"
    },
    "first_seen": "2026-11-02"
  }]
}
```

An emulator can compute the CRC32 of the ROM the user loads and offer every entry whose `source_crc32` matches, then
download the patch from `url` (raw.githubusercontent.com allows cross-origin requests).

IPS patches contain no checksums, so `source_crc32` / `target_crc32` are `null` for them. They can be filled in by hand
in [`overrides.json`](overrides.json), keyed by the patch `sha256`; overrides survive every crawl.

## Credits

Every patch belongs to its author and is mirrored with a link to where it was published (`origin` in the index,
"Source" in the catalog). Please follow each author's own terms — for example, the authors ask that their AI
translation patches **not be uploaded to romhacking sites**. Get the original Japanese ROMs from your own cartridges.

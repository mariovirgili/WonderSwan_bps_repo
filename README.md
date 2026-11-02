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
  "schema": 2,
  "count": 180,
  "entries": [{
    "id": "3f2a…",                 // first 16 hex digits of the sha256
    "game": "Ring Infinity",       // from the release name, the ROM name in the notes, or the repo/file name
    "variant": "ring-infinity-special",
    "version": "1.1",              // from asset / file / release name
    "language": "en",              // ISO 639-1, best effort; null if unknown
    "platform": "wonderswan",      // "wonderswan", "wonderswan_color" or null (unknown)
    "latest": true,                // newest non-superseded version of this game from this source
    "superseded": false,           // the release says "(superseded by …)"
    "duplicate_of": null,          // id of an identical file kept elsewhere in the same source
    "format": "bps",               // or "ips"
    "source_crc32": "14ADBD4B",    // ROM the patch needs (shortcut for source_rom.crc32)
    "target_crc32": "BC3C951B",    // ROM it produces
    "source_rom": {                // everything known about the required ROM
      "crc32": "14ADBD4B", "crc32_from": "bps",   // "bps" (in the patch), "notes" (release notes / README)
      "size": 4194304,
      "sha1": "…", "md5": "…", "sha256": "…",     // only when the notes give them
      "name": "Ring Infinity (Japan).ws",          // No-Intro style name, when the notes give it
      "notes_crc32_mismatch": "…"                  // notes mention a different CRC32: their hashes were ignored
    },
    "target_rom": { "crc32": "BC3C951B", "crc32_from": "bps", "size": 4194304 },
    "file_name": "ring-infinity-special.bps",
    "path": "patches/PokemonGoDB/…/ring-infinity-special.bps",
    "url": "https://raw.githubusercontent.com/mariovirgili/WonderSwan_bps_repo/main/patches/…",
    "size": 1234, "sha256": "…", "patch_crc32": "…",
    "origin": {
      "source": "PokemonGoDB/wonderswan-english-patches",
      "kind": "release_asset",     // or "repo_file"
      "release_tag": "ring-infinity-v1.1", "release_name": "…", "release_url": "…",
      "asset_name": "ring-infinity-v1.1.zip", "archive_member": "…/ring-infinity-special.bps"
    },
    "first_seen": "2026-11-02"
  }],
  "excluded": [ /* patches for other systems (Saturn, PS1, DS…): listed, not stored */ ]
}
```

**Where the ROM checksums come from.** A BPS patch contains the CRC32 and size of the ROM it needs and of the ROM it
produces, so those are exact. IPS patches contain no checksums, so for them the crawler reads the release notes (and,
for patches committed to a repository, the README files in the same folder and its parents) and picks up CRC32, MD5,
SHA-1 and SHA-256 values labelled as belonging to the original or the patched ROM, plus a No-Intro style name such as
`Game (Japan).wsc`. Unlabelled hex strings (commit ids, dates) are ignored. When a patch is a BPS, the notes only add
the stronger hashes and the name, and only if their CRC32 agrees with the one in the patch.

An emulator can compute the CRC32 of the ROM the user loads, offer the entries whose `source_crc32` matches (preferring
`latest` and skipping `duplicate_of`), then download the patch from `url` (raw.githubusercontent.com allows
cross-origin requests).

Anything can be corrected by hand in [`overrides.json`](overrides.json), keyed by the patch `sha256`; overrides
survive every crawl.

## Credits

Every patch belongs to its author and is mirrored with a link to where it was published (`origin` in the index,
"Source" in the catalog). Please follow each author's own terms — for example, the authors ask that their AI
translation patches **not be uploaded to romhacking sites**. Get the original Japanese ROMs from your own cartridges.

# vendor/airplay/ provenance

This directory ships prebuilt AirPlay sender binaries used by `app/output/airplay.py`. They are pulled verbatim from [Music Assistant](https://github.com/music-assistant/server) and committed directly so Jukeplox builds reproducibly without depending on Music Assistant's release cadence remaining available.

## Binaries

| File | Architecture | Protocol | Size (bytes) | SHA-256 |
|------|--------------|----------|--------------|---------|
| `cliap2-linux-x86_64` | linux/amd64 | AirPlay 2 | 514840 | `243734f79420ccba9c514f5a2f8b4e1957b93097b79d23c766b289c47e127393` |
| `cliap2-linux-aarch64` | linux/arm64 | AirPlay 2 | 555496 | `db419ab8d7e90d871c785b802ed905be275ffd5fcbbb90eef942ae1dabb7b497` |
| `cliraop-linux-x86_64` | linux/amd64 | AirPlay 1 (RAOP) | 6245600 | `b07bf67a92fc4a077db61c83e30e37570e36a9b230be0afc94d9ddb43ceede32` |
| `cliraop-linux-aarch64` | linux/arm64 | AirPlay 1 (RAOP) | 5916080 | `d2f26fd32297833f75d57c7fdab2e4bd4d388e84297677227a8f41ec1a7c856c` |

ELF magic verified (`7F 45 4C 46`); e_machine field matches the filename suffix.

## Source

- **Upstream repo:** [`music-assistant/server`](https://github.com/music-assistant/server)
- **Path within upstream:** `music_assistant/providers/airplay/bin/`
- **Source commit at fetch time:** `d3073756d34e6ac72c0fc5027dc253c0de98346e` (dev branch, 2026-06-06)
- **Fetched via:** `https://raw.githubusercontent.com/music-assistant/server/dev/music_assistant/providers/airplay/bin/<filename>`

Music Assistant builds these binaries from two C codebases derived from OwnTone:

- `cliap2` — [music-assistant/cliairplay](https://github.com/music-assistant/cliairplay) (AirPlay 2 sender)
- `cliraop` — [music-assistant/libraop](https://github.com/music-assistant/libraop) (AirPlay 1 / RAOP sender, fork of philippe44/libraop)

## License

- **cliap2** — `music-assistant/cliairplay` declares **GPL-2.0** (confirmed upstream).
- **cliraop** — neither `music-assistant/libraop` nor its parent `philippe44/libraop`
  ships an explicit LICENSE file; it is treated as **GPL-2.0 by OwnTone heritage**.
  This is an inheritance assumption, not an upstream-declared license — upstream
  clarification should be requested. No non-commercial / personal-use restriction
  exists anywhere in this lineage (philippe44's *AirConnect* is MIT, but that is a
  separate project, not the cliraop lineage).

Jukeplox interacts with both only via subprocess invocation (no library linkage),
which is "mere aggregation" per the FSF GPL FAQ and does not place Jukeplox's own
code under additional obligations. Jukeplox itself is GPL-2.0-or-later; its top-level
`LICENSE` contains the GPL-2.0 text required to accompany these binaries. See
`THIRD_PARTY_LICENSES` for the full attribution with the pinned source commit.

## Rebuilding

If these binaries ever need to be rebuilt locally:

1. Clone the upstream source repos (`cliairplay` for cliap2, `libraop` for cliraop).
2. Follow the CI workflow in each repo (Music Assistant uses GitHub Actions; the workflow file shows the toolchain and target triples).
3. Replace the file in this directory, update the SHA-256 in this table, and update the source commit reference.

## Updating

To pick up a newer Music Assistant build:

1. Confirm the upstream files at `https://github.com/music-assistant/server/tree/dev/music_assistant/providers/airplay/bin` still match the filenames in the table above.
2. Re-fetch each file, recompute SHA-256, update this document.
3. Test on the actual target speakers (JBL Charge 5 Wi-Fi SE and WiiM Pro are the documented working set; HomePod is known not to work and not a target).

# CLAUDE.md

Bash+Python media automation for Bazarr/Sonarr/Radarr. Architecture details in engram.

## Validation

```bash
bash -n automation/scripts/transcode/library_codec_manager.sh
bash -n automation/scripts/subtitles/sonarr_profile_extract_on_import.sh
PYTHONPATH=automation/scripts python3 -m pytest automation/scripts/streaming/tests/ -x -q
```

## Layout

- `automation/scripts/` — canonical source
- `scripts/` — compat copies for Sonarr/Radarr hooks (sync after edits, `bash -n`)

## Rules

- DRY: use shared libs and helpers, never hardcode repeated values
- Discord: rich embeds only, no plain-text (details in engram `conventions/discord-and-coding`)
- Commits: conventional (`feat(subtitles):`, `fix(transcode):`), no AI attribution

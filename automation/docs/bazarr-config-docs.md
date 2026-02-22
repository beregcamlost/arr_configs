# Bazarr Configuration Documentation

Source config read:
- `/opt/bazarr/data/config/config.yaml`

## Instance
- Name: `Bazarr`
- IP/Port: `127.0.0.1:6767`
- Base URL: `/bazarr`
- Branch: `master`
- Auto update: `true`
- Theme: `auto`

## Integrations
- Sonarr enabled: `true` (`127.0.0.1:8989`, base URL `/sonarr`)
- Radarr enabled: `true` (`127.0.0.1:7878`, base URL `/radarr`)
- Plex enabled: `false`

## Subtitle Providers (enabled)
- `embeddedsubtitles`
- `subdivx`
- `subf2m`
- `opensubtitlescom`

## Search and Processing
- Adaptive searching: `true` (delay `3w`, delta `1w`)
- Wanted search frequency (series/movies): `6` hours
- Minimum score (series/movies): `65 / 65`
- Upgrade subtitles: `true`
- Upgrade frequency: `12`
- Use post-processing: `false`
- Use embedded subtitles: `false`

## Language/Profile Defaults
- Series default profile: `1` (enabled)
- Movie default profile: `1` (enabled)
- Single language mode: `false`
- Subtitle subfolder mode: `current`

## Backup
- Frequency: `Weekly`
- Day: `6`
- Hour: `3`
- Retention: `31`
- Folder: `/opt/bazarr/data/backup`

## Security and Secrets
Sensitive values are present in config and were intentionally redacted in this documentation:
- Bazarr API/auth keys
- Sonarr/Radarr API keys
- Subtitle provider credentials
- Anti-captcha key
- Flask secret key

## Notes
- Config includes active OpenSubtitles/OpenSubtitles.com credentials.
- Auth type is `form` with local username configured.
- File ownership under `/opt/bazarr/data` appears to be user `abc`.

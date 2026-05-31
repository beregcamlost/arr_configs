# Bazarr Automation Workspace

This folder centralizes automation scripts, logs, and docs for subtitle/profile workflows and codec audit/transcode tasks.

## Structure
- `scripts/subtitles/` subtitle/profile extraction + Sonarr/Radarr hooks
- `scripts/transcode/` library codec manager
- `scripts/streaming/` streaming availability checker
- `scripts/translation/` Ollama (local LLM) EN->ES subtitle translator + embedded-Spanish extraction
- `logs/` automation logs
- `docs/` persistent context and operational notes

## Important
Compatibility script paths are kept in `/config/berenstuff/scripts/` for backward compatibility.
Canonical script source remains in `/config/berenstuff/automation/scripts/`.

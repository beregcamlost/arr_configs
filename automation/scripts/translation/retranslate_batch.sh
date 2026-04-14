#!/usr/bin/env bash
# Backward-compat wrapper — delegates to retranslate.sh --mode=batch
exec "${BASH_SOURCE[0]%/*}/retranslate.sh" --mode=batch "$@"

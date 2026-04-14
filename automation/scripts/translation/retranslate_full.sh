#!/usr/bin/env bash
# Backward-compat wrapper — delegates to retranslate.sh --mode=full
exec "${BASH_SOURCE[0]%/*}/retranslate.sh" --mode=full "$@"

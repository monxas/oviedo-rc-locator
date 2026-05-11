#!/usr/bin/env bash
# Mantenimiento del caché de oviedo_rc.
# Uso (en cron, una vez por mes):
#   0 3 1 * *  /Users/ramonkamibayashicarrera/oviedo_rc_locator/update.sh
#
# Modos:
#   ./update.sh            update incremental (sheets + planes nuevos/cambiados)
#   ./update.sh full       descarga TODO desde cero
#   ./update.sh verify     verifica integridad y reintenta los corruptos
#   ./update.sh refresh    re-descarga planes > 90 días

set -e
cd "$(dirname "$0")/.."
PYTHONPATH="$(pwd)/src:$(pwd)/scripts:${PYTHONPATH:-}" export PYTHONPATH
PREFETCH="scripts/prefetch.py"

LOG="${HOME}/.cache/oviedo_rc/update.log"
mkdir -p "$(dirname "$LOG")"

log() { echo "[$(date +%FT%T)] $*" | tee -a "$LOG"; }

case "${1:-update}" in
  update)
    log "Update incremental"
    # Actualizar listado de hojas (siempre)
    rm -f ~/.cache/oviedo_rc/sheets.json
    python3 "$PREFETCH" plans --workers 6 2>&1 | tee -a "$LOG"
    python3 "$PREFETCH" verify 2>&1 | tee -a "$LOG"
    python3 "$PREFETCH" status 2>&1 | tee -a "$LOG"
    ;;

  full)
    log "Full prefetch"
    python3 "$PREFETCH" all --workers 4 --fix 2>&1 | tee -a "$LOG"
    ;;

  verify)
    log "Verify"
    python3 "$PREFETCH" verify --fix 2>&1 | tee -a "$LOG"
    ;;

  refresh)
    log "Refresh planes > 90 días"
    python3 "$PREFETCH" refresh plans --older-than-days 90 --workers 6 2>&1 | tee -a "$LOG"
    ;;

  gc)
    log "GC"
    python3 "$PREFETCH" gc 2>&1 | tee -a "$LOG"
    ;;

  status)
    python3 "$PREFETCH" status
    ;;

  *)
    echo "Uso: $0 {update|full|verify|refresh|gc|status}"
    exit 1
    ;;
esac
log "OK"

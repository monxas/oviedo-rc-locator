#!/usr/bin/env bash
# Reanudar prefetch DNPRC distribuido entre las 3 Raspberry Pis.
# Comprueba el ban en cada Pi, divide las refs pendientes entre las disponibles,
# lanza workers, recoge resultados al cache local.
#
# Uso:
#   ./distributed_dnprc.sh           # check + lanzar
#   ./distributed_dnprc.sh stop      # parar todo y recoger
#   ./distributed_dnprc.sh status    # ver progreso

set -e
cd "$(dirname "$0")"

CACHE=~/.cache/oviedo_rc/parcels
PIS=(raspberrypi1-felguera raspberrypi2-gijon raspberrypi3-gijon-satelite)
USE_SNI=(0 0 1)  # Pi3 necesita SNI (DNS roto)

ssh_pi() { ssh -o BatchMode=yes -o ConnectTimeout=8 "chamoun@$1" "$2"; }

cmd_check_ban() {
    for i in 0 1 2; do
        H=${PIS[$i]}
        printf "%-32s " "$H"
        if [ "${USE_SNI[$i]}" = "1" ]; then
            R=$(ssh_pi "$H" "curl -sS -o /dev/null -w '%{http_code}' --max-time 8 --resolve 'ovc.catastro.meh.es:443:195.66.151.66' 'https://ovc.catastro.meh.es/OVCServWeb/OVCWcfCallejero/COVCCallejero.svc/json/Consulta_DNPRC?RefCat=8653019TP6085S'" 2>/dev/null)
        else
            R=$(ssh_pi "$H" "curl -sS -o /dev/null -w '%{http_code}' --max-time 8 'https://ovc.catastro.meh.es/OVCServWeb/OVCWcfCallejero/COVCCallejero.svc/json/Consulta_DNPRC?RefCat=8653019TP6085S'" 2>/dev/null)
        fi
        echo "HTTP $R"
    done
}

cmd_collect() {
    for i in 0 1 2; do
        H=${PIS[$i]}
        echo "Recogiendo de $H..."
        rsync -aq --remove-source-files \
            "chamoun@$H:/tmp/dnprc_out/" "$CACHE/" 2>/dev/null || true
        ssh_pi "$H" "rm -rf /tmp/dnprc_out 2>/dev/null" 2>/dev/null || true
    done
    echo "Cache local: $(find $CACHE -name 'dnprc_*.json' | wc -l) archivos"
}

cmd_status() {
    echo "Cache local:  $(find $CACHE -name 'dnprc_*.json' | wc -l) DNPRC"
    for H in "${PIS[@]}"; do
        N=$(ssh_pi "$H" "ls /tmp/dnprc_out 2>/dev/null | wc -l" 2>/dev/null || echo "?")
        P=$(ssh_pi "$H" "pgrep -f 'dnprc_worker' | head -1" 2>/dev/null || echo "")
        printf "  %-32s pendientes=%s  pid=%s\n" "$H" "$N" "${P:-stopped}"
    done
}

cmd_stop() {
    for H in "${PIS[@]}"; do
        ssh_pi "$H" "pkill -f dnprc_worker 2>/dev/null; true" 2>/dev/null || true
    done
    echo "Workers parados. Recogiendo..."
    cmd_collect
}

cmd_start() {
    # 1. Ver qué Pis están NO baneadas
    echo "=== Comprobando bans ==="
    declare -a OK_IDX
    for i in 0 1 2; do
        H=${PIS[$i]}
        if [ "${USE_SNI[$i]}" = "1" ]; then
            R=$(ssh_pi "$H" "curl -sS -o /dev/null -w '%{http_code}' --max-time 8 --resolve 'ovc.catastro.meh.es:443:195.66.151.66' 'https://ovc.catastro.meh.es/OVCServWeb/OVCWcfCallejero/COVCCallejero.svc/json/Consulta_DNPRC?RefCat=8653019TP6085S'" 2>/dev/null)
        else
            R=$(ssh_pi "$H" "curl -sS -o /dev/null -w '%{http_code}' --max-time 8 'https://ovc.catastro.meh.es/OVCServWeb/OVCWcfCallejero/COVCCallejero.svc/json/Consulta_DNPRC?RefCat=8653019TP6085S'" 2>/dev/null)
        fi
        echo "  $H: HTTP $R"
        [ "$R" = "200" ] && OK_IDX+=("$i")
    done
    if [ ${#OK_IDX[@]} -eq 0 ]; then
        echo "Ninguna Pi disponible. Esperar más."
        exit 1
    fi

    # 2. Generar lista de refs pendientes
    OK_IDX_CSV=$(IFS=,; echo "${OK_IDX[*]}")
    python3 - <<EOF
import json
from pathlib import Path
cache = Path("$CACHE")
all_refcats = set()
for jf in cache.glob("bbox_*.json"):
    try:
        for p in json.loads(jf.read_text()): all_refcats.add(p["refcat"])
    except: pass
existing = {f.stem.replace("dnprc_","") for f in cache.glob("dnprc_*.json")}
todo = sorted(all_refcats - existing)
print(f"Pendientes: {len(todo)}")
n = len(todo)
ok_idx = [int(x) for x in "$OK_IDX_CSV".split(",") if x]
parts = len(ok_idx)
chunk = n // parts
for i, idx in enumerate(ok_idx):
    start = i * chunk
    end = start + chunk if i < parts-1 else n
    Path(f"/tmp/refs_pi{idx}.txt").write_text("\n".join(todo[start:end]))
    print(f"Pi{idx} → {end-start}")
EOF

    # 3. Lanzar workers
    for i in "${OK_IDX[@]}"; do
        H=${PIS[$i]}
        scp -o BatchMode=yes /tmp/refs_pi${i}.txt "chamoun@$H:/tmp/refs_pi.txt" >/dev/null
        if [ "${USE_SNI[$i]}" = "1" ]; then
            scp -o BatchMode=yes /tmp/dnprc_worker_pi3.py "chamoun@$H:/tmp/dnprc_worker.py" >/dev/null 2>&1 || \
              scp -o BatchMode=yes "$(dirname "$0")/../scripts/distributed/dnprc_worker_sni.py" "chamoun@$H:/tmp/dnprc_worker.py" >/dev/null
        else
            scp -o BatchMode=yes "$(dirname "$0")/../scripts/distributed/dnprc_worker.py" "chamoun@$H:/tmp/dnprc_worker.py" >/dev/null
        fi
        ssh_pi "$H" "mkdir -p /tmp/dnprc_out"
        ssh -o BatchMode=yes -f "chamoun@$H" \
            "WORKERS=6 nohup python3 /tmp/dnprc_worker.py /tmp/refs_pi.txt > /tmp/worker.log 2>&1 < /dev/null &"
        echo "Lanzado $H"
    done

    sleep 5
    cmd_status
}

case "${1:-start}" in
    start) cmd_start ;;
    stop) cmd_stop ;;
    status) cmd_status ;;
    check) cmd_check_ban ;;
    collect) cmd_collect ;;
    *) echo "Uso: $0 {start|stop|status|check|collect}"; exit 1 ;;
esac

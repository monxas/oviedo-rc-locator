#!/usr/bin/env bash
# Test del modo offline: prueba la CLI del paquete oviedo_rc.
set -e
cd "$(dirname "$0")/.."
PYTHONPATH="$(pwd)/src:$(pwd)/scripts:${PYTHONPATH:-}" export PYTHONPATH

echo "=== Estado del caché ==="
python3 scripts/prefetch.py status

echo ""
echo "=== Test 1: locate-only para 4 RCs calibradas ==="
for rc in 8653019TP6085S0002QW 9254317TP6095S0001ZT 9651017TP6095S0001IT 8953305TP6085S0002TW; do
  echo -n "  $rc: "
  python3 -m oviedo_rc "$rc" --locate-only 2>/dev/null | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    print(f'{d[\"sheet_name\"]} ({d[\"cell\"]} {d[\"sub_quadrant\"]})')
except Exception:
    print('ERROR')
"
done

echo ""
echo "=== Test 2: bundle completo (cache-first, fallback online) ==="
python3 -m oviedo_rc 9651017TP6095S0001IT --no-wms --json 2>/dev/null | python3 -c "
import json, sys
d = json.load(sys.stdin)
print(f'OK: {d[\"rc\"]} → {d[\"sheet_name\"]}')
print(f'   dir: {d[\"bundle_dir\"]}')
"

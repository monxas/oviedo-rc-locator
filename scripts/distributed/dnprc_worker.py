#!/usr/bin/env python3
"""Worker remoto: descarga DNPRC de cada refcat de la lista a /tmp/dnprc_out/."""
import sys, json, urllib.request, urllib.parse, os, time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

OUT = Path("/tmp/dnprc_out")
OUT.mkdir(exist_ok=True)
WORKERS = int(os.environ.get("WORKERS", "4"))
HEADERS = {"User-Agent": "Mozilla/5.0 (rc-prefetch)"}


def fetch_one(refcat):
    out = OUT / f"dnprc_{refcat}.json"
    if out.exists() and out.stat().st_size > 50:
        return refcat, "skip"
    url = (f"https://ovc.catastro.meh.es/OVCServWeb/OVCWcfCallejero/COVCCallejero.svc/json/"
           f"Consulta_DNPRC?RefCat={refcat}")
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        d = json.loads(urllib.request.urlopen(req, timeout=15).read())
    except Exception as e:
        return refcat, f"err:{e}"
    res = d.get("consulta_dnprcResult", {})

    def parse_record(r):
        rc = r.get("rc", r.get("idbi", {}).get("rc", {}))
        full = rc.get("pc1","") + rc.get("pc2","") + rc.get("car","") + rc.get("cc1","") + rc.get("cc2","")
        dir_d = r.get("dt", {}).get("locs", {}).get("lous", {}).get("lourb", {}).get("dir", {})
        loint = r.get("dt", {}).get("locs", {}).get("lous", {}).get("lourb", {}).get("loint", {})
        debi = r.get("debi", {})
        def num(s):
            if s is None: return 0
            try: return int(round(float(str(s).replace(",", "."))))
            except: return 0
        VIA = {"CL":"C/", "AV":"Av.", "PZ":"Pza.", "PJ":"Psje.", "GL":"Glta.", "RD":"Rda.", "TR":"Trav."}
        return {
            "rc": full,
            "address": f"{VIA.get(dir_d.get('tv',''),dir_d.get('tv',''))} {dir_d.get('nv','').strip()} {dir_d.get('pnp','').strip()}".strip(),
            "floor": loint.get("pt", ""), "door": loint.get("pu", ""), "stair": loint.get("es", ""),
            "use": debi.get("luso", ""),
            "area_m2": num(debi.get("sfc")),
            "year": int(debi.get("ant","0")) if str(debi.get("ant","")).isdigit() else None,
        }

    units = []
    recs = res.get("lrcdnp", {}).get("rcdnp", [])
    if recs:
        if isinstance(recs, dict): recs = [recs]
        units.extend(parse_record(r) for r in recs)
    bico = res.get("bico", {})
    if bico and not units:
        bi = bico.get("bi", {})
        if bi:
            units.append(parse_record({**bi, "rc": bi.get("idbi", {}).get("rc", {})}))

    out.write_text(json.dumps({"refcat14": refcat, "units": units}, ensure_ascii=False))
    return refcat, "ok"


def main():
    list_file = sys.argv[1]
    refs = [l.strip() for l in open(list_file) if l.strip()]
    print(f"[{time.strftime('%H:%M:%S')}] {len(refs)} refs, {WORKERS} workers")
    done = ok = err = skip = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for fut in as_completed([ex.submit(fetch_one, r) for r in refs]):
            r, st = fut.result()
            done += 1
            if st == "ok": ok += 1
            elif st == "skip": skip += 1
            else: err += 1
            if done % 200 == 0 or done == len(refs):
                rate = done / (time.time()-t0)
                eta = (len(refs)-done) / max(0.1, rate)
                print(f"[{time.strftime('%H:%M:%S')}] {done}/{len(refs)}  ok={ok} skip={skip} err={err}  {rate:.1f}/s  ETA {eta/60:.0f}m")
    print(f"DONE in {(time.time()-t0)/60:.1f}m")


if __name__ == "__main__":
    main()

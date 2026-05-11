#!/usr/bin/env python3
"""Worker Pi3: usa IP fija de catastro con SNI correcto (DNS roto en Pi3)."""
import sys, json, os, socket, ssl, time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

OUT = Path("/tmp/dnprc_out"); OUT.mkdir(exist_ok=True)
WORKERS = int(os.environ.get("WORKERS", "4"))
CATASTRO_IP = "195.66.151.66"
CATASTRO_HOST = "ovc.catastro.meh.es"

def fetch(refcat):
    out = OUT / f"dnprc_{refcat}.json"
    if out.exists() and out.stat().st_size > 50: return refcat, "skip"
    try:
        sock = socket.create_connection((CATASTRO_IP, 443), timeout=15)
        ctx = ssl.create_default_context()
        ssock = ctx.wrap_socket(sock, server_hostname=CATASTRO_HOST)
        path = f"/OVCServWeb/OVCWcfCallejero/COVCCallejero.svc/json/Consulta_DNPRC?RefCat={refcat}"
        req = (f"GET {path} HTTP/1.1\r\n"
               f"Host: {CATASTRO_HOST}\r\n"
               f"User-Agent: Mozilla/5.0\r\n"
               f"Connection: close\r\n\r\n").encode()
        ssock.sendall(req)
        chunks = []
        while True:
            c = ssock.recv(8192)
            if not c: break
            chunks.append(c)
        ssock.close()
        raw = b"".join(chunks)
        head, _, body = raw.partition(b"\r\n\r\n")
        if b"200 OK" not in head.split(b"\r\n",1)[0]:
            return refcat, "non200"
        # Body podría ser chunked
        if b"Transfer-Encoding: chunked" in head:
            # Decode chunked
            decoded = b""
            i = 0
            while i < len(body):
                end = body.find(b"\r\n", i)
                if end == -1: break
                size = int(body[i:end].split(b";")[0], 16)
                if size == 0: break
                start = end + 2
                decoded += body[start:start+size]
                i = start + size + 2
            body = decoded
        d = json.loads(body)
    except Exception as e:
        return refcat, f"err:{e}"
    res = d.get("consulta_dnprcResult", {})
    def parse(r):
        rc = r.get("rc", r.get("idbi",{}).get("rc",{}))
        full = rc.get("pc1","")+rc.get("pc2","")+rc.get("car","")+rc.get("cc1","")+rc.get("cc2","")
        dir_d = r.get("dt",{}).get("locs",{}).get("lous",{}).get("lourb",{}).get("dir",{})
        loint = r.get("dt",{}).get("locs",{}).get("lous",{}).get("lourb",{}).get("loint",{})
        debi = r.get("debi",{})
        def num(s):
            try: return int(round(float(str(s or 0).replace(",", "."))))
            except: return 0
        VIA = {"CL":"C/","AV":"Av.","PZ":"Pza.","PJ":"Psje.","GL":"Glta.","RD":"Rda.","TR":"Trav."}
        return {"rc":full,"address":f"{VIA.get(dir_d.get('tv',''),dir_d.get('tv',''))} {dir_d.get('nv','').strip()} {dir_d.get('pnp','').strip()}".strip(),
                "floor":loint.get("pt",""),"door":loint.get("pu",""),"stair":loint.get("es",""),
                "use":debi.get("luso",""),"area_m2":num(debi.get("sfc")),
                "year":int(debi.get("ant","0")) if str(debi.get("ant","")).isdigit() else None}
    units = []
    recs = res.get("lrcdnp",{}).get("rcdnp",[])
    if recs:
        if isinstance(recs, dict): recs=[recs]
        units.extend(parse(r) for r in recs)
    bico = res.get("bico",{})
    if bico and not units:
        bi = bico.get("bi",{})
        if bi: units.append(parse({**bi,"rc":bi.get("idbi",{}).get("rc",{})}))
    out.write_text(json.dumps({"refcat14":refcat,"units":units},ensure_ascii=False))
    return refcat, "ok"

def main():
    refs = [l.strip() for l in open(sys.argv[1]) if l.strip()]
    print(f"[{time.strftime('%H:%M:%S')}] {len(refs)} refs · {WORKERS} workers · IP fija + SNI", flush=True)
    done=ok=err=skip=0; t0=time.time()
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for fut in as_completed([ex.submit(fetch,r) for r in refs]):
            r,st = fut.result(); done+=1
            if st=="ok": ok+=1
            elif st=="skip": skip+=1
            else: err+=1
            if done%200==0 or done==len(refs):
                rate = done/(time.time()-t0); eta=(len(refs)-done)/max(0.1,rate)
                print(f"[{time.strftime('%H:%M:%S')}] {done}/{len(refs)} ok={ok} skip={skip} err={err} {rate:.1f}/s ETA {eta/60:.0f}m", flush=True)
    print(f"DONE", flush=True)

if __name__ == "__main__": main()

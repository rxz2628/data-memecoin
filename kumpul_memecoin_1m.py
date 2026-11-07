"""
kumpul_memecoin_1m.py  --  KUMPULKAN data 1 menit SEMUA token baru (tanpa bias 'hanya yang bertahan').

Cara kerja (API publik GeckoTerminal, gratis, maks 30 permintaan/menit):
  1. Setiap ~2 menit: ambil daftar POOL BARU di jaringan (default solana) -> semua dicatat saat LAHIR,
     sebelum ketahuan mana yang terbang dan mana yang mati (ini kunci menghindari bias).
  2. Dari pool yang tercatat, diambil SAMPEL ACAK sesuai kuota permintaan; setelah pool berumur >= 6 jam,
     unduh candle 1 menit sejak lahir (maks 1000 menit ~ 16 jam). Token yang jadi nol tetap tersimpan.
  3. Hasil: folder memecoin_1m/  (satu CSV per pool) + memecoin_daftar.csv (semua pool yang lahir).

Pakai (folder quantum_finance_panjang, biarkan PC menyala):
  python kumpul_memecoin_1m.py --jam 24                (kumpulkan 24 jam; boleh dihentikan Ctrl+C kapan saja)
  python kumpul_memecoin_1m.py --jam 48 --jaringan solana
  python kumpul_memecoin_1m.py --jam 2 --sampel 1 --liq-min 5000   (uji aturan: hanya likuiditas awal >= $5.000)
  python kumpul_memecoin_1m.py --jam 0                              (hanya unduh antrean; tidak mencatat pool baru)
  python kumpul_memecoin_1m.py --selftest
Batas: 30 permintaan/menit. Data 1 menit tidak bisa menguji pegang 30 detik dengan tepat (lihat uji_pump_scalp.py).
"""
import os
import sys
import csv
import json
import time
import random
import argparse
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
API = "https://api.geckoterminal.com/api/v2"
HDR = {"Accept": "application/json;version=20230302", "User-Agent": "skripsi-quantum-finance/1.0"}
WAIT = 2.1
OUT = os.path.join(HERE, "memecoin_1m")
LIST = os.path.join(HERE, "memecoin_daftar.csv")
MIN_AGE_H = 6
LIST_EVERY_S = 120
LIST_PAGES = 5


GAGAL = "GAGAL"              # penanda: permintaan gagal (beda dengan 404 = pool memang tidak ada)
JEDA = {"s": WAIT, "ok": 0}  # jeda antar permintaan, menyesuaikan otomatis bila kena batas


def get(url, tries=6):
    for k in range(tries):
        try:
            req = urllib.request.Request(url, headers=HDR)
            with urllib.request.urlopen(req, timeout=30) as r:
                JEDA["ok"] += 1
                if JEDA["ok"] >= 60 and JEDA["s"] > WAIT:          # lancar lama -> pelan2 percepat lagi
                    JEDA["s"] = max(WAIT, JEDA["s"] * 0.9)
                    JEDA["ok"] = 0
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                JEDA["s"] = min(JEDA["s"] * 1.5, 15.0)
                JEDA["ok"] = 0
                print(f"    batas permintaan tercapai, tunggu 60 dtk (jeda kini {JEDA['s']:.1f} dtk/permintaan) ...", flush=True)
                time.sleep(60)
                continue
            if e.code == 404:
                return None
            time.sleep(3 * (k + 1))
        except Exception:
            time.sleep(3 * (k + 1))
    return GAGAL


def parse_new_pools(d):
    if d is GAGAL:
        return []
    out = []
    for p in (d or {}).get("data") or []:
        a = p.get("attributes") or {}
        dex = (((p.get("relationships") or {}).get("dex") or {}).get("data") or {}).get("id", "")
        base = (((p.get("relationships") or {}).get("base_token") or {}).get("data") or {}).get("id", "")
        m5 = ((a.get("transactions") or {}).get("m5") or {})
        out.append(dict(address=a.get("address"), name=a.get("name"), created=a.get("pool_created_at"),
                        liq=float(a.get("reserve_in_usd") or 0), dex=dex, mint=base.split("_", 1)[-1],
                        buys_m5=m5.get("buys"), sells_m5=m5.get("sells"),
                        buyers_m5=m5.get("buyers"), sellers_m5=m5.get("sellers")))
    return [x for x in out if x["address"]]


def parse_ohlcv(d):
    lst = (((d or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
    return sorted((int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5])) for x in lst)


def iso_to_ts(s):
    from datetime import datetime
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except Exception:
        return None


RUG = os.path.join(OUT, "_meta", "rugcheck.csv")     # di subfolder agar tidak dianggap file candle


def rugcheck(pool, mint, now):
    """laporan keamanan RugCheck SAAT pool ditemukan (informasi yang benar-benar tersedia waktu itu)."""
    if not mint:
        return
    try:
        req = urllib.request.Request(f"https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary", headers=HDR)
        with urllib.request.urlopen(req, timeout=20) as r:
            j = json.loads(r.read())
    except Exception:
        return
    os.makedirs(os.path.dirname(RUG), exist_ok=True)
    new = not os.path.exists(RUG)
    with open(RUG, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["address", "mint", "dicek", "token2022", "score_norm", "lp_locked_pct", "risks"])
        w.writerow([pool, mint, int(now), int(j.get("tokenProgram") == "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"),
                    j.get("score_normalised"), j.get("lpLockedPct"),
                    "|".join(f"{x.get('name')}:{x.get('level')}" for x in (j.get("risks") or []))])


DET = os.path.join(OUT, "_meta", "deteksi.csv")      # info anti-honeypot SAAT pool ditemukan


def catat_deteksi(p, now):
    """mint, apakah mint berakhiran 'pump' (dibuat lewat pump.fun), dan transaksi beli/jual 5 menit terakhir."""
    os.makedirs(os.path.dirname(DET), exist_ok=True)
    new = not os.path.exists(DET)
    with open(DET, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["address", "mint", "dicek", "mint_pump", "buys_m5", "sells_m5", "buyers_m5", "sellers_m5"])
        mint = p.get("mint") or ""
        w.writerow([p["address"], mint, int(now), int(mint.endswith("pump")), p.get("buys_m5"), p.get("sells_m5"),
                    p.get("buyers_m5"), p.get("sellers_m5")])


def load_seen():
    seen = {}
    if os.path.exists(LIST):
        with open(LIST, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                seen[r["address"]] = r
    return seen


def save_row(r):
    new = not os.path.exists(LIST)
    with open(LIST, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["address", "name", "created", "liq_awal", "dex", "dicatat", "sampel"])
        if new:
            w.writeheader()
        w.writerow(r)


def fetch_candles(net, addr, created_ts):
    end = created_ts + 1000 * 60
    d = get(f"{API}/networks/{net}/pools/{addr}/ohlcv/minute?aggregate=1&limit=1000&currency=usd&token=base"
            f"&before_timestamp={end}")
    if d is GAGAL:
        return GAGAL
    return [x for x in parse_ohlcv(d) if x[0] >= created_ts - 60]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jam", type=float, default=24)
    ap.add_argument("--jaringan", default="solana")
    ap.add_argument("--sampel", type=float, default=0.25, help="peluang pool dipilih untuk diunduh (acak)")
    ap.add_argument("--liq-min", type=float, default=0.0,
                    help="hanya sampel pool dengan likuiditas awal >= nilai ini (USD), mis. 5000 untuk uji aturan yang dikunci")
    ap.add_argument("--maks-menit", type=float, default=0.0,
                    help="berhenti paksa setelah N menit walau antrean belum habis (untuk GitHub Actions)")
    ap.add_argument("--dex", default="pumpswap",
                    help="hanya sampel/unduh pool dari DEX ini (pisahkan koma), 'semua' = semua DEX. Default: pumpswap")
    ap.add_argument("--tanpa-rugcheck", dest="rugcheck", action="store_false",
                    help="matikan pengambilan laporan RugCheck (default: aktif; disimpan di memecoin_1m/_meta/rugcheck.csv)")
    ap.add_argument("--ulang-kosong", action="store_true", help="unduh ulang file yang isinya kosong (mungkin dulu gagal)")
    a = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)
    seen = load_seen()

    def sudah(addr):
        f = os.path.join(OUT, addr + ".csv")
        if not os.path.exists(f):
            return False
        if a.ulang_kosong and os.path.getsize(f) < 60:                # hanya baris judul
            return False
        return True
    dexok = (lambda d: True) if a.dex == "semua" else (lambda d, s=set(a.dex.split(",")): d in s)
    queue = [r for r in seen.values() if r.get("sampel") == "1" and dexok(r.get("dex", "")) and not sudah(r["address"])]
    n_gagal = 0
    t_end = time.time() + a.jam * 3600
    t_stop = time.time() + a.maks_menit * 60 if a.maks_menit > 0 else float("inf")
    last_list = 0
    n_dl = 0
    print(f"  mengumpulkan {a.jam} jam di {a.jaringan} | DEX: {a.dex} | antre awal {len(queue)}. "
          f"Tercatat sebelumnya: {len(seen)} pool. Ctrl+C untuk berhenti.")
    try:
        while time.time() < t_end or queue:
            now = time.time()
            if now >= t_stop:
                print(f"  batas {a.maks_menit:g} menit tercapai; sisa antre {len(queue)} dilanjut sesi berikutnya", flush=True)
                break
            if now < t_end and now - last_list > LIST_EVERY_S:
                added = 0
                for page in range(1, LIST_PAGES + 1):
                    d = get(f"{API}/networks/{a.jaringan}/new_pools?page={page}")
                    time.sleep(WAIT)
                    for p in parse_new_pools(d):
                        if p["address"] in seen:
                            continue
                        s = "1" if (random.random() < a.sampel and p["liq"] >= a.liq_min and dexok(p["dex"])) else "0"
                        r = dict(address=p["address"], name=p["name"], created=p["created"], liq_awal=p["liq"],
                                 dex=p["dex"], dicatat=int(now), sampel=s)
                        seen[p["address"]] = r
                        save_row(r)
                        added += 1
                        if s == "1":
                            queue.append(r)
                            catat_deteksi(p, time.time())
                            if a.rugcheck:
                                rugcheck(p["address"], p.get("mint"), time.time())
                                time.sleep(0.3)
                last_list = now
                print(f"  [{time.strftime('%H:%M')}] +{added} pool baru | total {len(seen)} | antre unduh {len(queue)} "
                      f"| sudah diunduh {n_dl}", flush=True)
            ready = [r for r in queue if (iso_to_ts(r["created"]) or now) + MIN_AGE_H * 3600 < now]
            if not ready:
                if time.time() >= t_end and queue:
                    print("  menunggu pool sampel berumur 6 jam ... (Ctrl+C untuk berhenti; data yang sudah ada aman)")
                    time.sleep(60)
                else:
                    time.sleep(5)
                continue
            r = ready[0]
            queue.remove(r)
            ts = iso_to_ts(r["created"])
            rows = fetch_candles(a.jaringan, r["address"], ts) if ts else []
            time.sleep(JEDA["s"])
            if rows is GAGAL:                                         # jangan simpan file kosong; coba lagi nanti
                queue.append(r)
                n_gagal += 1
                continue
            with open(os.path.join(OUT, r["address"] + ".csv"), "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["timestamp", "open", "high", "low", "close", "volume"])
                w.writerows(rows)
            n_dl += 1
            if n_dl % 10 == 0 or not queue:
                print(f"  [{time.strftime('%H:%M')}] diunduh {n_dl} | sisa antre {len(queue)} | gagal (diulang) {n_gagal} "
                      f"| jeda {JEDA['s']:.1f} dtk | perkiraan sisa ~{len(queue) * (JEDA['s'] + 0.5) / 60:.0f} menit", flush=True)
    except KeyboardInterrupt:
        print("\n  dihentikan. Data yang sudah terkumpul tersimpan.")
    print(f"  selesai: {len(seen)} pool tercatat, {n_dl} diunduh ke {OUT}")


def selftest():
    d = {"data": [{"attributes": {"address": "AbC", "name": "DOG / SOL", "pool_created_at": "2026-11-03T10:00:00Z",
                                  "reserve_in_usd": "1234.5"}, "relationships": {"dex": {"data": {"id": "pump-fun"}}}}]}
    p = parse_new_pools(d)
    assert p[0]["address"] == "AbC" and p[0]["liq"] == 1234.5 and p[0]["dex"] == "pump-fun"
    o = {"data": {"attributes": {"ohlcv_list": [[120, 1, 2, 0.5, 1.5, 10], [60, 1, 1, 1, 1, 5]]}}}
    assert parse_ohlcv(o)[0][0] == 60
    assert iso_to_ts("2026-11-03T10:00:00Z") is not None
    print("SELFTEST kumpul_memecoin_1m OK")


if __name__ == "__main__":
    selftest() if "--selftest" in sys.argv else main()

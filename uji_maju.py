"""
uji_maju.py -- UJI MAJU KERTAS (paper trading) aturan "kecil tapi likuid" di GitHub Actions.
HANYA MEMBACA data publik. Tidak membeli, tidak menjual, tidak memegang kunci dompet. Bukan nasihat keuangan.

Aturan (DIKUNCI 22 Nov 2026, lihat aturan_kunci_corpus.txt):
  Populasi : pool PumpSwap baru (token lulus dari pump.fun), dilihat lewat GeckoTerminal new_pools.
  Masuk    : saat pool berumur 180-300 detik (menit ke-3), angka dari DexScreener:
             likuiditas $13.000-23.400, market cap <= $106.000, volume 24 jam <= $36.000, volume 5 mnt <= $35.400,
             beli 5 mnt <= 320, beli/(jual+1) <= 2,26, harga sekarang > -30% dari harga pertama yang terlihat,
             dev <= 1 token lain dalam 24 jam (pumpdata gratis), simbol <= 2 token lain dalam 24 jam (DexScreener).
  Harga masuk kertas = harga pada pengecekan BERIKUTNYA (+-30 dtk, meniru keterlambatan eksekusi).
  Keluar   : jual 50% saat harga >= 2x harga masuk (harga pengecekan berikutnya); sisa 50% ditutup setelah 24 jam.
  Biaya    : 3% pulang-pergi + 0,5% bila jual dua kali.
  Pembanding acak: untuk setiap token lolos, 1 token likuid (>= $13k) seumur yang TIDAK lolos dipilih acak dan
             dicatat dengan aturan keluar yang sama (uji placebo live).

Pakai:
  python uji_maju.py --menit 55          (loop 55 menit, cek tiap 30 detik)
  python uji_maju.py --ringkas            (cetak hasil sementara)
  python uji_maju.py --selftest           (uji logika dengan data palsu, tanpa internet)
"""
import os, sys, csv, json, time, random, argparse, calendar, urllib.request, urllib.error, statistics

HERE = os.path.dirname(os.path.abspath(__file__))
DIR = os.path.join(HERE, "uji_maju")
F_STATE = os.path.join(DIR, "state.json")
F_POS = os.path.join(DIR, "posisi.csv")
F_LOG = os.path.join(DIR, "log_cek.csv")
HDR = {"Accept": "application/json", "User-Agent": "skripsi-uji-maju/1.0"}
WSOL = "So11111111111111111111111111111111111111112"
A = dict(liq_min=13000, liq_max=23400, mcap_max=106000, v24_max=36000, v5_max=35400, b5_max=320, rbj_max=2.26,
         naik_min=-0.30, dev_max=1, tiruan_max=2, umur_min=180, umur_max=300, tp=2.0, porsi=0.5,
         tahan_s=24 * 3600, biaya=0.03, biaya2=0.005,
         modal=10.0, per_trade=1.0, fee=0.015, tetap=0.012)
# fee   = 1,5% per sisi (beli dan setiap jual) ~ fee platform pump.fun/PumpSwap + slippage kecil
# tetap = biaya tetap per transaksi Solana ~ 145.000 lamport x harga SOL ~$78 (base + priority fee)
# Rent akun token (~0,002 SOL) dianggap kembali karena akun ditutup setelah jual.
KOLOM = ["id", "jenis", "mint", "pair", "simbol", "masuk_ts", "harga_masuk", "liq", "mcap", "v24", "v5", "b5", "s5",
         "naik", "dev", "tiruan", "status", "jual50_ts", "harga_jual50", "tutup_ts", "harga_tutup", "puncak_x",
         "hasil_bersih", "dompet", "masuk_usd", "keluar_usd", "hasil_usd", "kas_setelah", "catatan"]


def get(url, tries=3, wait=2.0):
    for k in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=HDR), timeout=25) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code == 429:
                time.sleep(15 * (k + 1)); continue
            time.sleep(wait * (k + 1))
        except Exception:
            time.sleep(wait * (k + 1))
    return None


def dompet_baru():
    return {"modal": A["modal"], "kas": A["modal"], "trade": 0, "ditolak_kas": 0, "berhenti": False, "berhenti_ts": None}


def muat_state():
    st = {"pool": {}, "posisi": {}, "tutup": []}
    if os.path.exists(F_STATE):
        with open(F_STATE) as f:
            st = json.load(f)
    st.setdefault("dompet", {"pilihan": dompet_baru(), "acak": dompet_baru()})
    return st


def dompet_ambil(st, jenis, now):
    """Dompet virtual $10, $1 per trade. True bila trade ini diambil; berhenti permanen saat kas habis."""
    d = st["dompet"][jenis]
    if d["berhenti"]:
        return False
    if d["kas"] < A["per_trade"] + A["tetap"]:
        d["ditolak_kas"] += 1
        if not any(p["jenis"] == jenis and p.get("dompet") == "ya" for p in st["posisi"].values()):
            d["berhenti"] = True; d["berhenti_ts"] = int(now)     # modal habis & tidak ada posisi yang bisa mengembalikan kas
        return False
    d["kas"] -= A["per_trade"] + A["tetap"]; d["trade"] += 1
    return True


def dompet_terima(st, pos, usd):
    if pos.get("dompet") == "ya" and usd > 0:
        st["dompet"][pos["jenis"]]["kas"] += usd
    pos["keluar_usd"] = round(float(pos.get("keluar_usd") or 0) + max(usd, 0), 4)


def simpan(st):
    os.makedirs(DIR, exist_ok=True)
    # buang pool lama (> 2 jam) yang tidak dipegang
    now = time.time()
    st["pool"] = {k: v for k, v in st["pool"].items() if now - v["lahir"] < 7200 or k in st["posisi"]}
    with open(F_STATE + ".tmp", "w") as f:
        json.dump(st, f)
    os.replace(F_STATE + ".tmp", F_STATE)
    rows = list(st["tutup"]) + list(st["posisi"].values())
    with open(F_POS, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=KOLOM, extrasaction="ignore"); w.writeheader()
        for r in sorted(rows, key=lambda r: r["masuk_ts"]):
            w.writerow(r)


def log_cek(baris):
    os.makedirs(DIR, exist_ok=True)
    baru = not os.path.exists(F_LOG)
    with open(F_LOG, "a", newline="") as f:
        w = csv.writer(f)
        if baru:
            w.writerow(["ts", "pair", "simbol", "umur_s", "lolos", "alasan", "liq", "mcap", "v24", "v5", "b5", "s5", "naik"])
        w.writerow(baris)


# ---------------- sumber data ----------------
def pool_baru():
    """GeckoTerminal: pool baru di PumpSwap. -> list (pair, mint, lahir_unix)"""
    out = []
    for hal in (1, 2):
        d = get(f"https://api.geckoterminal.com/api/v2/networks/solana/new_pools?page={hal}")
        if not d:
            continue
        for p in d.get("data", []):
            a = p.get("attributes", {}); rel = p.get("relationships", {})
            dex = (((rel.get("dex") or {}).get("data") or {}).get("id") or "")
            if "pumpswap" not in dex:
                continue
            base = (((rel.get("base_token") or {}).get("data") or {}).get("id") or "").replace("solana_", "")
            quote = (((rel.get("quote_token") or {}).get("data") or {}).get("id") or "").replace("solana_", "")
            if quote != WSOL or not base:
                continue
            try:
                lahir = calendar.timegm(time.strptime(a["pool_created_at"][:19], "%Y-%m-%dT%H:%M:%S"))
            except Exception:
                continue
            out.append((a.get("address"), base, lahir))
        time.sleep(2.2)
    return out


def dex_pairs(pairs):
    """DexScreener: angka banyak pair sekaligus (maks 30)."""
    hasil = {}
    for i in range(0, len(pairs), 30):
        d = get("https://api.dexscreener.com/latest/dex/pairs/solana/" + ",".join(pairs[i:i + 30]))
        if not d:
            continue
        for p in (d.get("pairs") or ([d["pair"]] if d.get("pair") else [])):
            if p:
                hasil[p["pairAddress"]] = p
    return hasil


def angka(p):
    tx = (p.get("txns") or {}).get("m5") or {}
    return dict(harga=float(p.get("priceUsd") or 0), liq=float((p.get("liquidity") or {}).get("usd") or 0),
                mcap=float(p.get("marketCap") or p.get("fdv") or 0), v24=float((p.get("volume") or {}).get("h24") or 0),
                v5=float((p.get("volume") or {}).get("m5") or 0), b5=int(tx.get("buys") or 0), s5=int(tx.get("sells") or 0),
                simbol=(p.get("baseToken") or {}).get("symbol") or "")


def cek_dev(mint):
    """pumpdata gratis: jumlah token lain dari deployer yang sama dalam 24 jam. None bila tidak bisa dicek."""
    d = get(f"https://pumpdata.fun/free/launch/{mint}")
    dep = ((d or {}).get("deployer") or {}).get("address")
    if not dep:
        return None
    t = int(time.time()) - 86400
    d2 = get(f"https://pumpdata.fun/launches/since.json?deployer={dep}&t={t}")
    if d2 is None:
        return None
    rows = d2.get("rows") or d2.get("launches") or d2.get("data") or []
    kol = d2.get("columns")
    mints = []
    for r in rows:
        if isinstance(r, dict):
            mints.append(r.get("mint"))
        elif kol and isinstance(r, list) and "mint" in kol:
            mints.append(r[kol.index("mint")])
    return len([m for m in mints if m and m != mint])


def cek_tiruan(mint, simbol):
    d = get("https://api.dexscreener.com/latest/dex/search?q=" + urllib.request.quote(simbol))
    if d is None:
        return None
    now = time.time() * 1000; lain = set()
    for p in d.get("pairs") or []:
        if (p.get("chainId") == "solana" and (p.get("baseToken") or {}).get("symbol", "").upper().strip() == simbol.upper().strip()
                and p["baseToken"]["address"] != mint and p.get("pairCreatedAt") and now - p["pairCreatedAt"] <= 86400e3):
            lain.add(p["baseToken"]["address"])
    return len(lain)


# ---------------- aturan ----------------
def nilai_angka(x, harga_awal):
    alasan = []
    if not (A["liq_min"] <= x["liq"] <= A["liq_max"]): alasan.append("likuiditas")
    if not (0 < x["mcap"] <= A["mcap_max"]): alasan.append("mcap")
    if x["v24"] > A["v24_max"]: alasan.append("vol24")
    if x["v5"] > A["v5_max"]: alasan.append("vol5")
    if x["b5"] > A["b5_max"]: alasan.append("beli5")
    if x["b5"] / (x["s5"] + 1) > A["rbj_max"]: alasan.append("rasio_beli_jual")
    naik = (x["harga"] / harga_awal - 1) if harga_awal else 0.0
    if naik <= A["naik_min"]: alasan.append("turun>30%")
    return alasan, naik


def buka(st, jenis, pair, mint, x, naik, dev, tir, now):
    pid = f"{jenis}:{pair}"
    st["posisi"][pid] = dict(id=pid, jenis=jenis, mint=mint, pair=pair, simbol=x["simbol"], masuk_ts=int(now),
                             harga_masuk="", liq=round(x["liq"]), mcap=round(x["mcap"]), v24=round(x["v24"]),
                             v5=round(x["v5"]), b5=x["b5"], s5=x["s5"], naik=round(naik, 4), dev=dev, tiruan=tir,
                             status="menunggu_harga", jual50_ts="", harga_jual50="", tutup_ts="", harga_tutup="",
                             puncak_x=1.0, hasil_bersih="", catatan="", dompet="", masuk_usd=A["per_trade"],
                             keluar_usd=0.0, hasil_usd="", kas_setelah="")
    pos = st["posisi"][pid]
    pos["dompet"] = "ya" if dompet_ambil(st, jenis, now) else "tidak"
    if pos["dompet"] == "tidak" and st["dompet"][jenis]["berhenti"]:
        pos["catatan"] = "dompet sudah berhenti (modal habis) - dicatat untuk statistik saja"


def kelola(st, pos, x, now):
    h = x["harga"] if x else 0.0
    if pos["status"] == "menunggu_harga":
        if h > 0:
            pos["harga_masuk"] = h; pos["status"] = "terbuka"
            pos["_token"] = A["per_trade"] * (1 - A["fee"]) / h      # token yang didapat setelah fee beli
        return
    pe = float(pos["harga_masuk"])
    if h > 0:
        pos["puncak_x"] = round(max(float(pos["puncak_x"]), h / pe), 3)
        if x["liq"] < 1000:
            pos["catatan"] = "likuiditas habis"
    if pos["status"] == "terbuka" and h >= A["tp"] * pe:
        pos["status"] = "tunggu_jual50"
    elif pos["status"] == "tunggu_jual50" and h > 0:
        pos["harga_jual50"] = h; pos["jual50_ts"] = int(now); pos["status"] = "sisa50"
        tok = pos.get("_token") or A["per_trade"] * (1 - A["fee"]) / pe
        dompet_terima(st, pos, A["porsi"] * tok * h * (1 - A["fee"]) - A["tetap"])
    if now - pos["masuk_ts"] >= A["tahan_s"] or (x is None and now - pos["masuk_ts"] > 3600 and pos.get("_hilang", 0) > 20):
        akhir = h if h > 0 else float(pos.get("_terakhir") or 0)
        tok = pos.get("_token") or A["per_trade"] * (1 - A["fee"]) / pe
        sisa = tok * ((1 - A["porsi"]) if pos["harga_jual50"] else 1.0)
        nilai = sisa * akhir * (1 - A["fee"]) - A["tetap"]
        if pos.get("catatan") == "likuiditas habis" or nilai <= 0:
            nilai = 0.0                                              # tidak layak dijual: sisa dianggap nol
        dompet_terima(st, pos, nilai)
        hasil = float(pos["keluar_usd"]) - A["per_trade"] - A["tetap"]
        pos.update(harga_tutup=akhir, tutup_ts=int(now), hasil_usd=round(hasil, 4), hasil_bersih=round(hasil / A["per_trade"], 4),
                   status="tutup")
        if pos.get("dompet") == "ya":
            pos["kas_setelah"] = round(st["dompet"][pos["jenis"]]["kas"], 4)
    if h > 0:
        pos["_terakhir"] = h; pos["_hilang"] = 0
    else:
        pos["_hilang"] = pos.get("_hilang", 0) + 1


def satu_putaran(st, now, sumber):
    # 1) pool baru
    if now - st.get("_list_ts", 0) >= 60:
        for pair, mint, lahir in sumber["pool_baru"]():
            if pair and pair not in st["pool"] and now - lahir < 300:
                st["pool"][pair] = dict(mint=mint, lahir=lahir, harga_awal=None, dinilai=False)
        st["_list_ts"] = now
    # 2) angka semua pool aktif + posisi
    aktif = [p for p, v in st["pool"].items() if not v["dinilai"] and now - v["lahir"] <= A["umur_max"] + 60]
    pegang = [v["pair"] for v in st["posisi"].values()]
    X = sumber["dex_pairs"](list(dict.fromkeys(aktif + pegang)))
    lolos = []
    for pair in aktif:
        v = st["pool"][pair]; x = X.get(pair)
        if not x:
            continue
        x = angka(x)
        if v["harga_awal"] is None and x["harga"] > 0:
            v["harga_awal"] = x["harga"]
        umur = now - v["lahir"]
        if A["umur_min"] <= umur <= A["umur_max"]:
            v["dinilai"] = True
            alasan, naik = nilai_angka(x, v["harga_awal"])
            dev = tir = None
            if not alasan:
                dev = sumber["cek_dev"](v["mint"]); tir = sumber["cek_tiruan"](v["mint"], x["simbol"])
                if dev is not None and dev > A["dev_max"]: alasan.append("dev_serial")
                if tir is not None and tir > A["tiruan_max"]: alasan.append("tiruan")
            v["x"] = x; v["naik"] = naik; v["alasan"] = alasan
            log_cek([int(now), pair, x["simbol"], int(umur), int(not alasan), "|".join(alasan), round(x["liq"]), round(x["mcap"]),
                     round(x["v24"]), round(x["v5"]), x["b5"], x["s5"], round(naik, 4)])
            if not alasan:
                lolos.append((pair, x, naik, dev, tir))
    # pilihan dibuka setelah SEMUA pool seumur dinilai, lalu dipasangkan dengan pembanding acak
    for pair, x, naik, dev, tir in lolos:
        v = st["pool"][pair]
        buka(st, "pilihan", pair, v["mint"], x, naik, dev, tir, now)
        if dev is None: st["posisi"][f"pilihan:{pair}"]["catatan"] = "dev tidak bisa dicek"
        calon = [p for p, w in st["pool"].items() if w.get("dinilai") and w.get("alasan") and p != pair
                 and w["x"]["liq"] >= A["liq_min"] and f"acak:{p}" not in st["posisi"] and now - w["lahir"] <= 900]
        if calon:
            pa = random.choice(calon); w = st["pool"][pa]
            buka(st, "acak", pa, w["mint"], w["x"], w["naik"], None, None, now)
    # 3) kelola posisi
    for pid in list(st["posisi"]):
        pos = st["posisi"][pid]; x = X.get(pos["pair"])
        kelola(st, pos, angka(x) if x else None, now)
        if pos["status"] == "tutup":
            st["tutup"].append({k: v for k, v in pos.items() if not k.startswith("_")}); del st["posisi"][pid]


def ringkas():
    st = muat_state(); rows = st["tutup"]
    print(f"posisi terbuka: {len(st['posisi'])} | selesai: {len(rows)}")
    for jenis in ("pilihan", "acak"):
        r = [float(x["hasil_bersih"]) for x in rows if x["jenis"] == jenis and x["hasil_bersih"] != ""]
        if not r:
            print(f"  {jenis}: belum ada"); continue
        r.sort()
        print(f"  {jenis:8s} n={len(r)} | rata {statistics.mean(r)*100:+.1f}% | median {statistics.median(r)*100:+.1f}% | "
              f"untung {sum(v>0 for v in r)/len(r)*100:.0f}% | tanpa 1 terbaik {statistics.mean(r[:-1])*100 if len(r)>1 else float('nan'):+.1f}%")
    for jenis, d in st.get("dompet", {}).items():
        terbuka = sum(1 for p in st["posisi"].values() if p["jenis"] == jenis and p.get("dompet") == "ya")
        status = ("BERHENTI (modal habis) sejak " + time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(d["berhenti_ts"]))) if d["berhenti"] else "aktif"
        print(f"  dompet {jenis:8s}: modal ${d['modal']:.2f} | kas ${d['kas']:.2f} | posisi terbuka {terbuka} | trade diambil {d['trade']} | "
              f"ditolak karena kas kurang {d['ditolak_kas']} | {status}")
    print("Bukan nasihat keuangan.")


def selftest():
    import tempfile
    global DIR, F_STATE, F_POS, F_LOG
    DIR = tempfile.mkdtemp(); F_STATE = os.path.join(DIR, "s.json"); F_POS = os.path.join(DIR, "p.csv"); F_LOG = os.path.join(DIR, "l.csv")
    t0 = 1_800_000_000.0; harga = {"P1": 1.0, "P2": 1.0}
    def pb(): return [("P1", "M1pump", t0), ("P2", "M2pump", t0)]
    def dp(ps):
        out = {}
        for p in ps:
            if p == "P1": out[p] = dict(pairAddress=p, priceUsd=harga[p], liquidity={"usd": 18000}, marketCap=60000, volume={"h24": 9000, "m5": 4000}, txns={"m5": {"buys": 60, "sells": 40}}, baseToken={"symbol": "BAGUS", "address": "M1pump"})
            if p == "P2": out[p] = dict(pairAddress=p, priceUsd=harga[p], liquidity={"usd": 40000}, marketCap=300000, volume={"h24": 90000, "m5": 50000}, txns={"m5": {"buys": 600, "sells": 40}}, baseToken={"symbol": "BESAR", "address": "M2pump"})
        return out
    src = dict(pool_baru=pb, dex_pairs=dp, cek_dev=lambda m: 0, cek_tiruan=lambda m, s: 0)
    st = muat_state(); now = t0 + 10
    while now < t0 + 25 * 3600:
        if now > t0 + 600: harga["P1"] = 2.5 if now < t0 + 3600 else 1.2
        harga["P2"] = 0.5 if now > t0 + 900 else 1.0
        satu_putaran(st, now, src); now += 30 if now < t0 + 7200 else 600
    simpan(st)
    r = {x["jenis"]: x for x in st["tutup"]}
    assert r["pilihan"]["harga_jual50"] == 2.5, r
    f, c = A["fee"], A["tetap"]; tok = (1 - f) / 1.0
    exp = (0.5 * tok * 2.5 * (1 - f) - c) + (0.5 * tok * 1.2 * (1 - f) - c) - 1 - c
    assert abs(r["pilihan"]["hasil_usd"] - round(exp, 4)) < 1e-4, (r["pilihan"]["hasil_usd"], exp)
    expa = (tok * 0.5 * (1 - f) - c) - 1 - c
    assert abs(r["acak"]["hasil_usd"] - round(expa, 4)) < 1e-4, (r["acak"]["hasil_usd"], expa)
    d = st["dompet"]["pilihan"]; assert abs(d["kas"] - (10 + exp)) < 1e-3, d
    # uji modal habis: dompet dengan kas kecil harus berhenti dan menolak trade baru
    st2 = muat_state(); st2["dompet"]["pilihan"]["kas"] = 0.5
    assert dompet_ambil(st2, "pilihan", t0) is False and st2["dompet"]["pilihan"]["berhenti"] is True
    print(f"selftest OK: pilihan {r['pilihan']['hasil_usd']:+.3f} USD | acak {r['acak']['hasil_usd']:+.3f} USD | kas pilihan ${d['kas']:.3f}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--menit", type=float, default=55)
    ap.add_argument("--ringkas", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest: selftest(); sys.exit()
    if a.ringkas: ringkas(); sys.exit()
    src = dict(pool_baru=pool_baru, dex_pairs=dex_pairs, cek_dev=cek_dev, cek_tiruan=cek_tiruan)
    st = muat_state(); akhir = time.time() + a.menit * 60; n = 0
    total = os.environ.get("BERHENTI_TOTAL", "tidak").lower() == "ya"
    while time.time() < akhir:
        if total and st["dompet"]["pilihan"]["berhenti"] and not st["posisi"]:
            open(os.path.join(DIR, "BERHENTI.txt"), "w").write("Dompet pilihan habis; pencatatan dihentikan.\n")
            print("dompet habis & tidak ada posisi: berhenti total"); break
        t = time.time()
        try:
            satu_putaran(st, t, src)
        except Exception as e:
            print("galat putaran:", repr(e)[:200])
        n += 1
        if n % 10 == 0: simpan(st)
        time.sleep(max(1, 30 - (time.time() - t)))
    simpan(st); ringkas()

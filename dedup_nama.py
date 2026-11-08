"""
Hampton Dedup - Pengecekan Nama & Nama Ibu Kandung
===================================================
Membandingkan 2 record per baris CSV (nama & nama ibu kandung),
lalu menghasilkan V (sama) / X (beda) beserta alasannya.

Contoh pakai:
    python dedup_nama.py input.csv
    python dedup_nama.py input.csv -o hasil.csv \
        --nama1 NAMA_A --nama2 NAMA_B --ibu1 IBU_A --ibu2 IBU_B
    python dedup_nama.py input.csv --nama1 name_a --nama2 name_b --tanpa-ibu

Aturan (dicek berurutan, aturan pertama yang cocok dipakai):
    1. Kosong / placeholder (-, N/A, UNKNOWN, TANPA NAMA, ...)  -> X
    2. Sama persis / beda huruf besar-kecil, spasi, tanda baca  -> V
    3. Akhiran generasi berbeda (JR vs SR, II vs III)           -> X
    4. Sama setelah normalisasi (gelar depan/belakang, sapaan,
       ejaan lama, variasi Muhammad/Lim/Oei, dll)                -> V
    5. Sama secara pelafalan (huruf dobel, y/i, ph/f, dll)       -> V
    6. Beda spasi saja (ALMANSOURI vs AL MANSOURI)               -> V
    7. Urutan kata berbeda                                       -> V
    8. Inisial / nama terpotong, dicek kata per kata sesuai urutan -> V
       (BAGAS P vs BAGAS PAMUNGKAS, WIDYA G P / WIDYA GP vs WIDYA GALUH PRATIWI,
        WIDYA GALUH PRAT vs WIDYA GALUH PRATIWI, WIDYA GALUH vs WIDYA GALUH PRATIWI)
    9. Typo 1 huruf pada 1 kata (kata >= 5 huruf, bukan huruf terakhir) -> V
       Beda huruf terakhir (YULIANTO vs YULIANTI) / kata pendek    -> X
   10. Nama sebagian (salah satu nama berisi semua kata nama lain,
       minimal 2 kata bermakna)                                  -> V
   11. Selain itu                                                -> X
"""

import argparse
import csv
import re
import sys
import time
import unicodedata
from functools import lru_cache

import pandas as pd
from rapidfuzz import fuzz
from rapidfuzz.distance import OSA

# ---------------------------------------------------------------------------
# Konfigurasi
# ---------------------------------------------------------------------------
MIN_TYPO_LEN = 5   # kata minimal 5 huruf agar huruf hilang/lebih/tertukar dianggap typo
MIN_TYPO_GANTI = 6 # kata minimal 6 huruf agar huruf yang DIGANTI dianggap typo (ANISA vs ANITA = X)
MIN_POTONG = 2     # minimal huruf tersisa pada kata terpotong ("PR" dari "PRATIWI")

# Nilai yang dianggap kosong / placeholder (sudah dibersihkan, uppercase)
PLACEHOLDER = {
    "", "NAN", "NONE", "NULL", "NIL", "N A", "NA", "NN", "UNKNOWN", "TIDAK ADA",
    "TDK ADA", "TANPA NAMA", "IBU KANDUNG", "NAMA IBU", "NAMA IBU KANDUNG",
    "TIDAK DIKETAHUI", "TDK DIKETAHUI", "BELUM ADA", "NO NAME", "NONAME",
    "TEST", "TES", "DUMMY", "KOSONG",
}

# Gelar / sapaan di DEPAN nama
GELAR_DEPAN = {
    "H", "HJ", "HAJI", "HAJJAH", "DR", "DRS", "DRA", "IR", "PROF", "KH",
    "BPK", "BAPAK", "IBU", "NY", "NYONYA", "TN", "TUAN", "SDR", "SDRI",
    "MR", "MRS", "MS", "MISS", "MDM", "ALM", "ALMH", "ALMARHUM", "ALMARHUMAH",
}
# Gelar di BELAKANG nama
GELAR_BELAKANG = {
    "SE", "SH", "SKOM", "SPD", "SPDI", "SAG", "SSOS", "SIP", "SKM", "SKEP",
    "SFARM", "SPSI", "SHI", "SSI", "ST", "SP", "SS", "SSTP", "SKED", "SIKOM",
    "AMD", "AMDKEB", "AMK", "AMKEB", "AMDKEP", "MM", "MSI", "MPD", "MKES",
    "MH", "MT", "MAG", "MBA", "MSC", "MS", "PHD", "SPOG", "SPA", "MKOM",
}
# Akhiran generasi (JR vs SR = orang berbeda)
GENERASI = {"JR": "JR", "JUNIOR": "JR", "SR": "SR", "SENIOR": "SR",
            "II": "II", "III": "III", "IV": "IV"}

# Kata sambung/partikel yang tidak dihitung sebagai kata bermakna
PARTIKEL = {"I", "NI", "LUH", "AL", "EL", "VAN", "VON", "DER", "DEN", "DE",
            "DI", "DA", "DOS", "DEL", "LA", "LE", "BIN", "BINTI", "BT", "BN"}

# Variasi nama yang dianggap sama
VARIAN_KATA = {
    "MUHAMMAD": {"MUHAMMAD", "MUHAMAD", "MUHAMMED", "MOHAMMAD", "MOHAMAD",
                 "MOHAMMED", "MOHAMED", "MUCHAMMAD", "MUCHAMAD", "MOCHAMMAD",
                 "MOCHAMAD", "MOCH", "MOH", "MUH", "MOHD", "MHD", "MUHD",
                 "MOEHAMMAD", "MUHAMMMAD"},
    "ABDUL": {"ABDUL", "ABD", "ABDOEL", "ABDOL"},
    "NUR": {"NUR", "NOER", "NOOR", "NURUL"},
    "SITI": {"SITI", "SITTI", "SITY"},
    "LIM": {"LIM", "LIEM"},
    "OEI": {"OEI", "OEY"},
    "LI": {"LI", "LEE", "LIE"},
}


def _dobel(t: str) -> str:
    """Huruf dobel -> tunggal (MOHAAMMED -> MOHAMED)."""
    return re.sub(r"(.)\1+", r"\1", t)


VARIAN_MAP = {_dobel(v): k for k, vs in VARIAN_KATA.items() for v in vs}

# Ejaan lama -> ejaan baru
EJAAN_LAMA = [("OE", "U"), ("DJ", "J"), ("TJ", "C"), ("SJ", "SY"), ("NJ", "NY")]

# Aturan fonetik ringan (dipakai di tahap fonetik saja)
FONETIK = [
    (r"PH", "F"), (r"TH", "T"), (r"DH", "D"), (r"KH", "K"), (r"CH", "K"),
    (r"SH", "SY"), (r"Q", "K"), (r"X", "KS"), (r"Z", "S"), (r"V", "F"),
    (r"W", "U"), (r"Y", "I"), (r"(.)\1+", r"\1"),       # huruf dobel -> tunggal
    (r"H\b", ""),                                        # H di akhir kata
]


# ---------------------------------------------------------------------------
# Normalisasi
# ---------------------------------------------------------------------------
@lru_cache(maxsize=500_000)
def bersihkan(s: str) -> str:
    """Uppercase, hilangkan aksen & tanda baca, rapikan spasi."""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = s.upper()
    s = s.replace("'", "").replace("`", "")
    s = re.sub(r"[^A-Z ]+", " ", s)       # tanda baca/angka -> spasi
    return re.sub(r"\s+", " ", s).strip()


def is_kosong(x) -> bool:
    if x is None:
        return True
    s = str(x).strip()
    if s in {"-", "0", "--", "."}:
        return True
    c = bersihkan(s)
    return c in PLACEHOLDER or re.fullmatch(r"X+", c) is not None


@lru_cache(maxsize=500_000)
def normalisasi(s: str) -> tuple:
    """Kembalikan (tokens_normal, info_perubahan, generasi)."""
    raw = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().upper()
    # gabung gelar bertitik: "S.KOM" -> "SKOM", "A.MD" -> "AMD"
    raw = re.sub(r"\b([A-Z])\.(?=[A-Z]{1,4}\b)", r"\1", raw)
    tokens = bersihkan(raw).split()
    info = set()

    # buang gelar/sapaan di depan (hanya jika sisa nama >= 2 kata,
    # supaya 'H RODRIGUEZ' tetap dibaca sebagai inisial H)
    while len(tokens) > 2 and tokens[0] in GELAR_DEPAN:
        info.add("gelar/sapaan")
        tokens = tokens[1:]
    # buang gelar & akhiran generasi di belakang
    generasi = None
    while len(tokens) > 1 and (tokens[-1] in GELAR_BELAKANG or tokens[-1] in GENERASI):
        if tokens[-1] in GENERASI:
            generasi = generasi or GENERASI[tokens[-1]]
        else:
            info.add("gelar/sapaan")
        tokens = tokens[:-1]

    hasil = []
    for t in tokens:
        t = re.sub(r"^(.)\1+", r"\1", t)          # DDJONG -> DJONG
        if _dobel(t) in VARIAN_MAP:                 # cek varian sebelum ejaan lama
            t2 = VARIAN_MAP[_dobel(t)]
            if t2 != t:
                info.add("variasi penulisan")
        else:
            t2 = t
            # ejaan lama hanya jika ada penanda jelas (OE/DJ/TJ/SJ, atau NJ di awal kata);
            # NJ di tengah kata dibiarkan (SIMANJUNTAK, PANJI = ejaan modern)
            if re.search(r"OE|DJ|TJ|SJ|^NJ", t2):
                t2 = re.sub(r"(?<![DTSN])J", "Y", t2)   # J tunggal = Y (DJOJO -> JOYO)
                for lama, baru in EJAAN_LAMA:
                    t2 = t2.replace(lama, baru)
                info.add("ejaan lama")
            if _dobel(t2) in VARIAN_MAP:
                t2 = VARIAN_MAP[_dobel(t2)]
                info.add("variasi penulisan")
        hasil.append(t2)
    return tuple(hasil), frozenset(info), generasi


@lru_cache(maxsize=500_000)
def fonetik(token: str) -> str:
    for pola, ganti in FONETIK:
        token = re.sub(pola, ganti, token)
    return token


PARTIKEL_SEMUA = PARTIKEL | {fonetik(p) for p in PARTIKEL}


def bermakna(tokens) -> list:
    """Kata bermakna: bukan partikel (I, NI, AL, VAN, BIN, ...)."""
    return [t for t in tokens if t not in PARTIKEL_SEMUA]


# ---------------------------------------------------------------------------
# Perbandingan
# ---------------------------------------------------------------------------
def pecah_inisial_gabungan(tokens: tuple) -> tuple:
    """'GP' (2-3 huruf tanpa vokal) dianggap inisial gabungan -> 'G', 'P'."""
    hasil = []
    for t in tokens:
        if 2 <= len(t) <= 3 and not re.search(r"[AIUEOY]", t):
            hasil.extend(t)
        else:
            hasil.append(t)
    return tuple(hasil)


def cocok_sejajar(ta: tuple, tb: tuple):
    """
    Bandingkan kata per kata sesuai urutan, dengan toleransi:
      - inisial         : 'P' == 'PAMUNGKAS', 'G P' / 'GP' == 'GALUH PRATIWI'
      - kata terpotong  : kata terakhir terpotong, 'PRAT' == 'PRATIWI'
      - kata belakang hilang karena terpotong: 'WIDYA GALUH' == 'WIDYA GALUH PRATIWI'
    Syarat: nama yang lebih pendek minimal 2 kata bermakna (inisial dihitung)
    & minimal 1 kata utuh bermakna yang cocok.
    Return set jenis toleransi yang dipakai, atau None jika tidak cocok.
    """
    a, b = pecah_inisial_gabungan(ta), pecah_inisial_gabungan(tb)
    if len(a) > len(b):
        a, b = b, a
    if len(bermakna(a)) < 2:
        return None

    jenis, kata_utuh = set(), 0
    for i, (x, y) in enumerate(zip(a, b)):
        if x == y or fonetik(x) == fonetik(y):
            if len(x) >= 2 and x not in PARTIKEL:
                kata_utuh += 1
            continue
        if (len(x) == 1 and y.startswith(x)) or (len(y) == 1 and x.startswith(y)):
            jenis.add("inisial")
            continue
        terakhir = i == len(a) - 1
        if terakhir and len(x) >= MIN_POTONG and y.startswith(x):
            jenis.add("kata terakhir terpotong")
            continue
        if terakhir and len(a) == len(b) and len(y) >= MIN_POTONG and x.startswith(y):
            jenis.add("kata terakhir terpotong")
            continue
        return None

    if len(b) > len(a):
        jenis.add(f"{len(b) - len(a)} kata belakang tidak ada/terpotong")
    if kata_utuh == 0 or not jenis:
        return None
    return jenis


def cek_typo(ta: tuple, tb: tuple, label: str):
    """
    Typo = semua kata sama kecuali tepat 1 kata yang beda 1 huruf
    (hilang/lebih/tertukar posisi/diganti). Urutan kata boleh berbeda.
    """
    if len(ta) != len(tb):
        return None
    fa = [fonetik(t) for t in ta]
    fb = [fonetik(t) for t in tb]
    sisa_a = list(range(len(ta)))
    sisa_b = list(range(len(tb)))
    for i in list(sisa_a):                      # buang kata yang sama
        for j in sisa_b:
            if fa[i] == fb[j]:
                sisa_a.remove(i); sisa_b.remove(j)
                break
    if len(sisa_a) != 1:
        return None
    x, y = ta[sisa_a[0]], tb[sisa_b[0]]
    if OSA.distance(x, y) != 1:
        return None
    urutan = "" if fa[:sisa_a[0]] + fa[sisa_a[0]+1:] == fb[:sisa_b[0]] + fb[sisa_b[0]+1:]         else "urutan kata berbeda dan "
    diganti = len(x) == len(y) and sum(c1 != c2 for c1, c2 in zip(x, y)) == 1
    if diganti and x[:-1] == y[:-1]:
        return "X", (f"{label} berbeda huruf terakhir ('{x}' vs '{y}'), "
                     f"kemungkinan nama berbeda")
    if min(len(x), len(y)) < (MIN_TYPO_GANTI if diganti else MIN_TYPO_LEN):
        return "X", (f"{label} berbeda 1 huruf pada kata pendek ('{x}' vs '{y}'), "
                     f"terlalu berisiko dianggap typo")
    return "V", f"{label} mirip, {urutan}kemungkinan typo 1 huruf ('{x}' vs '{y}')"


def bandingkan_inti(ta, tb, info, label):
    if ta == tb:
        info = sorted(info) or ["format"]
        return "V", f"{label} sama setelah normalisasi ({', '.join(info)})"

    fa = tuple(fonetik(t) for t in ta)
    fb = tuple(fonetik(t) for t in tb)
    sa, sb = " ".join(ta), " ".join(tb)
    if fa == fb:
        return "V", f"{label} sama secara pelafalan/variasi ejaan ('{sa}' vs '{sb}')"

    if "".join(fa) == "".join(fb):
        return "V", f"{label} sama, hanya beda spasi/penggabungan kata ('{sa}' vs '{sb}')"

    if sorted(fa) == sorted(fb):
        return "V", f"{label} sama, hanya urutan kata berbeda"

    jenis = cocok_sejajar(ta, tb)
    if jenis:
        return "V", f"{label} cocok dengan {', '.join(sorted(jenis))} ('{sa}' vs '{sb}')"

    typo = cek_typo(ta, tb, label)
    if typo:
        return typo

    set_a, set_b = set(fa), set(fb)
    kecil, besar = (set_a, set_b) if len(set_a) <= len(set_b) else (set_b, set_a)
    if len(bermakna(kecil)) >= 2 and kecil <= besar:
        lebih = len(besar) - len(kecil)
        return "V", f"{label} sebagian, salah satu data memiliki {lebih} kata tambahan"

    skor = fuzz.token_sort_ratio(sa, sb)
    if set(bermakna(kecil)) & besar:
        return "X", f"{label} berbeda, hanya sebagian kata yang sama (skor {skor:.0f})"
    return "X", f"{label} berbeda (skor kemiripan {skor:.0f})"


def bandingkan(a, b, label: str) -> tuple:
    """Return (hasil 'V'/'X', alasan)."""
    ka, kb = is_kosong(a), is_kosong(b)
    if ka and kb:
        return "X", f"{label} kosong/placeholder di kedua data"
    if ka or kb:
        return "X", f"{label} kosong/placeholder di salah satu data"

    a, b = str(a).strip(), str(b).strip()
    if a == b:
        return "V", f"{label} sama persis"
    if bersihkan(a) == bersihkan(b):
        return "V", f"{label} sama (beda huruf besar/kecil, spasi, atau tanda baca)"

    ta, info_a, gen_a = normalisasi(a)
    tb, info_b, gen_b = normalisasi(b)
    if not ta or not tb:
        return "X", f"{label} tidak valid setelah dibersihkan"
    if gen_a and gen_b and gen_a != gen_b:
        return "X", f"{label} akhiran generasi berbeda ({gen_a} vs {gen_b})"

    hasil, alasan = bandingkan_inti(ta, tb, info_a | info_b, label)
    if hasil == "V" and (gen_a or gen_b) and gen_a != gen_b:
        alasan += f"; akhiran {gen_a or gen_b} hanya ada di salah satu data"
    return hasil, alasan


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def deteksi_format(path: str) -> tuple:
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            with open(path, encoding=enc) as f:
                sample = f.read(64_000)
            break
        except UnicodeDecodeError:
            continue
    try:
        sep = csv.Sniffer().sniff(sample, delimiters=",;|\t").delimiter
    except csv.Error:
        sep = ","
    return enc, sep


def main():
    p = argparse.ArgumentParser(description="Hampton Dedup - cek nama & nama ibu kandung")
    p.add_argument("input", help="File CSV input")
    p.add_argument("-o", "--output", help="File CSV output (default: <input>_hasil.csv)")
    p.add_argument("--nama1", default="nama_1")
    p.add_argument("--nama2", default="nama_2")
    p.add_argument("--ibu1", default="ibu_1")
    p.add_argument("--ibu2", default="ibu_2")
    p.add_argument("--tanpa-ibu", action="store_true",
                   help="Hanya cek nama (untuk file tanpa kolom nama ibu)")
    p.add_argument("--chunksize", type=int, default=100_000)
    args = p.parse_args()

    output = args.output or re.sub(r"\.csv$", "", args.input, flags=re.I) + "_hasil.csv"
    enc, sep = deteksi_format(args.input)
    print(f"Membaca {args.input} (encoding={enc}, delimiter={sep!r})")

    t0 = time.time()
    total = 0
    rekap = {"V": 0, "X": 0}
    first = True
    reader = pd.read_csv(args.input, dtype=str, keep_default_na=False,
                         encoding=enc, sep=sep, chunksize=args.chunksize)
    for chunk in reader:
        if first:
            kolom = [args.nama1, args.nama2]
            if not args.tanpa_ibu:
                kolom += [args.ibu1, args.ibu2]
            hilang = [k for k in kolom if k not in chunk.columns]
            if hilang:
                sys.exit(f"Kolom tidak ditemukan: {hilang}\n"
                         f"Kolom tersedia: {list(chunk.columns)}\n"
                         f"Gunakan --nama1/--nama2/--ibu1/--ibu2 untuk mengatur nama kolom,"
                         f" atau --tanpa-ibu jika file tidak punya kolom nama ibu.")

        hn, an, hi, ai, hf, af = [], [], [], [], [], []
        if args.tanpa_ibu:
            for n1, n2 in zip(chunk[args.nama1], chunk[args.nama2]):
                r_n, a_n = bandingkan(n1, n2, "Nama")
                hf.append(r_n); af.append(a_n)
                rekap[r_n] += 1
        else:
            for n1, n2, i1, i2 in zip(chunk[args.nama1], chunk[args.nama2],
                                      chunk[args.ibu1], chunk[args.ibu2]):
                r_n, a_n = bandingkan(n1, n2, "Nama")
                r_i, a_i = bandingkan(i1, i2, "Nama ibu")
                final = "V" if r_n == "V" and r_i == "V" else "X"
                hn.append(r_n); an.append(a_n)
                hi.append(r_i); ai.append(a_i)
                hf.append(final); af.append(f"{a_n}; {a_i}")
                rekap[final] += 1
            chunk["hasil_nama"], chunk["alasan_nama"] = hn, an
            chunk["hasil_ibu"], chunk["alasan_ibu"] = hi, ai

        chunk["hasil"], chunk["alasan"] = hf, af
        chunk.to_csv(output, mode="w" if first else "a", header=first,
                     index=False, encoding="utf-8-sig" if first else "utf-8")
        first = False
        total += len(chunk)
        print(f"  {total:,} baris diproses ({time.time() - t0:.1f} detik)")

    print(f"\nSelesai: {total:,} baris dalam {time.time() - t0:.1f} detik")
    print(f"  V (sama): {rekap['V']:,}")
    print(f"  X (beda): {rekap['X']:,}")
    print(f"Output: {output}")


if __name__ == "__main__":
    main()

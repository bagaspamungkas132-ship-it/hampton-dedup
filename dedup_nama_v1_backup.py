"""
Hampton Dedup - Pengecekan Nama & Nama Ibu Kandung
===================================================
Membandingkan 2 record per baris CSV (nama & nama ibu kandung),
lalu menghasilkan V (sama) / X (beda) beserta alasannya.

Contoh pakai:
    python dedup_nama.py input.csv
    python dedup_nama.py input.csv -o hasil.csv \
        --nama1 NAMA_A --nama2 NAMA_B --ibu1 IBU_A --ibu2 IBU_B

Aturan (dicek berurutan, aturan pertama yang cocok dipakai):
    1. Kosong          -> X  (salah satu / kedua nilai kosong)
    2. Sama persis     -> V
    3. Sama setelah normalisasi (huruf besar/kecil, spasi, tanda baca,
       gelar, ejaan lama, variasi Muhammad)            -> V
    4. Sama secara fonetik (huruf dobel, y/i, ph/f, dll) -> V
    5. Urutan kata berbeda                             -> V
    6. Inisial / nama terpotong, dicek kata per kata sesuai urutan -> V
       (BAGAS P vs BAGAS PAMUNGKAS, WIDYA G P / WIDYA GP vs WIDYA GALUH PRATIWI,
        WIDYA GALUH PRAT vs WIDYA GALUH PRATIWI, WIDYA GALUH vs WIDYA GALUH PRATIWI)
    7. Typo minor (skor kemiripan >= threshold)        -> V
    8. Nama sebagian (salah satu nama berisi semua kata nama lain,
       minimal 2 kata)                                 -> V
    9. Selain itu                                      -> X
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
from rapidfuzz.distance import Levenshtein

# ---------------------------------------------------------------------------
# Konfigurasi
# ---------------------------------------------------------------------------
TYPO_THRESHOLD = 90       # skor kemiripan minimal (0-100) agar dianggap typo
MAX_TYPO_EDITS = 2        # maksimal selisih karakter untuk typo

# Gelar / sebutan yang dibuang (sudah tanpa titik, uppercase)
GELAR = {
    "H", "HJ", "HAJI", "HAJJAH", "DR", "DRS", "DRA", "IR", "PROF",
    "SE", "SH", "SKOM", "SPD", "SPDI", "SAG", "SSOS", "SIP", "SKM", "SKEP",
    "SFARM", "SPSI", "SHI", "SSI", "ST", "SP", "SS", "SSTP", "AMD", "AMDKEB",
    "AMK", "AMKEB", "MM", "MSI", "MPD", "MKES", "MH", "MT", "MA", "MAG",
    "MBA", "MSC", "PHD", "BPK", "BAPAK", "IBU", "NY", "NYONYA", "TN", "TUAN",
    "SDR", "SDRI", "ALM", "ALMH", "ALMARHUM", "ALMARHUMAH", "KH", "RA", "RR",
}

# Variasi nama yang dianggap sama
VARIAN_KATA = {
    "MUHAMMAD": {"MUHAMMAD", "MUHAMAD", "MUHAMMED", "MOHAMMAD", "MOHAMAD",
                 "MOHAMMED", "MOHAMED", "MUCHAMMAD", "MUCHAMAD", "MOCHAMMAD",
                 "MOCHAMAD", "MOCH", "MOH", "MUH", "MOHD", "MHD", "MUHD",
                 "MOEHAMMAD", "MUHAMMMAD"},
    "ABDUL": {"ABDUL", "ABD", "ABDOEL", "ABDOL"},
    "NUR": {"NUR", "NOER", "NOOR", "NURUL"},
    "SITI": {"SITI", "SITTI", "SITY"},
}
VARIAN_MAP = {v: k for k, vs in VARIAN_KATA.items() for v in vs}

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
def is_kosong(x) -> bool:
    if x is None:
        return True
    s = str(x).strip().upper()
    return s in {"", "NAN", "NONE", "NULL", "-", "0"}


@lru_cache(maxsize=500_000)
def bersihkan(s: str) -> str:
    """Uppercase, hilangkan aksen & tanda baca, rapikan spasi."""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = s.upper()
    s = s.replace("'", "").replace("`", "")
    s = re.sub(r"[^A-Z ]+", " ", s)       # tanda baca/angka -> spasi
    return re.sub(r"\s+", " ", s).strip()


@lru_cache(maxsize=500_000)
def normalisasi(s: str) -> tuple:
    """Kembalikan (tokens_normal, info_perubahan)."""
    raw = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().upper()
    # gabung gelar bertitik: "S.KOM" -> "SKOM", "A.MD" -> "AMD"
    raw = re.sub(r"\b([A-Z])\.(?=[A-Z]{1,4}\b)", r"\1", raw)
    tokens = bersihkan(raw).split()

    info = set()
    hasil = []
    for t in tokens:
        if t in GELAR and len(tokens) > 1:
            info.add("gelar")
            continue
        t2 = t
        for lama, baru in EJAAN_LAMA:
            t2 = t2.replace(lama, baru)
        if t2 != t:
            info.add("ejaan lama")
        if t2 in VARIAN_MAP:
            if VARIAN_MAP[t2] != t2:
                info.add("variasi penulisan")
            t2 = VARIAN_MAP[t2]
        hasil.append(t2)
    return tuple(hasil), frozenset(info)


@lru_cache(maxsize=500_000)
def fonetik(token: str) -> str:
    for pola, ganti in FONETIK:
        token = re.sub(pola, ganti, token)
    return token


# ---------------------------------------------------------------------------
# Perbandingan
# ---------------------------------------------------------------------------
MIN_POTONG = 2  # minimal huruf tersisa pada kata yang terpotong (mis. "PR" dari "PRATIWI")


def pecah_inisial_gabungan(tokens: tuple) -> tuple:
    """'GP' (2-3 huruf tanpa vokal) dianggap inisial gabungan -> 'G', 'P'."""
    hasil = []
    for t in tokens:
        if 2 <= len(t) <= 3 and not re.search(r"[AIUEO]", t):
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
    Syarat: nama yang lebih pendek minimal 2 kata & minimal 1 kata utuh cocok.
    Return set jenis toleransi yang dipakai, atau None jika tidak cocok.
    """
    a, b = pecah_inisial_gabungan(ta), pecah_inisial_gabungan(tb)
    if len(a) > len(b):
        a, b = b, a
    if len(a) < 2:
        return None

    jenis, kata_utuh = set(), 0
    for i, (x, y) in enumerate(zip(a, b)):
        if x == y or fonetik(x) == fonetik(y):
            if len(x) >= 2:
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


def bandingkan(a, b, label: str) -> tuple:
    """Return (hasil 'V'/'X', alasan)."""
    if is_kosong(a) and is_kosong(b):
        return "X", f"{label} kosong di kedua data"
    if is_kosong(a) or is_kosong(b):
        return "X", f"{label} kosong di salah satu data"

    a, b = str(a).strip(), str(b).strip()
    if a == b:
        return "V", f"{label} sama persis"

    if bersihkan(a) == bersihkan(b):
        return "V", f"{label} sama (beda huruf besar/kecil, spasi, atau tanda baca)"

    ta, info_a = normalisasi(a)
    tb, info_b = normalisasi(b)
    if not ta or not tb:
        return "X", f"{label} tidak valid setelah dibersihkan"

    if ta == tb:
        info = sorted(info_a | info_b) or ["format"]
        return "V", f"{label} sama setelah normalisasi ({', '.join(info)})"

    fa = tuple(fonetik(t) for t in ta)
    fb = tuple(fonetik(t) for t in tb)
    if fa == fb:
        return "V", f"{label} sama secara pelafalan/variasi ejaan ('{' '.join(ta)}' vs '{' '.join(tb)}')"

    if sorted(fa) == sorted(fb):
        return "V", f"{label} sama, hanya urutan kata berbeda"

    jenis = cocok_sejajar(ta, tb)
    if jenis:
        return "V", (f"{label} cocok dengan {', '.join(sorted(jenis))} "
                     f"('{' '.join(ta)}' vs '{' '.join(tb)}')")

    sa, sb = " ".join(ta), " ".join(tb)
    skor = fuzz.ratio(sa, sb)
    jarak = Levenshtein.distance(sa, sb)
    if skor >= TYPO_THRESHOLD and jarak <= MAX_TYPO_EDITS:
        return "V", f"{label} mirip, kemungkinan typo (selisih {jarak} karakter, skor {skor:.0f})"

    set_a, set_b = set(fa), set(fb)
    kecil, besar = (set_a, set_b) if len(set_a) <= len(set_b) else (set_b, set_a)
    if len(kecil) >= 2 and kecil <= besar:
        lebih = len(besar) - len(kecil)
        return "V", f"{label} sebagian, salah satu data memiliki {lebih} kata tambahan"

    skor_token = fuzz.token_sort_ratio(sa, sb)
    if kecil & besar:
        return "X", f"{label} berbeda, hanya sebagian kata yang sama (skor {skor_token:.0f})"
    return "X", f"{label} berbeda (skor kemiripan {skor_token:.0f})"


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

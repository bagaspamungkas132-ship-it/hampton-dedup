"""
Hampton Dedup - Validasi V/X per field + kombinasi Match/Suspect/Not Match
==========================================================================

Input CSV berisi pasangan kolom <field>_left & <field>_right:
    cfcif#, cfna1, cfbirth, ktp, sid, npwp, cfmotn, hp, email

Output: setiap pasangan disisipi kolom hasil tepat setelahnya:
    <field>_validasi                      -> V / X (semua field kecuali cfcif#)
    <field>_skor, <field>_keterangan      -> khusus field nama (cfna1, cfmotn)
Lalu di akhir:
    kombinasi  -> 8 karakter V/X sesuai URUTAN_KOMBINASI (cfcif# tidak ikut)
    hasil      -> Match / Suspect / Not Match dari file matrix

Pakai:
    python hampton_dedup.py data.csv
    python hampton_dedup.py data.csv -m matrix_kombinasi.csv -o hasil.csv
    python hampton_dedup.py --buat-matrix          # buat template matrix dummy
    python hampton_dedup.py --llm-tes              # tes koneksi LLM
    python hampton_dedup.py data.csv --llm         # nama abu-abu dicek ke LLM

Dengan --llm, field nama mendapat kolom tambahan:
    <field>_validasi        hasil FINAL (LLM jika ada, selain itu aturan)
    <field>_keterangan      alasan FINAL; jika dari LLM diawali "[LLM] "
    <field>_sumber          RULE / LLM / RULE (abu-abu, tidak mengubah hasil) / RULE (LLM gagal)
    <field>_validasi_rule, <field>_keterangan_rule   hasil & alasan aturan asli
    <field>_validasi_llm, <field>_yakin_llm, <field>_alasan_llm

Aturan validasi:
    - Kosong di salah satu / kedua sisi                      -> X
    - Nama (cfna1, cfmotn): logika fuzzy di dedup_nama.py    -> V / X + skor + keterangan
    - Lainnya: dinormalisasi dulu, lalu dibandingkan exact:
        cfbirth : dibaca dari berbagai format (hari-bulan-tahun), ditulis ulang
                  ke yyyy-mm-dd di kolom left/right; tidak terbaca/tidak valid -> X
        ktp     : angka saja
        npwp    : angka saja (tanpa penyamaan 15 vs 16 digit)
        hp      : angka saja, awalan +62 / 62 / 0 disamakan
        email   : huruf kecil, tanpa spasi
        sid     : huruf besar, tanpa spasi
    - cfcif#: wajib ada & ikut di output, tidak divalidasi
"""

import argparse
import csv
import itertools
import os
import re
import sys
import time
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path

import pandas as pd
from rapidfuzz import fuzz

import dedup_nama
import hampton_llm

# ---------------------------------------------------------------------------
# Konfigurasi
# ---------------------------------------------------------------------------
# ===== SATU-SATUNYA TEMPAT YANG PERLU DIUBAH JIKA NAMA KOLOM BERUBAH =====
#
# kolom     : nama dasar kolom di CSV (tanpa akhiran _left/_right)
# jenis     : nama | tanggal | angka | hp | email | id
#             None = kolom wajib ada & ikut di output, tapi TIDAK divalidasi (mis. cfcif#)
# kombinasi : True  = ikut kombinasi V/X (urutan posisi = urutan di daftar ini)
#             False = tidak ikut kombinasi
# label     : (khusus jenis nama) dipakai di kolom keterangan
#
# Contoh: kolom nama berubah jadi cfname_left/cfname_right
#         -> cukup ganti "cfna1" menjadi "cfname" di bawah ini.
FIELDS = [
    {"kolom": "cfcif#",  "jenis": None,      "kombinasi": False},
    {"kolom": "cfna1",   "jenis": "nama",    "kombinasi": True, "label": "Nama"},
    {"kolom": "cfbirth", "jenis": "tanggal", "kombinasi": True},
    {"kolom": "ktp",     "jenis": "angka",   "kombinasi": True},
    {"kolom": "sid",     "jenis": "id",      "kombinasi": True},
    {"kolom": "npwp",    "jenis": "angka",   "kombinasi": True},
    {"kolom": "cfmotn",  "jenis": "nama",    "kombinasi": True, "label": "Nama ibu"},
    {"kolom": "hp",      "jenis": "hp",      "kombinasi": True},
    {"kolom": "email",   "jenis": "email",   "kombinasi": True},
]
# Akhiran nama kolom input & output
SUFFIX_KIRI = "_left"
SUFFIX_KANAN = "_right"
SUFFIX_VALIDASI = "_validasi"
SUFFIX_SKOR = "_skor"
SUFFIX_KETERANGAN = "_keterangan"
# =========================================================================

URUTAN_KOMBINASI = [f["kolom"] for f in FIELDS if f["kombinasi"]]
HASIL_VALID = {"Match", "Suspect", "Not Match"}
TIDAK_ADA_DI_MATRIX = "TIDAK ADA DI MATRIX"
MATRIX_DEFAULT = Path(__file__).with_name("matrix_kombinasi.csv")


# ---------------------------------------------------------------------------
# Normalisasi field non-nama
# ---------------------------------------------------------------------------
KOSONG = {"", "-", "--", ".", "0", "NAN", "NONE", "NULL", "NIL", "N/A", "NA",
          "TIDAK ADA", "UNKNOWN"}
# Nilai dummy (setelah dinormalisasi) -> X walau sama di kedua sisi.
# Angka yang hanya 1 digit diulang (0000..., 9999...) otomatis dianggap dummy.
# shortcut: daftar contoh umum, lengkapi dengan nilai dummy resmi dari sistem Hampton
DUMMY = {"noemail@gmail.com", "noemail@email.com", "tidakada@gmail.com",
         "no@email.com", "-@-", "1900-01-01"}


def is_kosong(x) -> bool:
    return x is None or str(x).strip().upper() in KOSONG


BULAN = {
    "JAN": 1, "JANUARI": 1, "JANUARY": 1, "FEB": 2, "FEBRUARI": 2, "FEBRUARY": 2,
    "MAR": 3, "MARET": 3, "MARCH": 3, "APR": 4, "APRIL": 4, "MEI": 5, "MAY": 5,
    "JUN": 6, "JUNI": 6, "JUNE": 6, "JUL": 7, "JULI": 7, "JULY": 7,
    "AGU": 8, "AGT": 8, "AGS": 8, "AGUSTUS": 8, "AUG": 8, "AUGUST": 8,
    "SEP": 9, "SEPT": 9, "SEPTEMBER": 9, "OKT": 10, "OKTOBER": 10, "OCT": 10,
    "OCTOBER": 10, "NOV": 11, "NOPEMBER": 11, "NOVEMBER": 11,
    "DES": 12, "DESEMBER": 12, "DEC": 12, "DECEMBER": 12,
}


def _tahun(y: int) -> int:
    """Tahun 2 digit -> 4 digit (pivot: > tahun ini = 19xx)."""
    if y >= 100:
        return y
    batas = date.today().year % 100
    return 2000 + y if y <= batas else 1900 + y


def _buat(y, m, d):
    try:
        return date(_tahun(int(y)), int(m), int(d)).isoformat()
    except ValueError:
        return None


@lru_cache(maxsize=200_000)
def norm_tanggal(x: str):
    """Ubah berbagai format tanggal ke yyyy-mm-dd. Return None jika gagal."""
    s = str(x).strip().upper()
    s = re.sub(r"[ T]\d{1,2}:\d{2}(:\d{2})?(\.\d+)?$", "", s).strip()  # buang jam

    # yyyy-mm-dd / yyyy/mm/dd / yyyy.mm.dd
    m = re.fullmatch(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", s)
    if m:
        return _buat(m[1], m[2], m[3])
    # dd-mm-yyyy / dd/mm/yy / dd.mm.yyyy  (urutan hari-bulan-tahun)
    m = re.fullmatch(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{2}|\d{4})", s)
    if m:
        return _buat(m[3], m[2], m[1])
    # 8 digit: yyyymmdd atau ddmmyyyy
    m = re.fullmatch(r"\d{8}", s)
    if m:
        if 1900 <= int(s[:4]) <= 2100:
            hasil = _buat(s[:4], s[4:6], s[6:])
            if hasil:
                return hasil
        return _buat(s[4:], s[2:4], s[:2])
    # 12 JAN 1990 / 12-Januari-1990 / 12 AUG 90
    m = re.fullmatch(r"(\d{1,2})[\s\-/.]*([A-Z]+)[\s\-/.]*(\d{2}|\d{4})", s)
    if m and m[2] in BULAN:
        return _buat(m[3], BULAN[m[2]], m[1])
    # nomor seri tanggal Excel (mis. 32874 = 1990-01-01); 5 digit saja supaya "1990" tidak ikut
    m = re.fullmatch(r"\d{5}(\.0+)?", s)
    if m and 10000 <= float(s) <= 80000:
        return (datetime(1899, 12, 30) + timedelta(days=int(float(s)))).date().isoformat()
    return None


def norm_angka(x: str) -> str:
    return re.sub(r"\D", "", str(x))


def norm_hp(x: str) -> str:
    d = norm_angka(x)
    if d.startswith("0062"):                 # awalan internasional 00
        d = d[4:]
    elif d.startswith("62"):
        d = d[2:]
    d = d.lstrip("0")
    return "0" + d if d else ""


def norm_email(x: str) -> str:
    return re.sub(r"\s+", "", str(x)).lower()


def norm_id(x: str) -> str:
    return re.sub(r"\s+", "", str(x)).upper()


NORMALISASI = {"angka": norm_angka, "hp": norm_hp, "email": norm_email, "id": norm_id}


def validasi_exact(a, b, jenis) -> str:
    if is_kosong(a) or is_kosong(b):
        return "X"
    if jenis == "tanggal":
        na, nb = norm_tanggal(a), norm_tanggal(b)
        if na is None or nb is None:            # format tak dikenal / tanggal tidak valid
            return "X"
    else:
        f = NORMALISASI[jenis]
        na, nb = f(a), f(b)
    if not na or na != nb or na in DUMMY or (jenis == "angka" and len(set(na)) == 1):
        return "X"
    return "V"


# ---------------------------------------------------------------------------
# Nama
# ---------------------------------------------------------------------------
def skor_nama(a, b):
    """Skor kemiripan 0-100 setelah normalisasi (gelar, ejaan, urutan kata diabaikan)."""
    if dedup_nama.is_kosong(a) or dedup_nama.is_kosong(b):
        return ""
    ta, _, gen_a = dedup_nama.normalisasi(str(a).strip())
    tb, _, gen_b = dedup_nama.normalisasi(str(b).strip())
    if not ta or not tb:
        return 0
    sa = " ".join(ta + ((gen_a,) if gen_a else ()))     # akhiran JR/SR tetap dihitung
    sb = " ".join(tb + ((gen_b,) if gen_b else ()))
    return round(fuzz.token_sort_ratio(sa, sb))


# ---------------------------------------------------------------------------
# Matrix kombinasi
# ---------------------------------------------------------------------------
def buat_matrix(path: Path):
    rows = []
    for combo in itertools.product("VX", repeat=len(URUTAN_KOMBINASI)):
        k = "".join(combo)
        hasil = "Match" if k == "V" * len(k) else "Not Match" if k == "X" * len(k) else "Suspect"
        rows.append([k, *combo, hasil])
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["kombinasi", *URUTAN_KOMBINASI, "hasil"])
        w.writerows(rows)
    print(f"Template matrix dummy ({len(rows)} kombinasi) dibuat: {path}")
    print(f"Urutan posisi: {' | '.join(URUTAN_KOMBINASI)}")
    print("Isi kolom 'hasil' dengan Match / Suspect / Not Match.")


def muat_matrix(path: Path):
    """Return (dict exact, list wildcard). Wildcard pakai '?' (mis. VV??????)."""
    if not path.exists():
        sys.exit(f"File matrix tidak ditemukan: {path}\n"
                 f"Buat template dengan: python hampton_dedup.py --buat-matrix")
    m = pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    m.columns = [c.strip().lower() for c in m.columns]
    if not {"kombinasi", "hasil"} <= set(m.columns):
        sys.exit("File matrix harus punya kolom 'kombinasi' dan 'hasil'.")
    # cek header posisi di matrix sama dengan URUTAN_KOMBINASI di script
    posisi_matrix = [c for c in m.columns if c not in ("kombinasi", "hasil")]
    posisi_script = [c.lower() for c in URUTAN_KOMBINASI]
    if posisi_matrix and posisi_matrix != posisi_script:
        print("PERINGATAN: urutan kolom di matrix berbeda dengan urutan kombinasi di script!")
        print(f"  matrix : {' | '.join(posisi_matrix)}")
        print(f"  script : {' | '.join(posisi_script)}")
        print("  Jika hanya ganti nama kolom, samakan header matrix. Jika urutan berubah, "
              "susun ulang isi matrix.")

    exact, wildcard, salah = {}, [], []
    n = len(URUTAN_KOMBINASI)
    for i, (k, h) in enumerate(zip(m["kombinasi"], m["hasil"]), start=2):
        k, h = k.strip().upper(), h.strip()
        if not re.fullmatch(rf"[VX?]{{{n}}}", k) or h not in HASIL_VALID:
            salah.append(f"  baris {i}: kombinasi='{k}' hasil='{h}'")
            continue
        if "?" in k:
            wildcard.append((re.compile(k.replace("?", "[VX]")), h))
        else:
            exact[k] = h
    if salah:
        print(f"PERINGATAN: {len(salah)} baris matrix tidak valid & diabaikan "
              f"(kombinasi harus {n} karakter V/X/?, hasil harus {sorted(HASIL_VALID)}):")
        print("\n".join(salah[:10]))
    return exact, wildcard


def cari_hasil(k, exact, wildcard):
    if k in exact:
        return exact[k]
    for pola, h in wildcard:
        if pola.fullmatch(k):
            return h
    return TIDAK_ADA_DI_MATRIX


# ---------------------------------------------------------------------------
# Proses
# ---------------------------------------------------------------------------
def kolom_input() -> list:
    return [f["kolom"] + s for f in FIELDS for s in (SUFFIX_KIRI, SUFFIX_KANAN)]


def cek_llm(df, validasi, out, exact, wildcard, llm, cache, tanpa_filter, stat):
    """
    Tentukan pasangan nama abu-abu yang perlu ke LLM, panggil LLM (dengan cache),
    lalu timpa validasi dengan keputusan LLM. Hasil aturan tetap disimpan.
    """
    nama_fields = [f for f in FIELDS if f["jenis"] == "nama"]
    n = len(df)
    for f in nama_fields:
        k = f["kolom"]
        out[k + "_validasi_rule"] = list(validasi[k])
        out[k + "_keterangan_rule"] = list(out[k + SUFFIX_KETERANGAN])
        out[k + "_sumber"] = [hampton_llm.SUMBER_RULE] * n
        out[k + "_validasi_llm"] = [""] * n
        out[k + "_yakin_llm"] = [""] * n
        out[k + "_alasan_llm"] = [""] * n

    zona = {f["kolom"]: [hampton_llm.zona(h, ket, s) for h, ket, s in zip(
        validasi[f["kolom"]], out[f["kolom"] + SUFFIX_KETERANGAN], out[f["kolom"] + SUFFIX_SKOR])]
        for f in nama_fields}
    posisi = {k: i for i, k in enumerate(URUTAN_KOMBINASI)}

    pending = []                                   # (baris, field)
    for i in range(n):
        abu = [f["kolom"] for f in nama_fields if zona[f["kolom"]][i] == "ABU"]
        if not abu:
            continue
        if not tanpa_filter:
            # kirim hanya jika keputusan nama bisa mengubah hasil akhir
            dasar = [validasi[k][i] for k in URUTAN_KOMBINASI]
            hasil_mungkin = set()
            for combo in itertools.product("VX", repeat=len(abu)):
                c = list(dasar)
                for k, v in zip(abu, combo):
                    if k in posisi:
                        c[posisi[k]] = v
                hasil_mungkin.add(cari_hasil("".join(c), exact, wildcard))
            if len(hasil_mungkin) == 1:
                for k in abu:
                    out[k + "_sumber"][i] = hampton_llm.SUMBER_SKIP
                stat["skip"] += len(abu)
                continue
        pending += [(i, k) for k in abu]

    if not pending:
        return
    # pasangan unik yang belum ada di cache
    label = {f["kolom"]: f.get("label", f["kolom"]) for f in nama_fields}
    baru = {}
    for i, k in pending:
        a, b = df[k + SUFFIX_KIRI].iat[i], df[k + SUFFIX_KANAN].iat[i]
        key = hampton_llm.kunci(a, b)
        if cache.get(key) is None and key not in baru:
            baru[key] = {"key": key, "nama_1": str(a).strip(), "nama_2": str(b).strip(),
                         "hasil_aturan": validasi[k][i],
                         "alasan_aturan": out[k + SUFFIX_KETERANGAN][i]}
    stat["dikirim"] += len(pending)
    stat["cache_hit"] += len(pending) - len(baru)
    if baru:
        print(f"    LLM: {len(pending):,} pengecekan, {len(baru):,} pasangan baru "
              f"(sisanya dari cache)", flush=True)
        llm.nilai(list(baru.values()), simpan=cache.simpan)

    for i, k in pending:
        key = hampton_llm.kunci(df[k + SUFFIX_KIRI].iat[i], df[k + SUFFIX_KANAN].iat[i])
        jawab = cache.get(key)
        if jawab is None:
            out[k + "_sumber"][i] = hampton_llm.SUMBER_GAGAL
            stat["gagal"] += 1
            continue
        kep, yakin, alasan = jawab
        validasi[k][i] = kep
        out[k + "_sumber"][i] = hampton_llm.SUMBER_LLM
        out[k + "_validasi_llm"][i] = kep
        out[k + "_yakin_llm"][i] = yakin
        out[k + "_alasan_llm"][i] = alasan
        out[k + SUFFIX_KETERANGAN][i] = f"[LLM] {alasan}"   # selalu sejalan dengan _validasi
        stat["llm"] += 1
        if kep != out[k + "_validasi_rule"][i]:
            stat["berubah"] += 1


def proses_chunk(df, exact, wildcard, llm=None, cache=None, tanpa_filter=False, stat=None):
    out = {}
    validasi = {}
    for f in FIELDS:
        field, jenis = f["kolom"], f["jenis"]
        kiri, kanan = field + SUFFIX_KIRI, field + SUFFIX_KANAN
        L, R = df[kiri], df[kanan]
        if jenis is None:
            continue
        if jenis == "nama":
            hasil = [dedup_nama.bandingkan(a, b, f.get("label", field)) for a, b in zip(L, R)]
            validasi[field] = [h for h, _ in hasil]
            out[field + SUFFIX_SKOR] = [skor_nama(a, b) for a, b in zip(L, R)]
            out[field + SUFFIX_KETERANGAN] = [k for _, k in hasil]
        else:
            validasi[field] = [validasi_exact(a, b, jenis) for a, b in zip(L, R)]
            if jenis == "tanggal":          # tulis ulang ke yyyy-mm-dd bila bisa dibaca
                df[kiri] = [norm_tanggal(x) or x for x in L]
                df[kanan] = [norm_tanggal(x) or x for x in R]

    if llm is not None:
        df = df.reset_index(drop=True)
        cek_llm(df, validasi, out, exact, wildcard, llm, cache, tanpa_filter, stat)
    for k in validasi:
        out[k + SUFFIX_VALIDASI] = validasi[k]

    kombinasi = ["".join(v) for v in zip(*(validasi[f] for f in URUTAN_KOMBINASI))]

    # susun kolom: urutan asli file dipertahankan; kolom hasil tiap field
    # disisipkan tepat setelah kolom <field>_right
    setelah_kanan = {f["kolom"] + SUFFIX_KANAN: f for f in FIELDS if f["jenis"] is not None}
    hasil_df = {}
    for c in df.columns:
        hasil_df[c] = df[c]
        f = setelah_kanan.get(c)
        if f is None:
            continue
        field = f["kolom"]
        hasil_df[field + SUFFIX_VALIDASI] = out[field + SUFFIX_VALIDASI]
        if f["jenis"] == "nama":
            hasil_df[field + SUFFIX_SKOR] = out[field + SUFFIX_SKOR]
            hasil_df[field + SUFFIX_KETERANGAN] = out[field + SUFFIX_KETERANGAN]
            if llm is not None:
                for s in ("_sumber", "_validasi_rule", "_keterangan_rule",
                          "_validasi_llm", "_yakin_llm", "_alasan_llm"):
                    hasil_df[field + s] = out[field + s]
    hasil_df["kombinasi"] = kombinasi
    hasil_df["hasil"] = [cari_hasil(k, exact, wildcard) for k in kombinasi]
    return pd.DataFrame(hasil_df, index=df.index)


class InputError(ValueError):
    """File input tidak sesuai format (mis. kolom wajib tidak ada)."""


def cek_kolom(columns) -> list:
    """Samakan nama kolom (tanpa peduli huruf besar/kecil & spasi). Raise InputError jika kurang."""
    wajib = {c.lower(): c for c in kolom_input()}
    kolom = [wajib.get(str(c).strip().lower(), str(c).strip()) for c in columns]
    hilang = [c for c in wajib.values() if c not in kolom]
    if hilang:
        raise InputError(f"Kolom tidak ditemukan: {', '.join(hilang)}. "
                         f"Kolom yang ada: {', '.join(map(str, columns))}")
    return kolom


def proses_file(input_path, output_path, exact, wildcard, llm=None, cache=None,
                tanpa_filter=False, chunksize=100_000, progress=None):
    """
    Proses satu file CSV -> file hasil. Dipakai CLI & aplikasi web.
    Output asli baru muncul setelah semua baris selesai (ditulis ke .tmp dulu).
    Return (total_baris, rekap_hasil, stat_llm). Raise InputError / hampton_llm.LLMFatal.
    """
    stat = {"skip": 0, "dikirim": 0, "cache_hit": 0, "llm": 0, "gagal": 0, "berubah": 0}
    enc, sep = dedup_nama.deteksi_format(input_path)
    total, first, rekap = 0, True, {}
    tmp = str(output_path) + ".tmp"
    reader = pd.read_csv(input_path, dtype=str, keep_default_na=False,
                         encoding=enc, sep=sep, chunksize=chunksize)
    for chunk in reader:
        if first:
            kolom = cek_kolom(chunk.columns)
        chunk.columns = kolom
        hasil = proses_chunk(chunk, exact, wildcard, llm, cache, tanpa_filter, stat)
        for h, n in hasil["hasil"].value_counts().items():
            rekap[h] = rekap.get(h, 0) + int(n)
        hasil.to_csv(tmp, mode="w" if first else "a", header=first, index=False,
                     encoding="utf-8-sig" if first else "utf-8")
        first = False
        total += len(chunk)
        if progress:
            progress(total)
    os.replace(tmp, output_path)
    return total, rekap, stat


def main():
    p = argparse.ArgumentParser(description="Hampton Dedup - validasi V/X & kombinasi")
    p.add_argument("input", nargs="?", help="File CSV input")
    p.add_argument("-o", "--output", help="File CSV output (default: <input>_hasil.csv)")
    p.add_argument("-m", "--matrix", default=str(MATRIX_DEFAULT), help="File matrix kombinasi")
    p.add_argument("--buat-matrix", action="store_true", help="Buat template matrix dummy")
    p.add_argument("--chunksize", type=int, default=100_000)
    g = p.add_argument_group("LLM (Qwen internal)")
    g.add_argument("--llm", action="store_true", help="Cek nama abu-abu ke LLM")
    g.add_argument("--llm-tes", action="store_true", help="Tes koneksi ke LLM lalu keluar")
    g.add_argument("--llm-tanpa-filter", action="store_true",
                   help="Kirim semua nama abu-abu, walau tidak mengubah hasil akhir")
    g.add_argument("--llm-cache", default=str(Path(__file__).with_name("llm_cache.jsonl")),
                   help="File cache jawaban LLM")
    g.add_argument("--llm-batch", type=int, default=hampton_llm.BATCH_SIZE)
    g.add_argument("--llm-workers", type=int, default=hampton_llm.WORKERS)
    args = p.parse_args()

    if args.llm_tes:
        try:
            ok = hampton_llm.LLMClient().tes_koneksi()
        except RuntimeError as e:
            sys.exit(str(e))
        sys.exit(0 if ok else "Tes LLM gagal: tidak ada jawaban valid.")

    llm = cache = None
    if args.llm:
        try:
            llm = hampton_llm.LLMClient(batch_size=args.llm_batch, workers=args.llm_workers)
        except RuntimeError as e:
            sys.exit(str(e))
        cache = hampton_llm.Cache(Path(args.llm_cache))
        print(f"LLM aktif: {llm.model} @ {llm.url} (cache: {len(cache.data):,} pasangan)")

    if args.buat_matrix:
        path = Path(args.matrix)
        if path.exists():
            sys.exit(f"{path} sudah ada, tidak ditimpa. Hapus/rename dulu jika ingin membuat ulang.")
        buat_matrix(path)
        return
    if not args.input:
        p.error("file input wajib diisi (atau gunakan --buat-matrix)")

    exact, wildcard = muat_matrix(Path(args.matrix))
    output = args.output or re.sub(r"\.csv$", "", args.input, flags=re.I) + "_hasil.csv"
    print(f"Matrix: {args.matrix} ({len(exact)} kombinasi, {len(wildcard)} wildcard)")
    print(f"Urutan kombinasi: {' | '.join(URUTAN_KOMBINASI)}")

    t0 = time.time()
    try:
        total, rekap, stat = proses_file(
            args.input, output, exact, wildcard, llm, cache, args.llm_tanpa_filter,
            args.chunksize,
            progress=lambda n: print(f"  {n:,} baris diproses ({time.time() - t0:.1f} detik)"))
    except InputError as e:
        sys.exit(str(e))
    except hampton_llm.LLMFatal as e:
        sys.exit(f"\nLLM error: {e}\nProses dihentikan, file output TIDAK dibuat. "
                 f"Jawaban LLM yang sudah didapat tersimpan di cache, jadi aman dijalankan ulang.")

    print(f"\nSelesai: {total:,} baris dalam {time.time() - t0:.1f} detik")
    for h in ["Match", "Suspect", "Not Match", TIDAK_ADA_DI_MATRIX]:
        if h in rekap:
            print(f"  {h}: {rekap[h]:,}")
    if llm is not None:
        print("\nRingkasan LLM (per pengecekan nama):")
        print(f"  Abu-abu tapi tidak mengubah hasil (skip) : {stat['skip']:,}")
        print(f"  Dikirim ke LLM                           : {stat['dikirim']:,}"
              f" (dari cache: {stat['cache_hit']:,})")
        print(f"  Dijawab LLM                              : {stat['llm']:,}"
              f" (berbeda dari aturan: {stat['berubah']:,})")
        print(f"  Gagal -> pakai hasil aturan              : {stat['gagal']:,}")
        print(f"  Request: {llm.statistik['request']:,}, request gagal: {llm.statistik['gagal']:,}")
    print(f"Output: {output}")


if __name__ == "__main__":
    main()

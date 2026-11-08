"""Tes regresi Hampton Dedup. Jalankan setiap kali aturan diubah:  python test_hampton.py"""
import hampton_dedup as h
import hampton_llm
from dedup_nama import bandingkan

# (nama 1, nama 2, V/X yang diharapkan, zona LLM yang diharapkan)
NAMA = [
    ("Budi Santoso", "Budi Santoso", "V", "PASTI"),
    ("budi  santoso", "BUDI SANTOSO", "V", "PASTI"),
    ("H. Ahmad Fauzi, S.Kom", "Ahmad Fauzi", "V", "PASTI"),
    ("Soekarno Djojo", "Sukarno Joyo", "V", "PASTI"),
    ("Moch. Rizky Pratama", "Muhammad Rizky Pratama", "V", "PASTI"),
    ("Annisa Fitriani", "Anisa Fitriyani", "V", "PASTI"),
    ("Santoso Budi", "Budi Santoso", "V", "PASTI"),
    ("LIEM KWEI", "LIM KWEI", "V", "PASTI"),
    ("AISHA ALMANSOURI", "AISHA AL MANSOURI", "V", "PASTI"),
    ("M. Rizky Pratama", "Muhammad Rizky Pratama", "V", "ABU"),
    ("Bagas P", "Bagas Pamungkas", "V", "ABU"),
    ("Widya Galuh Pratiwi", "Widya G P", "V", "ABU"),
    ("Widya Galuh Pratiwi", "Widya GP", "V", "ABU"),
    ("Widya Galuh Prat", "Widya Galuh Pratiwi", "V", "ABU"),
    ("Widya G Prat", "Widya Galuh Pratiwi", "V", "ABU"),
    ("Rina Kartika Putri", "Rina Kartika", "V", "ABU"),
    ("H RODRIGUEZ", "HANS RODRIGUEZ", "V", "ABU"),
    ("GILANG SETYO SANTSO", "GILANG SETYO SANTOSO", "V", "ABU"),
    ("SUPRATO ADITYA", "ADITYA SUPRAPTO", "V", "ABU"),
    ("Yulianto", "Yulianti", "X", "ABU"),
    ("Andi Wijaya", "Andika Wijaya", "X", "ABU"),
    ("SUGENG DEDI DANI", "SUGENG DEDI DONI", "X", "ABU"),
    ("Andreas Sutanto JR", "Andreas Sutanto SR", "X", "PASTI"),
    ("Budi Santoso", "Agus Salim", "X", "PASTI"),
    ("Bagas P", "Bagas Santoso", "X", "PASTI"),
    ("Dewi", "Dewi Lestari", "X", "PASTI"),
    ("YOUSSEF AL MANSOURI", "YOUSSEF AL", "X", "PASTI"),
    ("UNKNOWN", "UNKNOWN", "X", "PASTI"),
    ("Budi Santoso", "", "X", "PASTI"),
]

# (nilai kiri, nilai kanan, jenis, V/X yang diharapkan)
EXACT = [
    ("12.345.678.9-012.000", "123456789012000", "angka", "V"),
    ("00.000.000.0-000.000", "000000000000000", "angka", "X"),   # NPWP dummy
    ("0000000000000000", "0000000000000000", "angka", "X"),       # KTP dummy
    ("9999999999999999", "9999999999999999", "angka", "X"),
    ("123456789012000", "0123456789012000", "angka", "X"),        # NPWP 15 vs 16 digit
    ("+62 812-3456-7890", "081234567890", "hp", "V"),
    ("0062 812 3456 7890", "081234567890", "hp", "V"),
    ("81234567890", "6281234567890", "hp", "V"),
    ("00000000", "0000", "hp", "X"),
    ("Budi.Santoso@Gmail.com ", "budi.santoso@gmail.com", "email", "V"),
    ("noemail@gmail.com", "NOEMAIL@gmail.com", "email", "X"),     # email dummy
    ("12/05/1990", "1990-05-12", "tanggal", "V"),
    ("19900512", "12-05-1990", "tanggal", "V"),
    ("12 Jan 1985", "1985-01-12", "tanggal", "V"),
    ("32874", "1990-01-01", "tanggal", "V"),                      # nomor seri Excel
    ("1990-01-01 00:00:00", "01.01.1990", "tanggal", "V"),
    ("31/02/1990", "31/02/1990", "tanggal", "X"),                 # tanggal tidak ada
    ("1900-01-01", "01/01/1900", "tanggal", "X"),                 # tanggal dummy
    ("idd5 ", "IDD5", "id", "V"),
    ("-", "-", "id", "X"),
]

gagal = []
for a, b, harap, harap_zona in NAMA:
    hasil, alasan = bandingkan(a, b, "Nama")
    zona = hampton_llm.zona(hasil, alasan, h.skor_nama(a, b))
    if (hasil, zona) != (harap, harap_zona):
        gagal.append(f"NAMA  {a!r} vs {b!r}: dapat {hasil}/{zona}, harap {harap}/{harap_zona} ({alasan})")
for a, b, jenis, harap in EXACT:
    hasil = h.validasi_exact(a, b, jenis)
    if hasil != harap:
        gagal.append(f"{jenis.upper():6}{a!r} vs {b!r}: dapat {hasil}, harap {harap}")
assert h.norm_tanggal("1990") is None, "tahun saja tidak boleh dibaca sebagai tanggal Excel"

# Jalur LLM: keputusan & alasan LLM menimpa _validasi dan _keterangan, aturan asli tetap tersimpan.
import pandas as pd


class CachePalsu:
    def get(self, key):
        return ("V", "tinggi", "kemungkinan typo, kata lain cocok")


baris = {f"{f['kolom']}{s}": "x" for f in h.FIELDS for s in (h.SUFFIX_KIRI, h.SUFFIX_KANAN)}
baris.update({"cfna1_left": "SUGENG DEDI DANI", "cfna1_right": "SUGENG DEDI DONI",
              "cfbirth_left": "1990-01-01", "cfbirth_right": "1990-01-01"})
stat = dict.fromkeys(["skip", "dikirim", "cache_hit", "llm", "gagal", "berubah"], 0)
out = h.proses_chunk(pd.DataFrame([baris]), {}, [], llm=object(), cache=CachePalsu(),
                     tanpa_filter=True, stat=stat).iloc[0]
cek = {"cfna1_validasi": "V", "cfna1_keterangan": "[LLM] kemungkinan typo, kata lain cocok",
       "cfna1_sumber": "LLM", "cfna1_validasi_rule": "X", "cfna1_validasi_llm": "V"}
for kolom, harap in cek.items():
    if out[kolom] != harap:
        gagal.append(f"LLM   {kolom}: dapat {out[kolom]!r}, harap {harap!r}")
if "kata pendek" not in out["cfna1_keterangan_rule"]:
    gagal.append(f"LLM   cfna1_keterangan_rule hilang: {out['cfna1_keterangan_rule']!r}")
if out["kombinasi"][0] != "V":
    gagal.append(f"LLM   kombinasi tidak memakai hasil final: {out['kombinasi']}")
# cfcif# ikut di output tapi tanpa kolom validasi, dan kombinasi tetap 8 posisi
if "cfcif#_validasi" in out.index or "cfcif#_left" not in out.index or len(out["kombinasi"]) != 8:
    gagal.append("CFCIF cfcif# harus ada tanpa _validasi, kombinasi 8 karakter")

# kolom tambahan tetap di posisi aslinya; hasil disisipkan setelah kolom _right
kolom = list(baris)
kolom.insert(kolom.index("cfna1_right") + 1, "cabang")
kolom.append("catatan_ops")
df_urut = pd.DataFrame([{**baris, "cabang": "JKT", "catatan_ops": "-"}])[kolom]
hasil_kolom = list(h.proses_chunk(df_urut, {}, []).columns)
harap = ["cfna1_right", "cfna1_validasi", "cfna1_skor", "cfna1_keterangan", "cabang", "cfbirth_left"]
i = hasil_kolom.index("cfna1_right")
if hasil_kolom[i:i + 6] != harap or hasil_kolom[-3:] != ["catatan_ops", "kombinasi", "hasil"] \
        or hasil_kolom[:2] != ["cfcif#_left", "cfcif#_right"]:
    gagal.append(f"URUTAN kolom output salah: {hasil_kolom}")

print("\n".join(gagal) or f"Semua {len(NAMA) + len(EXACT) + 1 + len(cek) + 4} tes lulus.")
raise SystemExit(1 if gagal else 0)

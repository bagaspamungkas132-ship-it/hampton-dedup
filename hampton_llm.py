"""
Hampton Dedup - Pengecekan nama zona abu-abu menggunakan LLM (Qwen internal)
=============================================================================
Dipakai oleh hampton_dedup.py saat dijalankan dengan --llm.

Setting koneksi lewat environment variable (API key TIDAK ditulis di script):
    HAMPTON_LLM_URL      base URL OpenAI-compatible, mis. http://10.0.0.5:8000/v1
    HAMPTON_LLM_API_KEY  API key
    HAMPTON_LLM_MODEL    nama model di server (default: QWEN3.8-27B)

Alur:
    1. Hasil aturan (dedup_nama.py) diklasifikasi: PASTI atau ABU-ABU
    2. Baris ABU-ABU hanya dikirim jika keputusan nama bisa mengubah hasil akhir
       (Match/Suspect/Not Match) menurut matrix
    3. Pasangan unik dikirim per batch, paralel, dengan cache di file .jsonl
    4. Jika LLM gagal / jawaban tidak valid -> kembali ke hasil aturan
"""

import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Konfigurasi
# ---------------------------------------------------------------------------
ENV_URL = "HAMPTON_LLM_URL"
ENV_KEY = "HAMPTON_LLM_API_KEY"
ENV_MODEL = "HAMPTON_LLM_MODEL"
MODEL_DEFAULT = "QWEN3.8-27B"

SKOR_MIN_ABU = 70        # X dengan skor >= ini dianggap abu-abu
BATCH_SIZE = 20          # pasangan per request
WORKERS = 4              # request paralel
TIMEOUT = 120            # detik per request
MAX_RETRY = 3
MAX_TOKENS = 4096
MAX_GAGAL_BERUNTUN = 5   # batch gagal total berturut-turut -> proses dihentikan

# Kata kunci di keterangan aturan untuk menentukan zona abu-abu
KATA_V_LONGGAR = ["terpotong", "sebagian, salah satu", "typo", "inisial"]
KATA_X_BERISIKO = ["huruf terakhir", "kata pendek"]
KATA_X_PASTI = ["kosong", "generasi", "tidak valid"]

SUMBER_RULE = "RULE"
SUMBER_LLM = "LLM"
SUMBER_SKIP = "RULE (abu-abu, tidak mengubah hasil)"
SUMBER_GAGAL = "RULE (LLM gagal)"

SYSTEM_PROMPT = """Kamu adalah validator data nasabah bank di Indonesia. Tugasmu menilai apakah dua penulisan nama merujuk ke ORANG YANG SAMA (V) atau BERBEDA (X).

Pertimbangkan konvensi nama di Indonesia:
- Ejaan lama vs baru: oe=u, dj=j, tj=c, j=y (Soekarno=Sukarno, Djojo=Joyo)
- Gelar & sapaan tidak berpengaruh: H., Hj., Dr., Drs., Ir., S.Kom, S.E., MM, Bpk, Ibu, Mr, Mrs
- Variasi penulisan: Muhammad/Mohammad/Moch./M., Nur/Noer, Abdul/Abd., Siti/Sitti
- Inisial & singkatan wajar (Bagas P = Bagas Pamungkas) bila kata lain cocok
- Nama terpotong karena batas panjang kolom wajar (Widya Galuh Prat = Widya Galuh Pratiwi)
- Typo ketik 1 huruf wajar pada kata panjang (Santso = Santoso)
- Akhiran berbeda sering berarti nama/gender berbeda: -to vs -ti (Yulianto vs Yulianti), -a vs -i (Hendra vs Hendri)
- Nama pendek yang beda 1 huruf biasanya orang berbeda (Dani vs Doni, Devi vs Desi)
- Jr/Sr atau II/III yang berbeda = orang berbeda
- Nama Bali: I/Ni + urutan lahir (Wayan, Made, Nyoman, Ketut, Putu, Gede, Kadek, Komang); urutan lahir berbeda = orang berbeda
- Nama Tionghoa-Indonesia: Lie/Lee/Li, Liem/Lim, Oei/Oey; urutan marga bisa terbalik
- Nama Arab: bin/binti/al/el
- Satu kata saja yang cocok (mis. hanya nama depan) TIDAK cukup untuk V

Kamu juga diberi hasil & alasan dari aturan otomatis sebagai bahan pertimbangan; kamu boleh setuju atau tidak.

Jawab HANYA dengan JSON array, tanpa teks lain, format:
[{"id": <id>, "keputusan": "V" atau "X", "yakin": "tinggi"|"sedang"|"rendah", "alasan": "<maks 20 kata, bahasa Indonesia>"}]
Satu objek untuk setiap pasangan, id sama dengan input."""


# ---------------------------------------------------------------------------
# Zona
# ---------------------------------------------------------------------------
def zona(hasil_rule: str, keterangan: str, skor) -> str:
    """Return 'ABU' atau 'PASTI'."""
    k = keterangan or ""
    if hasil_rule == "V":
        return "ABU" if any(w in k for w in KATA_V_LONGGAR) else "PASTI"
    if any(w in k for w in KATA_X_PASTI):
        return "PASTI"
    if any(w in k for w in KATA_X_BERISIKO):
        return "ABU"
    try:
        return "ABU" if float(skor) >= SKOR_MIN_ABU else "PASTI"
    except (TypeError, ValueError):
        return "PASTI"


# ---------------------------------------------------------------------------
# Klien LLM
# ---------------------------------------------------------------------------
class LLMFatal(RuntimeError):
    """Kesalahan konfigurasi (key/URL/model salah) -> hentikan proses."""


class LLMClient:
    def __init__(self, batch_size=BATCH_SIZE, workers=WORKERS):
        url = os.environ.get(ENV_URL, "").strip()
        if not url:
            raise RuntimeError(
                f"Environment variable {ENV_URL} belum di-set. "
                f"Lihat cara setting di README / bagian atas hampton_llm.py.")
        url = url.rstrip("/")
        if not url.endswith("/chat/completions"):
            url += "/chat/completions"
        self.url = url
        self.base = url[: -len("/chat/completions")]
        self.key = os.environ.get(ENV_KEY, "").strip()
        self.model = os.environ.get(ENV_MODEL, "").strip() or MODEL_DEFAULT
        self.batch_size = batch_size
        self.workers = workers
        self.kirim_no_think = True      # chat_template_kwargs untuk mematikan mode thinking Qwen3
        self.session = requests.Session()
        if self.key:
            self.session.headers["Authorization"] = f"Bearer {self.key}"
        self.statistik = {"request": 0, "gagal": 0, "detik": 0.0}
        self._gagal_beruntun = 0
        self._lock = threading.Lock()
        self.on_progress = None         # opsional: fungsi(batch_selesai, total_batch), dipakai web app

    # -- koneksi --------------------------------------------------------------
    def tes_koneksi(self):
        print(f"URL   : {self.url}")
        print(f"Model : {self.model}")
        print(f"APIKEY: {'(ada)' if self.key else '(kosong)'}")
        try:
            r = self.session.get(self.base + "/models", timeout=15)
            if r.ok:
                ids = [m.get("id") for m in r.json().get("data", [])]
                print(f"Model tersedia di server: {ids}")
                if ids and self.model not in ids:
                    print(f"PERINGATAN: model '{self.model}' tidak ada di daftar. "
                          f"Set {ENV_MODEL} ke salah satu nama di atas.")
            else:
                print(f"/models -> HTTP {r.status_code} (tidak masalah jika server tidak mendukung)")
        except requests.RequestException as e:
            print(f"/models gagal: {e}")
        contoh = [{"id": 1, "nama_1": "BAGAS P", "nama_2": "BAGAS PAMUNGKAS",
                   "hasil_aturan": "V", "alasan_aturan": "cocok dengan inisial"},
                  {"id": 2, "nama_1": "YULIANTO", "nama_2": "YULIANTI",
                   "hasil_aturan": "X", "alasan_aturan": "berbeda huruf terakhir"}]
        t0 = time.time()
        hasil = self._kirim_batch(contoh)
        print(f"Uji 2 pasangan ({time.time() - t0:.1f} detik):")
        for i, h in sorted(hasil.items()):
            print(f"  {contoh[i - 1]['nama_1']} vs {contoh[i - 1]['nama_2']} -> {h}")
        return bool(hasil)

    def _payload(self, items):
        body = {
            "model": self.model,
            "temperature": 0,
            "max_tokens": MAX_TOKENS,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(items, ensure_ascii=False) + "\n/no_think"},
            ],
        }
        if self.kirim_no_think:
            body["chat_template_kwargs"] = {"enable_thinking": False}
        return body

    def _kirim_batch(self, items):
        """Kirim 1 batch. Return {id: (keputusan, yakin, alasan)} untuk jawaban valid."""
        ids_valid = {it["id"] for it in items}
        for percobaan in range(1, MAX_RETRY + 1):
            t0 = time.time()
            try:
                r = self.session.post(self.url, json=self._payload(items), timeout=TIMEOUT)
                if r.status_code == 400 and self.kirim_no_think:
                    # server tidak mengenal chat_template_kwargs -> coba tanpa
                    self.kirim_no_think = False
                    continue
                if r.status_code in (401, 403):
                    raise LLMFatal(f"HTTP {r.status_code}: API key salah / tidak punya akses. "
                                   f"Cek {ENV_KEY}.")
                if r.status_code == 404:
                    raise LLMFatal(f"HTTP 404: URL atau nama model salah ({self.url}, "
                                   f"model={self.model}). Cek {ENV_URL} dan {ENV_MODEL}.")
                r.raise_for_status()
                teks = r.json()["choices"][0]["message"]["content"] or ""
                hasil = parse_jawaban(teks, ids_valid)
                with self._lock:
                    self.statistik["request"] += 1
                    self.statistik["detik"] += time.time() - t0
                if hasil:
                    return hasil
            except (requests.RequestException, KeyError, ValueError, IndexError):
                pass
            time.sleep(2 ** percobaan)
        with self._lock:
            self.statistik["gagal"] += 1
        return {}

    # -- proses banyak pasangan -----------------------------------------------
    def nilai(self, pasangan: list, progress=True, simpan=None) -> dict:
        """
        pasangan: list of dict {key, nama_1, nama_2, hasil_aturan, alasan_aturan}
        simpan  : fungsi dipanggil per batch selesai (mis. Cache.simpan)
        Return {key: (keputusan, yakin, alasan)}
        """
        batches = [pasangan[i:i + self.batch_size]
                   for i in range(0, len(pasangan), self.batch_size)]
        hasil, selesai, t0 = {}, 0, time.time()

        def kerja(batch):
            items = [{"id": i + 1, "nama_1": p["nama_1"], "nama_2": p["nama_2"],
                      "hasil_aturan": p["hasil_aturan"], "alasan_aturan": p["alasan_aturan"]}
                     for i, p in enumerate(batch)]
            if self._gagal_beruntun >= MAX_GAGAL_BERUNTUN:
                raise LLMFatal(f"{MAX_GAGAL_BERUNTUN} batch berturut-turut gagal total. "
                               f"Server LLM kemungkinan mati / tidak bisa dihubungi ({self.url}).")
            jawab = self._kirim_batch(items)
            with self._lock:
                self._gagal_beruntun = 0 if jawab else self._gagal_beruntun + 1
            # pasangan yang tidak terjawab (sebagian saja) dicoba ulang satu per satu
            sisa = [it for it in items if it["id"] not in jawab]
            if jawab and sisa and len(items) > 1:
                for it in sisa:
                    j = self._kirim_batch([dict(it, id=1)])
                    if 1 in j:
                        jawab[it["id"]] = j[1]
            return {batch[i - 1]["key"]: v for i, v in jawab.items()}

        with ThreadPoolExecutor(max_workers=self.workers) as ex:
            futures = [ex.submit(kerja, b) for b in batches]
            for f in as_completed(futures):
                try:
                    jawab = f.result()
                except LLMFatal:
                    for sisa in futures:
                        sisa.cancel()
                    raise
                hasil.update(jawab)
                if simpan and jawab:
                    simpan(jawab)
                selesai += 1
                if self.on_progress:
                    self.on_progress(selesai, len(batches))
                if progress and (selesai % 10 == 0 or selesai == len(batches)):
                    print(f"    LLM: {selesai}/{len(batches)} batch "
                          f"({time.time() - t0:.0f} detik)", flush=True)
        return hasil


def parse_jawaban(teks: str, ids_valid: set) -> dict:
    """Ambil JSON array dari jawaban LLM (buang blok <think> jika ada)."""
    teks = re.sub(r"<think>.*?</think>", "", teks, flags=re.S).strip()
    teks = re.sub(r"^```(?:json)?|```$", "", teks.strip(), flags=re.M).strip()
    awal, akhir = teks.find("["), teks.rfind("]")
    if awal < 0 or akhir < 0:
        return {}
    try:
        data = json.loads(teks[awal:akhir + 1])
    except json.JSONDecodeError:
        return {}
    hasil = {}
    for d in data if isinstance(data, list) else []:
        try:
            i = int(d.get("id"))
        except (TypeError, ValueError, AttributeError):
            continue
        kep = str(d.get("keputusan", "")).strip().upper()
        if i in ids_valid and kep in ("V", "X"):
            yakin = str(d.get("yakin", "")).strip().lower()
            hasil[i] = (kep, yakin, str(d.get("alasan", "")).strip())
    return hasil


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------
def kunci(a, b) -> str:
    """Kunci cache: tidak peduli urutan kiri/kanan & spasi/huruf besar."""
    x = re.sub(r"\s+", " ", str(a)).strip().upper()
    y = re.sub(r"\s+", " ", str(b)).strip().upper()
    return "||".join(sorted([x, y]))


class Cache:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.data = {}
        if self.path.exists():
            with open(self.path, encoding="utf-8") as f:
                for baris in f:
                    try:
                        d = json.loads(baris)
                        self.data[d["key"]] = (d["keputusan"], d.get("yakin", ""), d.get("alasan", ""))
                    except (json.JSONDecodeError, KeyError):
                        continue

    def get(self, k):
        return self.data.get(k)

    def simpan(self, hasil: dict):
        with open(self.path, "a", encoding="utf-8") as f:
            for k, (kep, yakin, alasan) in hasil.items():
                self.data[k] = (kep, yakin, alasan)
                f.write(json.dumps({"key": k, "keputusan": kep, "yakin": yakin,
                                    "alasan": alasan}, ensure_ascii=False) + "\n")

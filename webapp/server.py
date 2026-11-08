"""
Hampton Dedup - Web App (backend FastAPI)
=========================================
Tim ops login (Active Directory/LDAP) -> upload CSV -> proses berjalan di server
-> download hasil. Logika dedup memakai hampton_dedup.py (sama dengan CLI).

Jalankan (dari folder project):
    uvicorn webapp.server:app --host 0.0.0.0 --port 8000

Konfigurasi lewat environment variable (lihat webapp/README.md):
    HAMPTON_LDAP_URL       mis. ldaps://dc01.kantor.local
    HAMPTON_LDAP_DOMAIN    mis. KANTOR  (login KANTOR\\user) atau kantor.co.id (user@kantor.co.id)
    HAMPTON_LDAP_BASE_DN   mis. DC=kantor,DC=local         (wajib jika pakai GROUP)
    HAMPTON_LDAP_GROUP     opsional: DN grup yang boleh akses, mis. CN=Ops-Hampton,OU=Groups,DC=kantor,DC=local
    HAMPTON_DEV_LOGIN=1    HANYA untuk uji lokal: login tanpa LDAP
    HAMPTON_RETENSI_JAM    default 24
    HAMPTON_MAX_UPLOAD_MB  default 500
    HAMPTON_COOKIE_SECURE  1 jika aplikasi diakses lewat https
    HAMPTON_LLM_URL / _API_KEY / _MODEL   (opsional) aktifkan pengecekan LLM
"""

import logging
import os
import queue
import re
import secrets
import shutil
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import pandas as pd
from fastapi import Cookie, Depends, FastAPI, File, Form, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import dedup_nama  # noqa: E402
import hampton_dedup  # noqa: E402
import hampton_llm  # noqa: E402

# ---------------------------------------------------------------------------
# Konfigurasi
# ---------------------------------------------------------------------------
ENV = os.environ.get
DATA_DIR = Path(ENV("HAMPTON_DATA_DIR", str(Path(__file__).with_name("jobs"))))
RETENSI_DETIK = float(ENV("HAMPTON_RETENSI_JAM", "24")) * 3600
MAX_UPLOAD = int(ENV("HAMPTON_MAX_UPLOAD_MB", "500")) * 1024 * 1024
COOKIE_SECURE = ENV("HAMPTON_COOKIE_SECURE", "0") == "1"
DEV_LOGIN = ENV("HAMPTON_DEV_LOGIN", "0") == "1"
SESI_DETIK = 8 * 3600
LLM_TERSEDIA = bool(ENV(hampton_llm.ENV_URL))

audit = logging.getLogger("hampton.audit")
audit.setLevel(logging.INFO)
_h = logging.FileHandler(ENV("HAMPTON_AUDIT_LOG", str(Path(__file__).with_name("audit.log"))),
                         encoding="utf-8")
_h.setFormatter(logging.Formatter("%(asctime)s\t%(message)s"))
audit.addHandler(_h)

# ---------------------------------------------------------------------------
# Login (Active Directory / LDAP)
# ---------------------------------------------------------------------------
USERNAME_OK = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def cek_ldap(username: str, password: str) -> bool:
    if DEV_LOGIN:
        return True
    from ldap3 import NONE, SUBTREE, Connection, Server
    from ldap3.core.exceptions import LDAPException
    from ldap3.utils.conv import escape_filter_chars

    url, domain = ENV("HAMPTON_LDAP_URL"), ENV("HAMPTON_LDAP_DOMAIN", "")
    if not url:
        raise HTTPException(500, "Login belum dikonfigurasi (HAMPTON_LDAP_URL kosong).")
    akun = f"{username}@{domain}" if "." in domain else f"{domain}\\{username}" if domain else username
    try:
        conn = Connection(Server(url, get_info=NONE, connect_timeout=5), user=akun,
                          password=password, receive_timeout=10)
        if not conn.bind():
            return False
        grup = ENV("HAMPTON_LDAP_GROUP")
        if grup:  # hanya anggota grup ini (termasuk grup bertingkat) yang boleh masuk
            conn.search(ENV("HAMPTON_LDAP_BASE_DN", ""),
                        f"(&(sAMAccountName={escape_filter_chars(username)})"
                        f"(memberOf:1.2.840.113556.1.4.1941:={escape_filter_chars(grup)}))",
                        SUBTREE, attributes=["sAMAccountName"])
            ok = bool(conn.entries)
        else:
            ok = True
        conn.unbind()
        return ok
    except LDAPException:
        raise HTTPException(503, "Server login (LDAP) tidak bisa dihubungi. Coba lagi nanti.")


SESI: dict = {}          # token -> {"user", "exp"}
SESI_LOCK = threading.Lock()


def user_aktif(hampton_sesi: str | None = Cookie(default=None)) -> str:
    with SESI_LOCK:
        s = SESI.get(hampton_sesi or "")
        if not s or s["exp"] < time.time():
            SESI.pop(hampton_sesi or "", None)
            raise HTTPException(401, "Sesi berakhir, silakan login kembali.")
        return s["user"]


# ---------------------------------------------------------------------------
# Job & antrian
# ---------------------------------------------------------------------------
JOBS: dict = {}
JOBS_LOCK = threading.Lock()
ANTRIAN: "queue.Queue[str]" = queue.Queue()


def info_job(j: dict) -> dict:
    return {k: j[k] for k in ("id", "nama_file", "dibuat", "status", "baris_selesai",
                              "baris_total", "llm_selesai", "llm_total", "pakai_llm",
                              "rekap", "stat_llm", "error", "selesai", "dihapus_pada")}


def proses_job(job_id: str):
    with JOBS_LOCK:
        j = JOBS[job_id]
        j["status"] = "proses"
    folder = DATA_DIR / job_id
    try:
        exact, wildcard = hampton_dedup.muat_matrix(hampton_dedup.MATRIX_DEFAULT)
        llm = cache = None
        if j["pakai_llm"]:
            llm = hampton_llm.LLMClient()
            llm.on_progress = lambda a, b: j.update(llm_selesai=a, llm_total=b)
            cache = hampton_llm.Cache(folder / "llm_cache.jsonl")   # per proses, ikut terhapus
        total, rekap, stat = hampton_dedup.proses_file(
            folder / "input.csv", folder / "hasil.csv", exact, wildcard, llm, cache,
            chunksize=20_000,             # lebih kecil dari CLI supaya progres di layar sering bergerak
            progress=lambda n: j.update(baris_selesai=n))
        j.update(status="selesai", baris_selesai=total, rekap=rekap,
                 stat_llm=stat if llm else None)
        audit.info(f"{j['user']}\tselesai\t{job_id}\t{j['nama_file']}\t{total} baris\t{rekap}")
    except (hampton_dedup.InputError, hampton_llm.LLMFatal, RuntimeError, ValueError) as e:
        j.update(status="gagal", error=str(e))
        audit.info(f"{j['user']}\tgagal\t{job_id}\t{j['nama_file']}\t{e}")
    except Exception as e:  # jangan sampai worker mati; detail teknis hanya ke log server
        j.update(status="gagal", error="Terjadi kesalahan saat memproses file.")
        logging.exception("job %s gagal", job_id)
        audit.info(f"{j['user']}\tgagal\t{job_id}\t{j['nama_file']}\t{type(e).__name__}")
    finally:
        (folder / "input.csv").unlink(missing_ok=True)       # data input tidak disimpan
        for f in folder.glob("*.tmp"):
            f.unlink(missing_ok=True)
        if j["status"] == "gagal":
            shutil.rmtree(folder, ignore_errors=True)
        j["selesai"] = time.time()
        j["dihapus_pada"] = j["selesai"] + RETENSI_DETIK


def worker():
    while True:
        job_id = ANTRIAN.get()
        try:
            proses_job(job_id)
        finally:
            ANTRIAN.task_done()


def pembersih():
    while True:
        sekarang = time.time()
        with JOBS_LOCK:
            kadaluarsa = [i for i, j in JOBS.items() if j["dihapus_pada"] and j["dihapus_pada"] < sekarang]
            for i in kadaluarsa:
                JOBS.pop(i)
        for i in kadaluarsa:
            shutil.rmtree(DATA_DIR / i, ignore_errors=True)
            audit.info(f"-\tdihapus\t{i}")
        with SESI_LOCK:
            for t in [t for t, s in SESI.items() if s["exp"] < sekarang]:
                SESI.pop(t)
        time.sleep(300)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(_app):
    # status job hanya di memori: sisa file dari run sebelumnya tidak bisa diakses -> hapus
    shutil.rmtree(DATA_DIR, ignore_errors=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=pembersih, daemon=True).start()
    if DEV_LOGIN:
        logging.warning("HAMPTON_DEV_LOGIN aktif: login TANPA LDAP. Jangan dipakai di produksi!")
    yield


app = FastAPI(title="Hampton Dedup", docs_url=None, redoc_url=None, openapi_url=None,
              lifespan=lifespan)


class Login(BaseModel):
    username: str
    password: str


@app.post("/api/login")
def login(data: Login, response: Response):
    username = data.username.strip()
    if "\\" in username:                      # terima juga format DOMAIN\user
        username = username.split("\\", 1)[1]
    if not USERNAME_OK.match(username) or not data.password:   # password kosong = anonymous bind
        raise HTTPException(401, "Username atau password salah.")
    if not cek_ldap(username, data.password):
        audit.info(f"{username}\tlogin gagal")
        raise HTTPException(401, "Username atau password salah, atau akun tidak punya akses.")
    token = secrets.token_urlsafe(32)
    with SESI_LOCK:
        SESI[token] = {"user": username, "exp": time.time() + SESI_DETIK}
    response.set_cookie("hampton_sesi", token, max_age=SESI_DETIK, httponly=True,
                        samesite="strict", secure=COOKIE_SECURE)
    audit.info(f"{username}\tlogin")
    return {"user": username}


@app.post("/api/logout")
def logout(response: Response, hampton_sesi: str | None = Cookie(default=None)):
    with SESI_LOCK:
        SESI.pop(hampton_sesi or "", None)
    response.delete_cookie("hampton_sesi")
    return {"ok": True}


@app.get("/api/me")
def me(user: str = Depends(user_aktif)):
    return {"user": user, "llm_tersedia": LLM_TERSEDIA, "dev_login": DEV_LOGIN,
            "retensi_jam": RETENSI_DETIK / 3600, "max_upload_mb": MAX_UPLOAD // (1024 * 1024),
            "kolom_wajib": hampton_dedup.kolom_input()}


@app.post("/api/jobs")
async def upload(file: UploadFile = File(...), pakai_llm: bool = Form(False),
                 user: str = Depends(user_aktif)):
    if not (file.filename or "").lower().endswith(".csv"):
        raise HTTPException(400, "File harus berformat .csv")
    if pakai_llm and not LLM_TERSEDIA:
        raise HTTPException(400, "Pengecekan LLM belum dikonfigurasi di server.")
    job_id = uuid.uuid4().hex
    folder = DATA_DIR / job_id
    folder.mkdir(parents=True)
    path, ukuran = folder / "input.csv", 0
    try:
        with open(path, "wb") as f:
            while blok := await file.read(1024 * 1024):
                ukuran += len(blok)
                if ukuran > MAX_UPLOAD:
                    raise HTTPException(413, f"File lebih dari {MAX_UPLOAD // (1024 * 1024)} MB.")
                f.write(blok)
        enc, sep = dedup_nama.deteksi_format(str(path))
        kolom = pd.read_csv(path, nrows=0, encoding=enc, sep=sep).columns
        hampton_dedup.cek_kolom(kolom)
        with open(path, "rb") as f:
            baris_total = max(sum(1 for _ in f) - 1, 0)
        if baris_total == 0:
            raise HTTPException(400, "File tidak berisi data.")
    except hampton_dedup.InputError as e:
        shutil.rmtree(folder, ignore_errors=True)
        raise HTTPException(400, str(e))
    except HTTPException:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise HTTPException(400, "File CSV tidak bisa dibaca.")

    nama = Path(file.filename).name[:200]
    with JOBS_LOCK:
        JOBS[job_id] = {"id": job_id, "user": user, "nama_file": nama, "dibuat": time.time(),
                        "status": "antri", "baris_selesai": 0, "baris_total": baris_total,
                        "llm_selesai": 0, "llm_total": 0, "pakai_llm": pakai_llm, "rekap": None,
                        "stat_llm": None, "error": None, "selesai": None, "dihapus_pada": None}
    ANTRIAN.put(job_id)
    audit.info(f"{user}\tupload\t{job_id}\t{nama}\t{baris_total} baris\tllm={pakai_llm}")
    return info_job(JOBS[job_id])


@app.get("/api/jobs")
def daftar_job(user: str = Depends(user_aktif)):
    with JOBS_LOCK:
        milik = [info_job(j) for j in JOBS.values() if j["user"] == user]
    return sorted(milik, key=lambda j: j["dibuat"], reverse=True)


@app.get("/api/jobs/{job_id}/download")
def download(job_id: str, user: str = Depends(user_aktif)):
    j = JOBS.get(job_id)
    if not j or j["user"] != user:
        raise HTTPException(404, "Proses tidak ditemukan.")
    path = DATA_DIR / job_id / "hasil.csv"
    if j["status"] != "selesai" or not path.exists():
        raise HTTPException(409, "Hasil belum tersedia atau sudah dihapus.")
    audit.info(f"{user}\tdownload\t{job_id}\t{j['nama_file']}")
    return FileResponse(path, media_type="text/csv",
                        filename=re.sub(r"\.csv$", "", j["nama_file"], flags=re.I) + "_hasil.csv")


# Tampilan React hasil build (npm run build) dilayani dari sini
DIST = Path(__file__).with_name("frontend") / "dist"
if DIST.exists():
    app.mount("/", StaticFiles(directory=DIST, html=True), name="frontend")

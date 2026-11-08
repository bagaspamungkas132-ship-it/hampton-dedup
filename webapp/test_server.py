"""Tes backend web app. Jalankan dari folder project:  python webapp/test_server.py"""
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ["HAMPTON_DEV_LOGIN"] = "1"
os.environ["HAMPTON_DATA_DIR"] = tempfile.mkdtemp(prefix="hampton_test_")
os.environ["HAMPTON_AUDIT_LOG"] = os.path.join(os.environ["HAMPTON_DATA_DIR"] + "_audit.log")
os.environ.pop("HAMPTON_LLM_URL", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402
from webapp import server  # noqa: E402

SAMPLE = Path(__file__).resolve().parent.parent / "sample" / "contoh_hampton.csv"


def login(c, user):
    r = c.post("/api/login", json={"username": user, "password": "x"})
    assert r.status_code == 200, r.text


with TestClient(server.app) as a, TestClient(server.app) as b:
    # tanpa login & input login berbahaya ditolak
    assert a.get("/api/jobs").status_code == 401
    assert a.post("/api/login", json={"username": "ops1", "password": ""}).status_code == 401
    assert a.post("/api/login", json={"username": "*)(uid=*", "password": "x"}).status_code == 401

    login(a, "ops1")
    login(b, "OCBC\\ops2")                       # format DOMAIN\user diterima
    assert b.get("/api/me").json()["user"] == "ops2"

    # file salah ditolak sebelum masuk antrian
    r = a.post("/api/jobs", files={"file": ("data.txt", b"a,b\n1,2\n")})
    assert r.status_code == 400, r.text
    r = a.post("/api/jobs", files={"file": ("salah.csv", b"pair_key,name_a\n1,BUDI\n")})
    assert r.status_code == 400 and "Kolom tidak ditemukan" in r.json()["detail"], r.text

    # upload -> proses -> selesai
    r = a.post("/api/jobs", files={"file": ("contoh.csv", SAMPLE.read_bytes(), "text/csv")})
    assert r.status_code == 200, r.text
    job = r.json()
    assert job["baris_total"] == 7
    for _ in range(100):
        job = next(j for j in a.get("/api/jobs").json() if j["id"] == job["id"])
        if job["status"] in ("selesai", "gagal"):
            break
        time.sleep(0.1)
    assert job["status"] == "selesai", job
    assert job["rekap"] == {"Match": 1, "Suspect": 6}, job["rekap"]

    # input sudah dihapus, hasil ada & bisa didownload pemiliknya saja
    folder = Path(os.environ["HAMPTON_DATA_DIR"]) / job["id"]
    assert not (folder / "input.csv").exists()
    r = a.get(f"/api/jobs/{job['id']}/download")
    assert r.status_code == 200 and "contoh_hasil.csv" in r.headers["content-disposition"]
    assert r.text.lstrip("﻿").startswith("pair_id,cfcif#_left")
    assert b.get(f"/api/jobs/{job['id']}/download").status_code == 404
    assert b.get("/api/jobs").json() == []

    # logout mengakhiri sesi
    a.post("/api/logout")
    assert a.get("/api/jobs").status_code == 401

print("Semua tes backend lulus.")

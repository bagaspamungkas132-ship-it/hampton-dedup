# Hampton Dedup – Web App

Tim ops login dengan akun Windows kantor, upload CSV, lalu download hasil.
Semua proses berjalan di server; data tidak disimpan di laptop siapa pun.

```
Browser tim ops ──▶ FastAPI (webapp/server.py) ──▶ hampton_dedup.py ──▶ Qwen internal (opsional)
                         │
                         └─ tampilan React (webapp/frontend/dist)
```

## 1. Persiapan di server (sekali saja)

Buka folder project di VS Code (Remote-SSH ke server), lalu di terminal:

```bash
# Python (lewat mirror kantor, sesuaikan URL-nya)
pip install -r webapp/requirements.txt --index-url https://MIRROR-PYPI-KANTOR/simple

# Frontend: install & build tampilan React
cd webapp/frontend
npm config set registry https://MIRROR-NPM-KANTOR/     # jika memakai mirror
npm install
npm run build
cd ../..
```

Library yang perlu tersedia di mirror:
- **Python:** pandas, rapidfuzz, requests, fastapi, uvicorn, python-multipart, ldap3
- **npm:** react, react-dom, vite, @vitejs/plugin-react

## 2. Konfigurasi (environment variable)

Minta detail Active Directory ke tim IT:

| Variabel | Contoh | Keterangan |
|---|---|---|
| `HAMPTON_LDAP_URL` | `ldaps://dc01.kantor.local` | **Wajib.** Alamat server AD (pakai `ldaps://`) |
| `HAMPTON_LDAP_DOMAIN` | `KANTOR` atau `kantor.co.id` | Login dibentuk jadi `KANTOR\user` atau `user@kantor.co.id` |
| `HAMPTON_LDAP_GROUP` | `CN=Ops-Hampton,OU=Groups,DC=kantor,DC=local` | Opsional, tapi **disarankan**: hanya anggota grup ini yang boleh masuk |
| `HAMPTON_LDAP_BASE_DN` | `DC=kantor,DC=local` | Wajib jika memakai `HAMPTON_LDAP_GROUP` |
| `HAMPTON_COOKIE_SECURE` | `1` | Set `1` jika aplikasi diakses lewat **https** |
| `HAMPTON_RETENSI_JAM` | `24` | File hasil dihapus otomatis setelah N jam |
| `HAMPTON_MAX_UPLOAD_MB` | `500` | Batas ukuran upload |
| `HAMPTON_LLM_URL` / `HAMPTON_LLM_API_KEY` / `HAMPTON_LLM_MODEL` | | Opsional: aktifkan pengecekan AI (Qwen) |

Contoh (Linux, bash):
```bash
export HAMPTON_LDAP_URL="ldaps://dc01.kantor.local"
export HAMPTON_LDAP_DOMAIN="KANTOR"
export HAMPTON_LDAP_BASE_DN="DC=kantor,DC=local"
export HAMPTON_LDAP_GROUP="CN=Ops-Hampton,OU=Groups,DC=kantor,DC=local"
```

## 3. Menjalankan

Dari folder project (bukan dari folder `webapp`):
```bash
uvicorn webapp.server:app --host 0.0.0.0 --port 8000
```
Tim ops membuka `http://NAMA-SERVER:8000` di browser.

Penting: **jalankan hanya 1 proses uvicorn** (jangan pakai `--workers`). Status proses dan
sesi login disimpan di memori, jadi kalau server direstart, semua proses yang sedang
berjalan dan file hasil ikut terhapus, dan user perlu login ulang.

## 4. Uji lokal tanpa Active Directory

```bash
HAMPTON_DEV_LOGIN=1 uvicorn webapp.server:app --port 8000
```
Username/password apa pun diterima, dan muncul banner kuning "Mode pengujian".
**Jangan pernah dipakai dengan data asli.**

Mengubah tampilan dengan hot-reload: jalankan backend di atas, lalu
`cd webapp/frontend && npm run dev` dan buka `http://localhost:5173`.

## 5. Tes

```bash
python test_hampton.py          # logika dedup
python webapp/test_server.py    # backend web (login, upload, isolasi antar user, download)
```

## Keamanan & data

- File upload **dihapus segera setelah diproses**. File hasil dihapus setelah `HAMPTON_RETENSI_JAM`.
- Cache AI hanya hidup selama 1 proses dan ikut terhapus.
- Saat server start, semua sisa file dari run sebelumnya dihapus.
- Setiap user hanya bisa melihat & mendownload prosesnya sendiri.
- `webapp/audit.log` mencatat login, upload, selesai/gagal, download, dan penghapusan
  (user, waktu, nama file, jumlah baris, ringkasan). **Isi data nasabah tidak dicatat.**
- Proses berjalan satu per satu (antrian) supaya GPU Qwen tidak kebanjiran.
- Matrix kombinasi dibaca dari `matrix_kombinasi.csv` di folder project setiap kali proses dimulai,
  jadi admin cukup mengganti file itu, tanpa restart.

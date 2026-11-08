import { useCallback, useEffect, useRef, useState } from 'react'

// ---------------------------------------------------------------------------
// API
// ---------------------------------------------------------------------------
class SesiHabis extends Error {}

async function api(path, options = {}) {
  const r = await fetch(path, { credentials: 'same-origin', ...options })
  const data = await r.json().catch(() => ({}))
  if (r.status === 401 && path !== '/api/login') throw new SesiHabis(data.detail)
  if (!r.ok) throw new Error(data.detail || `Terjadi kesalahan (HTTP ${r.status}).`)
  return data
}

// XMLHttpRequest dipakai (bukan fetch) supaya progres upload file besar bisa ditampilkan
function uploadFile(file, pakaiLlm, onProgress) {
  return new Promise((resolve, reject) => {
    const form = new FormData()
    form.append('file', file)
    form.append('pakai_llm', pakaiLlm ? 'true' : 'false')
    const xhr = new XMLHttpRequest()
    xhr.open('POST', '/api/jobs')
    xhr.upload.onprogress = (e) => e.lengthComputable && onProgress(e.loaded / e.total)
    xhr.onload = () => {
      let data = {}
      try { data = JSON.parse(xhr.responseText) } catch { /* bukan JSON */ }
      if (xhr.status === 401) reject(new SesiHabis(data.detail))
      else if (xhr.status >= 400) reject(new Error(data.detail || `Upload gagal (HTTP ${xhr.status}).`))
      else resolve(data)
    }
    xhr.onerror = () => reject(new Error('Koneksi ke server terputus saat upload.'))
    xhr.send(form)
  })
}

// ---------------------------------------------------------------------------
// Utilitas tampilan
// ---------------------------------------------------------------------------
const angka = (n) => (n ?? 0).toLocaleString('id-ID')
const waktu = (t) => new Date(t * 1000).toLocaleString('id-ID', {
  day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit',
})
const ukuran = (b) => (b > 1024 * 1024 ? `${(b / 1024 / 1024).toFixed(1)} MB` : `${Math.ceil(b / 1024)} KB`)

const STATUS = {
  antri: { label: 'Menunggu antrian', kelas: 'netral' },
  proses: { label: 'Sedang diproses', kelas: 'info' },
  selesai: { label: 'Selesai', kelas: 'sukses' },
  gagal: { label: 'Gagal', kelas: 'bahaya' },
}
const HASIL = [
  ['Match', 'match'],
  ['Suspect', 'suspect'],
  ['Not Match', 'notmatch'],
  ['TIDAK ADA DI MATRIX', 'netral'],
]

const Ikon = ({ d, size = 20 }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor"
    strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{d}</svg>
)
const IKON_UPLOAD = <><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" /><path d="M17 8l-5-5-5 5" /><path d="M12 3v12" /></>
const IKON_DOWNLOAD = <><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" /><path d="M7 10l5 5 5-5" /><path d="M12 15V3" /></>
const IKON_FILE = <><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" /><path d="M14 2v6h6" /></>
const IKON_KELUAR = <><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" /><path d="M16 17l5-5-5-5" /><path d="M21 12H9" /></>
const IKON_GEMBOK = <><rect x="3" y="11" width="18" height="11" rx="2" /><path d="M7 11V7a5 5 0 0 1 10 0v4" /></>

function Progres({ label, nilai, total }) {
  const persen = total ? Math.min(100, Math.round((nilai / total) * 100)) : 0
  return (
    <div className="progres">
      <div className="progres-label">
        <span>{label}</span>
        <span>{angka(nilai)} / {angka(total)} ({persen}%)</span>
      </div>
      <div className="progres-bar" role="progressbar" aria-label={label}
        aria-valuemin={0} aria-valuemax={100} aria-valuenow={persen}>
        <div style={{ width: `${persen}%` }} />
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Halaman login
// ---------------------------------------------------------------------------
function Login({ onMasuk, pesanAwal }) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState(pesanAwal)
  const [loading, setLoading] = useState(false)

  async function submit(e) {
    e.preventDefault()
    setLoading(true)
    setError('')
    try {
      await api('/api/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password }),
      })
      onMasuk()
    } catch (err) {
      setError(err.message)
      setPassword('')
    } finally {
      setLoading(false)
    }
  }

  return (
    <main className="login-latar">
      <form className="login-kartu" onSubmit={submit} noValidate>
        <img src="/ocbc-logo.png" alt="OCBC" className="login-logo" />
        <h1>Hampton Dedup</h1>
        <p className="teks-redup">Validasi duplikasi data nasabah</p>

        <label htmlFor="username">Username</label>
        <input id="username" value={username} onChange={(e) => setUsername(e.target.value)}
          autoComplete="username" autoFocus required aria-describedby="username-bantuan" />
        <p id="username-bantuan" className="bantuan">Gunakan akun Windows kantor (tanpa domain).</p>

        <label htmlFor="password">Password</label>
        <input id="password" type="password" value={password}
          onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" required />

        {error && <p className="pesan-error" role="alert">{error}</p>}

        <button type="submit" className="tombol utama penuh" disabled={loading || !username || !password}>
          <Ikon d={IKON_GEMBOK} size={18} />
          {loading ? 'Memeriksa akun…' : 'Masuk'}
        </button>
      </form>
      <p className="catatan-kaki">Khusus penggunaan internal. Data nasabah bersifat rahasia.</p>
    </main>
  )
}

// ---------------------------------------------------------------------------
// Upload
// ---------------------------------------------------------------------------
function Upload({ me, onTerkirim, onSesiHabis }) {
  const [file, setFile] = useState(null)
  const [pakaiLlm, setPakaiLlm] = useState(me.llm_tersedia)
  const [progres, setProgres] = useState(null)
  const [error, setError] = useState('')
  const [seret, setSeret] = useState(false)
  const inputRef = useRef(null)

  function pilih(f) {
    setError('')
    if (!f) return
    if (!f.name.toLowerCase().endsWith('.csv')) return setError('File harus berformat .csv')
    if (f.size > me.max_upload_mb * 1024 * 1024) return setError(`File lebih dari ${me.max_upload_mb} MB.`)
    setFile(f)
  }

  async function kirim(e) {
    e.preventDefault()
    setError('')
    setProgres(0)
    try {
      await uploadFile(file, pakaiLlm, setProgres)
      setFile(null)
      inputRef.current.value = ''
      onTerkirim()
    } catch (err) {
      if (err instanceof SesiHabis) return onSesiHabis(err.message)
      setError(err.message)
    } finally {
      setProgres(null)
    }
  }

  const mengunggah = progres !== null
  return (
    <section className="kartu" aria-labelledby="judul-upload">
      <h2 id="judul-upload">Upload data</h2>
      <form onSubmit={kirim}>
        <label htmlFor="file-csv"
          className={`dropzone${seret ? ' aktif' : ''}${file ? ' terisi' : ''}`}
          onDragOver={(e) => { e.preventDefault(); setSeret(true) }}
          onDragLeave={() => setSeret(false)}
          onDrop={(e) => { e.preventDefault(); setSeret(false); pilih(e.dataTransfer.files[0]) }}>
          <Ikon d={file ? IKON_FILE : IKON_UPLOAD} size={28} />
          {file ? (
            <span><strong>{file.name}</strong><br /><span className="teks-redup">{ukuran(file.size)} · klik untuk ganti file</span></span>
          ) : (
            <span><strong>Pilih file CSV</strong> atau tarik ke sini<br />
              <span className="teks-redup">Maksimal {me.max_upload_mb} MB</span></span>
          )}
        </label>
        <input id="file-csv" ref={inputRef} type="file" accept=".csv,text/csv" className="tersembunyi"
          onChange={(e) => pilih(e.target.files[0])} disabled={mengunggah} />

        <label className="centang">
          <input type="checkbox" checked={pakaiLlm} disabled={!me.llm_tersedia || mengunggah}
            onChange={(e) => setPakaiLlm(e.target.checked)} />
          <span>
            Cek nama yang meragukan dengan AI (Qwen internal)
            <span className="bantuan blok">
              {me.llm_tersedia
                ? 'Nama & nama ibu kandung yang meragukan dinilai ulang oleh AI. Proses lebih lama.'
                : 'Belum dikonfigurasi di server.'}
            </span>
          </span>
        </label>

        {error && <p className="pesan-error" role="alert">{error}</p>}
        {mengunggah && <Progres label="Mengunggah" nilai={Math.round(progres * 100)} total={100} />}

        <button type="submit" className="tombol utama" disabled={!file || mengunggah}>
          <Ikon d={IKON_UPLOAD} size={18} />
          {mengunggah ? 'Mengunggah…' : 'Mulai proses'}
        </button>
      </form>

      <details className="detail-kolom">
        <summary>Kolom wajib di file CSV ({me.kolom_wajib.length})</summary>
        <ul>{me.kolom_wajib.map((k) => <li key={k}><code>{k}</code></li>)}</ul>
      </details>
      <p className="bantuan">
        File upload dihapus segera setelah diproses. File hasil dihapus otomatis {me.retensi_jam} jam setelah selesai.
      </p>
    </section>
  )
}

// ---------------------------------------------------------------------------
// Daftar proses
// ---------------------------------------------------------------------------
function Job({ j }) {
  const s = STATUS[j.status]
  return (
    <li className="job">
      <div className="job-kepala">
        <div className="job-nama">
          <Ikon d={IKON_FILE} size={18} />
          <span><strong>{j.nama_file}</strong>
            <span className="teks-redup"> · {angka(j.baris_total)} baris · diunggah {waktu(j.dibuat)}{j.pakai_llm ? ' · dengan AI' : ''}</span>
          </span>
        </div>
        <span className={`badge ${s.kelas}`}>{s.label}</span>
      </div>

      {j.status === 'proses' && (
        <>
          <Progres label="Baris diproses" nilai={j.baris_selesai} total={j.baris_total} />
          {j.pakai_llm && j.llm_total > 0 && <Progres label="Pengecekan AI (batch)" nilai={j.llm_selesai} total={j.llm_total} />}
        </>
      )}

      {j.status === 'gagal' && <p className="pesan-error" role="alert">{j.error}</p>}

      {j.status === 'selesai' && (
        <>
          <ul className="rekap" aria-label="Ringkasan hasil">
            {HASIL.filter(([k]) => j.rekap?.[k]).map(([k, kelas]) => (
              <li key={k} className={`chip ${kelas}`}><span>{k}</span><strong>{angka(j.rekap[k])}</strong></li>
            ))}
          </ul>
          {j.stat_llm && (
            <p className="bantuan">
              AI menilai {angka(j.stat_llm.llm)} nama ({angka(j.stat_llm.berubah)} berbeda dari aturan)
              {j.stat_llm.gagal ? `, ${angka(j.stat_llm.gagal)} gagal dan memakai hasil aturan` : ''}.
            </p>
          )}
          <div className="job-aksi">
            <a className="tombol utama" href={`/api/jobs/${j.id}/download`} download>
              <Ikon d={IKON_DOWNLOAD} size={18} /> Download hasil
            </a>
            <span className="bantuan">Dihapus otomatis {waktu(j.dihapus_pada)}</span>
          </div>
        </>
      )}
    </li>
  )
}

function DaftarJob({ jobs }) {
  return (
    <section className="kartu" aria-labelledby="judul-proses">
      <h2 id="judul-proses">Proses saya</h2>
      {jobs.length === 0
        ? <p className="kosong">Belum ada proses. Upload file CSV untuk memulai.</p>
        : <ul className="daftar-job" aria-live="polite">{jobs.map((j) => <Job key={j.id} j={j} />)}</ul>}
    </section>
  )
}

// ---------------------------------------------------------------------------
// Aplikasi
// ---------------------------------------------------------------------------
export default function App() {
  const [me, setMe] = useState(undefined)        // undefined = memuat, null = belum login
  const [pesanLogin, setPesanLogin] = useState('')
  const [jobs, setJobs] = useState([])

  const sesiHabis = useCallback((pesan) => {
    setMe(null)
    setJobs([])
    setPesanLogin(pesan || 'Sesi berakhir, silakan login kembali.')
  }, [])

  const muatMe = useCallback(() => {
    api('/api/me').then(setMe).catch(() => setMe(null))
  }, [])

  const muatJobs = useCallback(() => {
    api('/api/jobs').then(setJobs).catch((err) => err instanceof SesiHabis && sesiHabis(err.message))
  }, [sesiHabis])

  useEffect(muatMe, [muatMe])
  useEffect(() => { if (me) muatJobs() }, [me, muatJobs])

  // perbarui status tiap 2 detik selama ada proses yang berjalan
  const adaAktif = jobs.some((j) => j.status === 'antri' || j.status === 'proses')
  useEffect(() => {
    if (!me || !adaAktif) return undefined
    const t = setInterval(muatJobs, 2000)
    return () => clearInterval(t)
  }, [me, adaAktif, muatJobs])

  async function keluar() {
    await api('/api/logout', { method: 'POST' }).catch(() => {})
    setMe(null)
    setJobs([])
    setPesanLogin('')
  }

  if (me === undefined) return <div className="memuat" role="status">Memuat…</div>
  if (me === null) return <Login onMasuk={() => { setPesanLogin(''); muatMe() }} pesanAwal={pesanLogin} />

  return (
    <>
      <a href="#konten" className="lewati">Langsung ke konten</a>
      <header className="header">
        <div className="header-isi">
          <div className="merek">
            <img src="/ocbc-logo.png" alt="OCBC" />
            <span className="pemisah" aria-hidden="true" />
            <span className="nama-app">Hampton Dedup</span>
          </div>
          <div className="pengguna">
            <span className="teks-redup">Masuk sebagai <strong>{me.user}</strong></span>
            <button className="tombol sekunder" onClick={keluar}>
              <Ikon d={IKON_KELUAR} size={18} /> Keluar
            </button>
          </div>
        </div>
      </header>
      {me.dev_login && (
        <p className="banner-dev" role="status">
          Mode pengujian: login tanpa Active Directory. Jangan dipakai untuk data asli.
        </p>
      )}
      <main id="konten" className="konten">
        <Upload me={me} onTerkirim={muatJobs} onSesiHabis={sesiHabis} />
        <DaftarJob jobs={jobs} />
      </main>
      <footer className="catatan-kaki">Khusus penggunaan internal. Data nasabah bersifat rahasia.</footer>
    </>
  )
}

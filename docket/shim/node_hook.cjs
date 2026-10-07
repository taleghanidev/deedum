// docket Node hook. Loaded with NODE_OPTIONS="--require <this file>" by `docket run` / `docket env`.
// Records every fetch() and http/https request the process makes into the same trail, with the
// same record shape, redaction and secret naming as the Python hooks. Every line of it is
// defensive: a failure here is reported once on stderr and never reaches the program.
'use strict'
;(() => {
  if (!process.env.DOCKET_RUN) return
  let fs, path, os, crypto
  try {
    fs = require('node:fs')
    path = require('node:path')
    os = require('node:os')
    crypto = require('node:crypto')
  } catch {
    return
  }
  let warned = false
  const warn = (e) => {
    if (warned) return
    warned = true
    try {
      process.stderr.write(`[docket] node hook: ${e && e.message ? e.message : e}\n`)
    } catch {}
  }

  // ---- where the trail is (same rules as store.find_dir) ------------------------------------
  function findDir() {
    const exp = (p) => (p.startsWith('~') ? path.join(os.homedir(), p.slice(1)) : p)
    if (process.env.DOCKET_DIR) return path.resolve(exp(process.env.DOCKET_DIR))
    let d = process.cwd()
    for (;;) {
      const t = path.join(d, '.docket')
      try {
        const st = fs.statSync(t)
        if (st.isDirectory() && fs.existsSync(path.join(t, 'calls'))) return t
        if (st.isFile()) return path.resolve(exp(fs.readFileSync(t, 'utf8').trim()))
      } catch {}
      const up = path.dirname(d)
      if (up === d) break
      d = up
    }
    const g = path.resolve(exp(process.env.DOCKET_HOME || '~/.docket'))
    return fs.existsSync(path.join(g, 'calls')) ? g : null
  }
  const DIR = findDir()
  if (!DIR) return

  // ---- config (defaults mirror store.DEFAULT_CONFIG) ------------------------------------------
  const DEFAULTS = {
    read_request_max: 16384,
    read_response_max: 2048,
    write_request_max: 262144,
    write_response_max: 65536,
    read_path_regex:
      '(?i)(query|search|report|get[A-Z_/]|/get$|list|status|download|lookup|stream|whoami|/me$|token|oauth|auth)',
    rules: [],
    redact_headers: ['authorization', 'developertoken', 'developer-token', 'x-api-key', 'api-key', 'apikey',
      'cookie', 'set-cookie', 'x-auth-token', 'authenticationtoken', 'proxy-authorization', 'x-goog-api-key'],
    redact_body_keys: ['password', 'secret', 'access_token', 'refresh_token', 'client_secret', 'id_token',
      'api_key', 'apikey', 'token', 'authorization'],
    auth_path_regex: '(?i)(oauth|/token|/auth\\b|login|signin|credential|/session)',
    scrub_regex:
      "(?i)\\b(token|access_token|refresh_token|id_token|secret|client_secret|password|passwd|pwd|sig|signature|api_key|apikey|key|auth|authorization|session|sessionid|bearer)=([^&\\s\"'<>]+)",
    ignore_hosts: ['*.sentry.io', '*.posthog.com', '*.datadoghq.com'],
    ignore_read_hosts: ['localhost', '127.0.0.1', '::1', '0.0.0.0', '*.local', 'registry.npmjs.org', 'pypi.org',
      'files.pythonhosted.org', 'fonts.googleapis.com', 'fonts.gstatic.com', 'objects.githubusercontent.com',
      'raw.githubusercontent.com', 'cdn.jsdelivr.net', 'unpkg.com', 'metadata.google.internal'],
    require_why_hosts: [],
    services: {},
  }
  let cfg = DEFAULTS
  try {
    cfg = { ...DEFAULTS, ...JSON.parse(fs.readFileSync(path.join(DIR, 'config.json'), 'utf8')) }
  } catch {}
  const re = (src) => {
    if (!src) return /$^/
    const ci = src.startsWith('(?i)')
    return new RegExp(ci ? src.slice(4) : src, ci ? 'i' : '')
  }
  const READ_RE = re(cfg.read_path_regex)
  const AUTH_RE = re(cfg.auth_path_regex)
  const SCRUB_RE = new RegExp(re(cfg.scrub_regex).source, 'gi')
  const glob = (pat, s) =>
    new RegExp(`^${pat.replace(/[.+^${}()|[\]\\]/g, '\\$&').replace(/\*/g, '.*').replace(/\?/g, '.')}$`, 'i').test(s)
  let SERVICES = []
  try {
    SERVICES = JSON.parse(fs.readFileSync(path.join(__dirname, 'services.json'), 'utf8'))
  } catch {}
  const serviceOf = (host) => {
    for (const [pat, name] of [...Object.entries(cfg.services || {}), ...SERVICES]) if (glob(pat, host)) return name
    return host
  }
  const validHost = (h) => !!h && /^[A-Za-z0-9._:\-[\]%]{1,253}$/.test(h) && !/^[-.]/.test(h)

  // ---- project (same rules as store.project_of) ------------------------------------------
  function projectOf(cwd) {
    let root = null
    let name = null
    let branch = null
    const home = os.homedir()
    let d = cwd
    for (;;) {
      try {
        const m = path.join(d, '.docket-project')
        if (fs.statSync(m).isFile()) {
          root = d
          name = fs.readFileSync(m, 'utf8').trim().split('\n')[0] || path.basename(d)
        }
      } catch {}
      const g = path.join(d, '.git')
      if (fs.existsSync(g)) {
        try {
          let gd = g
          if (fs.statSync(g).isFile()) {
            const t = fs.readFileSync(g, 'utf8').trim()
            if (t.startsWith('gitdir:')) gd = path.resolve(d, t.slice(7).trim())
          }
          const head = fs.readFileSync(path.join(gd, 'HEAD'), 'utf8').trim()
          branch = head.includes('refs/heads/') ? head.split('refs/heads/')[1] : head.slice(0, 12)
        } catch {}
        if (!root) {
          root = d
          name = path.basename(d)
        }
        break
      }
      if (root || d === home) break
      const up = path.dirname(d)
      if (up === d) break
      d = up
    }
    if (!root) name = cwd === home ? '~' : path.basename(cwd)
    name = process.env.DOCKET_PROJECT || name
    let folder = null
    if (root) {
      const rel = path.relative(root, cwd)
      folder = rel === '' ? '' : rel
    }
    return { name, folder, branch }
  }
  const CWD = process.cwd()
  const PROJECT = projectOf(CWD)

  // ---- secrets: known values by name, then shape-based scrubbing ------------------------------
  const SECRET_ENV_RE = /(key|token|secret|passw|pwd|credential|auth|private|dsn|cookie|session)/i
  function secretValues() {
    const pairs = Object.entries(process.env)
    const home = os.homedir()
    let d = CWD
    for (;;) {
      try {
        for (const f of fs.readdirSync(d)) {
          if (!f.startsWith('.env') || /\.(example|sample|template)$/.test(f)) continue
          const full = path.join(d, f)
          try {
            if (!fs.statSync(full).isFile()) continue
            for (let ln of fs.readFileSync(full, 'utf8').split('\n')) {
              ln = ln.trim()
              if (!ln || ln.startsWith('#') || !ln.includes('=')) continue
              ln = ln.replace(/^export\s+/, '')
              const i = ln.indexOf('=')
              pairs.push([ln.slice(0, i).trim(), ln.slice(i + 1).trim().replace(/^['"]|['"]$/g, '')])
            }
          } catch {}
        }
      } catch {}
      if (d === home) break
      const up = path.dirname(d)
      if (up === d) break
      d = up
    }
    const named = new Map()
    for (const [k, v] of pairs) {
      if (!v || v.length < 12 || /^(\/|http)/.test(v) || !/^[A-Za-z_][A-Za-z0-9_]*$/.test(k)) continue
      if (SECRET_ENV_RE.test(k) && !named.has(v)) named.set(v, k)
    }
    return [...named.entries()].sort((a, b) => b[0].length - a[0].length)
  }
  const SECRETS = secretValues()
  const fp = (v) => crypto.createHash('sha256').update(v).digest('hex').slice(0, 4)
  const tag = (name, v) => `<secret:${name}#${fp(v)}>`
  const secretsIn = (...texts) => {
    const blob = texts.filter((t) => typeof t === 'string').join('\n')
    const out = []
    for (const [v, n] of SECRETS) if (blob.includes(v) && !out.includes(n)) out.push(n)
    return out
  }
  const VALUE_RES = [
    /-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----/g,
    /\b(bearer|basic)(\s+|%20)[A-Za-z0-9._~+/=-]{8,}/gi,
    /(?<=:\/\/)[^/\s:@"']+:[^/\s@"']+(?=@)/g,
    /\b(sk|rk|pk_live|sk_live|sk_test|rk_live|whsec|xai|gsk|pplx|fc)[-_][A-Za-z0-9_-]{20,}/g,
    /\bsk-(ant|or|proj|svcacct|admin)?-?[A-Za-z0-9_-]{20,}/g,
    /\bAIza[0-9A-Za-z_-]{35}/g,
    /\bya29\.[0-9A-Za-z_-]{20,}/g,
    /\b1\/\/0[0-9A-Za-z_-]{30,}/g,
    /\bGOCSPX-[A-Za-z0-9_-]{20,}/g,
    /\bEAA[A-Za-z0-9]{30,}/g,
    /\b(gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})/g,
    /\bxox[abprs]-[A-Za-z0-9-]{10,}/g,
    /\b(AKIA|ASIA)[0-9A-Z]{16}\b/g,
    /\bre_[A-Za-z0-9]{8}_[A-Za-z0-9]{16,}/g,
    /\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}/g,
    /\b[a-z]{2,6}_[A-Za-z0-9]{32,}\b/g,
  ]
  const NAMEVAL_RE =
    /(?<!<)\b([A-Za-z0-9_-]*(?:token|secret|passw|credential|auth[-_]?key|api[-_]?key|access[-_]?key|private[-_]?key|client[-_]?key|websocket-key)[A-Za-z0-9_-]*\s*[:=]\s*)(?!<redacted>|<secret:)[^\s"'&<>,;\\]{6,}/gi
  function scrubValues(text) {
    for (const [v, n] of SECRETS) {
      if (text.includes(v)) {
        text = text.split(v).join(tag(n, v))
        const esc = JSON.stringify(v).slice(1, -1)
        if (esc !== v) text = text.split(esc).join(tag(n, v))
      }
    }
    for (const r of VALUE_RES) text = text.replace(r, '<redacted>')
    return text.replace(NAMEVAL_RE, (_m, g1) => `${g1}<redacted>`)
  }

  // ---- record shaping ---------------------------------------------------------------------
  const REDACT_H = new Set(cfg.redact_headers.map((h) => h.toLowerCase()))
  const REDACT_B = new Set(cfg.redact_body_keys.map((h) => h.toLowerCase()))
  const SECRET_NAME_RE = /(token|secret|passw|auth|credential|proof|signature|cookie|session|key$)/i
  function redactHeaders(h) {
    const out = {}
    for (const [k, raw] of Object.entries(h || {})) {
      const v = Array.isArray(raw) ? raw.join(', ') : String(raw)
      if (REDACT_H.has(k.toLowerCase()) || SECRET_NAME_RE.test(k)) {
        const named = secretsIn(v)
        const first = v.split(' ')[0]
        const scheme = v.includes(' ') && first.length < 12 ? `${first} ` : ''
        const hit = named.length ? SECRETS.find(([, n]) => n === named[0]) : null
        out[k] = scheme + (hit ? tag(hit[1], hit[0]) : '<redacted>')
      } else out[k] = v.slice(0, 500)
    }
    return out
  }
  function redactObj(o, depth = 0) {
    if (depth > 30) return o
    if (Array.isArray(o)) return o.map((v) => redactObj(v, depth + 1))
    if (o && typeof o === 'object') {
      const out = {}
      for (const [k, v] of Object.entries(o))
        out[k] = REDACT_B.has(k.toLowerCase()) || SECRET_NAME_RE.test(k) ? '<redacted>' : redactObj(v, depth + 1)
      return out
    }
    return o
  }
  function body(raw, limit) {
    if (raw == null) return null
    let text
    if (typeof raw === 'string') text = raw
    else if (raw instanceof URLSearchParams) text = raw.toString()
    else if (Buffer.isBuffer(raw) || raw instanceof Uint8Array || raw instanceof ArrayBuffer) {
      const buf = Buffer.from(raw)
      text = buf.toString('utf8')
      if (text.includes('�')) return { _binary_bytes: buf.length }
    } else return { _stream: true }
    text = text.replace(SCRUB_RE, (_m, k) => `${k}=<redacted>`)
    const size = text.length
    if (size <= limit * 4) {
      try {
        return { _json: redactObj(JSON.parse(text)), ...(size > limit ? { _bytes: size, _truncated_after: limit } : {}) }
      } catch {}
    }
    return size > limit ? { _text: text.slice(0, limit), _bytes: size, _truncated: true } : { _text: text }
  }
  function kindOf(method, host, p, rawBody) {
    for (const r of cfg.rules || []) if (glob(r.host || '*', host) && re(r.path_regex || '').test(p)) return r.kind
    if (typeof rawBody === 'string' && rawBody.slice(0, 200).includes('"query"')) {
      try {
        const q = JSON.parse(rawBody).query || ''
        const m = /^\s*(mutation|query|subscription)\b/i.exec(q)
        if (m) return m[1].toLowerCase() === 'mutation' ? 'write' : 'read'
        if (q.trimStart().startsWith('{')) return 'read'
      } catch {}
    }
    if (['GET', 'HEAD', 'OPTIONS'].includes(method)) return 'read'
    if (method === 'POST' && READ_RE.test(p)) return 'read'
    return 'write'
  }

  const pad = (n, w = 2) => String(n).padStart(w, '0')
  function nowIso() {
    const d = new Date()
    const off = -d.getTimezoneOffset()
    const sign = off >= 0 ? '+' : '-'
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}${sign}${pad(Math.floor(Math.abs(off) / 60))}:${pad(Math.abs(off) % 60)}`
  }
  const SESSION = (process.env.CLAUDE_CODE_SESSION_ID || process.env.DOCKET_SESSION || 'default').replace(/[^A-Za-z0-9_-]/g, '').slice(0, 64)
  let RUN = process.env.DOCKET_RUN
  if (RUN === 'always')
    RUN = process.env.CLAUDE_CODE_SESSION_ID ? `session-${process.env.CLAUDE_CODE_SESSION_ID.slice(0, 8)}` : null
  RUN = RUN || crypto.randomBytes(4).toString('hex')
  function why() {
    if (process.env.DOCKET_WHY) return process.env.DOCKET_WHY
    try {
      const d = JSON.parse(fs.readFileSync(path.join(DIR, '.state', 'why', `${SESSION}.json`), 'utf8'))
      if (Date.now() - Date.parse(d.at) > 12 * 3600 * 1000) return ''
      return d.why || ''
    } catch {
      return ''
    }
  }
  const ARGV = scrubValues(process.argv.join(' ')).slice(0, 2000)

  function gate(method, host, p, rawBody) {
    if (kindOf(method, host, p, rawBody) !== 'write' || AUTH_RE.test(p)) return null
    if (process.env.DOCKET_DRY === '1') return 'docket dry run: write not sent'
    if (!why() && (cfg.require_why_hosts || []).some((g) => glob(g, host)))
      return `docket: writes to ${host} need a reason (docket run --why, or docket why "...")`
    return null
  }

  function record(r) {
    try {
      const host = r.host || ''
      if (!validHost(host) || (cfg.ignore_hosts || []).some((g) => glob(g, host))) return
      let kind = kindOf(r.method, host, r.path, typeof r.reqBody === 'string' ? r.reqBody : null)
      if (kind === 'read' && (cfg.ignore_read_hosts || []).some((g) => glob(g, host))) return
      let reqBody = r.reqBody
      let respBody = r.respBody
      if (AUTH_RE.test(r.path || '')) {
        reqBody = '<redacted: credential exchange>'
        respBody = '<redacted: credential exchange>'
        kind = 'read'
      }
      const used = secretsIn(JSON.stringify(r.headers || {}), typeof r.reqBody === 'string' ? r.reqBody : null, JSON.stringify(r.query || {}), ARGV)
      const w = kind === 'write'
      const ts = nowIso()
      const id = `${ts.slice(0, 10).replace(/-/g, '')}-${ts.slice(11, 19).replace(/:/g, '')}-${crypto.randomBytes(3).toString('hex')}`
      const reason = why()
      const query = r.query
        ? Object.fromEntries(Object.entries(r.query).map(([k, v]) => [k, re(cfg.scrub_regex).test(`${k}=x`) ? '<redacted>' : v]))
        : null
      const out = {
        id, ts, run: RUN, why: reason, kind,
        project: PROJECT.name, folder: PROJECT.folder, branch: PROJECT.branch,
        service: serviceOf(host), secrets: used.length ? used : null,
        transport: 'http', method: r.method, host, path: r.path, query,
        request: { headers: redactHeaders(r.headers), body: body(reqBody, w ? cfg.write_request_max : cfg.read_request_max) },
        status: r.status ?? null,
        response: { body: body(respBody, w ? cfg.write_response_max : cfg.read_response_max) },
        ms: r.ms ?? null, error: r.error || null, cwd: CWD, argv: ARGV, runtime: 'node',
      }
      const line = scrubValues(JSON.stringify(out))
      fs.mkdirSync(path.join(DIR, 'calls'), { recursive: true })
      fs.appendFileSync(path.join(DIR, 'calls', `${ts.slice(0, 10)}.jsonl`), `${line}\n`)
      if (w) {
        const st = out.status ?? out.error ?? '?'
        const proj = out.project ? `[${out.project}] ` : ''
        fs.appendFileSync(
          path.join(DIR, 'CHANGES.md'),
          scrubValues(`- ${ts.slice(0, 16).replace('T', ' ')} · ${proj}${out.service} · ${r.method} ${host}${r.path} · ${st}${reason ? ` · why: ${reason}` : ''} · \`${id}\`\n`),
        )
      }
    } catch (e) {
      warn(e)
    }
  }

  // ---- fetch (undici) -----------------------------------------------------------------------
  try {
    if (typeof globalThis.fetch === 'function' && !globalThis.fetch.__docket) {
      const orig = globalThis.fetch
      const wrapped = async function fetch(input, init) {
        let method = 'GET'
        let url = ''
        let headers = {}
        let reqBody = null
        try {
          const req = typeof input === 'object' && input && 'url' in input ? input : null
          url = req ? req.url : String(input)
          method = String((init && init.method) || (req && req.method) || 'GET').toUpperCase()
          const h = new Headers((init && init.headers) || (req && req.headers) || undefined)
          h.forEach((v, k) => {
            headers[k] = v
          })
          const b = init && init.body
          if (typeof b === 'string' || b instanceof URLSearchParams || Buffer.isBuffer(b) || b instanceof Uint8Array || b instanceof ArrayBuffer)
            reqBody = b instanceof URLSearchParams ? b.toString() : typeof b === 'string' ? b : Buffer.from(b).toString('utf8')
          else if (b != null) reqBody = { _stream: true }
        } catch {}
        let u = null
        try {
          u = new URL(url)
        } catch {}
        const host = u ? u.hostname : ''
        const p = u ? u.pathname || '/' : '/'
        const query = u && u.search ? Object.fromEntries(u.searchParams) : null
        const no = u ? gate(method, host, p, typeof reqBody === 'string' ? reqBody : null) : null
        if (no) {
          record({ method, host, path: p, query, headers, reqBody, status: 'BLOCKED', error: no, ms: 0 })
          throw new TypeError(no)
        }
        const t0 = Date.now()
        let resp
        try {
          resp = await orig.call(this, input, init)
        } catch (e) {
          if (u) record({ method, host, path: p, query, headers, reqBody, status: null, error: String(e && e.message).slice(0, 500), ms: Date.now() - t0 })
          throw e
        }
        if (u) {
          const status = resp.status
          const ms = Date.now() - t0
          const ct = resp.headers.get('content-type') || ''
          if (/json|text|xml|x-www-form/i.test(ct) && !/event-stream/i.test(ct)) {
            resp
              .clone()
              .text()
              .then((t) => record({ method, host, path: p, query, headers, reqBody, status, respBody: t, ms }))
              .catch(() => record({ method, host, path: p, query, headers, reqBody, status, ms }))
          } else record({ method, host, path: p, query, headers, reqBody, status, ms })
        }
        return resp
      }
      wrapped.__docket = true
      globalThis.fetch = wrapped
    }
  } catch (e) {
    warn(e)
  }

  // ---- http / https (axios, got, the Stripe and AWS SDKs) ------------------------------------
  for (const modName of ['http', 'https']) {
    try {
      const mod = require(`node:${modName}`)
      if (mod.request.__docket) continue
      const origRequest = mod.request
      const wrap = function request(...args) {
        let method = 'GET'
        let host = ''
        let p = '/'
        let query = null
        let headers = {}
        try {
          let opts = {}
          for (const a of args) {
            if (typeof a === 'string' || a instanceof URL) {
              const u = new URL(String(a))
              opts = { ...opts, hostname: u.hostname, path: u.pathname + u.search }
            } else if (a && typeof a === 'object' && typeof a !== 'function') opts = { ...opts, ...a }
          }
          method = String(opts.method || 'GET').toUpperCase()
          host = String(opts.hostname || opts.host || 'localhost').replace(/:\d+$/, '')
          const full = String(opts.path || '/')
          p = full.split('?')[0] || '/'
          if (full.includes('?')) query = Object.fromEntries(new URLSearchParams(full.split('?')[1]))
          headers = { ...(opts.headers || {}) }
        } catch {}
        const no = gate(method, host, p, null)
        if (no) {
          record({ method, host, path: p, query, headers, status: 'BLOCKED', error: no, ms: 0 })
          const req = origRequest.call(this, 'http://127.0.0.1:9/')
          req.destroy(new Error(no))
          return req
        }
        const t0 = Date.now()
        const req = origRequest.apply(this, args)
        try {
          const chunks = []
          let size = 0
          const ow = req.write
          req.write = function (chunk, ...rest) {
            try {
              if (size < 262144 && chunk != null && typeof chunk !== 'function') {
                const b = Buffer.from(chunk)
                chunks.push(b)
                size += b.length
              }
            } catch {}
            return ow.call(this, chunk, ...rest)
          }
          const oe = req.end
          req.end = function (chunk, ...rest) {
            try {
              if (chunk != null && typeof chunk !== 'function') chunks.push(Buffer.from(chunk))
            } catch {}
            return oe.call(this, chunk, ...rest)
          }
          let done = false
          const finish = (status, error) => {
            if (done) return
            done = true
            const b = chunks.length ? Buffer.concat(chunks).toString('utf8') : null
            record({ method, host, path: p, query, headers: { ...headers, ...(req.getHeaders ? req.getHeaders() : {}) }, reqBody: b, status, error, ms: Date.now() - t0 })
          }
          req.once('response', (res) => finish(res.statusCode, null))
          req.once('error', (e) => finish(null, String(e && e.message).slice(0, 500)))
        } catch (e) {
          warn(e)
        }
        return req
      }
      wrap.__docket = true
      mod.request = wrap
      mod.get = function get(...args) {
        const r = mod.request(...args)
        r.end()
        return r
      }
    } catch (e) {
      warn(e)
    }
  }
})()

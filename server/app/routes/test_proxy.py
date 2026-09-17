"""Debug proxy — forward arbitrary requests to any URL and return the response.

Gated behind a simple env-var flag so it cannot accidentally be exposed
in production.  Set ``ENABLE_TEST_PROXY=1`` to enable.
"""
import asyncio
import os
import socket
import time

from curl_cffi import requests as cffi_requests, CurlOpt
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from ..config import PROXY_URL

ENABLED = os.getenv("ENABLE_TEST_PROXY", "").strip().lower() in {"1", "true", "yes", "on"}

router = APIRouter()


@router.get("/proxy")
async def proxy_gui():
    if not ENABLED:
        return JSONResponse(503, {"error": "test proxy disabled — set ENABLE_TEST_PROXY=1"})
    return HTMLResponse(_HTML)


@router.api_route("/proxy", methods=["POST"], include_in_schema=False)
async def proxy_forward(request: Request):
    if not ENABLED:
        return JSONResponse(503, {"error": "test proxy disabled — set ENABLE_TEST_PROXY=1"})

    body = await request.json()
    url = body.get("url", "")
    method = (body.get("method") or "GET").upper()
    raw_headers = body.get("headers") or {}
    payload = body.get("body")
    timeout_s = body.get("timeout", 30)
    use_resolve = body.get("resolve", False)

    if not url:
        return JSONResponse(400, {"error": "url is required"})

    headers = {}
    for k, v in raw_headers.items():
        if k and v:
            headers[k] = v

    t0 = time.monotonic()
    use_warp = body.get("use_warp", False)
    session = cffi_requests.Session(impersonate="chrome")
    kwargs = {"headers": headers, "timeout": timeout_s}

    if use_warp and PROXY_URL:
        kwargs["proxy"] = PROXY_URL

    if use_resolve:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        hostname = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        try:
            results = socket.getaddrinfo(hostname, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
            ips = list({r[4][0] for r in results})
            if ips:
                resolve_entries = [f"{hostname}:{port}:{ip}" for ip in ips]
                session.curl.setopt(CurlOpt.RESOLVE, resolve_entries)
                kwargs["_resolved_ips"] = ips
        except Exception:
            pass

    if method in ("POST", "PUT", "PATCH"):
        ct = headers.get("Content-Type", headers.get("content-type", ""))
        if "json" in ct:
            kwargs["json"] = payload
        elif payload:
            kwargs["data"] = payload.encode() if isinstance(payload, str) else payload

    resolved_info = kwargs.pop("_resolved_ips", None)
    resp = await asyncio.to_thread(session.request, method, url, **kwargs)
    elapsed = time.monotonic() - t0
    await asyncio.to_thread(session.close)

    resp_headers = dict(resp.headers)
    resp_body = resp.text

    try:
        resp_json = resp.json()
    except Exception:
        resp_json = None

    result = {
        "status": resp.status_code,
        "elapsed_ms": round(elapsed * 1000),
        "headers": resp_headers,
        "body": resp_body,
        "body_json": resp_json,
    }
    if resolved_info:
        result["resolved_to"] = resolved_info
    if use_warp:
        result["proxy_used"] = PROXY_URL or "not configured"
    return result


_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>iCloudEMS Request Tester</title>
<style>
  :root { --bg: #0d1117; --card: #161b22; --border: #30363d; --text: #c9d1d9;
          --accent: #58a6ff; --green: #3fb950; --red: #f85149; --yellow: #d29922;
          --input-bg: #0d1117; --hover: #1f2937; }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'SF Mono', 'Cascadia Code', 'Fira Code', monospace;
         background: var(--bg); color: var(--text); padding: 16px; font-size: 13px; }

  h1 { font-size: 16px; margin-bottom: 12px; color: var(--accent); }

  .row { display: flex; gap: 8px; margin-bottom: 8px; align-items: center; }
  .row.stretch { align-items: stretch; }

  select, input, textarea {
    font-family: inherit; font-size: 13px; background: var(--input-bg);
    color: var(--text); border: 1px solid var(--border); border-radius: 6px;
    padding: 6px 10px; outline: none;
  }
  select:focus, input:focus, textarea:focus { border-color: var(--accent); }
  select { cursor: pointer; }
  input[type="text"], textarea { flex: 1; width: 100%; }
  textarea { resize: vertical; min-height: 80px; line-height: 1.5; tab-size: 2; }

  button {
    font-family: inherit; font-size: 13px; padding: 6px 16px; border-radius: 6px;
    border: 1px solid var(--border); background: var(--accent); color: #000;
    cursor: pointer; font-weight: 600; white-space: nowrap;
  }
  button:hover { opacity: 0.9; }
  button:disabled { opacity: 0.4; cursor: default; }
  button.secondary { background: var(--card); color: var(--text); }

  .section-label {
    font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em;
    color: #8b949e; margin: 12px 0 4px; font-weight: 600;
  }

  .kv-row { display: flex; gap: 6px; margin-bottom: 4px; align-items: center; }
  .kv-row input { flex: 1; }
  .kv-row button { padding: 4px 8px; font-size: 11px; background: var(--card); color: var(--red); border-color: var(--border); }

  .status-badge {
    display: inline-block; padding: 2px 10px; border-radius: 12px;
    font-weight: 700; font-size: 14px;
  }
  .s-ok { background: #238636; color: #fff; }
  .s-err { background: #da3633; color: #fff; }
  .s-warn { background: #9e6a03; color: #fff; }
  .s-info { background: var(--border); color: var(--text); }

  #response-box { background: var(--card); border: 1px solid var(--border);
                  border-radius: 8px; padding: 12px; margin-top: 12px; }
  #response-headers { font-size: 11px; color: #8b949e; margin-top: 6px;
                      max-height: 150px; overflow-y: auto; }
  #response-headers details summary { cursor: pointer; margin-bottom: 4px; }
  #response-body { background: var(--input-bg); border: 1px solid var(--border);
                   border-radius: 6px; padding: 10px; margin-top: 8px;
                   white-space: pre-wrap; word-break: break-all;
                   max-height: 500px; overflow-y: auto; line-height: 1.4; }
  #elapsed { color: #8b949e; margin-left: 12px; font-size: 12px; }
  #loading { display: none; color: var(--yellow); margin-left: 12px; }
</style>
</head>
<body>

<h1>iCloudEMS Request Tester <span id="loading">sending...</span></h1>

<div class="row">
  <select id="method" style="width:90px">
    <option>GET</option><option selected>POST</option><option>PUT</option><option>PATCH</option><option>DELETE</option>
  </select>
  <input type="text" id="url" placeholder="https://krmu.icloudems.com/..." value="https://krmu.icloudems.com/">
  <button id="send-btn" onclick="send()">Send</button>
</div>

<div class="section-label">Headers</div>
<div id="headers-list">
  <div class="kv-row">
    <input type="text" placeholder="Key" value="User-Agent">
    <input type="text" placeholder="Value" value="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36">
    <button onclick="this.parentElement.remove()">x</button>
  </div>
  <div class="kv-row">
    <input type="text" placeholder="Key" value="Accept">
    <input type="text" placeholder="Value" value="application/json">
    <button onclick="this.parentElement.remove()">x</button>
  </div>
</div>
<div class="row" style="margin-top:4px">
  <button class="secondary" onclick="addHeader()">+ Add Header</button>
</div>

<div class="section-label">Body (JSON or raw text)</div>
<textarea id="body" rows="6" placeholder='{"action": "wdefault", "empid": "2091"}'></textarea>

<div class="row" style="margin-top:8px">
  <label style="font-size:12px"><input type="checkbox" id="follow" checked> Follow redirects</label>
  <label style="font-size:12px; margin-left:16px">Timeout:
    <input type="number" id="timeout" value="30" style="width:60px">s
  </label>
  <label style="font-size:12px; margin-left:16px; color:#58a6ff; font-weight:600">
    <input type="checkbox" id="resolve"> DNS Resolve bypass
  </label>
  <label style="font-size:12px; margin-left:16px; color:#3fb950; font-weight:600">
    <input type="checkbox" id="use_warp"> Use WARP proxy
  </label>
</div>

<div id="response-box" style="display:none">
  <div id="response-status"></div>
  <div id="response-headers"></div>
  <div id="response-body"></div>
</div>

<script>
function addHeader(k, v) {
  const row = document.createElement('div');
  row.className = 'kv-row';
  row.innerHTML = `<input type="text" placeholder="Key" value="${k||''}">
                   <input type="text" placeholder="Value" value="${v||''}">
                   <button onclick="this.parentElement.remove()">x</button>`;
  document.getElementById('headers-list').appendChild(row);
}

function getHeaders() {
  const h = {};
  document.querySelectorAll('#headers-list .kv-row').forEach(row => {
    const inputs = row.querySelectorAll('input');
    const k = inputs[0].value.trim();
    const v = inputs[1].value.trim();
    if (k) h[k] = v;
  });
  return h;
}

async function send() {
  const btn = document.getElementById('send-btn');
  const loading = document.getElementById('loading');
  btn.disabled = true;
  loading.style.display = 'inline';

  const body = {
    url: document.getElementById('url').value,
    method: document.getElementById('method').value,
    headers: getHeaders(),
    body: null,
    follow_redirects: document.getElementById('follow').checked,
    timeout: parseInt(document.getElementById('timeout').value) || 30,
    resolve: document.getElementById('resolve').checked,
    use_warp: document.getElementById('use_warp').checked,
  };

  const bodyText = document.getElementById('body').value.trim();
  if (bodyText) {
    try { body.body = JSON.parse(bodyText); }
    catch { body.body = bodyText; }
  }

  const rb = document.getElementById('response-box');
  const rs = document.getElementById('response-status');
  const rh = document.getElementById('response-headers');
  const rbo = document.getElementById('response-body');

  try {
    const t0 = performance.now();
    const resp = await fetch('/test/proxy', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    const data = await resp.json();
    const elapsed = data.elapsed_ms != null ? data.elapsed_ms : Math.round(performance.now() - t0);

    rb.style.display = 'block';
    const sc = data.status || resp.status;
    let cls = 's-info';
    if (sc >= 200 && sc < 300) cls = 's-ok';
    else if (sc >= 400 && sc < 500) cls = 's-err';
    else if (sc >= 500) cls = 's-err';
    else if (sc >= 300 && sc < 400) cls = 's-warn';
    rs.innerHTML = `<span class="status-badge ${cls}">${sc}</span><span id="elapsed">${elapsed}ms</span>`;

    if (data.headers) {
      let hdrHtml = '<details><summary>Response Headers</summary><pre>';
      for (const [k, v] of Object.entries(data.headers)) {
        hdrHtml += `${k}: ${v}\n`;
      }
      hdrHtml += '</pre></details>';
      rh.innerHTML = hdrHtml;
    }

    let displayBody = data.body_json != null ? JSON.stringify(data.body_json, null, 2) : data.body;
    if (data.resolved_to) {
      displayBody = `Resolved to: ${data.resolved_to.join(', ')}\n\n` + displayBody;
    }
    if (data.proxy_used) {
      displayBody = `Proxy: ${data.proxy_used}\n\n` + displayBody;
    }
    rbo.textContent = displayBody || '(empty)';
  } catch (e) {
    rb.style.display = 'block';
    rs.innerHTML = `<span class="status-badge s-err">Error</span>`;
    rh.innerHTML = '';
    rbo.textContent = e.message;
  } finally {
    btn.disabled = false;
    loading.style.display = 'none';
  }
}

document.getElementById('url').addEventListener('keydown', e => {
  if (e.key === 'Enter') send();
});
</script>
</body>
</html>
"""

"""Small web panel (Home Assistant ingress) for the configuration memory:
take and download backups, upload a memory image, review the differences
against the controller and restore them.

Standard library only (``http.server``). All URLs in the page are relative,
so it works under the ingress path prefix. Requests from addresses outside
``allowed_clients`` (the Supervisor ingress proxy, in the add-on) get 403.
"""

import json
import logging
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .cardio2e_memory import MEMORY_SIZE

_LOGGER = logging.getLogger(__name__)

MAX_JSON_BODY = 4096


def _make_handler(service, allowed_clients):
    class Handler(BaseHTTPRequestHandler):
        server_version = "cardio2e"

        def log_message(self, fmt, *args):
            _LOGGER.debug("web: %s - %s", self.client_address[0], fmt % args)

        def _allowed(self):
            if allowed_clients and self.client_address[0] not in allowed_clients:
                self._send(403, "text/plain", b"forbidden")
                return False
            return True

        def _send(self, code, ctype, body, headers=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code, obj):
            self._send(code, "application/json", json.dumps(obj).encode())

        def _body(self, limit):
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = -1
            if length < 0 or length > limit:
                raise ValueError("request body too large")
            return self.rfile.read(length)

        def do_GET(self):
            if not self._allowed():
                return
            path = urllib.parse.urlsplit(self.path).path
            if path in ("/", "/index.html"):
                self._send(200, "text/html; charset=utf-8", PAGE.encode())
            elif path == "/api/status":
                self._json(200, service.status())
            elif path.startswith("/backups/"):
                name = urllib.parse.unquote(path[len("/backups/"):])
                file_path = service.backup_path(name)
                if file_path is None:
                    self._send(404, "text/plain", b"not found")
                    return
                with open(file_path, "rb") as f:
                    data = f.read()
                self._send(200, "application/octet-stream", data,
                           {"Content-Disposition": 'attachment; filename="%s"' % name})
            else:
                self._send(404, "text/plain", b"not found")

        def do_POST(self):
            if not self._allowed():
                return
            url = urllib.parse.urlsplit(self.path)
            try:
                if url.path == "/api/upload":
                    query = urllib.parse.parse_qs(url.query)
                    filename = query.get("filename", ["upload.bin"])[0]
                    name = service.save_upload(filename, self._body(MEMORY_SIZE))
                    self._json(200, {"name": name, "job": service.start_compare(name)})
                    return
                body = json.loads(self._body(MAX_JSON_BODY) or b"{}")
                if url.path == "/api/backup":
                    self._json(200, {"job": service.start_backup()})
                elif url.path == "/api/compare":
                    self._json(200, {"job": service.start_compare(str(body.get("name", "")))})
                elif url.path == "/api/delete":
                    service.delete_backup(str(body.get("name", "")))
                    self._json(200, {"deleted": body.get("name")})
                elif url.path == "/api/cancel":
                    self._json(200, {"job": service.cancel()})
                elif url.path == "/api/apply":
                    self._json(200, {"job": service.start_apply(str(body.get("plan", "")))})
                else:
                    self._send(404, "text/plain", b"not found")
            except (ValueError, RuntimeError) as e:
                self._json(409, {"error": str(e)})

    return Handler


def start_web_server(service, port, allowed_clients=()):
    """Serve the panel on ``port`` in a daemon thread; returns the server."""
    server = ThreadingHTTPServer(("0.0.0.0", port), _make_handler(service, set(allowed_clients)))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True, name="cardio2e-web").start()
    _LOGGER.info("Memory web panel listening on port %d.", port)
    return server


PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Cardio2e memory</title>
<style>
:root{--bg:#fafafa;--card:#fff;--fg:#212121;--muted:#757575;--line:#e0e0e0;--accent:#03a9f4;--bad:#c62828;--good:#2e7d32;--warn:#fff3e0}
@media (prefers-color-scheme:dark){:root{--bg:#111;--card:#1c1c1c;--fg:#e1e1e1;--muted:#9e9e9e;--line:#333;--bad:#ef9a9a;--good:#a5d6a7;--warn:#3a2a10}}
body{margin:0;background:var(--bg);color:var(--fg);font:14px Roboto,"Noto Sans",Arial,sans-serif}
main{max-width:960px;margin:0 auto;padding:16px;display:flex;flex-direction:column;gap:16px}
.card{background:var(--card);border-radius:12px;box-shadow:0 1px 3px rgba(0,0,0,.2);padding:14px 16px}
h1{font-size:20px;font-weight:500;margin:4px 0}h2{font-size:17px;font-weight:500;margin:0 0 10px}
button{background:var(--accent);color:#fff;border:0;border-radius:4px;padding:7px 14px;font-weight:500;cursor:pointer}
button.o{background:transparent;color:var(--accent);border:1px solid var(--accent)}
button.r{background:#db4437}button.o.r{background:transparent;color:#db4437;border-color:#db4437}button:disabled{opacity:.45;cursor:default}
table{border-collapse:collapse;width:100%}td,th{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:500;font-size:12px}.m{font-family:monospace;font-size:12.5px;word-break:break-all}
.bar{height:6px;background:var(--line);border-radius:3px;margin:10px 0 4px}.bar i{display:block;height:6px;width:0;background:var(--accent);border-radius:3px}
.muted{color:var(--muted);font-size:12px}.old{color:var(--bad)}.new{color:var(--good)}
.warn{background:var(--warn);border-left:4px solid #ff9800;padding:8px 10px;font-size:13px;margin:10px 0}
.err{color:var(--bad)}.scroll{overflow-x:auto}td.nw{white-space:nowrap}
</style></head><body><main>
<h1>Cardio2e configuration memory</h1>
<div class="card"><h2>Status</h2><div id="conn"></div><div id="job"></div>
<div class="bar"><i id="bar"></i></div><div class="muted" id="prog"></div>
<p id="cancelp" hidden><button class="o" id="cancel">Cancel</button> <span class="muted">stops reading; nothing is written</span></p></div>
<div class="card"><h2>Backups</h2>
<button id="bk">Back up now</button> <span class="muted">reads the whole memory (~15 min). Commands still work, but take ~12 s while the controller leaves memory mode (its timers, such as auto off, are stopped in it); reading then pauses 30 s</span>
<div class="scroll"><table style="margin-top:10px"><thead><tr><th>File</th><th>Date</th><th>Size</th><th></th></tr></thead><tbody id="list"></tbody></table></div></div>
<div class="card"><h2>Restore</h2>
<input type="file" id="file" accept=".bin"> <button class="o" id="up">Upload, compare &amp; restore…</button>
<div class="muted">Or press <b>Compare &amp; restore…</b> on a backup above. The configuration area of the controller is read first (~5 min) and the differences are shown; nothing is written until you press <b>Apply</b>.</div>
<div id="plan"></div></div>
</main><script>
const $=id=>document.getElementById(id);let busy=false;
function esc(s){return String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
async function post(url,body,raw){const r=await fetch(url,{method:'POST',body:raw?body:JSON.stringify(body||{}),headers:raw?{'Content-Type':'application/octet-stream'}:{'Content-Type':'application/json'}});
 const j=await r.json().catch(()=>({error:r.statusText}));if(!r.ok)alert(j.error||r.statusText);refresh();return j}
function render(s){
 $('conn').innerHTML=s.connected?'Connected to cardio2e.':'<span class="err">Not connected to cardio2e.</span>';
 const j=s.job;busy=!!(j&&j.state==='running');$('cancelp').hidden=!(busy&&j.cancellable);
 if(j){const pct=j.total?Math.floor(100*j.done/j.total):0;$('bar').style.width=pct+'%';
  $('job').innerHTML='Last job: <b>'+esc(j.kind)+'</b> — '+esc(j.state)+(j.message?': <span class="'+(j.state==='error'?'err':'')+'">'+esc(j.message)+'</span>':'');
  $('prog').textContent=j.done+' / '+j.total+' bytes ('+pct+'%) · started '+j.started+(j.finished?' · finished '+j.finished:'')}
 else{$('job').textContent='No job run yet.';$('prog').textContent='';$('bar').style.width='0'}
 $('list').innerHTML=s.backups.map(b=>'<tr><td class="m">'+esc(b.name)+'</td><td class="nw">'+esc(b.mtime.replace('T',' '))+'</td><td class="nw">'+(b.size/1024).toFixed(1)+' KB</td><td class="nw">'+
  '<a href="backups/'+encodeURIComponent(b.name)+'" download><button class="o">Download</button></a> <button class="o" data-c="'+esc(b.name)+'">Compare &amp; restore…</button> <button class="o r" data-d="'+esc(b.name)+'">Delete</button></td></tr>').join('')||'<tr><td colspan="4" class="muted">No backups yet.</td></tr>';
 document.querySelectorAll('[data-c]').forEach(e=>{e.disabled=busy||!s.connected;e.onclick=()=>post('api/compare',{name:e.dataset.c})});
 document.querySelectorAll('[data-d]').forEach(e=>{e.disabled=busy;e.onclick=()=>{if(confirm('Delete '+e.dataset.d+'? This only removes the file; the cardio2e is not touched.'))post('api/delete',{name:e.dataset.d})}});
 $('bk').disabled=busy||!s.connected;$('up').disabled=busy||!s.connected;
 const p=s.plan;
 if(!p){$('plan').innerHTML='';return}
 if(!p.changes.length){$('plan').innerHTML='<div class="warn">'+esc(p.source)+': no differences from the controller.</div>';return}
 $('plan').innerHTML='<div class="warn">Differences between <b>'+esc(p.source)+'</b> and the controller. Only these '+p.bytes+' byte(s) are written, then read back. Codes, emergency phones and the event log are never restored. The current values are saved first as a <i>before-restore</i> file.</div>'+
  '<div class="scroll"><table><thead><tr><th>Section</th><th>Record</th><th>Field</th><th>Controller</th><th>File</th></tr></thead><tbody>'+
  p.changes.map(c=>'<tr><td>'+esc(c.section)+'</td><td>'+esc(c.record)+'</td><td>'+esc(c.field)+'</td><td class="m old">'+esc(c.current)+'</td><td class="m new">'+esc(c.wanted)+'</td></tr>').join('')+
  '</tbody></table></div><p><button class="r" id="apply">Apply '+p.changes.length+' change(s)</button></p>';
 $('apply').disabled=busy||!s.connected;
 $('apply').onclick=()=>{if(confirm('Write '+p.bytes+' byte(s) from '+p.source+' to the cardio2e?'))post('api/apply',{plan:p.id})};
}
async function refresh(){try{render(await (await fetch('api/status')).json())}catch(e){$('conn').innerHTML='<span class="err">Panel unreachable.</span>'}}
$('bk').onclick=()=>post('api/backup');
$('cancel').onclick=()=>post('api/cancel');
$('up').onclick=async()=>{const f=$('file').files[0];if(!f){alert('Choose a .bin file first.');return}
 if(f.size!==__MEMORY_SIZE__){alert('A memory image has __MEMORY_SIZE__ bytes; this file has '+f.size+'.');return}
 post('api/upload?filename='+encodeURIComponent(f.name),await f.arrayBuffer(),true)};
refresh();setInterval(refresh,2000);
</script></body></html>
""".replace("__MEMORY_SIZE__", str(MEMORY_SIZE))

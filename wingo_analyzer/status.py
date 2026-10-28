"""Status server: ``GET /`` is the landing/status page, ``GET /status`` is the
JSON snapshot behind it, ``/healthz`` is the readiness probe.

Binds ``0.0.0.0:$PORT`` so the Freebuff preview can reach it.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

log = logging.getLogger("wingo.status")

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WINGO NEXUS AI · signal desk</title>
<style>
  :root{--bg:#060a16;--panel:#0d1428;--line:#1c2748;--ink:#e8efff;--muted:#8a99bd;
        --green:#35d0a5;--red:#ff5c6c;--amber:#ffb020;--accent:#6ea8ff;--violet:#c084fc;}
  *{box-sizing:border-box}
  body{margin:0;background:radial-gradient(1100px 520px at 15% -10%,#12204a 0%,transparent 60%),
       radial-gradient(900px 500px at 100% 0%,#2a1250 0%,transparent 55%),var(--bg);
       color:var(--ink);font:15px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
  .wrap{max-width:1080px;margin:0 auto;padding:34px 20px 60px}
  header{display:flex;flex-wrap:wrap;gap:18px;align-items:center;justify-content:space-between}
  h1{font-size:30px;letter-spacing:-.5px;margin:0}
  h1 span{color:var(--accent)}
  .tag{color:var(--muted);margin-top:6px}
  .pill{display:inline-flex;gap:8px;align-items:center;background:var(--panel);border:1px solid var(--line);
        padding:9px 15px;border-radius:999px;font-size:13px}
  .dot{width:9px;height:9px;border-radius:50%;background:var(--green);box-shadow:0 0 12px var(--green)}
  .dot.off{background:var(--red);box-shadow:0 0 12px var(--red)}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:14px;margin-top:26px}
  .card{background:linear-gradient(180deg,#0f1730,#0b1122);border:1px solid var(--line);
        border-radius:16px;padding:16px 17px;position:relative;overflow:hidden}
  .card:before{content:"";position:absolute;inset:0 0 auto 0;height:3px;background:var(--accent)}
  .card.g1:before{background:var(--amber)} .card.g2:before{background:var(--green)}
  .card.g3:before{background:var(--accent)} .card.g4:before{background:var(--violet)}
  .node{font-size:12px;letter-spacing:.14em;color:var(--muted);text-transform:uppercase}
  .issue{font:600 15px ui-monospace,SFMono-Regular,Menlo,monospace;margin-top:9px;color:#cfdcff}
  .row{display:flex;align-items:center;justify-content:space-between;margin-top:12px}
  .pick{width:52px;height:52px;border-radius:50%;display:grid;place-items:center;
        font-size:24px;font-weight:800;color:#07101f}
  .meta{font-size:13px;color:var(--muted);text-align:right}
  .meta b{color:var(--ink);font-size:15px}
  .time{font:800 26px ui-monospace,Menlo,monospace;letter-spacing:1px}
  .panels{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:14px;margin-top:14px}
  .panel{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:17px 18px}
  h2{font-size:13px;letter-spacing:.16em;text-transform:uppercase;color:var(--muted);margin:0 0 12px}
  .prov{display:flex;justify-content:space-between;gap:10px;padding:9px 0;border-top:1px solid #16204066;
        font-size:14px}
  .prov:first-of-type{border-top:0}
  .tag-ok{color:var(--green)} .tag-warn{color:var(--amber)} .tag-off{color:var(--red)}
  .big{font-size:26px;font-weight:800}
  .btn{display:inline-block;margin-top:18px;background:linear-gradient(180deg,#2a6bff,#1b4fd6);
       color:#fff;text-decoration:none;padding:13px 22px;border-radius:12px;font-weight:700}
  .btn:hover{filter:brightness(1.08)}
  footer{margin-top:34px;color:var(--muted);font-size:13px;border-top:1px solid var(--line);padding-top:16px}
  code{font-family:ui-monospace,Menlo,monospace;color:#b9c9f5}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div>
      <h1>WINGO <span>NEXUS AI</span></h1>
      <div class="tag">four live nodes · two analysts (ROB &amp; ROD) · Gemini-first AI with Groq failover</div>
    </div>
    <div style="display:flex;gap:10px;flex-wrap:wrap">
      <span class="pill"><span class="dot" id="botDot"></span><span id="botState">connecting…</span></span>
      <span class="pill"><span class="dot" id="aiDot"></span><span id="aiState">AI chain</span></span>
    </div>
  </header>

  <div class="grid" id="nodes"></div>

  <div class="panels">
    <div class="panel">
      <h2>AI provider loop</h2>
      <div id="providers"><div class="prov"><span>loading…</span></div></div>
      <div class="prov" style="margin-top:10px"><span>calls / failovers</span>
        <b id="aiCalls">–</b></div>
    </div>
    <div class="panel">
      <h2>Desk</h2>
      <div class="prov"><span>Rounds stored</span><b id="rounds">–</b></div>
      <div class="prov"><span>Predictions scored</span><b id="preds">–</b></div>
      <div class="prov"><span>Members (active)</span><b id="users">–</b></div>
      <div class="prov"><span>Snapshot images live</span><b id="snaps">–</b></div>
      <div class="prov"><span>Auto-delete retention</span><b id="retention">24h</b></div>
      <a class="btn" id="tg" href="https://t.me/" target="_blank" rel="noopener">Open the Telegram bot &rarr;</a>
    </div>
  </div>

  <footer>
    Cards update themselves in place &middot; snapshots delete themselves after 24h &middot;
    predictions are statistical reads, never guarantees &middot; 18+ only &middot; Gemini-first AI with Groq failover.
  </footer>
</div>
<script>
var COLORS={green:'#35d0a5',red:'#ff5c6c',violet:'#a06bff'};
function el(id){return document.getElementById(id);}
function stateTag(p){
  if(!p.configured) return ['tag-off','key missing'];
  if(p.disabled_s) return ['tag-off','disabled '+p.disabled_s+'s'];
  if(p.cooldown_s) return ['tag-warn','cooldown '+p.cooldown_s+'s'];
  return ['tag-ok','ready'];
}
function render(data){
  var bot=data.bot||{};
  el('botDot').className='dot'+(bot.started?'':' off');
  el('botState').textContent=bot.started?('bot @'+(bot.username||'…')):'bot offline (no token)';
  if(bot.username) el('tg').href='https://t.me/'+bot.username;
  var ai=data.ai||{};
  el('aiDot').className='dot'+(ai.ready?'':' off');
  el('aiState').textContent='AI: '+(ai.primary||'-')+(ai.ready?' up':' down');
  el('aiCalls').textContent=(ai.calls||0)+' / '+(ai.fallbacks||0);
  var providers=ai.providers||[];
  if(providers.length){
    el('providers').innerHTML=providers.map(function(p){
      var st=stateTag(p);
      return '<div class="prov"><span><b>'+p.name+'</b> <code>'+(p.model||'-')+'</code></span>'+
             '<span class="'+st[0]+'">'+st[1]+'</span></div>';
    }).join('');
  }
  var games=data.games||{}, keys=Object.keys(games).sort(function(a,b){
    return (games[a].node||0)-(games[b].node||0);});
  el('nodes').innerHTML=keys.map(function(k,i){
    var g=games[k], p=g.prediction||{}, last=g.last_round||{};
    var colour=(p.colours&&p.colours[0])||'green';
    var score=g.scorecard||{};
    return '<div class="card g'+(i+1)+'">'+
      '<div class="node">NODE 0'+(g.node||i+1)+' &middot; WINGO '+g.label+'</div>'+
      '<div class="issue">'+(g.issue||'syncing…')+'</div>'+
      '<div class="row"><div class="time" data-left="'+(g.seconds_left||0)+'">'+
        (g.seconds_left||0)+'s</div>'+
        '<div class="pick" style="background:'+(COLORS[colour]||'#6ea8ff')+'">'+(p.number!==undefined?p.number:'?')+'</div></div>'+
      '<div class="meta">last <b>'+(last.number!==undefined?last.number:'–')+'</b>'+
        ' &middot; streak <b>'+(score.streak_kind||'')+(score.streak_len||0)+'</b>'+
        ' &middot; '+(score.colour_rate||0)+'%<br>'+g.status+' &middot; '+(g.rounds||0)+' rounds</div>'+
      '</div>';
  }).join('');
  var u=data.users||{}, s=data.snapshots||{}, c=data.config||{};
  el('rounds').textContent=data.rounds_total||0;
  el('preds').textContent=data.predictions||0;
  el('users').textContent=(u.active||0)+' / '+(u.total||0);
  el('snaps').textContent=(s.live||0)+' live · '+(s.deleted||0)+' expired';
  el('retention').textContent=(c.snapshot_retention_hours||24)+'h';
}
function tick(){
  fetch('/status').then(function(r){return r.json();}).then(render).catch(function(){
    el('botDot').className='dot off'; el('botState').textContent='engine unreachable';
  });
}
tick(); setInterval(tick, 2000);
setInterval(function(){
  document.querySelectorAll('.time').forEach(function(node){
    var left=parseInt(node.getAttribute('data-left'),10);
    if(left>0){left-=1; node.setAttribute('data-left',left); node.textContent=left+'s';}
  });
}, 1000);
</script>
</body>
</html>
"""


class StatusServer:
    def __init__(self, payload: Callable[[], dict], host: str = "0.0.0.0", port: int = 8080):
        self.payload = payload
        self.host = host
        self.port = port
        self.httpd: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        payload = self.payload
        page = PAGE

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):  # silence per-request noise
                return

            def _send(self, code: int, body: bytes, content_type: str) -> None:
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def do_GET(self):  # noqa: N802
                path = self.path.split("?", 1)[0]
                if path in ("/", "/index.html"):
                    self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
                elif path in ("/status", "/status.json"):
                    try:
                        data = payload()
                    except Exception as exc:  # noqa: BLE001
                        data = {"ok": False, "error": str(exc)[:200]}
                    self._send(200, json.dumps(data).encode("utf-8"),
                               "application/json; charset=utf-8")
                elif path == "/healthz":
                    self._send(200, b'{"ok":true}', "application/json")
                else:
                    self._send(404, b'{"error":"not found"}', "application/json")

        self.httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="status-http",
                                       daemon=True)
        self.thread.start()
        log.info("status server on http://%s:%s/", self.host, self.port)

    def stop(self) -> None:
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()

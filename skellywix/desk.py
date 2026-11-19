"""The desk can read recommendations and evidence. It has no mutation routes."""
from html import escape
from http.server import BaseHTTPRequestHandler, HTTPServer
from importlib.resources import files
import re
import sqlite3
from urllib.parse import parse_qs, urlsplit

from . import engine

HOST = re.compile(r'(?:localhost|127\.0\.0\.1)(?::[1-9][0-9]{0,4})?\Z', re.ASCII)
POLICY = "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
CSS = files(__package__).joinpath('desk.css').read_text(encoding='utf-8')


def _tags(words):
    return ' '.join('<code>' + escape(word) + '</code>' for word in words)


def render(state, *, online=False, lookup=None):
    rev = state['revision']
    checks = state['counts']['check']
    score = f"{state['correct']} of {checks} checks pass" if checks else 'No checks recorded'
    pending = 'New evidence awaits a pass.' if state['pending'] else 'All recorded evidence has been evaluated.'
    checked = escape(state['checked'] or 'Not run yet')
    source = f"{state['demo_cases']} labeled starter examples; other examples are local." if state['demo_cases'] else 'Local labels only. No demonstration data.'
    guide_rows = []
    for g in state['guides']:
        learned = sorted(k for k, target in state['aliases'].items() if target == g['id'])
        guide_rows.append(f"<details><summary>{escape(g['title'])} <small>{escape(g['id'])}</small></summary><p>{escape(g['advice'])}</p><p>Native tags: {_tags(g['tags'])}</p>" + (f"<p>Learned tags: {_tags(learned)}</p>" if learned else '') + '</details>')
    queue = ''.join(f"<li>{_tags(r['tags'])}<br>Expected <b>{escape(r['expected'] or 'no guide')}</b> · got {escape(r['actual'] or 'no guide')}</li>" for r in state['queue'][:64])
    queue = '<ul>' + queue + '</ul>' if queue else '<p>No mismatches in the current check set.</p>' if checks else '<p>Add independently labeled checks to find routing misses.</p>'
    if len(state['queue']) > 64:
        queue += '<p>Showing the first 64. The status command lists every mismatch.</p>'
    receipts = []
    for r in state['history'][:32]:
        result = f"{r['before']} → {r['after']} of {r['checks']} · {r['regressions']} lost" if 'checks' in r else escape(r.get('guide', ''))
        receipts.append(f"<li><div><b>{escape(r['decision'])}</b> · revision {r['revision']} <span>{result}</span></div><time>{escape(r['at'])}</time></li>")
    desk_lookup = '<p>Run <code>python -m skellywix resolve test</code> to look up a guide.</p>'
    if online:
        desk_lookup = '<form action="/lookup" method="get"><label for="tags">Task tags</label><div class="lookup"><input id="tags" name="tags" required maxlength="263" placeholder="regression test" autocomplete="off"><button>Find guide</button></div></form>'
    if lookup is not None:
        g = lookup['guide']
        answer = f"<b>{escape(g['title'])}</b> · {escape(g['advice'])}" if g else 'No unique match. Use a more specific tag or let the harness choose its normal route.'
        desk_lookup += f'<p class="answer">Lookup at revision {lookup["revision"]}: {answer}</p>'
    refresh = '<a href="/">Refresh desk</a>' if online else 'Offline snapshot'
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Skellywix Harness Engine · Operator desk</title><style>{CSS}</style></head><body>
<a class="skip" href="#main">Skip to desk</a><header><div class="brand"><span class="mark" aria-hidden="true">S</span> Skellywix</div><nav aria-label="Workspaces"><a href="#index">Guide index</a><a href="#queue">Failure queue</a><a href="#history">Run history</a></nav></header>
<main id="main"><section class="hero"><div><h1>Harness upkeep</h1><p>Which guide should your agent use next? Check the evidence before you change the route.</p><p class="signature">— Skelly</p></div><div class="disc" aria-hidden="true"><i></i><strong>SKX</strong></div></section>
<div class="strip"><b>Revision {rev}</b><span>{score}</span><span>{refresh}</span></div>
<div class="layout"><div><section id="lookup"><h2>Find a guide</h2>{desk_lookup}</section><section id="index"><h2>Guide index <small>{len(guide_rows)}</small></h2>{''.join(guide_rows)}</section></div>
<aside><section><h2>Agent status</h2><p>{pending}</p><dl><dt>Last pass (UTC)</dt><dd>{checked}</dd><dt>Training examples</dt><dd>{state['counts']['train']}</dd><dt>Check examples</dt><dd>{checks}</dd><dt>Undo available</dt><dd>{'Yes' if state['can_rollback'] else 'No'}</dd></dl><p class="muted">{source}</p></section><section id="queue"><h2>Failure queue <small>{len(state['queue'])}</small></h2>{queue}</section></aside></div>
<section id="history"><h2>Run history</h2><ol class="ledger">{''.join(receipts)}</ol></section></main><footer>Skellywix Harness Engine · Local operator desk</footer></body></html>'''


def serve(home, *, port, container=False):
    class Handler(BaseHTTPRequestHandler):
        server_version = 'Skellywix'
        sys_version = ''
        timeout = 3

        def log_message(self, fmt, *args):
            # Request URLs may contain the operator's task tags. Never log them.
            return

        def do_GET(self):
            if not HOST.fullmatch(self.headers.get('Host', '')) or self.headers.get('Sec-Fetch-Site') == 'cross-site':
                self.send_error(403, 'Open the desk on localhost')
                return
            if len(self.path) > 512:
                self.send_error(414)
                return
            try:
                parts = urlsplit(self.path)
                if parts.netloc or parts.scheme or parts.fragment or parts.path not in ('/', '/lookup'):
                    self.send_error(404)
                    return
                lookup = None
                if parts.path == '/lookup':
                    query = parse_qs(parts.query, strict_parsing=True, max_num_fields=1)
                    if set(query) != {'tags'}:
                        self.send_error(400, 'Supply one to eight task tags')
                        return
                    lookup = engine.resolve(home, query['tags'][0].split())
                elif parts.query:
                    self.send_error(400)
                    return
                body = render(engine.status(home), online=True, lookup=lookup).encode('utf-8')
            except engine.WorkspaceMissing:
                self.send_error(503, 'Workspace is not initialized')
                return
            except ValueError:
                self.send_error(400, 'Invalid workspace or task tags')
                return
            except (sqlite3.Error, OSError):
                self.send_error(503, 'Workspace is unavailable')
                return
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def end_headers(self):
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Security-Policy', POLICY)
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            super().end_headers()

    with HTTPServer(('0.0.0.0' if container else '127.0.0.1', port), Handler) as http:
        print(f'Desk ready on port {http.server_port}', flush=True)
        http.serve_forever()

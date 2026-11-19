import argparse
import json
import os
from pathlib import Path
import signal
import sqlite3
import sys
from threading import Event

from . import desk, engine


def _emit(value):
    print(json.dumps(value, ensure_ascii=True, separators=(',', ':')), flush=True)


def main(argv=None):
    cli = argparse.ArgumentParser(prog='skellywix', description='Local guide routing with checked, reversible improvements.')
    cli.add_argument('--home', type=Path, default=Path(os.environ.get('SKELLYWIX_HOME') or Path.home() / '.skellywix'), help='Dedicated state directory (or SKELLYWIX_HOME).')
    sub = cli.add_subparsers(dest='action', required=True)
    init = sub.add_parser('init', help='Initialize an empty workspace without replacing existing state.')
    init.add_argument('--demo', action='store_true', help='Add clearly labeled example evidence to a new workspace.')
    lookup = sub.add_parser('resolve', help='Read the current guide for one to eight task tags.')
    lookup.add_argument('tags', nargs='+')
    label = sub.add_parser('feedback', help='Record a verified outcome or an independently labeled check.')
    label.add_argument('cohort', choices=['train', 'check'])
    label.add_argument('expected', help='Approved guide ID, or none for a checked abstention.')
    label.add_argument('tags', nargs='+')
    sub.add_parser('add-guide', help='Add one operator-approved JSON guide from standard input.')
    sub.add_parser('run', help='Evaluate once and promote only a nonregressing improvement.')
    sub.add_parser('rollback', help='Undo the latest promotion and hold until new evidence arrives.')
    sub.add_parser('status', help='Print the current index, backlog and decision receipts.')
    sub.add_parser('mcp', help='Serve the read-only guide tool over MCP stdio.')
    sub.add_parser('export-desk', help='Write a standalone, script-free HTML snapshot to standard output.')
    watch = sub.add_parser('watch', help='Initialize if needed, then run until stopped.')
    watch.add_argument('--interval', type=int, default=60, metavar='SECONDS', help='Seconds between passes (5 to 86400, default: 60).')
    serve = sub.add_parser('serve', help='Open a read-only HTTP desk on loopback.')
    serve.add_argument('--port', type=int, default=8760)
    serve.add_argument('--container', action='store_true', help='Bind inside a container; publish only on host loopback.')
    args = cli.parse_args(argv)
    home = args.home
    try:
        if args.action == 'init':
            _emit(engine.initialize(home, demo=args.demo))
        elif args.action == 'resolve':
            _emit(engine.resolve(home, args.tags))
        elif args.action == 'feedback':
            _emit(engine.record(home, args.cohort, args.expected, args.tags))
        elif args.action == 'add-guide':
            payload = sys.stdin.buffer.read(8193)
            if len(payload) > 8192:
                raise ValueError('Guide JSON exceeds 8 KiB.')
            _emit(engine.add_guide(home, json.loads(payload.decode('utf-8'))))
        elif args.action == 'run':
            _emit(engine.run(home))
        elif args.action == 'rollback':
            _emit(engine.rollback(home))
        elif args.action == 'status':
            _emit(engine.status(home))
        elif args.action == 'mcp':
            from . import adapter
            return adapter.run(home, sys.stdin.buffer, sys.stdout.buffer)
        elif args.action == 'export-desk':
            sys.stdout.buffer.write(desk.render(engine.status(home)).encode('utf-8'))
        elif args.action == 'serve':
            if not 1 <= args.port <= 65535:
                raise ValueError('Port must be between 1 and 65535.')
            desk.serve(home, port=args.port, container=args.container)
        elif args.action == 'watch':
            if not 5 <= args.interval <= 86400:
                raise ValueError('Interval must be between 5 and 86400 seconds.')
            stop = Event()
            for sig in (signal.SIGINT, signal.SIGTERM):
                signal.signal(sig, lambda *_: stop.set())
            engine.initialize(home)
            _emit({'worker': 'started', 'interval': args.interval})
            while not stop.is_set():
                result = engine.run(home)
                if result['decision'] != 'unchanged':
                    _emit(result)
                stop.wait(args.interval)
            _emit({'worker': 'stopped'})
    except (ValueError, OSError, sqlite3.Error) as exc:
        print(f'skellywix: {exc}', file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

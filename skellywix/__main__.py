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
    mcp = sub.add_parser('mcp', help='Serve local guide, discovery, and optional supervisor tools over MCP stdio.')
    mcp.add_argument('--project', type=Path, help='Override the workspace selected by the MCP launch environment.')
    mcp.add_argument('--supervisor-policy', type=Path, help='Enable supervisor tools using an owner-approved local JSON policy.')
    mcp.add_argument('--pi-agent-dir', type=Path, help='Select a nondefault Pi agent directory for the supervisor.')
    sub.add_parser('export-desk', help='Write a standalone, script-free HTML snapshot to standard output.')
    watch = sub.add_parser('watch', help='Initialize if needed, then run until stopped.')
    watch.add_argument('--interval', type=int, default=60, metavar='SECONDS', help='Seconds between passes (5 to 86400, default: 60).')
    serve = sub.add_parser('serve', help='Open a read-only HTTP desk on loopback.')
    serve.add_argument('--port', type=int, default=8760)
    serve.add_argument('--container', action='store_true', help='Bind inside a container; publish only on host loopback.')
    neural = sub.add_parser('neural-map', help='Watch local harness registrations and recorded prompt activity.')
    neural.add_argument('--port', type=int, default=8761)
    neural.add_argument('--agent-dir', type=Path, help='Use this OMP agent directory instead of the active profile.')
    neural.add_argument('--project', type=Path, help='Read project registrations from this directory (default: current directory).')
    neural.add_argument('--sessions-dir', type=Path, help='Read journals from this directory instead of the profile default.')
    neural.add_argument('--private-dir', type=Path, help='Read optional local metadata inserts from this directory.')
    neural.add_argument('--no-native', action='store_true', help='Read files only; skip installed CLI and scheduler probes.')
    neural.add_argument('--operator', action='store_true', help='Enable the private Pi/OMP settings and Hindsight tabs.')
    neural.add_argument('--pi-agent-dir', type=Path, help='Disambiguate a custom Pi agent directory in the operator desk.')
    neural.add_argument('--hindsight-http-origin', help='Permit read-only Hindsight requests to this exact HTTP origin without API keys.')
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
            return adapter.run(home, sys.stdin.buffer, sys.stdout.buffer, project=args.project,
                               supervisor_policy=args.supervisor_policy, pi_agent=args.pi_agent_dir)
        elif args.action == 'export-desk':
            sys.stdout.buffer.write(desk.render(engine.status(home)).encode('utf-8'))
        elif args.action == 'serve':
            if not 1 <= args.port <= 65535:
                raise ValueError('Port must be between 1 and 65535.')
            desk.serve(home, port=args.port, container=args.container)
        elif args.action == 'neural-map':
            from .neural import serve as serve_map
            if not 1 <= args.port <= 65535:
                raise ValueError('Port must be between 1 and 65535.')
            serve_map(args.port, agent=args.agent_dir, project=args.project, sessions=args.sessions_dir,
                      private=args.private_dir, native=not args.no_native, operator=args.operator,
                      pi_agent=args.pi_agent_dir, hindsight_http_origin=args.hindsight_http_origin)
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

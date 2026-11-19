"""Run after Compose startup. Test the published host socket, not an in-container port."""
import argparse
from http.client import HTTPConnection
import time


def check(port):
    last_error = None
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        client = HTTPConnection('127.0.0.1', port, timeout=2)
        try:
            client.request('GET', '/')
            response = client.getresponse()
            body = response.read().decode('utf-8')
            if response.status == 200 and '<h1>Harness upkeep</h1>' in body and 'Run history' in body:
                return
            last_error = f'HTTP {response.status}: no operator desk'
        except OSError as exc:
            last_error = str(exc)
        finally:
            client.close()
        time.sleep(.25)
    raise SystemExit(f'Desk did not answer on the published loopback port: {last_error}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8760)
    check(parser.parse_args().port)
    print('PASS: operator desk reachable on host loopback')

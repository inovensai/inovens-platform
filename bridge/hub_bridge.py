#!/usr/bin/env python3
import json, os, re, ssl, subprocess, sys, time
from urllib import request, error

BASE_URL = os.environ.get('INOVENS_HUB_URL', 'https://inovensai.com/panel').rstrip('/')
TOKEN = os.environ.get('INOVENS_HUB_BRIDGE_TOKEN', '')
OPENCLAW = os.environ.get('OPENCLAW_BIN', '/home/ege/.local/bin/openclaw')
PROFILE = os.environ.get('OPENCLAW_PROFILE', 'inovens')
POLL_SECONDS = max(5, int(os.environ.get('POLL_SECONDS', '15')))

if not TOKEN:
    raise SystemExit('INOVENS_HUB_BRIDGE_TOKEN is required')


def api(method, path, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    req = request.Request(
        BASE_URL + '/api/index.php/' + path.lstrip('/'),
        data=data,
        method=method,
        headers={'Authorization': 'Bearer ' + TOKEN, 'Accept': 'application/json', 'Content-Type': 'application/json'},
    )
    with request.urlopen(req, timeout=30, context=ssl.create_default_context()) as response:
        return json.loads(response.read().decode())


def run_openclaw(*args):
    command = [OPENCLAW, '--profile', PROFILE, *args]
    return subprocess.run(command, text=True, capture_output=True, timeout=45, check=True)


def pending_requests():
    proc = run_openclaw('pairing', 'list', 'telegram', '--json')
    # Some releases print harmless terminal control codes around JSON.
    text = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', proc.stdout)
    start, end = text.find('{'), text.rfind('}')
    if start < 0 or end < start:
        raise RuntimeError('OpenClaw pairing list returned invalid JSON')
    return json.loads(text[start:end + 1]).get('requests', [])


def sender_id(item):
    for key in ('senderId', 'sender_id', 'userId', 'user_id', 'from', 'id'):
        value = item.get(key)
        if isinstance(value, (str, int)) and str(value).isdigit():
            return str(value)
    nested = item.get('sender')
    if isinstance(nested, dict):
        return sender_id(nested)
    return None


def process(job):
    kind = job.get('job_type')
    payload = job.get('payload') or {}
    if kind != 'telegram_pair':
        raise RuntimeError('Unsupported bridge job type')
    code = str(payload.get('code', '')).upper()
    if not re.fullmatch(r'[A-HJ-NP-Z2-9]{8}', code):
        raise RuntimeError('Invalid pairing code')
    match = next((x for x in pending_requests() if str(x.get('code', '')).upper() == code), None)
    if not match:
        raise RuntimeError('Pairing code is missing or expired')
    telegram_id = sender_id(match)
    if not telegram_id:
        raise RuntimeError('Telegram sender id was not present in pairing request')
    run_openclaw('pairing', 'approve', 'telegram', code, '--notify')
    return {'ok': True, 'user_id': int(payload['user_id']), 'telegram_user_id': telegram_id,
            'result': {'message': 'Telegram account linked'}}


def main():
    backoff = POLL_SECONDS
    while True:
        try:
            jobs = api('GET', 'bridge/jobs').get('jobs', [])
            for job in jobs:
                try:
                    result = process(job)
                except Exception as exc:
                    result = {'ok': False, 'result': {'error': str(exc)[:300]}}
                api('POST', f"bridge/jobs/{int(job['id'])}", result)
            backoff = POLL_SECONDS
        except KeyboardInterrupt:
            return
        except Exception as exc:
            print(f'bridge error: {exc}', file=sys.stderr, flush=True)
            backoff = min(120, max(POLL_SECONDS, backoff * 2))
        time.sleep(backoff)


if __name__ == '__main__':
    main()

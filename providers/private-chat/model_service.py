"""LM Studio model selection and serialized resident-model switching. No chat state."""
import fcntl
import json
from pathlib import Path
import subprocess
import time
from urllib.request import Request

class ModelSwitchError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)

def lm_headers(state):
    headers = {'Content-Type': 'application/json'}
    token = state / 'api-token'
    if token.exists():
        headers['Authorization'] = 'Bearer ' + token.read_text().strip()
    return headers

def _models(opener, state):
    req = Request('http://127.0.0.1:1234/api/v1/models', headers=lm_headers(state))
    with opener.open(req) as response:
        return [m for m in json.load(response)['models'] if m.get('type') == 'llm']

def model_catalog(opener, state):
    return [dict(id=m['key'], name=m.get('display_name') or m['key'],
                 loaded=bool(m.get('loaded_instances'))) for m in _models(opener, state)]

def _resolve(model, models):
    if not isinstance(model, str) or not model:
        raise ValueError('Selected model unavailable')
    for m in models:
        if model == m['key'] or any(model == i['id'] for i in m.get('loaded_instances', [])):
            return m
    raise ValueError('Selected model unavailable')

def selected_model(model, opener, state):
    if model is None:
        return 'qwen3.5-9b'
    m = _resolve(model, _models(opener, state))
    instances = m.get('loaded_instances', [])
    return instances[0]['id'] if instances else m['key']

def _cli(*args):
    try:
        p = subprocess.run([str(Path.home()/'.lmstudio/bin/lms'), *args],
                           stdin=subprocess.DEVNULL, capture_output=True, text=True)
    except OSError as e:
        raise ModelSwitchError('model_switch_unavailable') from e
    if p.returncode:
        detail = (p.stdout + p.stderr).lower()
        if 'insufficient system resources' in detail or 'overload your system' in detail:
            raise ModelSwitchError('model_memory_unavailable')
        raise ModelSwitchError('model_load_failed')
    return p.stdout

def _loaded():
    try:
        return json.loads(_cli('ps', '--json'))
    except (ValueError, TypeError) as e:
        raise ModelSwitchError('model_switch_unavailable') from e

def switch_model(model, opener, state):
    # Validate before touching any resident model, including old instance-ID clients.
    key = _resolve(model, _models(opener, state))['key']
    state.mkdir(parents=True, exist_ok=True)
    with open(state/'model-switch.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        while True:
            others = [m for m in _loaded() if m.get('type') == 'llm' and m.get('modelKey') != key]
            if any(m.get('status') != 'idle' or m.get('queued', 0) for m in others):
                time.sleep(0.25)
                continue
            retry = False
            for m in others:
                # Recheck immediately before unloading. Never cancel running inference.
                current = next((n for n in _loaded() if n.get('identifier') == m['identifier']), None)
                if current is None:
                    continue
                if current.get('status') != 'idle' or current.get('queued', 0):
                    retry = True
                    break
                _cli('unload', current['identifier'])
            if retry:
                continue
            target = _resolve(key, _models(opener, state))
            if not target.get('loaded_instances'):
                _cli('load', key, '--identifier', key, '--no-engine-config-file')
            target = _resolve(key, _models(opener, state))
            if not target.get('loaded_instances'):
                raise ModelSwitchError('model_load_failed')
            return {'model': key, 'models': model_catalog(opener, state)}

def ensure_selected_model(model, opener, state):
    key = 'qwen3.5-9b' if model is None else model
    target = _resolve(key, _models(opener, state))
    if not target.get('loaded_instances'):
        switch_model(target['key'], opener, state)
    return selected_model(target['key'], opener, state)

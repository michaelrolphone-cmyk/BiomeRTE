"""LM Studio catalog and explicit chat-model validation. No conversation state."""
import json
from urllib.request import Request


class ModelError(ValueError):
    def __init__(self, code):
        self.public_code = code
        super().__init__(code)


def available_models(opener, state):
    req = Request('http://127.0.0.1:1234/api/v1/models', headers=lm_headers(state))
    with opener.open(req) as response:
        return [m for m in json.load(response)['models'] if m.get('type') == 'llm']

def lm_headers(state):
    headers = {'Content-Type': 'application/json'}
    token = state / 'api-token'
    if token.exists():
        headers['Authorization'] = 'Bearer ' + token.read_text().strip()
    return headers

def model_catalog(opener, state):
    models = available_models(opener, state)
    result = []
    for model in models:
        if model.get('type') != 'llm':
            continue
        instances = model.get('loaded_instances', [])
        key = model['key']
        name = model.get('display_name') or key
        if '@' in key:
            name += ' · ' + key.rsplit('@', 1)[1].upper()
        result.append(dict(id=key, name=name, loaded=bool(instances)))
    return result

def selected_model(model, opener, state):
    if not isinstance(model, str) or not model:
        raise ModelError('selected_model_required')
    for entry in available_models(opener, state):
        instances = entry.get('loaded_instances', [])
        ids = [i['id'] for i in instances]
        if model != entry['key'] and model not in ids:
            continue
        if instances:
            return model if model in ids else ids[0]
        try:
            req = Request('http://127.0.0.1:1234/api/v1/models/load',
                json.dumps({'model': entry['key']}).encode(), lm_headers(state))
            with opener.open(req) as response:
                loaded = json.load(response)
            instance = loaded['instance_id']
            confirmed = available_models(opener, state)
            if not any(m['key'] == entry['key'] and instance in [i['id'] for i in m.get('loaded_instances', [])] for m in confirmed):
                raise ModelError('model_load_failed')
            return instance
        except ModelError:
            raise
        except Exception:
            raise ModelError('model_load_failed') from None
    raise ModelError('selected_model_unavailable')


"""LM Studio catalog and explicit chat-model validation. No conversation state."""
import json
from urllib.request import Request

def lm_headers(state):
    headers = {'Content-Type': 'application/json'}
    token = state / 'api-token'
    if token.exists():
        headers['Authorization'] = 'Bearer ' + token.read_text().strip()
    return headers

def model_catalog(opener, state):
    req = Request('http://127.0.0.1:1234/api/v1/models', headers=lm_headers(state))
    with opener.open(req) as response:
        models = json.load(response)['models']
    result = []
    for model in models:
        if model.get('type') != 'llm':
            continue
        instances = model.get('loaded_instances', [])
        key = model['key']
        result.append(dict(id=instances[0]['id'] if instances else key,
            name=model.get('display_name') or key, loaded=bool(instances)))
    return result

def selected_model(model, opener, state):
    if model is None:
        return 'qwen3.5-9b'  # Compatible with existing paired clients.
    if not isinstance(model, str) or not model or model not in {m['id'] for m in model_catalog(opener, state)}:
        raise ValueError('Selected model unavailable')
    return model

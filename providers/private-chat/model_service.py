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
    result = {}
    for model in models:
        if model.get('type') != 'llm':
            continue
        instances = model.get('loaded_instances', [])
        key = model['key']
        name = model.get('display_name') or key
        if '@' in key:
            name += ' · ' + key.rsplit('@', 1)[1].upper()
        if key in result:
            result[key]["loaded"] |= bool(instances)
        else:
            result[key] = dict(id=key, name=name, loaded=bool(instances))
    return list(result.values())

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


# Native inference implementation. Container workers use the existing HostIO
# model channel instead of accessing native process admission or model tokens.
import os
import sys
import time
from pathlib import Path
from urllib.request import build_opener, ProxyHandler, HTTPRedirectHandler

def conversation_messages(messages):
    if not isinstance(messages, list) or not messages:
        raise ValueError('Invalid conversation')
    result = []
    for index, message in enumerate(messages):
        if (not isinstance(message, dict) or not {'role','content'} <= set(message)
                or set(message) - {'role','content','model'}
                or message['role'] not in ('system','user','assistant')
                or (message['role'] == 'system' and index != 0)
                or not isinstance(message['content'], str)
                or ('model' in message and (message['role'] != 'assistant' or not isinstance(message['model'], str)))):
            raise ValueError('Invalid conversation')
        result.append({'role': message['role'], 'content': message['content']})
    return result

def native_runtime():
    if sys.platform != 'darwin':
        raise RuntimeError('Native Mac model admission is required')
    directory = Path.home()/'.local/share/biome-model-runtime'
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))
    from model_runtime import ModelRuntime
    return ModelRuntime()

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise RuntimeError('Local model redirect refused')

class NativeChatBackend:
    def __init__(self, *, home=None, state=None, lm=None, runtime_factory=None, opener=None):
        self.home = Path(home) if home is not None else Path.home()
        self.state = Path(state) if state is not None else self.home/'.local/share/biome-private-chat'
        self.lm = Path(lm) if lm is not None else self.home/'.local/share/biome-lmstudio'
        self.runtime_factory = runtime_factory or native_runtime
        self.http = opener or build_opener(ProxyHandler({}), NoRedirect())

    def privacy_ready(self):
        cfg = json.loads((self.state/'privacy.json').read_text())
        if cfg != {'lmstudio_request_logging_disabled': True, 'qualification': 'verified'}:
            raise RuntimeError('LM Studio privacy qualification required')
        server = json.loads((self.home/'.lmstudio/.internal/http-server-config.json').read_text())
        if server.get('logSensitiveData') is not False or server.get('logIncomingTokens') is not False:
            raise RuntimeError('LM Studio sensitive logging must be disabled')
        return {'ready': True}

    def catalog(self):
        self.privacy_ready()
        return {'models': model_catalog(self.http, self.lm)}

    def inference(self, messages, model=None):
        self.privacy_ready()
        messages = conversation_messages(messages)
        if not isinstance(model, str) or not model:
            raise ModelError('selected_model_required')
        started = time.monotonic()
        with self.runtime_factory().slot('llm', model) as instance:
            admitted = time.monotonic()
            answer = self._inference(messages, instance)
            answer['diagnostics'].update(requested_model=model, resolved_model=instance,
                model_verified=True, admission_ms=round((admitted-started)*1000))
            return answer

    def _inference(self, messages, model):
        self.privacy_ready()
        messages = conversation_messages(messages)
        body = dict(model=model, messages=messages, stream=False, store=False)
        req = Request('http://127.0.0.1:1234/v1/chat/completions',
                      json.dumps(body).encode(), lm_headers(self.lm))
        started = time.monotonic()
        with self.http.open(req) as response:
            result = json.load(response)
        inference_ms = round((time.monotonic()-started)*1000)
        if result.get('model') != model:
            raise ModelError('model_response_mismatch')
        if 'response_id' in result:
            raise RuntimeError('Unexpected retained response')
        choice = result['choices'][0]
        if choice.get('finish_reason') not in ('stop', None):
            raise RuntimeError('Incomplete completion')
        text = choice['message']['content']
        if not isinstance(text, str) or not text:
            raise RuntimeError('Empty completion')
        usage = result.get('usage', {})
        usage = {key: usage[key] for key in ('prompt_tokens','completion_tokens','total_tokens')
                 if isinstance(usage, dict) and type(usage.get(key)) is int and usage[key] >= 0}
        diagnostics = {'v':1, 'response_model':result['model'],
                       'finish_reason':choice.get('finish_reason'),
                       'inference_ms':inference_ms, 'usage':usage}
        return {'text':text, 'model':result['model'], 'diagnostics':diagnostics}

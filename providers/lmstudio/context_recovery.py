"""Recover proven text-only context exhaustion without accepting truncated output.

Caller holds the host model-admission lock for metadata, reload and inference.
No tools, native stored chats, transport retries or fixed generation deadlines.
"""
import json
import os
from pathlib import Path


def positive(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def loaded_model(api, instance):
    found = [(m, i) for m in api('/api/v1/models')['models']
             for i in m.get('loaded_instances', []) if i.get('id') == instance]
    if len(found) != 1:
        raise RuntimeError('Cannot uniquely verify the admitted model instance')
    model, loaded = found[0]
    size = loaded.get('config', {}).get('context_length')
    maximum = model.get('max_context_length')
    if not positive(size) or not positive(maximum) or size > maximum:
        raise RuntimeError('Cannot verify loaded and supported context lengths')
    return model, loaded, size, maximum


def grow(api, model, loaded, target):
    # Only this admitted instance is unloaded. Do not change other model settings.
    api('/api/v1/models/unload', {'instance_id': loaded['id']})
    body = {'model': model['key'], 'context_length': target}
    for key in ('eval_batch_size', 'parallel', 'flash_attention',
                'num_experts', 'offload_kv_cache_to_gpu'):
        if key in loaded.get('config', {}):
            body[key] = loaded['config'][key]
    result = api('/api/v1/models/load', body)
    instance = result.get('instance_id')
    if not instance:
        raise RuntimeError('Context expansion did not return a loaded instance')
    actual, _, size, _ = loaded_model(api, instance)
    if actual['key'] != model['key'] or size != target:
        raise RuntimeError('Context expansion was not confirmed for the requested model')
    return instance


def complete(api, body, cache_path=None, log=lambda message: None, context_selector=None):
    """Identical pure inference may retry only after measured capacity increases."""
    if body.get('tools') or body.get('tool_choice') or body.get('stream'):
        raise ValueError('Context recovery requires non-streaming text-only inference')
    body = dict(body)
    history = []
    cache = {}
    path = Path(cache_path) if cache_path else None
    if path and path.exists():
        try:
            cache = json.loads(path.read_text())
            if not isinstance(cache, dict): cache = {}
        except (ValueError, OSError):
            cache = {}
    model, loaded, size, maximum = loaded_model(api, body['model'])
    remembered = cache.get(model['key'])
    if context_selector is None and positive(remembered) and size < remembered <= maximum:
        body['model'] = grow(api, model, loaded, remembered)

    while True:
        model, loaded, size, maximum = loaded_model(api, body['model'])
        memory = None
        if context_selector is not None:
            memory = context_selector(model, loaded, size, maximum)
            target = memory['target']
            if target not in (32768, 65536) or target > maximum:
                raise ValueError('Memory policy returned an unsupported context')
            if target != size:
                body['model'] = grow(api, model, loaded, target)
                model, loaded, size, maximum = loaded_model(api, body['model'])
                # A growing load can change pressure; measure again before inference.
                checked = context_selector(model, loaded, size, maximum)
                if checked['target'] < size:
                    body['model'] = grow(api, model, loaded, checked['target'])
                    model, loaded, size, maximum = loaded_model(api, body['model'])
                memory = dict(checked, target=size, rechecked_target=checked['target'])
        if any(attempt['context_length'] == size for attempt in history):
            raise ValueError('Context recovery made no capacity progress after memory changed; '
                             + json.dumps({'context_length': size, 'memory': memory}, sort_keys=True))
        result = api('/v1/chat/completions', body)
        if result.get('model') != body['model']:
            raise RuntimeError('Delegated inference returned a different model')
        choice = result['choices'][0]
        usage = result.get('usage', {})
        prompt = usage.get('prompt_tokens')
        output = usage.get('completion_tokens')
        total = usage.get('total_tokens')
        if not positive(total) and positive(prompt) and positive(output):
            total = prompt + output
        finish = choice.get('finish_reason')
        stop = result.get('stats', {}).get('stop_reason')
        metrics = {'model': result['model'], 'context_length': size,
                   'max_context_length': maximum, 'prompt_tokens': prompt,
                   'completion_tokens': output, 'total_tokens': total,
                   'finish_reason': finish, 'stop_reason': stop, 'memory': memory}
        history.append(metrics)
        log('Inference outcome ' + json.dumps(metrics, sort_keys=True))
        if finish != 'length':
            result['biome_inference'] = {'attempts': history}
            return result
        # llama.cpp reserves one token at the context boundary. An arbitrary
        # output cap or missing usage is not evidence that a larger context helps.
        exhausted = stop == 'contextLengthReached' or (
            positive(total) and total >= size - 1)
        safe = body.get('max_tokens') == -1 and not choice.get('message', {}).get('tool_calls')
        if not exhausted or not safe or size >= maximum:
            raise ValueError('LM Studio finish reason: length; ' + json.dumps(metrics, sort_keys=True))
        if context_selector is not None:
            memory = context_selector(model, loaded, size, maximum)
            target = memory['target']
            if target <= size:
                raise ValueError('LM Studio context exhausted within current 32K/64K memory allowance; '
                                 + json.dumps(dict(metrics, memory=memory), sort_keys=True))
        else:
            target = min(maximum, max(size * 2, (total + 1) if positive(total) else 0))
        body['model'] = grow(api, model, loaded, target)
        # Persist only verified capacity metadata, never prompts or responses.
        cache[model['key']] = target
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(path.name + '.new')
            temporary.write_text(json.dumps(cache, sort_keys=True) + '\n')
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        log('Expanded inference context ' + json.dumps({'model': model['key'], 'from': size, 'to': target}))

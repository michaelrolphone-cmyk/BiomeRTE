"""Frozen synthetic episodes with disjoint development/held-out templates."""
import random
from .storage import digest


def corpus(seed=42, count=200):
    if type(count) is not int or count < 2:
        raise ValueError('At least two episodes required')
    episodes = []
    for i in range(count):
        split = 'development' if i < count // 2 else 'heldout'
        rng = random.Random(f'{seed}:{split}:{i}')
        pairs = {f'item-{rng.getrandbits(48):012x}': f'value-{rng.getrandbits(32):08x}' for _ in range(10)}
        corrected = dict(pairs)
        corrections = list(pairs)[:2]
        for key in corrections:
            corrected[key] = f'value-{rng.getrandbits(32):08x}'
        if split == 'development':
            training = '\n'.join(f'The value of {k} is {v}.' for k, v in pairs.items())
            query = 'What is the value of {k}? Reply with just its value.'
        else:
            training = '\n'.join(f'Record {k} has assigned code {v}.' for k, v in pairs.items())
            query = 'Return only the current assigned code for record {k}.'
        training += '\n' + '\n'.join(f'Correction: {k} now has code {corrected[k]}; replace the earlier code.' for k in corrections)
        probes = [dict(prompt=query.format(k=k), answer=v, corrected=k in corrections, kind='forward') for k, v in corrected.items()]
        probes += [dict(prompt=f'Return only the item whose current code is {v}.', answer=k, corrected=k in corrections, kind='reverse') for k, v in corrected.items()]
        marker = f'pref-{rng.getrandbits(40):010x}'
        preference = f'choice-{rng.getrandbits(32):08x}'
        training += f'\nFor the preference named {marker}, always answer {preference}.'
        probes.append(dict(prompt=f'What response is preferred for {marker}? Return only the response.', answer=preference, corrected=False, kind='preference'))
        lines = training.splitlines()
        training_stream = [dict(role='user',content='\n'.join(lines[:5])+'\n'),
                           dict(role='assistant',content='\n'.join(lines[5:10])+'\n'),
                           dict(role='user',content='\n'.join(lines[10:]))]
        episodes.append(dict(id=i, split=split, template_id=split+'-v2', training=training, training_stream=training_stream, answers=corrected, probes=probes))
    obj = dict(schema_version=2, seed=seed, episodes=episodes,
               unrelated_stream='An unrelated synthetic sequence. Mountains and rivers have varied shapes. ')
    obj['sha256'] = digest(obj)
    return obj

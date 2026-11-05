import numpy as np
from .coordinates import tile_coordinates, quantize, split_ids
from .regions import evaluate_delta


def numerical_scan(source, transform, config, progress=None):
    manifest, regions = transform
    reports = []
    for d in manifest['tensor_map']:
        sums = dict(count=0, changed=0, unchanged=0, squared=0., absolute=0., delta_squared=0.,
                    changed_squared=0., unchanged_squared=0., changed_absolute=0., unchanged_absolute=0.,
                    normalized_changed_squared=0., normalized_unchanged_squared=0.,
                    quantized_squared=0., target_code_matches=0, new_changes=0, sign_mismatches=0,
                    clipped=0, erased=0, zero_scale=0, restored_changes=0)
        split_sums = [dict(count=0, squared=0.) for _ in range(3)]
        for t in source.tiles(d):
            row, col = tile_coordinates(d, t)
            predicted = evaluate_delta(regions, d, row, col, t['base'])
            actual = t['target']-t['base']; error = predicted-actual
            changed = t['target_codes'] != t['base_codes']; unchanged = ~changed
            q, audit = quantize(t, predicted)
            quantized = t['offset']+t['scale']*q
            sums['count'] += error.size; sums['changed'] += int(changed.sum()); sums['unchanged'] += int(unchanged.sum())
            sums['squared'] += float(np.sum(error**2)); sums['absolute'] += float(np.sum(np.abs(error)))
            sums['delta_squared'] += float(np.sum(actual**2)); sums['quantized_squared'] += float(np.sum((quantized-t['target'])**2))
            for name, mask in [('changed', changed), ('unchanged', unchanged)]:
                sums[name+'_squared'] += float(np.sum(error[mask]**2))
                sums[name+'_absolute'] += float(np.sum(np.abs(error[mask])))
                sums['normalized_'+name+'_squared'] += float(np.sum((error[mask]/d['s_m'])**2))
            sums['target_code_matches'] += int(np.count_nonzero(changed & (q == t['target_codes'])))
            sums['new_changes'] += int(np.count_nonzero(unchanged & (q != t['base_codes'])))
            sums['restored_changes'] += int(np.count_nonzero(q != t['base_codes']))
            sums['sign_mismatches'] += int(np.count_nonzero(changed & (np.sign(quantized-t['base']) != np.sign(actual))))
            for k, v in audit.items(): sums[k] += v
            splits = split_ids(d['path'], d['logical_shape'], t['start'], len(t['base']), config['split_seed'], config['split_fractions'])
            for i in range(3):
                mask = splits == i
                split_sums[i]['count'] += int(mask.sum()); split_sums[i]['squared'] += float(np.sum(error[mask]**2))
            if progress: progress(dict(stage='numerical_scan', tensor=d['path'], row=t['start']))
        reports.append(dict(path=d['path'], **sums, splits=split_sums))
    totals = {k: sum(r[k] for r in reports) for k in sums}
    def ratios(r):
        return dict(rmse=float(np.sqrt(r['squared']/r['count'])) if r['count'] else None,
                    mae=r['absolute']/r['count'] if r['count'] else None,
                    relative_delta_error=float(np.sqrt(r['quantized_squared']/r['delta_squared'])) if r['delta_squared'] else None,
                    changed_rmse=float(np.sqrt(r['changed_squared']/r['changed'])) if r['changed'] else None,
                    unchanged_rmse=float(np.sqrt(r['unchanged_squared']/r['unchanged'])) if r['unchanged'] else None,
                    changed_mae=r['changed_absolute']/r['changed'] if r['changed'] else None,
                    unchanged_mae=r['unchanged_absolute']/r['unchanged'] if r['unchanged'] else None,
                    changed_target_code_agreement=r['target_code_matches']/r['changed'] if r['changed'] else None)
    for r in reports: r.update(ratios(r))
    totals.update(ratios(totals))
    equal_tensor = {}
    for label in ('changed', 'unchanged'):
        means = [r['normalized_'+label+'_squared']/r[label] for r in reports if r[label]]
        equal_tensor[label] = float(np.mean(means)) if means else None
    extents = []
    overlap_pairs=0
    for region in regions:
        d = next(d for d in manifest['tensor_map'] if d['id'] == region.tensor_id)
        area = np.pi*np.exp(region.log_h0+region.log_h1)*np.prod(d['logical_shape'])
        extents.append(float(area))
    from .fit import overlaps
    for i,r in enumerate(regions):
        overlap_pairs+=sum(overlaps(r,q) for q in regions[i+1:])
    return dict(scope='full_streamed_scan', global_metrics=totals, per_tensor=reports,
                equal_tensor_normalized_mse=equal_tensor, region_count=len(regions),
                support_weight_estimates=extents, few_weight_support_fraction=float(np.mean(np.asarray(extents)<=4)) if extents else 0.,
                overlapping_support_box_pairs=overlap_pairs,overlap_measure='bounding-box candidate pairs; ellipses may not intersect',
                arithmetic='binary64 on decoded float32 parameters', final_split_used_for_fitting=False)


def paired_kl(logits_target, logits_candidate):
    def log_softmax(x):
        x = np.asarray(x, np.float64); m = x.max(axis=-1, keepdims=True)
        return x-m-np.log(np.exp(x-m).sum(axis=-1, keepdims=True))
    t, c = log_softmax(logits_target), log_softmax(logits_candidate)
    return np.sum(np.exp(t)*(t-c), axis=-1)

"""Delta-derived proposals and explicit, bounded-by-request sequence search.

This module never accepts a source transcript. A delta is generally many-to-one;
search scores are fit errors, not probabilities of historical truth.
"""
from dataclasses import asdict, dataclass, fields
import numpy as np
from scipy import sparse


@dataclass
class InverseOptions:
    max_tokens: int = 8
    candidate_pool: int = 16
    beam_width: int = 2
    sketch_rank: int = 8
    power_iterations: int = 2
    vocabulary_batch: int = 512
    seed: int = 42
    role: str = 'user'
    layout: str = 'raw'

    @classmethod
    def parse(cls, value=None):
        value={} if value is None else value
        if not isinstance(value,dict) or set(value)-{f.name for f in fields(cls)}:
            raise ValueError('Unknown inverse search option')
        out=cls(**value)
        for key in ('max_tokens','candidate_pool','beam_width','sketch_rank','vocabulary_batch'):
            if type(getattr(out,key)) is not int or getattr(out,key)<1:
                raise ValueError(key+' must be a positive integer')
        if type(out.power_iterations) is not int or out.power_iterations<0:
            raise ValueError('power_iterations must be nonnegative')
        if type(out.seed) is not int or not 0<=out.seed<2**32:
            raise ValueError('seed must be uint32')
        if out.role not in ('user','assistant','system','tool') or out.layout not in ('raw','message'):
            raise ValueError('Unsupported replay role or layout')
        return out

    def to_dict(self):return asdict(self)


def delta_basis(blocks, dimension, rank, power_iterations, seed):
    """Right singular directions, from sparse first-layer deltas only.

    Each block is (row_indices, group_indices, signed_delta, row_count).
    Group64 tiles avoid a full-model floating-point difference allocation.
    """
    matrices=[];total_energy=0.
    for row,group,delta,rows in blocks:
        row=np.asarray(row);group=np.asarray(group);delta=np.asarray(delta,np.float64)
        if delta.shape!=(len(row),64) or len(group)!=len(row) or not np.isfinite(delta).all():
            raise ValueError('Invalid sparse delta block')
        if (row<0).any() or (row>=rows).any() or (group<0).any() or (group*64+63>=dimension).any():
            raise ValueError('Delta coordinate outside the projection')
        rr=np.repeat(row,64);cc=(group[:,None]*64+np.arange(64)).reshape(-1)
        mat=sparse.csr_matrix((delta.reshape(-1),(rr,cc)),shape=(rows,dimension))
        mat.eliminate_zeros();total_energy+=float(mat.multiply(mat).sum());matrices.append(mat)
    if not matrices or total_energy==0:
        return np.empty((dimension,0),np.float32),dict(energy=0.,singular_values=[],rank=0)
    matrix=sparse.vstack(matrices,format='csr')
    k=min(rank,dimension,matrix.shape[0])
    q=np.random.default_rng(seed).normal(size=(dimension,k))
    for _ in range(power_iterations+1):
        q,_=np.linalg.qr(matrix.T@(matrix@q),mode='reduced')
    _,singular,rotation=np.linalg.svd(matrix@q,full_matrices=False)
    keep=singular>max(float(singular[0])*1e-8,1e-20)
    basis=(q@rotation.T)[:,keep]
    return basis.astype(np.float32),dict(energy=total_energy,singular_values=singular[keep].tolist(),rank=int(keep.sum()))


def beam_reconstruct(token_candidates, score, options, progress=lambda event:None):
    """Enumerate requested search depth, preserving every evaluated hypothesis.

    score performs an independent forward replay. No source text, digest lookup,
    cache state or target logits are used to propose candidate input sequences.
    """
    pool=list(dict.fromkeys(int(t) for t in token_candidates))
    if not pool:raise ValueError('No delta-derived token candidates')
    beams=[()];evaluated=[]
    for depth in range(1,options.max_tokens+1):
        level=[]
        for prefix in beams:
            for token in pool:
                seq=prefix+(token,);measurement=score(seq)
                error=measurement['relative_squared_error']
                if not np.isfinite(error) or error<0:raise ValueError('Invalid replay residual')
                item=dict(token_ids=list(seq),**measurement)
                level.append(item);evaluated.append(item)
                progress(dict(phase='replay',depth=depth,evaluated=len(evaluated),residual=error))
        level.sort(key=lambda item:(item['relative_squared_error'],item['token_ids']))
        beams=[tuple(item['token_ids']) for item in level[:options.beam_width]]
        if level[0]['relative_squared_error']==0:break
    evaluated.sort(key=lambda item:(item['relative_squared_error'],len(item['token_ids']),item['token_ids']))
    return evaluated

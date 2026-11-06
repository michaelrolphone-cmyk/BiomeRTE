"""Reverse options and retained legacy offline experiment helpers.

Legacy helpers below are not used by the service or reverse runtime. They are
kept for reading prior experimental artifacts, not as a fallback decoder.
"""
from dataclasses import asdict, dataclass
import numpy as np
from scipy import sparse


@dataclass
class InverseOptions:
    mode: str = 'reverse'

    @classmethod
    def parse(cls, value=None):
        value = {} if value is None else value
        if not isinstance(value, dict) or set(value)-{'mode'}:
            raise ValueError('Reverse propagation accepts no token-search or fixed-length options')
        out = cls(**value)
        if out.mode != 'reverse':
            raise ValueError('Vocabulary first-pass and replay modes are retired; use reverse propagation')
        return out

    def to_dict(self):
        return asdict(self)


@dataclass
class LegacyReplayOptions:
    """Offline historical helper only. Not accepted by the service."""
    max_tokens: int = 8
    beam_width: int = 2


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


def first_pass_result(proposals, decode):
    """Return the proposal ranking immediately, without inventing sequence order."""
    tokens=[int(item['token_id']) for item in proposals]
    candidates=[dict(token_ids=[int(item['token_id'])],
                     text=decode([int(item['token_id'])]),
                     proposal_score=float(item['proposal_score'])) for item in proposals]
    return dict(status='complete',context_text=decode(tokens),
                estimated_token_ids=tokens,candidate_kind='ranked_token_hypotheses',
                token_order='proposal_score_descending',sequence_order_inferred=False,
                candidates=candidates,token_proposals=proposals,replay_evaluations=0,
                best_relative_squared_error=None,
                uncertainty_and_gaps=[
                    'Approximate token hypotheses from the first vocabulary pass; no sequence replay was performed.',
                    'Text concatenates tokens in proposal-score order, not recovered source order.',
                    'Proposal scores rank compatibility with aggregate weight changes, not historical probabilities.',
                    'Original wording, repetition, order and topic relevance are unverified.'],
                scientific_reconstruction_success='not_established')


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


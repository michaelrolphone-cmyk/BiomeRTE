"""Direct, approximate embedding-space token quantization.

One-time construction loads the embedding table and partitions it with an explicit work stack.
Emission follows coordinate comparisons down one path to a token ID. It does
not compute vocabulary scores, search candidate tokens, replay the model, or
judge emitted output. This is not a learned decoder or semantic recovery.
"""
from dataclasses import dataclass
import os
from pathlib import Path
import tempfile

import numpy as np


@dataclass(frozen=True)
class _Node:
    axis: int
    threshold: float
    left: int
    right: int
    token: int


class PartitionTokenReadout:
    """A fixed embedding partition with one token at each terminal region.

    Build once for the embedding table in use, then reuse for emissions. Input
    rows are token embeddings, not historical input tokens. Identical vectors
    share a region and deterministically emit the smallest associated token ID.
    Regions approximate embedding geometry; outputs need not be nearest tokens
    or linguistically meaningful. Changes to embeddings require a new tree.
    """

    def __init__(self, embeddings, token_ids=None, *, normalize=True, check=lambda: None):
        check()
        vectors = np.asarray(embeddings, dtype=np.float32)
        if vectors.ndim != 2 or not all(vectors.shape):
            raise ValueError('Embeddings must be a nonempty finite matrix')
        for start in range(0, len(vectors), 1024):
            check()
            if not np.isfinite(vectors[start:start + 1024]).all():
                raise ValueError('Embeddings must be a nonempty finite matrix')
        ids = np.arange(len(vectors), dtype=np.int64) if token_ids is None else np.asarray(token_ids)
        if (ids.shape != (len(vectors),) or ids.dtype.kind not in 'iu' or
                np.any(ids < 0) or np.any(ids > np.iinfo(np.int64).max) or len(np.unique(ids)) != len(ids)):
            raise ValueError('Token IDs must be unique nonnegative integers, one per row')
        self.dimension = vectors.shape[1]
        self.normalize = bool(normalize)
        if self.normalize:
            vectors = vectors.copy()
            for start in range(0, len(vectors), 1024):
                check()
                tile = vectors[start:start + 1024].astype(np.float64)
                norms = np.linalg.norm(tile, axis=1)
                vectors[start:start + 1024] = tile / np.where(norms > 0, norms, 1)[:, None]
        nodes = [None]
        row_order = np.arange(len(vectors))
        pending = [(0, 0, len(vectors))]
        # Degenerate coordinates can create a deep, unbalanced tree. An
        # explicit stack handles that without a recursion limit. Subranges
        # share one row-order array rather than retaining ancestor copies.
        while pending:
            check()
            index, begin, end = pending.pop()
            rows = row_order[begin:end]
            low = np.full(self.dimension, np.inf, dtype=np.float32)
            high = np.full(self.dimension, -np.inf, dtype=np.float32)
            for start in range(0, len(rows), 1024):
                check()
                tile = vectors[rows[start:start + 1024]]
                low = np.minimum(low, tile.min(axis=0))
                high = np.maximum(high, tile.max(axis=0))
            spans = high.astype(np.float64) - low
            del tile, low, high
            axis = int(np.argmax(spans))
            if spans[axis] == 0:
                nodes[index] = _Node(-1, 0.0, -1, -1, int(ids[rows].min()))
                continue
            order = np.argsort(vectors[rows, axis], kind='stable')
            rows[:] = rows[order]
            coordinates = vectors[rows, axis]
            boundaries = np.flatnonzero(coordinates[1:] > coordinates[:-1]) + 1
            split = int(boundaries[np.argmin(np.abs(boundaries - len(rows) / 2))])
            # Compare in float64 so adjacent float32 coordinates remain on
            # opposite sides of the midpoint.
            threshold = (float(coordinates[split - 1]) + float(coordinates[split])) / 2
            del coordinates, order, boundaries, spans
            left, right = len(nodes), len(nodes) + 1
            nodes.extend((None, None))
            nodes[index] = _Node(axis, threshold, left, right, -1)
            pending.append((right, begin + split, end))
            pending.append((left, begin, begin + split))
        self._nodes = tuple(nodes)
        # Neither the embedding matrix nor token histories are retained.

    @classmethod
    def from_embeddings(cls, embeddings, token_ids=None, *, normalize=True, check=lambda: None):
        """Build once; normalize=False permits zero-copy float32 memmap input.

        That mode uses supplied coordinates as-is for construction and readout.
        For a pre-normalized table the caller must also normalize input signals.
        Input matrices are never modified. Tree construction reads row tiles,
        not full matrix copies, when normalization is disabled.
        """
        return cls(embeddings, token_ids, normalize=normalize, check=check)

    def save(self, path, baseline_fingerprint):
        """Atomically persist tree nodes only, bound to a baseline identity."""
        if not isinstance(baseline_fingerprint, str) or not baseline_fingerprint:
            raise ValueError('A nonempty baseline fingerprint is required')
        path = Path(path)
        integers = np.asarray([(n.axis, n.left, n.right, n.token) for n in self._nodes], dtype=np.int64)
        thresholds = np.asarray([n.threshold for n in self._nodes], dtype=np.float64)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.' + path.name + '.',
                                             suffix='.tmp', delete=False) as stream:
                temporary = stream.name
                np.savez(stream, version=np.asarray(1, dtype=np.int64),
                         dimension=np.asarray(self.dimension, dtype=np.int64),
                         normalize=np.asarray(self.normalize, dtype=np.bool_),
                         baseline_fingerprint=np.asarray(baseline_fingerprint),
                         nodes=integers, thresholds=thresholds)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None and os.path.exists(temporary):
                os.unlink(temporary)

    @classmethod
    def load(cls, path, expected_baseline_fingerprint):
        """Read a numeric-only artifact and reject mismatched baselines/trees."""
        if not isinstance(expected_baseline_fingerprint, str) or not expected_baseline_fingerprint:
            raise ValueError('A nonempty expected baseline fingerprint is required')
        with np.load(path, allow_pickle=False) as archive:
            if set(archive.files) != {'version', 'dimension', 'normalize',
                                      'baseline_fingerprint', 'nodes', 'thresholds'}:
                raise ValueError('Invalid readout artifact fields')
            version, dimension = archive['version'], archive['dimension']
            normalize, fingerprint = archive['normalize'], archive['baseline_fingerprint']
            nodes, thresholds = archive['nodes'], archive['thresholds']
            if (version.shape != () or version.dtype != np.dtype('int64') or int(version) != 1 or
                    dimension.shape != () or dimension.dtype != np.dtype('int64') or int(dimension) < 1 or
                    normalize.shape != () or normalize.dtype != np.dtype('bool') or
                    fingerprint.shape != () or fingerprint.dtype.kind != 'U'):
                raise ValueError('Invalid readout artifact metadata')
            if str(fingerprint) != expected_baseline_fingerprint:
                raise ValueError('Readout baseline fingerprint mismatch')
            if (nodes.dtype != np.dtype('int64') or nodes.ndim != 2 or nodes.shape[1] != 4 or
                    len(nodes) == 0 or thresholds.dtype != np.dtype('float64') or
                    thresholds.shape != (len(nodes),) or not np.isfinite(thresholds).all()):
                raise ValueError('Invalid readout node arrays')
            seen, pending, leaf_tokens = set(), [0], set()
            while pending:
                index = pending.pop()
                if index in seen or index < 0 or index >= len(nodes):
                    raise ValueError('Invalid readout tree links')
                seen.add(index)
                axis, left, right, token = map(int, nodes[index])
                if axis == -1:
                    if left != -1 or right != -1 or token < 0 or token in leaf_tokens:
                        raise ValueError('Invalid readout leaf')
                    leaf_tokens.add(token)
                else:
                    if not 0 <= axis < int(dimension) or token != -1 or left <= index or right <= index:
                        raise ValueError('Invalid readout branch')
                    pending.extend((left, right))
            if len(seen) != len(nodes):
                raise ValueError('Unreachable readout nodes')
            result = cls.__new__(cls)
            result.dimension, result.normalize = int(dimension), bool(normalize)
            result._nodes = tuple(_Node(int(row[0]), float(threshold), int(row[1]), int(row[2]), int(row[3]))
                                  for row, threshold in zip(nodes, thresholds))
            return result

    def __call__(self, embedding):
        return self.readout(embedding)

    def readout(self, embedding, trace=None):
        """Map one reverse-propagated embedding signal directly to one ID."""
        vector = np.asarray(embedding, dtype=np.float32)
        if vector.shape != (self.dimension,) or not np.isfinite(vector).all():
            raise ValueError('Readout signal must be a finite embedding vector')
        if self.normalize:
            norm = np.linalg.norm(vector.astype(np.float64))
            if norm > 0:
                vector = (vector / norm).astype(np.float32)
        index = 0
        branches = [] if trace is not None else None
        minimum_margin = closest_split = None
        while True:
            node = self._nodes[index]
            if node.axis < 0:
                if trace is not None:
                    trace(dict(vector=vector, leaf=index, token_id=node.token,
                        branches=branches, minimum_margin=minimum_margin,
                        closest_split=closest_split))
                return node.token
            coordinate = float(vector[node.axis])
            went_left = coordinate < node.threshold
            if trace is not None:
                branches.append([index, node.axis, node.threshold, went_left])
                margin = abs(coordinate - node.threshold)
                if minimum_margin is None or margin < minimum_margin:
                    minimum_margin = margin
                    closest_split = dict(node=index, coordinate=coordinate)
            index = node.left if went_left else node.right

"""Activity-transition descriptors. These are not correctness detectors."""
import numpy as np
from scipy.sparse.csgraph import connected_components
from scipy.linalg import eigvalsh,svdvals


def persistence(similarity):
    from ripser import ripser
    s=np.asarray(similarity,np.float64)
    if s.ndim!=2 or s.shape[0]!=s.shape[1] or not np.isfinite(s).all() or np.any(s<0) or not np.allclose(s,s.T):
        raise ValueError('Expected finite symmetric nonnegative similarity')
    distance=1-s/max(float(s.max()),1e-12);np.fill_diagonal(distance,0)
    diagrams=ripser(distance,distance_matrix=True,maxdim=1)['dgms']
    result={}
    for dimension,diagram in enumerate(diagrams):
        finite=diagram[np.isfinite(diagram[:,1])];life=finite[:,1]-finite[:,0]
        result['H'+str(dimension)]=dict(finite_count=len(finite),essential_count=int(np.sum(~np.isfinite(diagram[:,1]))),total_persistence=float(life.sum()),max_persistence=float(life.max()) if len(life) else 0.,intervals=[[float(b),None if not np.isfinite(d) else float(d)] for b,d in diagram])
    return dict(filtration='flag complex of 1-S/max(S); dissimilarity, triangle inequality not assumed',**result)


def describe(window,persistent_homology=False):
    e=np.asarray(window,np.float64)
    energy=float(np.mean(e*e));e=e/max(np.sqrt(energy),1e-12)
    A=e[:-1].T@e[1:]/(len(e)-1)
    self_persistence=np.diag(A).copy();np.fill_diagonal(A,0)
    S=(A+A.T)/2;d=S.sum(axis=1);isolated=d<=1e-15
    inv=np.zeros_like(d);inv[~isolated]=1/np.sqrt(d[~isolated])
    L=np.eye(len(d))-S*inv[:,None]*inv[None,:]
    L[isolated,:]=0;L[:,isolated]=0
    eigen=eigvalsh(L,check_finite=True)
    outgoing=A.sum(axis=1);incoming=A.sum(axis=0)
    D=A/np.sqrt(np.maximum(outgoing[:,None]*incoming[None,:],1e-30))
    layer=e.reshape(len(e),32,16).mean(axis=-1)
    autocorr={}
    for lag in (1,2,4,8):
        a=layer[:-lag]-layer[:-lag].mean(axis=0);b=layer[lag:]-layer[lag:].mean(axis=0)
        autocorr[str(lag)]=(np.sum(a*b,axis=0)/np.maximum(np.sqrt(np.sum(a*a,axis=0)*np.sum(b*b,axis=0)),1e-12)).tolist()
    result=dict(energy=energy,eigenvalues=eigen[:32].tolist(),heat_trace={str(t):float(np.exp(-t*eigen).sum()) for t in (.1,1,10)},isolated_nodes=int(isolated.sum()),components=int(connected_components(S>1e-15,directed=False,return_labels=False)),singular_values=svdvals(D)[:32].tolist(),antisymmetric_norm=float(np.linalg.norm(A-A.T)),layer_flow=A.reshape(32,16,32,16).sum(axis=(1,3)).tolist(),self_persistence=self_persistence.tolist(),layer_mean=layer.mean(axis=0).tolist(),layer_variance=layer.var(axis=0).tolist(),autocorrelation=autocorr,label="trajectory_novelty_unvalidated")
    if persistent_homology:result['persistence']=persistence(S)
    return result


def dtw(a,b,mean=None,scale=None):
    a=np.asarray(a,float);b=np.asarray(b,float)
    if mean is not None:a=a-mean;b=b-mean
    if scale is not None:a=a/np.maximum(scale,1e-12);b=b/np.maximum(scale,1e-12)
    prev=np.full(len(b)+1,np.inf);prev[0]=0
    for row in a:
        cur=np.full(len(b)+1,np.inf)
        for j,value in enumerate(b,1):cur[j]=np.linalg.norm(row-value)+min(prev[j],cur[j-1],prev[j-1])
        prev=cur
    return float(prev[-1]/max(len(a)+len(b),1))


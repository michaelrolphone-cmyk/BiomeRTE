"""Native tiled Metal path. No full floating point model shadow is allocated."""
import mlx.core as mx

_norm = None
_write = {}


def row_norm(packed, scales, offsets):
    global _norm
    rows,words=packed.shape
    if _norm is None:
        _norm=mx.fast.metal_kernel(name="af_row_norm",input_names=["packed","scales","offsets"],output_names=["norm"],source=r'''
          uint row=thread_position_in_grid.x;
          if (row >= packed_shape[0]) return;
          uint words=packed_shape[1]; float accum=0;
          for(uint j=0;j<words;j++) {
            uint word=packed[row*words+j]; uint g=row*(words/8)+j/8;
            float s=float(scales[g]),b=float(offsets[g]);
            for(uint k=0;k<8;k++) { float w=b+s*float((word>>(4*k))&15); accum+=w*w; }
          }
          norm[row]=max(sqrt(accum),1e-6f);
        ''')
    return _norm(inputs=[packed,scales,offsets],grid=(rows,1,1),threadgroup=(min(256,rows),1,1),output_shapes=[(rows,)],output_dtypes=[mx.float32])[0]


def write(packed, scales, offsets, x, z, gate, norms, *, eta, role_scale,
          step_fraction_max, seed, baseline_pull=0., anchor=None, delta_n=1):
    pull=baseline_pull>0
    if pull and anchor is None: raise ValueError("Anchor required")
    if pull not in _write:
        names=["packed","scales","offsets","x","z","gate","norms","settings","seed"]
        if pull:names.append("anchor")
        decay="float w0=b+s*float((anchor[index]>>(4*k))&15); proposal=w0+settings[3]*(proposal-w0);" if pull else ""
        source=r'''
          uint index=thread_position_in_grid.x;
          uint words=packed_shape[1];
          if(index>=packed_shape[0]*words) return;
          uint row=index/words, col=(index%words)*8, g=row*(words/8)+(index%words)/8;
          uint old=packed[index], result=0; float s=float(scales[g]), b=float(offsets[g]);
          float c=settings[0]*settings[1]*z[row]*float(gate[row]);
          c*=min(1.0f,settings[2]/max(abs(c),1e-12f));
          float changed=0,candidates=0,saturated=0,pnorm=0,rnorm=0,qerror=0;
          for(uint k=0;k<8;k++) {
            uint code=(old>>(4*k))&15, q=code;
            float w=b+s*float(code), proposal=w+norms[row]*c*x[col+k];
            DECAY
            if(s!=0 && proposal!=w) {
              candidates+=1; float raw=(proposal-b)/s;
              if(raw<0 || raw>15) saturated+=1;
              float r=clamp(raw,0.0f,15.0f),lo=floor(r);
              uint h=(index*8+k)^seed[0]; h=(h^(h>>16))*0x7feb352du;
              h=(h^(h>>15))*0x846ca68bu;h^=h>>16;
              float u=float(h>>8)/16777216.0f;
              q=uint(lo)+(u<r-lo ? 1u:0u);
            }
            float actual=b+s*float(q);changed+=float(q!=code);
            pnorm+=(proposal-w)*(proposal-w);rnorm+=(actual-w)*(actual-w);qerror+=(actual-proposal)*(actual-proposal);
            result |= q<<(4*k);
          }
          output[index]=result;
          // One small metric vector per packed word, reduced in tiles by host.
          if (AUDIT) {
            metrics[index*6]=changed;metrics[index*6+1]=candidates;metrics[index*6+2]=saturated;
            metrics[index*6+3]=pnorm;metrics[index*6+4]=rnorm;metrics[index*6+5]=qerror;
          }
        '''.replace("DECAY",decay)
        _write[pull]=mx.fast.metal_kernel(name="af_write_pull" if pull else "af_write_zero",input_names=names,output_names=["output","metrics"],source=source)
    # Per-matrix tiles constrain floating diagnostics to <=~3 MiB.
    rows,words=packed.shape
    chunks=[];metrics=[]
    # Counter uses global matrix coordinates despite tiling via seed? Kernels
    # below receive complete row tensors: tile size is the matrix here. Audit
    # values are separate and allocated only for explicit qualification calls.
    import math
    settings=mx.array([eta,role_scale,step_fraction_max,math.exp(-baseline_pull*delta_n)],mx.float32)
    inputs=[packed,scales,offsets,x,z,gate,norms,settings,mx.array([seed],mx.uint32)]
    if pull: inputs.append(anchor)
    out,metric=_write[pull](inputs=inputs,template=[("AUDIT",False)],grid=(rows*words,1,1),threadgroup=(256,1,1),output_shapes=[packed.shape,(1,)],output_dtypes=[mx.uint32,mx.float32])
    return out

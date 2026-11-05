from pathlib import Path
import json
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

root=Path(__file__).parent
candidates=json.loads((root/'candidates.json').read_text())
comparison=json.loads((root/'comparison.json').read_text())
selected=next(c for c in candidates if c['candidate_id']=='candidate-a940e6537235c7abbdd5')
fig,axes=plt.subplots(1,2,figsize=(12,4.6),layout='constrained')
fig.suptitle('Partial transform qualification',fontsize=17,fontweight='bold')
ax=axes[0]
ax.scatter([c['bytes']/1000 for c in candidates],[c['validation_loss'] for c in candidates],s=17,color='#6e8d9a',alpha=.65,label='Saved candidates')
ax.scatter([selected['bytes']/1000],[selected['validation_loss']],s=75,color='#9a4c97',label='11-region trial candidate',zorder=3)
ax.set(title='Sampled fitting loss',xlabel='Complete artifact size (decimal kB)',ylabel='Mean normalized validation loss')
ax.ticklabel_format(useOffset=False,axis='both')
ax.grid(alpha=.2);ax.legend(frameon=False,fontsize=9)
ax=axes[1];names=['arithmetic','code','instruction'];x=np.arange(len(names));width=.34
for offset,key,label,color in [(-width/2,'T_to_B','Target to baseline','#7d94a0'),(width/2,'T_to_R','Target to restored','#2a8a85')]:
    values=[comparison['paired_kl'][n][key]['mean'] for n in names]
    bars=ax.bar(x+offset,values,width,label=label,color=color)
    ax.bar_label(bars,labels=[f'{v:.5f}' for v in values],fontsize=8,padding=3)
ax.set(title='Matched-prefix behavior',ylabel='Mean KL divergence',xticks=x,xticklabels=names,ylim=(0,.00185))
ax.grid(axis='y',alpha=.2);ax.set_axisbelow(True);ax.legend(frameon=False,fontsize=9)
fig.supxlabel('Partial search; nonzero function in one tensor. All three models passed 3/3 general probes. Useful context retention is unestablished.',fontsize=9)
fig.savefig(root/'qualification.png',dpi=180)

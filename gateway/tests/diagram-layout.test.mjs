import {test} from 'node:test';
import assert from 'node:assert/strict';
import {layoutGraph} from '../dashboard/model.mjs';
const graph={start:'start',nodes:['start','choice','left','right','merge','finish'].map(id=>({id})),edges:[['start','choice'],['choice','left'],['choice','right'],['left','merge'],['right','merge'],['start','merge'],['merge','choice'],['merge','finish'],['finish','start']].map(([from,to])=>({from,to,label:'Branch'}))};
test('branches, shortcuts and multiple return paths route outside every node',()=>{
 const layout=layoutGraph(graph);assert.equal(layout.edges.length,graph.edges.length);
 for(const e of layout.edges)for(let i=1;i<e.points.length;i++){
 const a=e.points[i-1],b=e.points[i];assert.ok(a.x===b.x||a.y===b.y);
 for(const n of layout.nodes){const intersects=a.x===b.x?a.x>n.x&&a.x<n.x+n.w&&Math.max(a.y,b.y)>n.y&&Math.min(a.y,b.y)<n.y+n.h:a.y>n.y&&a.y<n.y+n.h&&Math.max(a.x,b.x)>n.x&&Math.min(a.x,b.x)<n.x+n.w;assert.equal(intersects,false,`${e.from} → ${e.to} crosses ${n.id}`);}
 }
 const external=layout.edges.filter(e=>e.external);assert.ok(external.length>=3);assert.equal(new Set(external.map(e=>Math.max(...e.points.map(p=>p.x)))).size,external.length);
 for(const e of layout.edges)for(const n of layout.nodes){const l=e.labelBox;assert.ok(l.y+l.h/2<=n.y||l.y-l.h/2>=n.y+n.h||l.x+l.w/2<=n.x||l.x-l.w/2>=n.x+n.w,'caption overlaps node');}
});
test('layout preserves topology, is stable and handles an isolated step and self-loop',()=>{
 assert.deepEqual(layoutGraph(graph),layoutGraph(graph));
 const layout=layoutGraph({start:'a',nodes:[{id:'a'},{id:'isolated'}],edges:[{from:'a',to:'a'}]});assert.equal(layout.nodes.length,2);assert.equal(layout.edges.length,1);assert.ok(layout.edges[0].feedback);assert.ok(layout.edges[0].points.length>=5);
});
test('horizontal flow keeps cards upright and routes edges outside all nodes',()=>{
 const layout=layoutGraph(graph,{direction:'horizontal'});assert.equal(layout.direction,'horizontal');assert.ok(layout.width>layout.height);
 for(const n of layout.nodes){assert.equal(n.w,264);assert.equal(n.h,132);}
 for(const e of layout.edges)for(let i=1;i<e.points.length;i++){const a=e.points[i-1],b=e.points[i];for(const n of layout.nodes){const hit=a.x===b.x?a.x>n.x&&a.x<n.x+n.w&&Math.max(a.y,b.y)>n.y&&Math.min(a.y,b.y)<n.y+n.h:a.y>n.y&&a.y<n.y+n.h&&Math.max(a.x,b.x)>n.x&&Math.min(a.x,b.x)<n.x+n.w;assert.equal(hit,false,`${e.from} → ${e.to} crosses ${n.id}`);}}
 const byId=new Map(layout.nodes.map(n=>[n.id,n]));for(const e of layout.edges.filter(e=>!e.feedback))assert.ok(byId.get(e.to).x>byId.get(e.from).x);
});

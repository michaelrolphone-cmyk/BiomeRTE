import fs from 'node:fs';
import path from 'node:path';
const root=process.cwd();
const read=name=>fs.readFileSync(path.join(root,'dashboard',name),'utf8');
let output='';
for(const [symbol,file] of Object.entries({dashboardHTML:'index.html',dashboardJS:'ui.js',dashboardCSS:'ui.css',dashboardModel:'model.mjs',dashboardDiagram:'diagram.mjs'}))output+='export const '+symbol+'='+JSON.stringify(read(file)+(file==='ui.css'?'\n'+read('diagram.css'):''))+';\n';
output+='export const dashboardLogo='+JSON.stringify(fs.readFileSync(path.join(root,'dashboard/logo.png')).toString('base64'))+';\n';
fs.writeFileSync(path.join(root,'lib/dashboard-assets.mjs'),output);
fs.writeFileSync(path.join(root,'lib/dashboard-defaults.mjs'),'export const defaultMappings='+JSON.stringify(JSON.parse(read('mappings.json')).mappings)+';\n');

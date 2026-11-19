import test from 'node:test';
import assert from 'node:assert/strict';
import {logicalServiceCall} from '../dist/server/index.js';
test('production Worker bundle includes logical ingress and preserves declared scope',async()=>{
 const provider='a'.repeat(64),authority='b'.repeat(64);const rows=new Map();
 const DB={prepare(sql){let args=[];return {bind(...values){args=values;return this},
  async first(){if(sql.startsWith('SELECT site'))return {site:'site'};
   if(sql.startsWith('SELECT service_id'))return rows.get(args.join('|'))??null;
   throw Error('Unexpected query')},
  async run(){if(sql.startsWith('CREATE TABLE'))return {};
   if(sql.startsWith('INSERT INTO')){const key=args.slice(0,3).join('|');if(!rows.has(key))rows.set(key,{service_id:args[3],authority:args[4]});return {}}
   throw Error('Unexpected write')}}}};
 const env={DB,SERVICE_AUTHORITY_ORIGIN:'https://service.example',
  SERVICE_AUTHORITY_DECLARATIONS:JSON.stringify([{service_id:'relay-authority',authority,providers:[provider]}])};
 const before=globalThis.fetch;globalThis.fetch=async()=>new Response('',{status:404});
 try{
  const response=await logicalServiceCall(env,{provider},JSON.stringify({site:'site'}));
  assert.equal(response.status,503);
  assert.equal(rows.get('site|provider|'+provider).authority,authority);
 }finally{globalThis.fetch=before}
});

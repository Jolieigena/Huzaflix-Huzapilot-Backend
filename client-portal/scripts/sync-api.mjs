import {writeFile} from 'node:fs/promises';
import {execFileSync} from 'node:child_process';
const response=await fetch((process.env.GATEWAY_URL||'http://localhost:8080')+'/api/docs.json');
if(!response.ok)throw Error('Gateway spec unavailable');
await writeFile('openapi.json',JSON.stringify(await response.json(),null,2));
execFileSync('npm',['run','api:generate'],{stdio:'inherit'});

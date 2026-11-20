import {chromium} from 'playwright';
import fs from 'node:fs';
import {randomBytes} from 'node:crypto';
import {checkSwagger} from './swagger-test.mjs';
const root=process.env.PROJECT_ROOT||process.cwd();
const manifest=JSON.parse(fs.readFileSync(root+'/.huzapilot.json'));
const seeds=JSON.parse(fs.readFileSync(root+'/browser-seeds.json'));
const frontend='http://127.0.0.1:'+(process.env.FRONTEND_PORT||'5173');
const gateway='http://127.0.0.1:'+(process.env.GATEWAY_PORT||'8080');
const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
const page=await browser.newPage();const errors=[];page.on('pageerror',error=>errors.push(error.message));
let fixtureAccountId=null;
try{
 await checkSwagger(page,gateway,manifest);
 await page.goto(frontend);
 await page.getByLabel('Username',{exact:true}).fill(process.env.AUTH_USERNAME);
 await page.getByLabel('Password',{exact:true}).fill(process.env.AUTH_PASSWORD);
 await page.getByRole('button',{name:'Sign in',exact:true}).click();
 await page.getByRole('heading',{name:'Modules',exact:true}).waitFor();
 if(manifest.frontend.componentLibrary?.enabled===false && await page.getByRole('link',{name:'Component studio',exact:true}).count())throw Error('Disabled gallery link remains visible');
 for(const seed of seeds){
  const svc=manifest.services.find(s=>s.name===seed.service),route=svc.routes.find(r=>r.module===seed.module);
  await page.goto(frontend+'/'+seed.service+'/'+seed.module);
  await page.locator('tbody tr').filter({hasText:String(Object.values(seed.data)[0])}).first().waitFor();
  await page.getByRole('button',{name:'Create',exact:true}).click();
  await page.getByRole('dialog').waitFor();
  const marker='browser-'+Date.now();const payload={};
  for(const [key,field] of Object.entries(route.schema.properties)){
   const input=page.getByRole('dialog').locator('[name="'+key+'"]');
   if(field.enum){payload[key]=field.enum[0];await input.selectOption(String(payload[key]));}
   else if(field.type==='boolean'){payload[key]=true;await input.check();}
   else if(field.type==='integer'||field.type==='number'){payload[key]=12;await input.fill('12');}
   else if(field.type==='object'||field.type==='array'){payload[key]=field.type==='object'?{}:[];await input.fill(JSON.stringify(payload[key]));}
   else{payload[key]=field.format==='email'?marker+'@example.com':field.format==='date'?'2026-01-01':marker;await input.fill(payload[key]);}
  }
  const roundtrip=page.waitForResponse(r=>r.request().method()==='POST'&&r.url().endsWith(route.list));
  await page.getByRole('button',{name:'Save',exact:true}).click();
  const response=await roundtrip;if(response.status()!==201)throw Error('Create modal failed: '+await response.text());
  const created=await response.json();
  await page.getByRole('dialog').waitFor({state:'detached'});
  await page.locator('tbody tr').filter({hasText:String(Object.values(payload)[0])}).first().waitFor();
  const tokens=await page.evaluate(()=>JSON.parse(sessionStorage.getItem('huza.session')));
  const actual=await fetch(gateway+route.list+created.id,{headers:{Authorization:'Bearer '+tokens.access_token}});
  if(actual.status!==200)throw Error('Modal record not persisted');
  const data=await actual.json();for(const [key,value] of Object.entries(payload))if(JSON.stringify(data[key])!==JSON.stringify(value))throw Error('Modal payload mismatch '+key);
  // Expiry recovery must retry the failed request using the real refresh endpoint.
  await page.evaluate(()=>{const t=JSON.parse(sessionStorage.getItem('huza.session'));t.access_token='expired';sessionStorage.setItem('huza.session',JSON.stringify(t));});
  await page.reload();await page.locator('tbody tr').first().waitFor();
 }
 // Manage an integration through the generated portal and reuse its original key.
 await page.goto(frontend+'/integrations');
 await page.getByRole('heading',{name:'Integration API keys',exact:true}).waitFor();
 await page.getByRole('button',{name:'Create API key',exact:true}).click();
 const first=manifest.services[0],route=first.routes[0],scope=first.name+':'+route.module;
 const keyName='browser-key-'+Date.now();
 await page.getByLabel('Integration name',{exact:true}).fill(keyName);
 await page.getByLabel(scope+':read',{exact:true}).check();
 await page.getByRole('button',{name:'Save permissions',exact:true}).click();
 const output=page.getByLabel('New API key').locator('output');await output.waitFor();
 const integrationKey=await output.textContent();
 const access=(method='GET')=>fetch(gateway+route.list,{method,headers:{'X-API-Key':integrationKey,'Content-Type':'application/json'},...(method==='POST'?{body:JSON.stringify(seeds[0].data)}:{})});
 if((await access()).status!==200||(await access('POST')).status!==403)throw Error('Portal key initial scopes incorrect');
 const row=page.locator('tbody tr').filter({hasText:keyName});
 await row.getByRole('button',{name:'Edit permissions',exact:true}).click();
 await page.getByLabel(scope+':create',{exact:true}).check();
 await page.getByRole('button',{name:'Save permissions',exact:true}).click();
 await page.getByRole('dialog').waitFor({state:'detached'});
 if((await access('POST')).status!==201)throw Error('Portal expansion did not affect same key');
 await row.getByRole('button',{name:'Edit permissions',exact:true}).click();
 await page.getByLabel(scope+':create',{exact:true}).uncheck();
 await page.getByRole('button',{name:'Save permissions',exact:true}).click();
 await page.getByRole('dialog').waitFor({state:'detached'});
 if((await access('POST')).status!==403)throw Error('Portal reduction did not affect same key');
 await row.getByRole('button',{name:'Revoke',exact:true}).click();
 await page.getByRole('button',{name:'Confirm revoke',exact:true}).click();
 await page.getByRole('dialog').waitFor({state:'detached'});
 if((await access()).status!==401)throw Error('Portal revocation did not disable key');
 // Roles are managed separately, then assigned to accounts using the generated UI.
 await page.goto(frontend+'/access');
 await page.getByRole('heading',{name:'Accounts and access roles',exact:true}).waitFor();
 const roleName='Browser role '+Date.now(),userName='browser-'+Date.now(),userPassword=randomBytes(24).toString('hex');
 await page.getByRole('button',{name:'Create role',exact:true}).click();
 await page.getByLabel('Role name',{exact:true}).fill(roleName);
 await page.getByLabel(scope+':read',{exact:true}).check();
 await page.getByRole('button',{name:'Save role',exact:true}).click();
 await page.getByRole('dialog').waitFor({state:'detached'});
 await page.getByRole('button',{name:'Create account',exact:true}).click();
 await page.getByRole('dialog').getByLabel('Username',{exact:true}).fill(userName);
 await page.getByRole('dialog').getByLabel('Password',{exact:true}).fill(userPassword);
 await page.getByLabel('Access role',{exact:true}).selectOption({label:roleName});
 const accountResponse=page.waitForResponse(r=>r.request().method()==='POST'&&r.url().endsWith('/auth/accounts'));
 await page.getByRole('button',{name:'Save account',exact:true}).click();
 const createdAccountResponse=await accountResponse;if(createdAccountResponse.status()!==201)throw Error('Portal account creation failed');fixtureAccountId=(await createdAccountResponse.json()).id;
 await page.getByRole('dialog').waitFor({state:'detached'});
 const adminSession=await page.evaluate(()=>JSON.parse(sessionStorage.getItem('huza.session')));
 if('refresh_token' in adminSession)throw Error('Refresh token exposed to JavaScript storage');
 const cookies=await page.context().cookies();const refreshCookie=cookies.find(c=>c.name==='huza_refresh');
 if(!refreshCookie?.httpOnly||refreshCookie.sameSite!=='Strict'||refreshCookie.path!=='/auth')throw Error('Refresh cookie protections missing');
 const hostile=await fetch(gateway+'/auth/login',{method:'POST',headers:{'Content-Type':'application/json','Origin':'https://untrusted.example'},body:JSON.stringify({username:process.env.AUTH_USERNAME,password:process.env.AUTH_PASSWORD,browser:true})});
 if(hostile.status!==403)throw Error('Hostile browser origin accepted');
 const limitedContext=await browser.newContext();const limited=await limitedContext.newPage();
 await limited.goto(frontend+'/login');await limited.getByLabel('Username',{exact:true}).fill(userName);await limited.getByLabel('Password',{exact:true}).fill(userPassword);await limited.getByRole('button',{name:'Sign in',exact:true}).click();
 await limited.getByRole('heading',{name:'Modules',exact:true}).waitFor();
 await limited.goto(frontend+'/'+first.name+'/'+route.module);await limited.locator('tbody tr').first().waitFor();
 if(await limited.getByRole('link',{name:'Accounts and roles',exact:true}).count())throw Error('Non-admin management navigation exposed');
 const original=await limited.evaluate(()=>JSON.parse(sessionStorage.getItem('huza.session')).access_token);
 const userAccess=(method='GET')=>fetch(gateway+route.list,{method,headers:{Authorization:'Bearer '+original,'Content-Type':'application/json'},...(method==='POST'?{body:JSON.stringify(seeds[0].data)}:{})});
 if((await userAccess('POST')).status!==403)throw Error('Read role wrote data');
 const roleRow=page.locator('tbody tr').filter({hasText:roleName}).filter({has:page.getByRole('button',{name:'Edit role',exact:true})});
 await roleRow.getByRole('button',{name:'Edit role',exact:true}).click();await page.getByLabel(scope+':create',{exact:true}).check();await page.getByRole('button',{name:'Save role',exact:true}).click();await page.getByRole('dialog').waitFor({state:'detached'});
 if((await userAccess('POST')).status!==201)throw Error('Portal role expansion did not affect same JWT');
 await roleRow.getByRole('button',{name:'Edit role',exact:true}).click();await page.getByLabel(scope+':create',{exact:true}).uncheck();await page.getByRole('button',{name:'Save role',exact:true}).click();await page.getByRole('dialog').waitFor({state:'detached'});
 if((await userAccess('POST')).status!==403)throw Error('Portal role reduction did not affect same JWT');
 await limited.getByRole('button',{name:'Sign out',exact:true}).click();await limited.getByRole('button',{name:'Sign in',exact:true}).waitFor();
 if((await userAccess()).status!==401)throw Error('Browser logout did not revoke access');
 await limitedContext.close();
 const accountRow=page.locator('tbody tr').filter({hasText:userName});
 await accountRow.getByRole('button',{name:'Edit account',exact:true}).click();await page.getByLabel('Account enabled',{exact:true}).uncheck();await page.getByRole('button',{name:'Save account',exact:true}).click();await page.getByRole('dialog').waitFor({state:'detached'});
 const disabledLogin=await fetch(gateway+'/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:userName,password:userPassword})});if(disabledLogin.status!==401)throw Error('Portal account disable did not block login');
 if(seeds.length!==manifest.services.reduce((count,service)=>count+service.routes.length,0))throw Error('Browser seed missing for a model');
 if(errors.length)throw Error(errors.join('\n'));
 console.log('Browser passed: login, real lists, modal create, persistence and JWT refresh for every model; API key create, expand, reduce and revoke; account/role management, live JWT scopes, cookie security and logout');
}finally{
 try{if(fixtureAccountId){const t=await page.evaluate(()=>JSON.parse(sessionStorage.getItem('huza.session')));const cleanup=await fetch(gateway+'/auth/accounts/'+fixtureAccountId,{method:'DELETE',headers:{Authorization:'Bearer '+t.access_token}});if(cleanup.status!==200)throw Error('Browser fixture account cleanup failed');}}
 finally{await browser.close();}
}

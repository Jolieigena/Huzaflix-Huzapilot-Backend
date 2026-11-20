export const baseUrl = (import.meta as ImportMeta & {env:Record<string,string>}).env.VITE_GATEWAY_URL || '';
type Tokens = {access_token:string};
const key='huza.session';
export function session():Tokens|null {try{return JSON.parse(sessionStorage.getItem(key)||'null');}catch{return null;}}
function clear(){sessionStorage.removeItem(key);window.dispatchEvent(new Event('session-change'));}
function save(tokens:Tokens){sessionStorage.setItem(key,JSON.stringify({access_token:tokens.access_token}));window.dispatchEvent(new Event('session-change'));}
const post=(path:string,body:object)=>fetch(baseUrl+path,{method:'POST',credentials:'include',headers:{'Content-Type':'application/json'},body:JSON.stringify({...body,browser:true})});
export async function logout(){try{const r=await post('/auth/logout',{});if(!r.ok)throw Error('Sign-out failed. Please retry.');clear();}catch(e){window.alert((e as Error).message);}}
export async function login(username:string,password:string){const response=await post('/auth/login',{username,password});if(!response.ok)throw Error(response.status===429?'Too many sign-in attempts. Please retry later.':'Login failed');save(await response.json());}
let refreshing:Promise<void>|null=null;
async function refresh(expectedToken:string){const run=async()=>{
 // A delayed 401 (or a queued lock) may refer to a token already replaced.
 // Rotating again would invalidate the access token used by in-flight retries.
 if(session()?.access_token!==expectedToken)return;
 const response=await post('/auth/refresh',{});if(!response.ok){if(response.status===401)clear();throw Error(response.status===401?'Your session expired. Please sign in.':'Authentication is temporarily unavailable.');}save(await response.json());};if(navigator.locks)await navigator.locks.request('huza-refresh',run);else await run();}
export async function authorizedFetch(input:Request){
 const run=(token?:string)=>{const req=input.clone();const headers=new Headers(req.headers);if(token)headers.set('Authorization','Bearer '+token);return fetch(new Request(req,{headers,credentials:'include'}));};
 const token=session()?.access_token;
 let response=await run(token);
 if(response.status===401&&token&&session()){
  if(session()?.access_token===token){refreshing ||= refresh(token).finally(()=>{refreshing=null;});await refreshing;}
  response=await run(session()?.access_token);
 }
 if(!response.ok){let message='Request failed ('+response.status+')';try{const body=await response.json();message=body.error||body.detail||message;}catch{}throw Error(String(message));}
 return response;
}

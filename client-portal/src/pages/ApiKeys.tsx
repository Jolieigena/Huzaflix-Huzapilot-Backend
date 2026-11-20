import {FormEvent,useEffect,useState} from 'react';
import {client} from '../generated';
import type {components} from '../api/schema';
import Modal from '../components/Modal';
import './ApiKeys.css';
type Key=components['schemas']['HuzaApiKey'];
export default function ApiKeys(){
 const [keys,setKeys]=useState<Key[]>([]),[catalog,setCatalog]=useState<string[]>([]),[error,setError]=useState('');
 const [editing,setEditing]=useState<Key|null|undefined>(undefined),[name,setName]=useState(''),[scopes,setScopes]=useState<string[]>([]),[expiry,setExpiry]=useState('');
 const [secret,setSecret]=useState(''),[busy,setBusy]=useState(false),[revoking,setRevoking]=useState<Key|null>(null);
 async function load(){const [k,s]=await Promise.all([client.GET('/auth/api-keys'),client.GET('/auth/scopes')]);setKeys(k.data||[]);setCatalog(s.data||[]);}
 useEffect(()=>{void load().catch(e=>setError(e.message));},[]);
 function open(key:Key|null){setEditing(key);setName(key?.name||'');setScopes(key?.scopes||[]);setExpiry(key?.expiresAt?String(key.expiresAt):'');setError('');setSecret('');}
 async function save(e:FormEvent){e.preventDefault();setBusy(true);setError('');try{
  const body={name,scopes,expiresAt:expiry?Number(expiry):null};
  if(editing){await client.PUT('/auth/api-keys/{id}',{params:{path:{id:editing.id}},body});}
  else{const r=await client.POST('/auth/api-keys',{body});if(!r.data)throw Error('Key creation failed');setSecret(r.data.key);}
  setEditing(undefined);await load();
 }catch(e){setError((e as Error).message);}finally{setBusy(false);}}
 return <section className="api-keys-page"><header className="api-keys-header"><h1 className="api-keys-title">Integration API keys</h1><button className="api-keys-create" onClick={()=>open(null)}>Create API key</button></header>
 <p className="api-keys-help">Choose the models and actions each integration may use. Saving permissions changes access for the existing key. Read permits both list and detail requests.</p>
 {error&&<p className="api-keys-error" role="alert">{error}</p>}
 {secret&&<aside className="api-keys-secret" aria-label="New API key"><p className="api-keys-secret-help">Save this key securely. It is shown only now. Send it in the X-API-Key header.</p><output className="api-keys-secret-value">{secret}</output><button className="api-keys-dismiss" onClick={()=>setSecret('')}>Dismiss key</button></aside>}
 <table className="api-keys-table"><thead className="api-keys-head"><tr className="api-keys-row"><th className="api-keys-cell">Name</th><th className="api-keys-cell">Scopes</th><th className="api-keys-cell">Status</th><th className="api-keys-cell">Actions</th></tr></thead><tbody className="api-keys-body">{keys.map(key=><tr className="api-keys-row" key={key.id}><td className="api-keys-cell">{key.name}</td><td className="api-keys-cell">{key.scopes.join(', ')||'No access'}</td><td className="api-keys-cell">{key.revoked?'Revoked':key.expiresAt&&key.expiresAt<=Date.now()/1000?'Expired':'Active'}</td><td className="api-keys-cell"><button className="api-keys-edit" disabled={key.revoked} onClick={()=>open(key)}>Edit permissions</button><button className="api-keys-revoke" disabled={key.revoked} onClick={()=>setRevoking(key)}>Revoke</button></td></tr>)}</tbody></table>
 {editing!==undefined&&<Modal title={editing?'Edit API key permissions':'Create API key'} onClose={()=>setEditing(undefined)}><form className="api-keys-form" onSubmit={save}><label className="api-keys-label">Integration name<input className="api-keys-input" name="integration-name" value={name} onChange={e=>setName(e.target.value)} required maxLength={120}/></label><label className="api-keys-label">Expiry (Unix seconds, optional)<input className="api-keys-input" name="expiresAt" type="number" step="1" value={expiry} onChange={e=>setExpiry(e.target.value)}/></label><fieldset className="api-keys-scopes"><legend className="api-keys-legend">Allowed scopes</legend>{catalog.map(scope=><label className="api-keys-scope" key={scope}><input className="api-keys-checkbox" type="checkbox" name="scope" value={scope} checked={scopes.includes(scope)} onChange={e=>setScopes(e.target.checked?[...scopes,scope]:scopes.filter(s=>s!==scope))}/>{scope}</label>)}</fieldset><button className="api-keys-save" disabled={busy} type="submit">Save permissions</button></form></Modal>}
 {revoking&&<Modal title="Revoke API key?" onClose={()=>setRevoking(null)}><p className="api-keys-confirm">This permanently disables {revoking.name}.</p><button className="api-keys-confirm-button" disabled={busy} onClick={async()=>{setBusy(true);try{await client.DELETE('/auth/api-keys/{id}',{params:{path:{id:revoking.id}}});setRevoking(null);await load();}catch(e){setError((e as Error).message);}finally{setBusy(false);}}}>Confirm revoke</button></Modal>}
 </section>;
}

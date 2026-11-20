import {ReactNode,useEffect,useState} from 'react';
import {Link} from 'react-router-dom';
import {modules,client} from '../generated';
import {logout} from '../api/session';
import {templateLinks} from '../template-routes';
import './Layout.css';
export default function Layout({children}:{children:ReactNode}){const [access,setAccess]=useState<{admin:boolean;scopes:string[]}|null>(null);useEffect(()=>{const load=()=>{void client.GET('/auth/me').then(r=>setAccess(r.data||null)).catch(()=>setAccess(null));};load();window.addEventListener('session-change',load);return()=>window.removeEventListener('session-change',load);},[]);return <div className="layout" data-theme="light"><header className="layout-header"><Link className="layout-home" to="/">Home</Link>{access?.admin&&<><Link className="layout-integrations" to="/integrations">API keys</Link><Link className="layout-access" to="/access">Accounts and roles</Link></>}{templateLinks}<button className="layout-logout" onClick={logout}>Sign out</button></header><nav className="layout-nav" aria-label="Modules">{modules.filter(m=>access?.scopes.includes(m.service+':'+m.name+':read')).map(m=><Link className="layout-link" key={m.key} to={m.path}>{m.service} / {m.name}</Link>)}</nav><main className="layout-main">{children}</main><footer className="layout-footer"/></div>;}

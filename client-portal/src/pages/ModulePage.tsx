import {useEffect,useState} from 'react';
import {useNavigate,useParams} from 'react-router-dom';
import DataTable,{Row} from '../components/DataTable';
import ModelForm,{Schema} from '../components/ModelForm';
import Modal from '../components/Modal';
import type {ModuleClient} from '../generated';
import './ModulePage.css';
export default function ModulePage({module,view}:{module:ModuleClient;view:string}){
 const navigate=useNavigate(),{id}=useParams();const [rows,setRows]=useState<Row[]>([]),[record,setRecord]=useState<Row|null>(null),[error,setError]=useState(''),[loading,setLoading]=useState(true),[deleting,setDeleting]=useState<Row|null>(null);
 async function load(){setLoading(true);setError('');try{setRows(await module.list());setRecord(id?await module.detail(id):null);}catch(e){setError((e as Error).message);}finally{setLoading(false);}}
 useEffect(()=>{void load();},[module.key,id,view]);
 const fields=Object.keys(module.schema.properties);
 return <section className="module-page"><header className="module-page-header"><h1 className="module-page-title">{module.service} / {module.name}</h1><button className="module-page-create" onClick={()=>navigate(module.path+'/new')}>Create</button></header>{error&&<p className="module-page-error" role="alert">{error}</p>}{loading?<p className="module-page-loading" role="status">Loading…</p>:<DataTable rows={rows} fields={fields} onView={r=>navigate(module.path+'/'+r.id)} onEdit={r=>navigate(module.path+'/'+r.id+'/edit')} onDelete={setDeleting}/>}
 {view==='detail'&&record&&<article className="module-page-detail"><h2 className="module-page-detail-title">Record</h2><dl className="module-page-fields">{Object.entries(record).map(([key,value])=><div className="module-page-field" key={key}><dt className="module-page-field-label">{key}</dt><dd className="module-page-field-value">{typeof value==='object'?JSON.stringify(value):String(value)}</dd></div>)}</dl><button className="module-page-back" onClick={()=>navigate(module.path)}>Back</button></article>}
 {(view==='create'||view==='edit'&&record)&&!loading&&<Modal title={view==='create'?'Create record':'Edit record'} onClose={()=>navigate(module.path)}><ModelForm key={id||'new'} schema={module.schema as Schema} initial={view==='edit'?record||{}:{}} onSave={async data=>{if(view==='edit')await module.update(id!,data);else await module.create(data);navigate(module.path);}}/></Modal>}
 {deleting&&<Modal title="Delete record?" onClose={()=>setDeleting(null)}><p className="module-page-confirm">This permanently deletes the record.</p><button className="module-page-delete" onClick={async()=>{try{await module.remove(deleting.id);setDeleting(null);await load();}catch(e){setError((e as Error).message);}}}>Confirm delete</button></Modal>}
 </section>;
}

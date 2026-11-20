import {useEffect,useState,ReactNode} from 'react';
import {Navigate,Route,Routes} from 'react-router-dom';
import {session} from './api/session';
import Layout from './components/Layout';
import Home from './pages/Home';
import Login from './pages/Login';
import Access from './pages/Access';
import ApiKeys from './pages/ApiKeys';
import {pageRoutes} from './pages/routes';
import {templateRoutes} from './template-routes';
import './App.css';
function Protected({children}:{children:ReactNode}){const [active,setActive]=useState(!!session());useEffect(()=>{const update=()=>setActive(!!session());window.addEventListener('session-change',update);return ()=>window.removeEventListener('session-change',update);},[]);return active?<Layout>{children}</Layout>:<Navigate to="/login" replace/>;}
export default function App(){return <Routes>{templateRoutes}<Route path="/login" element={<Login/>}/><Route path="/access" element={<Protected><Access/></Protected>}/><Route path="/integrations" element={<Protected><ApiKeys/></Protected>}/><Route path="/" element={<Protected><Home/></Protected>}/>{pageRoutes.map(({path,Page})=><Route key={path} path={path} element={<Protected><Page/></Protected>}/>)}<Route path="*" element={<Navigate to="/" replace/>}/></Routes>;}

import {Link} from 'react-router-dom';
import {modules} from '../generated';
import './Home.css';
export default function Home(){return <section className="home"><h1 className="home-title">Modules</h1><ul className="home-list">{modules.map(m=><li className="home-item" key={m.key}><Link className="home-link" to={m.path}>{m.service} / {m.name}</Link></li>)}</ul></section>;}

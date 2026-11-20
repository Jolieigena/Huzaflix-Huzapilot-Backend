import {useEffect,useRef,ReactNode} from 'react';
import './Modal.css';
export default function Modal({title,onClose,children}:{title:string;onClose:()=>void;children:ReactNode}){
 const ref=useRef<HTMLDialogElement>(null);
 useEffect(()=>{ref.current?.showModal();return ()=>ref.current?.close();},[]);
 return <dialog className="modal" ref={ref} onCancel={e=>{e.preventDefault();onClose();}} aria-labelledby="modal-title"><header className="modal-header"><h2 className="modal-title" id="modal-title">{title}</h2><button className="modal-close" onClick={onClose} aria-label="Close">Close</button></header><div className="modal-body">{children}</div></dialog>;
}

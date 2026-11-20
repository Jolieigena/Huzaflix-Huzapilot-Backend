import createClient from 'openapi-fetch';
import type {paths,components} from './api/schema';
import {authorizedFetch,baseUrl} from './api/session';
import type {Row} from './components/DataTable';
import type {Schema} from './components/ModelForm';
export const client=createClient<paths>({baseUrl,fetch:authorizedFetch});
export type QueryFilter=Record<string,unknown>;
export type QueryOptions={sort?:Record<string,1|-1>;skip?:number;limit?:number;projection?:Record<string,unknown>};
export type QueryPage={items:Row[];hasMore:boolean;nextAfter:string|null;total?:number};
export type ModuleClient={query:(filter?:QueryFilter,options?:QueryOptions)=>Promise<Row[]>;aggregate:(pipeline:Record<string,unknown>[])=>Promise<Record<string,unknown>[]>;count:(filter?:QueryFilter)=>Promise<number>;page:(options?:{filter?:QueryFilter;size?:number;after?:string;total?:boolean})=>Promise<QueryPage>;key:string;service:string;name:string;path:string;schema:Schema;list:()=>Promise<Row[]>;detail:(id:string)=>Promise<Row>;create:(body:Record<string,unknown>)=>Promise<Row>;update:(id:string,body:Record<string,unknown>)=>Promise<Row>;remove:(id:string)=>Promise<void>};

export const module0:ModuleClient={key:"projects__project",service:"projects",name:"project",path:"/projects/project",schema:{"type": "object", "properties": {"name": {"type": "string"}, "serviceName": {"type": "string", "default": "projects", "example": "projects", "description": "Editable initial-generation metadata; remove when no longer needed."}, "modelName": {"type": "string", "default": "project", "example": "project", "description": "Editable initial-generation metadata; remove when no longer needed."}}, "required": ["name"], "x-huza-placeholder": true},
query:async(filter={},options={})=>{const r=await client.POST("/api/projects/project/_query",{body:{filter,options}});return r.data as Row[];},
aggregate:async(pipeline)=>{const r=await client.POST("/api/projects/project/_aggregate",{body:{pipeline}});return r.data as Record<string,unknown>[];},
count:async(filter={})=>{const rows=await module0.aggregate([{$match:filter},{$count:'total'}]);return Number(rows[0]?.total||0);},
page:async(options={})=>{const size=options.size??50;if(!Number.isInteger(size)||size<1||size>999)throw new Error('Page size must be between 1 and 999');const filter=options.filter??{},predicate=options.after?{$and:[filter,{id:{$gt:options.after}}]}:filter;const rows=await module0.query(predicate,{sort:{id:1},limit:size+1}),items=rows.slice(0,size),hasMore=rows.length>size;const page:QueryPage={items,hasMore,nextAfter:hasMore?String(items[items.length-1].id):null};if(options.total)page.total=await module0.count(filter);return page;},
list:async()=>{const r=await client.GET("/api/projects/project/");return r.data as Row[];},
detail:async(id)=>{const r=await client.GET("/api/projects/project/{id}",{params:{path:{id}}});return r.data as Row;},
create:async(body)=>{const r=await client.POST("/api/projects/project/",{body:body as components['schemas']["projects__projectInput"]});return r.data as Row;},
update:async(id,body)=>{const r=await client.PUT("/api/projects/project/{id}",{params:{path:{id}},body:body as components['schemas']["projects__projectInput"]});return r.data as Row;},
remove:async(id)=>{await client.DELETE("/api/projects/project/{id}",{params:{path:{id}}});}
};

export const module1:ModuleClient={key:"projects__template",service:"projects",name:"template",path:"/projects/template",schema:{"type": "object", "properties": {"name": {"type": "string"}, "serviceName": {"type": "string", "default": "projects", "example": "projects", "description": "Editable initial-generation metadata; remove when no longer needed."}, "modelName": {"type": "string", "default": "template", "example": "template", "description": "Editable initial-generation metadata; remove when no longer needed."}}, "required": ["name"], "x-huza-placeholder": true},
query:async(filter={},options={})=>{const r=await client.POST("/api/projects/template/_query",{body:{filter,options}});return r.data as Row[];},
aggregate:async(pipeline)=>{const r=await client.POST("/api/projects/template/_aggregate",{body:{pipeline}});return r.data as Record<string,unknown>[];},
count:async(filter={})=>{const rows=await module1.aggregate([{$match:filter},{$count:'total'}]);return Number(rows[0]?.total||0);},
page:async(options={})=>{const size=options.size??50;if(!Number.isInteger(size)||size<1||size>999)throw new Error('Page size must be between 1 and 999');const filter=options.filter??{},predicate=options.after?{$and:[filter,{id:{$gt:options.after}}]}:filter;const rows=await module1.query(predicate,{sort:{id:1},limit:size+1}),items=rows.slice(0,size),hasMore=rows.length>size;const page:QueryPage={items,hasMore,nextAfter:hasMore?String(items[items.length-1].id):null};if(options.total)page.total=await module1.count(filter);return page;},
list:async()=>{const r=await client.GET("/api/projects/template/");return r.data as Row[];},
detail:async(id)=>{const r=await client.GET("/api/projects/template/{id}",{params:{path:{id}}});return r.data as Row;},
create:async(body)=>{const r=await client.POST("/api/projects/template/",{body:body as components['schemas']["projects__templateInput"]});return r.data as Row;},
update:async(id,body)=>{const r=await client.PUT("/api/projects/template/{id}",{params:{path:{id}},body:body as components['schemas']["projects__templateInput"]});return r.data as Row;},
remove:async(id)=>{await client.DELETE("/api/projects/template/{id}",{params:{path:{id}}});}
};

export const module2:ModuleClient={key:"builds__build",service:"builds",name:"build",path:"/builds/build",schema:{"type": "object", "properties": {"name": {"type": "string"}, "serviceName": {"type": "string", "default": "builds", "example": "builds", "description": "Editable initial-generation metadata; remove when no longer needed."}, "modelName": {"type": "string", "default": "build", "example": "build", "description": "Editable initial-generation metadata; remove when no longer needed."}}, "required": ["name"], "x-huza-placeholder": true},
query:async(filter={},options={})=>{const r=await client.POST("/api/builds/build/_query",{body:{filter,options}});return r.data as Row[];},
aggregate:async(pipeline)=>{const r=await client.POST("/api/builds/build/_aggregate",{body:{pipeline}});return r.data as Record<string,unknown>[];},
count:async(filter={})=>{const rows=await module2.aggregate([{$match:filter},{$count:'total'}]);return Number(rows[0]?.total||0);},
page:async(options={})=>{const size=options.size??50;if(!Number.isInteger(size)||size<1||size>999)throw new Error('Page size must be between 1 and 999');const filter=options.filter??{},predicate=options.after?{$and:[filter,{id:{$gt:options.after}}]}:filter;const rows=await module2.query(predicate,{sort:{id:1},limit:size+1}),items=rows.slice(0,size),hasMore=rows.length>size;const page:QueryPage={items,hasMore,nextAfter:hasMore?String(items[items.length-1].id):null};if(options.total)page.total=await module2.count(filter);return page;},
list:async()=>{const r=await client.GET("/api/builds/build/");return r.data as Row[];},
detail:async(id)=>{const r=await client.GET("/api/builds/build/{id}",{params:{path:{id}}});return r.data as Row;},
create:async(body)=>{const r=await client.POST("/api/builds/build/",{body:body as components['schemas']["builds__buildInput"]});return r.data as Row;},
update:async(id,body)=>{const r=await client.PUT("/api/builds/build/{id}",{params:{path:{id}},body:body as components['schemas']["builds__buildInput"]});return r.data as Row;},
remove:async(id)=>{await client.DELETE("/api/builds/build/{id}",{params:{path:{id}}});}
};

export const module3:ModuleClient={key:"builds__deployment",service:"builds",name:"deployment",path:"/builds/deployment",schema:{"type": "object", "properties": {"name": {"type": "string"}, "serviceName": {"type": "string", "default": "builds", "example": "builds", "description": "Editable initial-generation metadata; remove when no longer needed."}, "modelName": {"type": "string", "default": "deployment", "example": "deployment", "description": "Editable initial-generation metadata; remove when no longer needed."}}, "required": ["name"], "x-huza-placeholder": true},
query:async(filter={},options={})=>{const r=await client.POST("/api/builds/deployment/_query",{body:{filter,options}});return r.data as Row[];},
aggregate:async(pipeline)=>{const r=await client.POST("/api/builds/deployment/_aggregate",{body:{pipeline}});return r.data as Record<string,unknown>[];},
count:async(filter={})=>{const rows=await module3.aggregate([{$match:filter},{$count:'total'}]);return Number(rows[0]?.total||0);},
page:async(options={})=>{const size=options.size??50;if(!Number.isInteger(size)||size<1||size>999)throw new Error('Page size must be between 1 and 999');const filter=options.filter??{},predicate=options.after?{$and:[filter,{id:{$gt:options.after}}]}:filter;const rows=await module3.query(predicate,{sort:{id:1},limit:size+1}),items=rows.slice(0,size),hasMore=rows.length>size;const page:QueryPage={items,hasMore,nextAfter:hasMore?String(items[items.length-1].id):null};if(options.total)page.total=await module3.count(filter);return page;},
list:async()=>{const r=await client.GET("/api/builds/deployment/");return r.data as Row[];},
detail:async(id)=>{const r=await client.GET("/api/builds/deployment/{id}",{params:{path:{id}}});return r.data as Row;},
create:async(body)=>{const r=await client.POST("/api/builds/deployment/",{body:body as components['schemas']["builds__deploymentInput"]});return r.data as Row;},
update:async(id,body)=>{const r=await client.PUT("/api/builds/deployment/{id}",{params:{path:{id}},body:body as components['schemas']["builds__deploymentInput"]});return r.data as Row;},
remove:async(id)=>{await client.DELETE("/api/builds/deployment/{id}",{params:{path:{id}}});}
};

export const module4:ModuleClient={key:"workspaces__workspace",service:"workspaces",name:"workspace",path:"/workspaces/workspace",schema:{"type": "object", "properties": {"name": {"type": "string"}, "serviceName": {"type": "string", "default": "workspaces", "example": "workspaces", "description": "Editable initial-generation metadata; remove when no longer needed."}, "modelName": {"type": "string", "default": "workspace", "example": "workspace", "description": "Editable initial-generation metadata; remove when no longer needed."}}, "required": ["name"], "x-huza-placeholder": true},
query:async(filter={},options={})=>{const r=await client.POST("/api/workspaces/workspace/_query",{body:{filter,options}});return r.data as Row[];},
aggregate:async(pipeline)=>{const r=await client.POST("/api/workspaces/workspace/_aggregate",{body:{pipeline}});return r.data as Record<string,unknown>[];},
count:async(filter={})=>{const rows=await module4.aggregate([{$match:filter},{$count:'total'}]);return Number(rows[0]?.total||0);},
page:async(options={})=>{const size=options.size??50;if(!Number.isInteger(size)||size<1||size>999)throw new Error('Page size must be between 1 and 999');const filter=options.filter??{},predicate=options.after?{$and:[filter,{id:{$gt:options.after}}]}:filter;const rows=await module4.query(predicate,{sort:{id:1},limit:size+1}),items=rows.slice(0,size),hasMore=rows.length>size;const page:QueryPage={items,hasMore,nextAfter:hasMore?String(items[items.length-1].id):null};if(options.total)page.total=await module4.count(filter);return page;},
list:async()=>{const r=await client.GET("/api/workspaces/workspace/");return r.data as Row[];},
detail:async(id)=>{const r=await client.GET("/api/workspaces/workspace/{id}",{params:{path:{id}}});return r.data as Row;},
create:async(body)=>{const r=await client.POST("/api/workspaces/workspace/",{body:body as components['schemas']["workspaces__workspaceInput"]});return r.data as Row;},
update:async(id,body)=>{const r=await client.PUT("/api/workspaces/workspace/{id}",{params:{path:{id}},body:body as components['schemas']["workspaces__workspaceInput"]});return r.data as Row;},
remove:async(id)=>{await client.DELETE("/api/workspaces/workspace/{id}",{params:{path:{id}}});}
};

export const module5:ModuleClient={key:"workspaces__member",service:"workspaces",name:"member",path:"/workspaces/member",schema:{"type": "object", "properties": {"name": {"type": "string"}, "serviceName": {"type": "string", "default": "workspaces", "example": "workspaces", "description": "Editable initial-generation metadata; remove when no longer needed."}, "modelName": {"type": "string", "default": "member", "example": "member", "description": "Editable initial-generation metadata; remove when no longer needed."}}, "required": ["name"], "x-huza-placeholder": true},
query:async(filter={},options={})=>{const r=await client.POST("/api/workspaces/member/_query",{body:{filter,options}});return r.data as Row[];},
aggregate:async(pipeline)=>{const r=await client.POST("/api/workspaces/member/_aggregate",{body:{pipeline}});return r.data as Record<string,unknown>[];},
count:async(filter={})=>{const rows=await module5.aggregate([{$match:filter},{$count:'total'}]);return Number(rows[0]?.total||0);},
page:async(options={})=>{const size=options.size??50;if(!Number.isInteger(size)||size<1||size>999)throw new Error('Page size must be between 1 and 999');const filter=options.filter??{},predicate=options.after?{$and:[filter,{id:{$gt:options.after}}]}:filter;const rows=await module5.query(predicate,{sort:{id:1},limit:size+1}),items=rows.slice(0,size),hasMore=rows.length>size;const page:QueryPage={items,hasMore,nextAfter:hasMore?String(items[items.length-1].id):null};if(options.total)page.total=await module5.count(filter);return page;},
list:async()=>{const r=await client.GET("/api/workspaces/member/");return r.data as Row[];},
detail:async(id)=>{const r=await client.GET("/api/workspaces/member/{id}",{params:{path:{id}}});return r.data as Row;},
create:async(body)=>{const r=await client.POST("/api/workspaces/member/",{body:body as components['schemas']["workspaces__memberInput"]});return r.data as Row;},
update:async(id,body)=>{const r=await client.PUT("/api/workspaces/member/{id}",{params:{path:{id}},body:body as components['schemas']["workspaces__memberInput"]});return r.data as Row;},
remove:async(id)=>{await client.DELETE("/api/workspaces/member/{id}",{params:{path:{id}}});}
};

export const modules:ModuleClient[]=[module0,module1,module2,module3,module4,module5];

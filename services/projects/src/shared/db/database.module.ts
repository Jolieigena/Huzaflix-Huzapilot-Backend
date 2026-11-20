import {Global,Module} from '@nestjs/common';
import {readFileSync} from 'fs';
import {Store} from './store';

@Global()
@Module({providers:[{provide:Store,useFactory:async()=>{
  const cfg=JSON.parse(readFileSync('component.json','utf8'));
  const store=new Store();await store.init(cfg.role==='service'?cfg.modules:['api_keys']);return store;
}}],exports:[Store]})
export class DatabaseModule {}

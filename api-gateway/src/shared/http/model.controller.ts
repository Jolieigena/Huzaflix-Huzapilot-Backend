import {Request,Response} from 'express';
import {cached} from '../cache';
import {Body,Delete,Get,HttpCode,Param,Post,Put,Req,Res} from '@nestjs/common';
import {Model} from '../db/model';
import {Document,QueryError} from '../db/query';

/** Generated controllers inherit these routes and inject their module service. */
export abstract class ModelController {
  constructor(protected readonly service:Model){}
  @Get('/') list(@Req()req:Request,@Res({passthrough:true})res:Response){return cached(this.service.store.cache,this.service.name,req,res,null,()=>this.service.find());}
  @Post('/') create(@Body()body:Document){return this.service.create(body);}
  @Post('/_query') @HttpCode(200) query(@Body()body:Document,@Req()req:Request,@Res({passthrough:true})res:Response){
    if(!body||Object.keys(body).some(k=>!['filter','options'].includes(k)))throw new QueryError('Unknown query fields');
    if(Object.values(body).some(v=>!v||typeof v!=='object'||Array.isArray(v)))throw new QueryError('Filter and options must be objects');
    return cached(this.service.store.cache,this.service.name,req,res,body,()=>this.service.find((body.filter??{}) as Document,(body.options??{}) as Document));
  }
  @Post('/_aggregate') @HttpCode(200) aggregate(@Body()body:Document,@Req()req:Request,@Res({passthrough:true})res:Response){
    if(!body||Object.keys(body).length!==1||!Object.hasOwn(body,'pipeline'))throw new QueryError('Expected pipeline');
    return cached(this.service.store.cache,this.service.name,req,res,body,()=>this.service.aggregate(body.pipeline));
  }
  @Get('/:id') async detail(@Param('id')id:string,@Req()req:Request,@Res({passthrough:true})res:Response){return cached(this.service.store.cache,this.service.name,req,res,null,async()=>{const row=await this.service.findById(id);if(!row)throw new QueryError('Not found','NOT_FOUND',404);return row;});}
  @Put('/:id') update(@Param('id')id:string,@Body()body:Document){return this.service.updateById(id,body);}
  @Delete('/:id') remove(@Param('id')id:string){return this.service.deleteById(id);}
}

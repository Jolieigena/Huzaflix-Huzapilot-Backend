import {All,Controller,Req,Res} from '@nestjs/common';
import {Request,Response} from 'express';
import {GatewayProxy} from '../shared/http/gateway';

@Controller('api/workspaces')
export class WorkspacesRouter {
  constructor(private readonly proxy:GatewayProxy) {}
  @All('*') forward(@Req()req:Request,@Res()res:Response) { return this.proxy.forward("workspaces",req,res); }
}

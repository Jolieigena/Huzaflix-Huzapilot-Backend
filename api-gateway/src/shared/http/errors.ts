import {ArgumentsHost,Catch,ExceptionFilter} from '@nestjs/common';
import {Response} from 'express';
import {QueryError} from '../db/query';

@Catch(QueryError)
export class QueryExceptionFilter implements ExceptionFilter {
  catch(error:QueryError,host:ArgumentsHost){host.switchToHttp().getResponse<Response>().status(error.status).json({error:error.message,code:error.code});}
}

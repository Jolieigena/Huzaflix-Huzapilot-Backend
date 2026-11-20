import {Injectable} from '@nestjs/common';
import {Store} from '../../shared/db/store';
import {Model} from '../../shared/db/model';
import {schema} from './template.model';

/** Add business rules here. Override beforeCreate/beforeUpdate/etc as needed. */
@Injectable()
export class TemplateService extends Model {
  constructor(store:Store) { super(store,"template",schema); }
}

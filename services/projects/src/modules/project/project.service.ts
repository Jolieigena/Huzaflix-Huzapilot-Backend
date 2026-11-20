import {Injectable} from '@nestjs/common';
import {Store} from '../../shared/db/store';
import {Model} from '../../shared/db/model';
import {schema} from './project.model';

/** Add business rules here. Override beforeCreate/beforeUpdate/etc as needed. */
@Injectable()
export class ProjectService extends Model {
  constructor(store:Store) { super(store,"project",schema); }
}

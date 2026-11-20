import {Controller} from '@nestjs/common';
import {ModelController} from '../../shared/http/model.controller';
import {ProjectService} from './project.service';

@Controller("project")
export class ProjectController extends ModelController {
  constructor(service:ProjectService) { super(service); }
}

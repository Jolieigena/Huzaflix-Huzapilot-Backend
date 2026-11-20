import {Controller} from '@nestjs/common';
import {ModelController} from '../../shared/http/model.controller';
import {TemplateService} from './template.service';

@Controller("template")
export class TemplateController extends ModelController {
  constructor(service:TemplateService) { super(service); }
}

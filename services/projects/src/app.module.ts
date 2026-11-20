import {Module} from '@nestjs/common';
import {DatabaseModule} from './shared/db/database.module';
import {ProjectModule} from './modules/project/project.module';
import {TemplateModule} from './modules/template/template.module';
@Module({imports:[DatabaseModule,ProjectModule,TemplateModule]})
export class AppModule {}

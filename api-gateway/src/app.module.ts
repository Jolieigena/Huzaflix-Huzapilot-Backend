import {Module} from '@nestjs/common';
import {DatabaseModule} from './shared/db/database.module';
import {GatewayProxy} from './shared/http/gateway';
import {ProjectsRouter} from './routers/projects.router';
import {BuildsRouter} from './routers/builds.router';
import {WorkspacesRouter} from './routers/workspaces.router';
@Module({imports:[DatabaseModule],controllers:[ProjectsRouter,BuildsRouter,WorkspacesRouter],providers:[GatewayProxy]})
export class AppModule {}

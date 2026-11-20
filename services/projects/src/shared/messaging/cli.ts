import {EventBus,Event} from './bus';
import {readFileSync} from 'node:fs';
async function main(){
 const [command,broker,topic,group,count]=process.argv.slice(2);
 if(broker!=='kafka'&&broker!=='rabbitmq')throw new Error('Usage: cli prepare|publish|consume kafka|rabbitmq topic [group] [count]');
 const bus=new EventBus(broker);
 try {
  if(command==='prepare')await bus.prepare(topic,group);
  else if(command==='publish')await bus.publish(topic,JSON.parse(readFileSync(0,'utf8')) as Event);
  else if((command==='consume'||command==='consume-fail'))await bus.consume(topic,group,async message=>{if(command==='consume-fail')throw new Error('Intentional handler failure');console.log(JSON.stringify(message));},{limit:Number(count||1),timeoutMs:45000,retries:0});
  else throw new Error('Unknown messaging command');
 }finally{await bus.close();}
}
main().catch(error=>{console.error(error.message);process.exitCode=1;});

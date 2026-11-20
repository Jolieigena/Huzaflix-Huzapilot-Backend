import {event as observeEvent} from '../monitoring/telemetry';
import {randomUUID} from 'node:crypto';
import {readFileSync} from 'node:fs';
import {Kafka, Producer, logLevel} from 'kafkajs';
import * as amqp from 'amqplib';

export interface Event {specversion:'1.0';id:string;source:string;type:string;time:string;datacontenttype:'application/json';data:unknown;correlationid?:string}
export function event(type:string,data:unknown,source=process.env.SERVICE_NAME||'service',correlationid?:string):Event {
  return {specversion:'1.0',id:randomUUID(),source,type,time:new Date().toISOString(),datacontenttype:'application/json',data,...(correlationid?{correlationid}:{})};
}
function identifier(value:string){if(!/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(value))throw new Error('Invalid topic/group name');return value;}
const sleep=(ms:number)=>new Promise(resolve=>setTimeout(resolve,ms));
export interface ConsumeOptions {limit?:number;timeoutMs?:number;retries?:number;deadLetterTopic?:string;signal?:AbortSignal}
/** One serial worker per bus. At-least-once delivery: handlers must be idempotent.
 * Call prepare(topic, group) before RabbitMQ publication. Unroutable sends fail.
 * No implicit transaction spans the database and the broker.
 */
export class EventBus {
  private producer?:Producer;
  private connection?:amqp.Connection;
  private channel?:amqp.ConfirmChannel;
  constructor(readonly broker:'kafka'|'rabbitmq') {if(!['kafka','rabbitmq'].includes(broker))throw new Error('Unknown broker');}
  private kafka():Kafka {
    const tls=process.env.KAFKA_TLS==='true';
    return new Kafka({clientId:process.env.SERVICE_NAME||'service',brokers:(process.env.KAFKA_BROKERS||'').split(','),logLevel:logLevel.ERROR,
      ssl:tls?(process.env.KAFKA_CA_FILE?{ca:[readFileSync(process.env.KAFKA_CA_FILE,'utf8')]}:true):undefined,
      sasl:process.env.KAFKA_USERNAME?{mechanism:'plain',username:process.env.KAFKA_USERNAME,password:process.env.KAFKA_PASSWORD||''}:undefined});
  }
  private async rabbit(){
    if(!this.channel){this.connection=await amqp.connect(process.env.RABBITMQ_URL||'',{timeout:10000});this.channel=await this.connection.createConfirmChannel();}
    return this.channel;
  }
  async prepare(topic:string,group?:string){
    identifier(topic);if(group)identifier(group);
    if(this.broker==='kafka') {const admin=this.kafka().admin();await admin.connect();try{await admin.createTopics({waitForLeaders:true,topics:[{topic,numPartitions:1,replicationFactor:1}]});}finally{await admin.disconnect();}}
    else {const ch=await this.rabbit();await ch.assertExchange(topic,'direct',{durable:true});if(group){await ch.assertQueue(topic+'.'+group,{durable:true});await ch.bindQueue(topic+'.'+group,topic,topic);}}
  }
  async publish(topic:string,message:Event){try{await this.publishObserved(topic,message);observeEvent(this.broker,"publish","success");}catch(error){observeEvent(this.broker,"publish","error");throw error;}}
  private async publishObserved(topic:string,message:Event){
    identifier(topic);if(!message||typeof message.id!=='string')throw new Error('Expected event envelope with id');
    const raw=Buffer.from(JSON.stringify(message));if(raw.length>1048576)throw new Error('Event exceeds 1 MiB');
    if(this.broker==='kafka'){
      if(!this.producer){this.producer=this.kafka().producer({idempotent:true,maxInFlightRequests:1});await this.producer.connect();}
      await this.producer.send({topic,acks:-1,timeout:30000,messages:[{key:message.id,value:raw}]});
    }else{
      const ch=await this.rabbit();await ch.assertExchange(topic,'direct',{durable:true});
      let returned=false;const onReturn=(m:amqp.Message)=>{if(m.properties.messageId===message.id)returned=true;};ch.on('return',onReturn);
      try{await new Promise<void>((resolve,reject)=>ch.publish(topic,topic,raw,{persistent:true,mandatory:true,contentType:'application/json',messageId:message.id},error=>error?reject(error):returned?reject(new Error('Unroutable event')):resolve()));}
      finally{ch.removeListener('return',onReturn);}
    }
  }
  async consume(topic:string,group:string,handler:(message:Event)=>Promise<void>,options:ConsumeOptions={}){
    identifier(topic);identifier(group);const {limit=0,timeoutMs=0,retries=3,deadLetterTopic,signal}=options;
    if(!Number.isInteger(limit)||limit<0||timeoutMs<0||!Number.isInteger(retries)||retries<0||retries>10)throw new Error('Invalid consumer limits');
    if(deadLetterTopic){identifier(deadLetterTopic);if(deadLetterTopic===topic)throw new Error('Dead-letter topic must differ');}
    const deadline=timeoutMs?Date.now()+timeoutMs:Infinity;let count=0;
    const handle=async(raw:Buffer)=>{
      const message:Event=JSON.parse(raw.toString());if(!message||typeof message.id!=='string')throw new Error('Invalid event envelope');
      for(let attempt=0;;attempt++)try{await handler(message);observeEvent(this.broker,"handle","success");return;}catch(error){observeEvent(this.broker,"handle","error");
        if(attempt===retries){if(!deadLetterTopic)throw error;await this.publish(deadLetterTopic,event('delivery.failed',{original:message,attempts:attempt+1},undefined,message.correlationid));return;}
        await sleep(Math.min(2000,100*2**attempt));
      }
    };
    if(this.broker==='rabbitmq'){
      await this.prepare(topic,group);const ch=await this.rabbit();
      while((!limit||count<limit)&&!signal?.aborted){
        if(Date.now()>=deadline)throw new Error('Consumer deadline expired');
        const message=await ch.get(topic+'.'+group,{noAck:false});if(!message){await sleep(100);continue;}
        try{await handle(message.content);}catch(error){ch.nack(message,false,true);throw error;}ch.ack(message);count++;
      }
    }else{
      const consumer=this.kafka().consumer({groupId:group,retry:{retries:0,restartOnFailure:async()=>false}});await consumer.connect();
      let finished=false;let failure:unknown;let resolveDone:()=>void=()=>{};
      const done=new Promise<void>(resolve=>{resolveDone=resolve;});
      const finish=(error?:unknown)=>{if(finished)return;finished=true;failure=error;resolveDone();};
      const timer=setInterval(()=>{if(signal?.aborted)finish();else if(Date.now()>=deadline)finish(new Error('Consumer deadline expired'));},100);
      consumer.on(consumer.events.CRASH,e=>finish(e.payload.error));
      try{
        await consumer.subscribe({topic,fromBeginning:true});
        await consumer.run({autoCommit:false,eachMessage:async({topic,partition,message,heartbeat})=>{
          if(finished)return;
          try{await handle(message.value||Buffer.alloc(0));await heartbeat();await consumer.commitOffsets([{topic,partition,offset:(BigInt(message.offset)+1n).toString()}]);count++;if(limit&&count>=limit)finish();}
          catch(error){finish(error);return;}
        }});
        await done;
      }finally{clearInterval(timer);await consumer.stop();await consumer.disconnect();}
      if(failure)throw failure;
    }
    return count;
  }
  async close(){await this.producer?.disconnect();await this.channel?.close();await this.connection?.close();this.producer=undefined;this.channel=undefined;this.connection=undefined;}
}

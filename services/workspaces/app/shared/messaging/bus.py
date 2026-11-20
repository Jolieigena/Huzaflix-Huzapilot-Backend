"""Confirmed publishing and at-least-once consumption for Kafka and RabbitMQ.

Acknowledge only after handler success or confirmed dead-letter publication.
Consumers process serially. Handlers must be idempotent and bound their own I/O.
"""
from app.shared.monitoring import event as observe_event
import datetime
import json
import os
import re
import time
import uuid


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', value):
        raise ValueError('Invalid topic/group name')
    return value


def event(kind, data, source=None, correlation_id=None):
    return {'specversion': '1.0', 'id': str(uuid.uuid4()), 'source': source or os.getenv('SERVICE_NAME', 'service'),
            'type': kind, 'time': datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'datacontenttype': 'application/json', 'data': data,
            **({'correlationid': correlation_id} if correlation_id else {})}


class EventBus:
    def __init__(self, broker):
        if broker not in ('kafka', 'rabbitmq'): raise ValueError('Unknown broker')
        self.broker, self._producer, self._connection, self._channel = broker, None, None, None

    def _kafka_config(self):
        config = {'bootstrap.servers': os.environ['KAFKA_BROKERS'], 'client.id': os.getenv('SERVICE_NAME', 'service')}
        tls = os.getenv('KAFKA_TLS', 'false').lower() == 'true'
        if os.getenv('KAFKA_USERNAME'):
            config.update({'security.protocol': 'SASL_SSL' if tls else 'SASL_PLAINTEXT', 'sasl.mechanism': 'PLAIN', 'sasl.username': os.environ['KAFKA_USERNAME'], 'sasl.password': os.environ['KAFKA_PASSWORD']})
        elif tls: config['security.protocol'] = 'SSL'
        if os.getenv('KAFKA_CA_FILE'): config['ssl.ca.location'] = os.environ['KAFKA_CA_FILE']
        return config

    def _rabbit(self):
        if self._channel is None:
            import pika
            parameters = pika.URLParameters(os.environ['RABBITMQ_URL'])
            parameters.heartbeat = 30; parameters.blocked_connection_timeout = 10
            self._connection = pika.BlockingConnection(parameters)
            self._channel = self._connection.channel(); self._channel.confirm_delivery()
        return self._channel

    def prepare(self, topic, group=None):
        identifier(topic)
        if self.broker == 'kafka':
            from confluent_kafka.admin import AdminClient, NewTopic
            from confluent_kafka import KafkaError, KafkaException
            admin = AdminClient(self._kafka_config())
            for future in admin.create_topics([NewTopic(topic, num_partitions=1, replication_factor=1)], request_timeout=20).values():
                try: future.result()
                except KafkaException as error:
                    if error.args[0].code() != KafkaError.TOPIC_ALREADY_EXISTS: raise
        else:
            channel = self._rabbit(); channel.exchange_declare(exchange=topic, exchange_type='direct', durable=True)
            if group:
                queue = topic + '.' + identifier(group)
                channel.queue_declare(queue=queue, durable=True); channel.queue_bind(queue=queue, exchange=topic, routing_key=topic)

    def publish(self, topic, message):
        try:
            self._publish_observed(topic,message)
            observe_event(self.broker,"publish","success")
        except Exception:
            observe_event(self.broker,"publish","error")
            raise

    def _publish_observed(self, topic, message):
        identifier(topic)
        if not isinstance(message, dict) or not isinstance(message.get('id'), str): raise ValueError('Expected an event envelope with an id')
        raw = json.dumps(message, allow_nan=False).encode()
        if len(raw) > 1048576: raise ValueError('Event exceeds 1 MiB')
        if self.broker == 'kafka':
            from confluent_kafka import Producer
            if self._producer is None: self._producer = Producer({**self._kafka_config(), 'enable.idempotence': True, 'acks': 'all', 'delivery.timeout.ms': 30000})
            errors = []
            self._producer.produce(topic, key=message['id'].encode(), value=raw, on_delivery=lambda error, _message: errors.append(error) if error else None)
            if self._producer.flush(35) or errors: raise RuntimeError('Kafka did not confirm delivery')
        else:
            import pika
            channel = self._rabbit()
            channel.exchange_declare(exchange=topic, exchange_type='direct', durable=True)
            channel.basic_publish(exchange=topic, routing_key=topic, body=raw,
                                  properties=pika.BasicProperties(content_type='application/json', delivery_mode=2, message_id=message['id']), mandatory=True)

    def consume(self, topic, group, handler, *, limit=0, timeout=0, retries=3, dead_letter_topic=None, stop=None):
        identifier(topic); identifier(group)
        if limit < 0 or timeout < 0 or not 0 <= retries <= 10: raise ValueError('Invalid consumer limits')
        if dead_letter_topic:
            identifier(dead_letter_topic)
            if dead_letter_topic == topic: raise ValueError('Dead-letter topic must differ from source')
        deadline = time.monotonic() + timeout if timeout else float('inf')
        processed = 0
        def handle(raw):
            message = json.loads(raw)
            if not isinstance(message, dict) or not isinstance(message.get('id'), str): raise ValueError('Invalid event envelope')
            for attempt in range(retries + 1):
                try: handler(message); observe_event(self.broker,"handle","success"); return
                except Exception:
                    observe_event(self.broker,"handle","error")
                    if attempt == retries:
                        if not dead_letter_topic: raise
                        self.publish(dead_letter_topic, event('delivery.failed', {'original': message, 'attempts': attempt + 1}, correlation_id=message.get('correlationid')))
                        return
                    time.sleep(min(2, 0.1 * 2**attempt))
        if self.broker == 'kafka':
            from confluent_kafka import Consumer
            consumer = Consumer({**self._kafka_config(), 'group.id': group, 'enable.auto.commit': False, 'enable.auto.offset.store': False, 'auto.offset.reset': 'earliest'})
            consumer.subscribe([topic])
            try:
                while (not limit or processed < limit) and not (stop and stop()):
                    if time.monotonic() >= deadline: raise TimeoutError('Consumer deadline expired')
                    message = consumer.poll(min(1, max(0.01, deadline - time.monotonic())))
                    if message is None: continue
                    if message.error(): raise RuntimeError(str(message.error()))
                    handle(message.value())
                    consumer.commit(message=message, asynchronous=False)
                    processed += 1
            finally: consumer.close()
        else:
            self.prepare(topic, group); channel = self._rabbit(); queue = topic + '.' + group
            while (not limit or processed < limit) and not (stop and stop()):
                if time.monotonic() >= deadline: raise TimeoutError('Consumer deadline expired')
                delivery, _properties, raw = channel.basic_get(queue, auto_ack=False)
                if delivery is None:
                    self._connection.process_data_events(time_limit=0.1); continue
                try: handle(raw)
                except Exception:
                    channel.basic_nack(delivery.delivery_tag, requeue=True); raise
                channel.basic_ack(delivery.delivery_tag); processed += 1
        return processed

    def close(self):
        if self._producer is not None: self._producer.flush(10)
        if self._connection is not None and self._connection.is_open: self._connection.close()
        self._connection = self._channel = self._producer = None

    def __enter__(self): return self
    def __exit__(self, *_): self.close()

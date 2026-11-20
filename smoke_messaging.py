"""Real broker round-trips across every generated language; no mock delivery."""
import json
import uuid

COMMANDS = {
    'nestjs': ['node', 'dist/src/shared/messaging/cli.js'],
    'fastapi': ['python', '-m', 'app.shared.messaging.cli'],
    'go': ['./messaging'],
    'java': ['java', '-jar', 'app.jar', '--messaging'],
}


def run_checks(manifest, check, assert_, cmd, compose):
    brokers = manifest.get('messaging', {}).get('brokers', [])
    services = manifest['services']
    def call(service, operation, broker, topic, group, message=None):
        return cmd(compose + ['exec', '-T', service['containers'][0]] + COMMANDS[service['lang']] +
                   [operation, broker, topic, group], input=json.dumps(message) if message else None, timeout=75)
    for broker in brokers:
        for index, producer in enumerate(services):
            consumer = services[(index+1) % len(services)]
            def roundtrip(producer=producer, consumer=consumer, broker=broker):
                topic = 'huza-smoke-' + uuid.uuid4().hex
                group = 'roundtrip-' + uuid.uuid4().hex[:12]
                call(consumer, 'prepare', broker, topic, group)
                message = {'specversion': '1.0', 'id': str(uuid.uuid4()), 'source': producer['name'],
                           'type': 'smoke.message', 'datacontenttype': 'application/json',
                           'data': {'models': producer['modules'], 'database': producer['database'], 'nested': {'value': 42}}}
                call(producer, 'publish', broker, topic, group, message)
                # A failing handler must not acknowledge; a later consumer must receive it.
                try: call(consumer, 'consume-fail', broker, topic, group)
                except RuntimeError as error:
                    assert_('Intentional handler failure' in str(error), 'Failure probe did not reach its handler: ' + str(error))
                else: raise AssertionError('Failing handler was accepted')
                output = call(consumer, 'consume', broker, topic, group)
                events = []
                for line in output.splitlines():
                    try: events.append(json.loads(line))
                    except ValueError: pass
                assert_(message in events, 'Cross-language event changed or was lost: ' + output[-1500:])
                if broker == 'rabbitmq':
                    orphan = 'huza-unroutable-' + uuid.uuid4().hex
                    try: call(producer, 'publish', broker, orphan, group, message)
                    except RuntimeError: pass
                    else: raise AssertionError('Unroutable RabbitMQ publication incorrectly succeeded')
            check(f'Messaging {broker} {producer["lang"]} → {consumer["lang"]}: confirmed send, failed handler redelivery, payload integrity', roundtrip)

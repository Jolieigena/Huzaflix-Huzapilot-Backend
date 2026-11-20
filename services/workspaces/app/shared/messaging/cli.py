"""Broker diagnostics: prepare, publish a JSON envelope on stdin, or consume."""
import json
import sys
from .bus import EventBus


def main():
    if len(sys.argv) < 4: raise SystemExit('Usage: python -m app.shared.messaging.cli prepare|publish|consume kafka|rabbitmq topic [group] [count]')
    command, broker, topic = sys.argv[1:4]
    with EventBus(broker) as bus:
        if command == 'prepare': bus.prepare(topic, sys.argv[4] if len(sys.argv) > 4 else None)
        elif command == 'publish': bus.publish(topic, json.load(sys.stdin))
        elif command in ('consume', 'consume-fail'):
            def handler(message):
                if command == 'consume-fail': raise RuntimeError('Intentional handler failure')
                print(json.dumps(message), flush=True)
            bus.consume(topic, sys.argv[4], handler, limit=int(sys.argv[5]) if len(sys.argv) > 5 else 1, timeout=45, retries=0)
        else: raise SystemExit('Unknown messaging command')


if __name__ == '__main__': main()

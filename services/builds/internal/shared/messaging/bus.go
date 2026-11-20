// Package messaging provides confirmed sends and serial, at-least-once consumers.
// Handlers must be idempotent. Use one Bus per worker; do not share it concurrently.
package messaging

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"errors"
	"fmt"
	"github.com/google/uuid"
	amqp "github.com/rabbitmq/amqp091-go"
	"github.com/segmentio/kafka-go"
	"github.com/segmentio/kafka-go/sasl/plain"
	"huzapilot/component/internal/shared/monitoring"
	"net"
	"os"
	"regexp"
	"strings"
	"time"
)

type Event map[string]any

func NewEvent(kind string, data any) Event {
	return Event{"specversion": "1.0", "id": uuid.NewString(), "source": env("SERVICE_NAME", "service"), "type": kind, "time": time.Now().UTC().Format(time.RFC3339Nano), "datacontenttype": "application/json", "data": data}
}
func env(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

var namePattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`)

func identifier(value string) error {
	if !namePattern.MatchString(value) {
		return errors.New("invalid topic/group name")
	}
	return nil
}

type Bus struct {
	Broker     string
	writer     *kafka.Writer
	connection *amqp.Connection
	channel    *amqp.Channel
	returns    chan amqp.Return
}

func New(broker string) (*Bus, error) {
	if broker != "kafka" && broker != "rabbitmq" {
		return nil, errors.New("unknown broker")
	}
	return &Bus{Broker: broker}, nil
}
func dialer() (*kafka.Dialer, error) {
	d := &kafka.Dialer{Timeout: 10 * time.Second}
	if os.Getenv("KAFKA_TLS") == "true" {
		d.TLS = &tls.Config{MinVersion: tls.VersionTLS12}
		if file := os.Getenv("KAFKA_CA_FILE"); file != "" {
			raw, err := os.ReadFile(file)
			if err != nil {
				return nil, err
			}
			pool, err := x509.SystemCertPool()
			if err != nil {
				pool = x509.NewCertPool()
			}
			if !pool.AppendCertsFromPEM(raw) {
				return nil, errors.New("invalid Kafka CA")
			}
			d.TLS.RootCAs = pool
		}
	}
	if os.Getenv("KAFKA_USERNAME") != "" {
		d.SASLMechanism = plain.Mechanism{Username: os.Getenv("KAFKA_USERNAME"), Password: os.Getenv("KAFKA_PASSWORD")}
	}
	return d, nil
}
func (b *Bus) rabbit() error {
	if b.channel != nil {
		return nil
	}
	var err error
	b.connection, err = amqp.DialConfig(os.Getenv("RABBITMQ_URL"), amqp.Config{Heartbeat: 30 * time.Second, Dial: amqp.DefaultDial(10 * time.Second)})
	if err != nil {
		return err
	}
	b.channel, err = b.connection.Channel()
	if err != nil {
		_ = b.connection.Close()
		return err
	}
	if err = b.channel.Confirm(false); err != nil {
		return err
	}
	b.returns = b.channel.NotifyReturn(make(chan amqp.Return, 1))
	return nil
}
func (b *Bus) Prepare(ctx context.Context, topic, group string) error {
	if err := identifier(topic); err != nil {
		return err
	}
	if group != "" {
		if err := identifier(group); err != nil {
			return err
		}
	}
	if b.Broker == "rabbitmq" {
		if err := b.rabbit(); err != nil {
			return err
		}
		if err := b.channel.ExchangeDeclare(topic, "direct", true, false, false, false, nil); err != nil {
			return err
		}
		if group != "" {
			if _, err := b.channel.QueueDeclare(topic+"."+group, true, false, false, false, nil); err != nil {
				return err
			}
			return b.channel.QueueBind(topic+"."+group, topic, topic, false, nil)
		}
		return nil
	}
	d, err := dialer()
	if err != nil {
		return err
	}
	conn, err := d.DialContext(ctx, "tcp", strings.Split(os.Getenv("KAFKA_BROKERS"), ",")[0])
	if err != nil {
		return err
	}
	defer conn.Close()
	controller, err := conn.Controller()
	if err != nil {
		return err
	}
	admin, err := d.DialContext(ctx, "tcp", net.JoinHostPort(controller.Host, fmt.Sprint(controller.Port)))
	if err != nil {
		return err
	}
	defer admin.Close()
	if deadline, ok := ctx.Deadline(); ok {
		_ = admin.SetDeadline(deadline)
	}
	err = admin.CreateTopics(kafka.TopicConfig{Topic: topic, NumPartitions: 1, ReplicationFactor: 1})
	if errors.Is(err, kafka.TopicAlreadyExists) {
		return nil
	}
	return err
}
func (b *Bus) Publish(ctx context.Context, topic string, message Event) error {
	err := b.publishObserved(ctx, topic, message)
	outcome := "success"
	if err != nil {
		outcome = "error"
	}
	monitoring.Event(b.Broker, "publish", outcome, "")
	return err
}
func (b *Bus) publishObserved(ctx context.Context, topic string, message Event) error {
	if err := identifier(topic); err != nil {
		return err
	}
	id, ok := message["id"].(string)
	if !ok {
		return errors.New("expected event envelope with id")
	}
	raw, err := json.Marshal(message)
	if err != nil {
		return err
	}
	if len(raw) > 1048576 {
		return errors.New("event exceeds 1 MiB")
	}
	if b.Broker == "kafka" {
		if b.writer == nil {
			d, err := dialer()
			if err != nil {
				return err
			}
			b.writer = &kafka.Writer{Addr: kafka.TCP(strings.Split(os.Getenv("KAFKA_BROKERS"), ",")...), RequiredAcks: kafka.RequireAll, Async: false, BatchTimeout: 10 * time.Millisecond, WriteTimeout: 30 * time.Second, Transport: &kafka.Transport{TLS: d.TLS, SASL: d.SASLMechanism}}
		}
		return b.writer.WriteMessages(ctx, kafka.Message{Topic: topic, Key: []byte(id), Value: raw})
	}
	if err = b.rabbit(); err != nil {
		return err
	}
	if err = b.channel.ExchangeDeclare(topic, "direct", true, false, false, false, nil); err != nil {
		return err
	}
	confirmation, err := b.channel.PublishWithDeferredConfirmWithContext(ctx, topic, topic, true, false, amqp.Publishing{ContentType: "application/json", DeliveryMode: amqp.Persistent, MessageId: id, Body: raw})
	if err != nil {
		return err
	}
	ack, err := confirmation.WaitContext(ctx)
	if err != nil {
		return err
	}
	if !ack {
		return errors.New("broker rejected event")
	}
	select {
	case <-b.returns:
		return errors.New("unroutable event")
	default:
		return nil
	}
}

type Options struct {
	Limit           int
	Retries         int
	DeadLetterTopic string
}

func (b *Bus) Consume(ctx context.Context, topic, group string, handler func(context.Context, Event) error, options Options) (int, error) {
	if err := identifier(topic); err != nil {
		return 0, err
	}
	if err := identifier(group); err != nil {
		return 0, err
	}
	if options.Limit < 0 || options.Retries < 0 || options.Retries > 10 {
		return 0, errors.New("invalid consumer limits")
	}
	if options.DeadLetterTopic != "" {
		if err := identifier(options.DeadLetterTopic); err != nil {
			return 0, err
		}
		if topic == options.DeadLetterTopic {
			return 0, errors.New("dead-letter topic must differ")
		}
	}
	handle := func(raw []byte) error {
		var message Event
		if err := json.Unmarshal(raw, &message); err != nil {
			return err
		}
		if _, ok := message["id"].(string); !ok {
			return errors.New("invalid event envelope")
		}
		for attempt := 0; ; attempt++ {
			err := handler(ctx, message)
			if err == nil {
				monitoring.Event(b.Broker, "handle", "success", "")
				return nil
			}
			monitoring.Event(b.Broker, "handle", "error", "")
			if attempt == options.Retries {
				if options.DeadLetterTopic == "" {
					return err
				}
				return b.Publish(ctx, options.DeadLetterTopic, NewEvent("delivery.failed", map[string]any{"original": message, "attempts": attempt + 1}))
			}
			delay := 100 * time.Millisecond * time.Duration(1<<attempt)
			if delay > 2*time.Second {
				delay = 2 * time.Second
			}
			select {
			case <-ctx.Done():
				return ctx.Err()
			case <-time.After(delay):
			}
		}
	}
	count := 0
	if b.Broker == "kafka" {
		d, err := dialer()
		if err != nil {
			return count, err
		}
		reader := kafka.NewReader(kafka.ReaderConfig{Brokers: strings.Split(os.Getenv("KAFKA_BROKERS"), ","), Topic: topic, GroupID: group, Dialer: d, StartOffset: kafka.FirstOffset, CommitInterval: 0, MaxBytes: 1048576})
		defer reader.Close()
		for options.Limit == 0 || count < options.Limit {
			message, err := reader.FetchMessage(ctx)
			if err != nil {
				return count, err
			}
			if err = handle(message.Value); err != nil {
				return count, err
			}
			if err = reader.CommitMessages(ctx, message); err != nil {
				return count, err
			}
			count++
		}
	} else {
		if err := b.Prepare(ctx, topic, group); err != nil {
			return count, err
		}
		for options.Limit == 0 || count < options.Limit {
			if err := ctx.Err(); err != nil {
				return count, err
			}
			message, ok, err := b.channel.Get(topic+"."+group, false)
			if err != nil {
				return count, err
			}
			if !ok {
				select {
				case <-ctx.Done():
					return count, ctx.Err()
				case <-time.After(100 * time.Millisecond):
				}
				continue
			}
			if err = handle(message.Body); err != nil {
				_ = message.Nack(false, true)
				return count, err
			}
			if err = message.Ack(false); err != nil {
				return count, err
			}
			count++
		}
	}
	return count, nil
}
func (b *Bus) Close() error {
	var errs []error
	if b.writer != nil {
		errs = append(errs, b.writer.Close())
		if transport, ok := b.writer.Transport.(*kafka.Transport); ok {
			transport.CloseIdleConnections()
		}
	}
	if b.channel != nil {
		errs = append(errs, b.channel.Close())
	}
	if b.connection != nil {
		errs = append(errs, b.connection.Close())
	}
	return errors.Join(errs...)
}

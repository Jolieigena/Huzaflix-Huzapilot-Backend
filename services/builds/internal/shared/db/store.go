package db

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	mysqlDriver "github.com/go-sql-driver/mysql"
	"github.com/gocql/gocql"
	"github.com/google/uuid"
	sqliteDriver "github.com/mattn/go-sqlite3"
	"go.mongodb.org/mongo-driver/bson"
	"go.mongodb.org/mongo-driver/mongo"
	"go.mongodb.org/mongo-driver/mongo/options"
	"gorm.io/driver/mysql"
	"gorm.io/driver/postgres"
	"gorm.io/driver/sqlite"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"
	"huzapilot/component/internal/shared/cache"
	"huzapilot/component/internal/shared/db/query"
	"huzapilot/component/internal/shared/monitoring"
	"log"
	"net/url"
	"os"
	"strconv"
	"strings"
	"time"
)

type Doc = map[string]interface{}
type Record struct {
	ID   string `gorm:"primaryKey;size:36"`
	Data string `gorm:"type:text;not null"`
}
type Store struct {
	Cache *cache.Cache
	kind  string
	sql   *gorm.DB
	mongo *mongo.Database
	cass  *gocql.Session
}

func table(s string) string { return "hp_" + strings.ReplaceAll(s, "-", "_") }
func NewStore(modules []string) (*Store, error) {
	s := &Store{Cache: cache.New()}
	kind := os.Getenv("DB_TYPE")
	if kind == "" {
		kind = "postgres"
	}
	s.kind = kind
	host, user, password, name := os.Getenv("DB_HOST"), os.Getenv("DB_USER"), os.Getenv("DB_PASSWORD"), os.Getenv("DB_NAME")
	var err error
	switch kind {
	case "mongodb":
		var client *mongo.Client
		client, err = mongo.Connect(context.Background(), options.Client().SetMaxPoolSize(uint64(boundedEnv("DB_POOL_SIZE", 4, 1, 32))).SetServerSelectionTimeout(10*time.Second).ApplyURI("mongodb://"+host+":27017").SetAuth(options.Credential{Username: user, Password: password, AuthSource: "admin"}))
		if err == nil {
			err = client.Ping(context.Background(), nil)
			s.mongo = client.Database(name)
		}
	case "cassandra":
		cluster := gocql.NewCluster(host)
		cluster.Authenticator = gocql.PasswordAuthenticator{Username: user, Password: password}
		cluster.Timeout = 30 * time.Second
		cluster.ConnectTimeout = 30 * time.Second
		var init *gocql.Session
		init, err = cluster.CreateSession()
		if err != nil {
			return nil, err
		}
		err = init.Query(fmt.Sprintf("CREATE KEYSPACE IF NOT EXISTS %s WITH replication = {'class':'SimpleStrategy','replication_factor':1}", name)).Exec()
		init.Close()
		if err != nil {
			return nil, err
		}
		cluster.Keyspace = name
		s.cass, err = cluster.CreateSession()
		if err == nil {
			for _, m := range modules {
				err = s.cass.Query("CREATE TABLE IF NOT EXISTS " + table(m) + " (id text PRIMARY KEY, data text)").Exec()
				if err != nil {
					return nil, err
				}
			}
		}
	default:
		var dialect gorm.Dialector
		switch kind {
		case "postgres":
			u := url.URL{Scheme: "postgres", User: url.UserPassword(user, password), Host: host + ":5432", Path: name, RawQuery: "sslmode=disable"}
			dialect = postgres.Open(u.String())
		case "mysql":
			dialect = mysql.Open(user + ":" + password + "@tcp(" + host + ":3306)/" + name + "?charset=utf8mb4&parseTime=True&loc=UTC")
		case "sqlite":
			dialect = sqlite.Open("/data/app.sqlite?_busy_timeout=10000&_journal_mode=WAL")
		default:
			return nil, fmt.Errorf("unsupported database %s", kind)
		}
		s.sql, err = gorm.Open(dialect, &gorm.Config{Logger: logger.New(log.New(os.Stderr, "", log.LstdFlags), logger.Config{SlowThreshold: time.Second, LogLevel: logger.Warn, IgnoreRecordNotFoundError: true, ParameterizedQueries: true, Colorful: false})})
		if err == nil {
			connection, e := s.sql.DB()
			if e != nil {
				return nil, e
			}
			size := boundedEnv("DB_POOL_SIZE", 4, 1, 32)
			if kind == "sqlite" {
				size = 1
			}
			connection.SetMaxOpenConns(size)
			connection.SetMaxIdleConns(1)
			connection.SetConnMaxLifetime(30 * time.Minute)
		}
		if err == nil {
			for _, m := range modules {
				identityType := `varchar(36)`
				if kind == "mysql" {
					identityType = `varchar(36) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin`
				} else if kind == "postgres" {
					identityType = `varchar(36) COLLATE "C"`
				}
				err = s.ddl(table(m), "CREATE TABLE IF NOT EXISTS "+table(m)+" (id "+identityType+" PRIMARY KEY, data text NOT NULL)")
				if err != nil {
					return nil, err
				}
			}
		}
	}
	if err == nil {
		err = s.ensureIndexes(modules)
	}
	return s, err
}
func (s *Store) ensureIndexes(modules []string) error {
	raw, err := os.ReadFile("db-indexes.json")
	if os.IsNotExist(err) {
		return nil
	}
	if err != nil {
		return err
	}
	var indexes []struct {
		Module, Name, SQL string
		Unique            bool
		Fields            []struct {
			Path      string
			Direction int
		}
		PartialFilter Doc
	}
	if err = json.Unmarshal(raw, &indexes); err != nil {
		return err
	}
	allowed := map[string]bool{}
	for _, module := range modules {
		allowed[module] = true
	}
	for _, index := range indexes {
		if !allowed[index.Module] {
			continue
		}
		t := table(index.Module)
		if s.mongo != nil {
			fields := bson.D{}
			for _, field := range index.Fields {
				fields = append(fields, bson.E{Key: field.Path, Value: field.Direction})
			}
			settings := options.Index().SetName(index.Name).SetUnique(index.Unique).SetCollation(&options.Collation{Locale: "simple"})
			if index.Unique {
				settings.SetPartialFilterExpression(index.PartialFilter)
			}
			if _, err = s.mongo.Collection(t).Indexes().CreateOne(context.Background(), mongo.IndexModel{Keys: fields, Options: settings}); err != nil {
				return err
			}
		} else if s.sql != nil {
			if s.kind == "mysql" {
				var count int64
				if err = s.sql.Raw("SELECT COUNT(*) FROM information_schema.statistics WHERE table_schema=DATABASE() AND table_name=? AND index_name=?", t, index.Name).Scan(&count).Error; err != nil {
					return err
				}
				if count > 0 {
					continue
				}
			}
			if err = s.ddl(t, index.SQL); err != nil {
				if s.kind != "mysql" {
					return err
				}
				var count int64
				if e := s.sql.Raw("SELECT COUNT(*) FROM information_schema.statistics WHERE table_schema=DATABASE() AND table_name=? AND index_name=?", t, index.Name).Scan(&count).Error; e != nil || count == 0 {
					return err
				}
			}
		}
	}
	return nil
}
func (s *Store) observedList(module, id string) ([]Doc, error) {
	out := []Doc{}
	t := table(module)
	if s.mongo != nil {
		filter := bson.M{}
		if id != "" {
			filter["_id"] = id
		}
		cursor, err := s.mongo.Collection(t).Find(context.Background(), filter)
		if err != nil {
			return nil, err
		}
		defer cursor.Close(context.Background())
		for cursor.Next(context.Background()) {
			d := Doc{}
			if err = cursor.Decode(&d); err != nil {
				return nil, err
			}
			d["id"] = d["_id"]
			delete(d, "_id")
			raw, e := json.Marshal(d)
			if e != nil {
				return nil, e
			}
			var normalized Doc
			if e = json.Unmarshal(raw, &normalized); e != nil {
				return nil, e
			}
			out = append(out, normalized)
		}
		return out, cursor.Err()
	}
	rows := []Record{}
	if s.cass != nil {
		q := "SELECT id,data FROM " + t
		var iter *gocql.Iter
		if id != "" {
			iter = s.cass.Query(q+" WHERE id=?", id).Iter()
		} else {
			iter = s.cass.Query(q).Iter()
		}
		var rid, data string
		for iter.Scan(&rid, &data) {
			rows = append(rows, Record{rid, data})
		}
		if err := iter.Close(); err != nil {
			return nil, err
		}
	} else {
		q := s.sql.Table(t)
		if id != "" {
			q = q.Where("id = ?", id)
		}
		if err := q.Find(&rows).Error; err != nil {
			return nil, err
		}
	}
	for _, r := range rows {
		d := Doc{}
		if err := json.Unmarshal([]byte(r.Data), &d); err != nil {
			return nil, err
		}
		d["id"] = r.ID
		out = append(out, d)
	}
	return out, nil
}
func (s *Store) saveRaw(module string, data Doc, id string) (Doc, error) {
	fresh := id == ""
	if fresh {
		id = uuid.NewString()
	}
	delete(data, "id")
	delete(data, "_id")
	raw, err := json.Marshal(data)
	if err != nil {
		return nil, err
	}
	t := table(module)
	if s.mongo != nil {
		_, err = s.mongo.Collection(t).ReplaceOne(context.Background(), bson.M{"_id": id}, data, options.Replace().SetUpsert(true))
	} else if s.cass != nil {
		err = s.cass.Query("INSERT INTO "+t+" (id,data) VALUES (?,?)", id, string(raw)).Exec()
	} else {
		r := Record{id, string(raw)}
		if fresh {
			err = s.sql.Table(t).Create(&r).Error
		} else {
			err = s.sql.Table(t).Where("id = ?", id).Update("data", string(raw)).Error
		}
	}
	data["id"] = id
	return data, err
}
func (s *Store) removeUncached(module, id string) error {
	t := table(module)
	if s.mongo != nil {
		_, err := s.mongo.Collection(t).DeleteOne(context.Background(), bson.M{"_id": id})
		return err
	}
	if s.cass != nil {
		return s.cass.Query("DELETE FROM "+t+" WHERE id=?", id).Exec()
	}
	return s.sql.Table(t).Where("id = ?", id).Delete(&Record{}).Error
}

func (s *Store) observedScan(module string, maxRows, maxBytes int) ([]Doc, error) {
	out := []Doc{}
	bytes := 0
	t := table(module)
	appendRecord := func(d Doc, size int) error {
		bytes += size
		if len(out) >= maxRows || bytes > maxBytes {
			return &query.Error{Message: "Query scan limit exceeded; narrow the data or use an explicit native query", Code: "QUERY_LIMIT", Status: 413}
		}
		out = append(out, d)
		return nil
	}
	if s.mongo != nil {
		cursor, err := s.mongo.Collection(t).Find(context.Background(), bson.M{}, options.Find().SetLimit(int64(maxRows+1)).SetBatchSize(64))
		if err != nil {
			return nil, err
		}
		defer cursor.Close(context.Background())
		for cursor.Next(context.Background()) {
			d := Doc{}
			if err = cursor.Decode(&d); err != nil {
				return nil, err
			}
			d["id"] = d["_id"]
			delete(d, "_id")
			raw, err := json.Marshal(d)
			if err != nil {
				return nil, err
			}
			var normalized Doc
			if err = json.Unmarshal(raw, &normalized); err != nil {
				return nil, err
			}
			if err = appendRecord(normalized, len(raw)); err != nil {
				return nil, err
			}
		}
		return out, cursor.Err()
	}
	if s.cass != nil {
		iter := s.cass.Query(fmt.Sprintf("SELECT id,data FROM %s LIMIT %d", t, maxRows+1)).PageSize(64).Iter()
		defer iter.Close()
		var id, data string
		for iter.Scan(&id, &data) {
			d := Doc{}
			if err := json.Unmarshal([]byte(data), &d); err != nil {
				return nil, err
			}
			d["id"] = id
			if err := appendRecord(d, len(data)); err != nil {
				return nil, err
			}
		}
		return out, iter.Close()
	}
	rows, err := s.sql.Table(t).Select("id,data").Limit(maxRows + 1).Rows()
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	for rows.Next() {
		var id, data string
		if err = rows.Scan(&id, &data); err != nil {
			return nil, err
		}
		d := Doc{}
		if err = json.Unmarshal([]byte(data), &d); err != nil {
			return nil, err
		}
		d["id"] = id
		if err = appendRecord(d, len(data)); err != nil {
			return nil, err
		}
	}
	return out, rows.Err()
}
func (s *Store) observedQuery(module string, schema Doc, filter, optionsValue any, maxRows, maxBytes int, count bool) ([]Doc, int64, bool, error) {
	optionsDoc := Doc{}
	if optionsValue != nil {
		raw, _ := json.Marshal(query.Plain(optionsValue))
		json.Unmarshal(raw, &optionsDoc)
	}
	if s.cass != nil {
		ids, supported := query.CandidateIDs(filter)
		if !supported {
			return nil, 0, false, nil
		}
		if len(ids) > maxRows {
			return nil, 0, true, &query.Error{Message: "Query partition limit exceeded", Code: "QUERY_LIMIT", Status: 413}
		}
		rows := []Doc{}
		size := 0
		for _, id := range ids {
			batch, err := s.List(module, id)
			if err != nil {
				return nil, 0, true, err
			}
			for _, row := range batch {
				raw, err := json.Marshal(row)
				if err != nil {
					return nil, 0, true, err
				}
				size += len(raw)
				if size > maxBytes {
					return nil, 0, true, &query.Error{Message: "Query result byte limit exceeded", Code: "QUERY_LIMIT", Status: 413}
				}
				rows = append(rows, row)
			}
		}
		opts := optionsValue
		if count {
			opts = nil
		}
		result, err := query.Find(rows, filter, opts)
		if count {
			return nil, int64(len(result)), true, err
		}
		return result, 0, true, err
	}
	if s.mongo != nil {
		predicate := query.MongoPlan(schema, filter, optionsValue)
		if predicate == nil {
			return nil, 0, false, nil
		}
		collection := s.mongo.Collection(table(module))
		if count {
			total, err := collection.CountDocuments(context.Background(), predicate)
			return nil, total, true, err
		}
		sort := bson.D{}
		sortValues := Doc{}
		if value, ok := optionsDoc["sort"].(map[string]any); ok {
			sortValues = value
		}
		for _, path := range query.SortFields(optionsValue) {
			field := path
			if field == "id" {
				field = "_id"
			}
			sort = append(sort, bson.E{Key: field, Value: int(sortValues[path].(float64))})
		}
		if _, ok := sortValues["id"]; !ok {
			sort = append(sort, bson.E{Key: "_id", Value: 1})
		}
		fetch := maxRows + 1
		if value, ok := optionsDoc["limit"].(float64); ok && int(value) <= maxRows {
			fetch = int(value)
		}
		skip := 0
		if value, ok := optionsDoc["skip"].(float64); ok {
			skip = int(value)
		}
		cursor, err := collection.Find(context.Background(), predicate, options.Find().SetSort(sort).SetLimit(int64(fetch)).SetSkip(int64(skip)).SetBatchSize(64))
		if err != nil {
			return nil, 0, true, err
		}
		defer cursor.Close(context.Background())
		rows := []Doc{}
		bytes := 0
		for cursor.Next(context.Background()) {
			row := Doc{}
			if err := cursor.Decode(&row); err != nil {
				return nil, 0, true, err
			}
			row["id"] = row["_id"]
			delete(row, "_id")
			raw, err := json.Marshal(row)
			if err != nil {
				return nil, 0, true, err
			}
			var normalized Doc
			if err = json.Unmarshal(raw, &normalized); err != nil {
				return nil, 0, true, err
			}
			bytes += len(raw)
			if len(rows) >= maxRows || bytes > maxBytes {
				return nil, 0, true, &query.Error{Message: "Query result limit exceeded", Code: "QUERY_LIMIT", Status: 413}
			}
			rows = append(rows, normalized)
		}
		if err = cursor.Err(); err != nil {
			return nil, 0, true, err
		}
		projection := Doc{}
		if value, ok := optionsDoc["projection"]; ok {
			projection["projection"] = value
		}
		result, err := query.Find(rows, nil, projection)
		return result, 0, true, err
	}
	plan := query.SQLPlan(s.kind, schema, filter, optionsValue)
	if plan == nil {
		return nil, 0, false, nil
	}
	q := s.sql.Table(table(module)).Where(plan.Where, plan.Params...)
	if count {
		var total int64
		err := q.Count(&total).Error
		return nil, total, true, err
	}
	fetch := maxRows + 1
	if value, ok := optionsDoc["limit"].(float64); ok && int(value) <= maxRows {
		fetch = int(value)
	}
	skip := 0
	if value, ok := optionsDoc["skip"].(float64); ok {
		skip = int(value)
	}
	records, err := q.Select("id,data").Order(plan.Order).Limit(fetch).Offset(skip).Rows()
	if err != nil {
		return nil, 0, true, err
	}
	defer records.Close()
	rows := []Doc{}
	bytes := 0
	for records.Next() {
		var id, data string
		if err := records.Scan(&id, &data); err != nil {
			return nil, 0, true, err
		}
		bytes += len(data)
		if len(rows) >= maxRows || bytes > maxBytes {
			return nil, 0, true, &query.Error{Message: "Query result limit exceeded", Code: "QUERY_LIMIT", Status: 413}
		}
		row := Doc{}
		if err := json.Unmarshal([]byte(data), &row); err != nil {
			return nil, 0, true, err
		}
		row["id"] = id
		rows = append(rows, row)
	}
	if err := records.Err(); err != nil {
		return nil, 0, true, err
	}
	projection := Doc{}
	if value, ok := optionsDoc["projection"]; ok {
		projection["projection"] = value
	}
	result, err := query.Find(rows, nil, projection)
	return result, 0, true, err
}

func (s *Store) observedGroup(module string, schema Doc, predicate, group any, maxRows, maxBytes int) ([]Doc, bool, error) {
	compiled := query.NativeGroupPlan(s.kind, schema, predicate, group)
	if compiled == nil {
		return nil, false, nil
	}
	rows := []Doc{}
	bytes := 0
	number := func(value any) (float64, error) {
		if raw, ok := value.([]byte); ok {
			value = string(raw)
		}
		return strconv.ParseFloat(fmt.Sprint(value), 64)
	}
	appendRecord := func(record Doc) error {
		count, err := number(record["hp_count"])
		if err != nil {
			return err
		}
		if count == 0 {
			return nil
		}
		key := compiled.Key
		if !compiled.Constant {
			field := "hp_key"
			if s.mongo != nil {
				field = "_id"
			}
			key = record[field]
			if key != nil {
				switch compiled.KeyType {
				case "integer", "number":
					key, err = number(key)
					if err != nil {
						return err
					}
				case "boolean":
					if _, ok := key.(bool); !ok {
						n, e := number(key)
						if e != nil {
							return e
						}
						key = n != 0
					}
				default:
					if raw, ok := key.([]byte); ok {
						key = string(raw)
					}
				}
			}
		}
		row := Doc{"_id": key}
		for _, column := range compiled.Columns {
			value := record[column.Alias]
			if column.NullAlias != "" {
				missing, e := number(record[column.NullAlias])
				if e != nil {
					return e
				}
				if missing > 0 {
					value = nil
				}
			}
			if value != nil {
				value, err = number(value)
				if err != nil {
					return err
				}
			}
			row[column.Name] = value
		}
		raw, err := json.Marshal(row)
		if err != nil {
			return err
		}
		bytes += len(raw)
		if len(rows) >= maxRows || bytes > maxBytes {
			return &query.Error{Message: "Query result limit exceeded", Code: "QUERY_LIMIT", Status: 413}
		}
		rows = append(rows, row)
		return nil
	}
	if s.mongo != nil {
		pipeline := mongo.Pipeline{}
		for _, stage := range compiled.Mongo {
			for key, value := range stage {
				pipeline = append(pipeline, bson.D{{Key: key, Value: value}})
			}
		}
		pipeline = append(pipeline, bson.D{{Key: "$limit", Value: maxRows + 1}})
		cursor, err := s.mongo.Collection(table(module)).Aggregate(context.Background(), pipeline, options.Aggregate().SetBatchSize(64).SetAllowDiskUse(false).SetMaxTime(15*time.Second))
		if err != nil {
			return nil, true, err
		}
		defer cursor.Close(context.Background())
		for cursor.Next(context.Background()) {
			record := Doc{}
			if err = cursor.Decode(&record); err != nil {
				return nil, true, err
			}
			if err = appendRecord(record); err != nil {
				return nil, true, err
			}
		}
		return rows, true, cursor.Err()
	}
	statement := "SELECT " + compiled.Select + " FROM " + table(module) + " WHERE " + compiled.Filter.Where
	if compiled.GroupBy != "" {
		statement += " GROUP BY " + compiled.GroupBy
	}
	statement += fmt.Sprintf(" LIMIT %d", maxRows+1)
	result, err := s.sql.Raw(statement, compiled.Params...).Rows()
	if err != nil {
		return nil, true, err
	}
	defer result.Close()
	names, err := result.Columns()
	if err != nil {
		return nil, true, err
	}
	for result.Next() {
		values := make([]any, len(names))
		dest := make([]any, len(names))
		for i := range values {
			dest[i] = &values[i]
		}
		if err = result.Scan(dest...); err != nil {
			return nil, true, err
		}
		record := Doc{}
		for i, name := range names {
			record[name] = values[i]
		}
		if err = appendRecord(record); err != nil {
			return nil, true, err
		}
	}
	return rows, true, result.Err()
}

type Snapshot struct {
	Row   Doc
	Token any
}

func (s *Store) observedSnapshot(module, id string) (*Snapshot, error) {
	t := table(module)
	raw := ""
	if s.mongo != nil {
		var token bson.Raw
		err := s.mongo.Collection(t).FindOne(context.Background(), bson.M{"_id": id}).Decode(&token)
		if err == mongo.ErrNoDocuments {
			return nil, nil
		}
		if err != nil {
			return nil, err
		}
		row := Doc{}
		if err = bson.Unmarshal(token, &row); err != nil {
			return nil, err
		}
		row["id"] = row["_id"]
		delete(row, "_id")
		normalized, err := copyDoc(row)
		return &Snapshot{normalized, token}, err
	}
	if s.cass != nil {
		if err := s.cass.Query("SELECT data FROM "+t+" WHERE id=?", id).Scan(&raw); err != nil {
			if err == gocql.ErrNotFound {
				return nil, nil
			}
			return nil, err
		}
	} else {
		record := Record{}
		result := s.sql.Table(t).Select("id,data").Where("id=?", id).Take(&record)
		if result.Error == gorm.ErrRecordNotFound {
			return nil, nil
		}
		if result.Error != nil {
			return nil, result.Error
		}
		raw = record.Data
	}
	row := Doc{}
	if err := json.Unmarshal([]byte(raw), &row); err != nil {
		return nil, err
	}
	row["id"] = id
	return &Snapshot{row, raw}, nil
}
func (s *Store) compareWriteRaw(module, id string, token any, input Doc) (bool, error) {
	t := table(module)
	var body Doc
	var raw []byte
	var err error
	if input != nil {
		body, err = copyDoc(input)
		if err != nil {
			return false, err
		}
		delete(body, "id")
		delete(body, "_id")
		raw, err = json.Marshal(body)
		if err != nil {
			return false, err
		}
	}
	if s.mongo != nil {
		predicate := bson.M{"_id": id, "$expr": bson.M{"$eq": []any{"$$ROOT", bson.M{"$literal": token}}}}
		if body == nil {
			result, err := s.mongo.Collection(t).DeleteOne(context.Background(), predicate)
			if err != nil {
				return false, err
			}
			return result.DeletedCount == 1, nil
		}
		result, err := s.mongo.Collection(t).ReplaceOne(context.Background(), predicate, body)
		if err != nil {
			return false, err
		}
		return result.MatchedCount == 1, nil
	}
	if s.cass != nil {
		var q *gocql.Query
		if body == nil {
			q = s.cass.Query("DELETE FROM "+t+" WHERE id=? IF data=?", id, token)
		} else {
			q = s.cass.Query("UPDATE "+t+" SET data=? WHERE id=? IF data=?", string(raw), id, token)
		}
		return q.MapScanCAS(map[string]interface{}{})
	}
	comparison := "data=?"
	if s.kind == "mysql" {
		comparison = "CAST(data AS BINARY)=CAST(? AS BINARY)"
	}
	q := s.sql.Table(t).Where("id=?", id).Where(comparison, token)
	var result *gorm.DB
	if body == nil {
		result = q.Delete(&Record{})
	} else {
		result = q.Update("data", string(raw))
	}
	return result.RowsAffected == 1, result.Error
}
func databaseWriteError(err error) error {
	if err == nil {
		return nil
	}
	unique := mongo.IsDuplicateKeyError(err)
	var state interface{ SQLState() string }
	if errors.As(err, &state) && state.SQLState() == "23505" {
		unique = true
	}
	var mysqlError *mysqlDriver.MySQLError
	if errors.As(err, &mysqlError) && mysqlError.Number == 1062 {
		unique = true
	}
	var sqliteError sqliteDriver.Error
	if errors.As(err, &sqliteError) && (sqliteError.ExtendedCode == sqliteDriver.ErrConstraintUnique || sqliteError.ExtendedCode == sqliteDriver.ErrConstraintPrimaryKey) {
		unique = true
	}
	if unique {
		return &query.Error{Message: "Unique constraint violation", Code: "UNIQUE_CONFLICT", Status: 409}
	}
	return err
}
func (s *Store) observedSave(module string, body Doc, id string) (Doc, error) {
	row, err := s.saveRaw(module, body, id)
	if err == nil {
		s.Cache.Invalidate(module)
	}
	return row, databaseWriteError(err)
}
func (s *Store) observedCompareWrite(module, id string, token any, body Doc) (bool, error) {
	applied, err := s.compareWriteRaw(module, id, token, body)
	if err == nil && applied {
		s.Cache.Invalidate(module)
	}
	return applied, databaseWriteError(err)
}
func (s *Store) Native() any {
	if s.sql != nil {
		return s.sql
	}
	if s.mongo != nil {
		return s.mongo
	}
	return s.cass
}
func (s *Store) Close() error {
	s.Cache.Close()
	if s.sql != nil {
		db, err := s.sql.DB()
		if err != nil {
			return err
		}
		return db.Close()
	}
	if s.mongo != nil {
		return s.mongo.Client().Disconnect(context.Background())
	}
	if s.cass != nil {
		s.cass.Close()
	}
	return nil
}

// InsertIfAbsent never replaces a row or silently ignores another unique key.
func (s *Store) insertIfAbsentUncached(module, id string, input Doc) (bool, error) {
	body, err := copyDoc(input)
	if err != nil {
		return false, err
	}
	delete(body, "id")
	delete(body, "_id")
	raw, err := json.Marshal(body)
	if err != nil {
		return false, err
	}
	t := table(module)
	if s.mongo != nil {
		body["_id"] = id
		_, err = s.mongo.Collection(t).InsertOne(context.Background(), body)
	} else if s.cass != nil {
		return s.cass.Query("INSERT INTO "+t+" (id,data) VALUES (?,?) IF NOT EXISTS", id, string(raw)).MapScanCAS(map[string]interface{}{})
	} else {
		err = s.sql.Table(t).Create(&Record{ID: id, Data: string(raw)}).Error
	}
	if err == nil {
		return true, nil
	}
	err = databaseWriteError(err)
	var conflict *query.Error
	if errors.As(err, &conflict) && conflict.Code == "UNIQUE_CONFLICT" {
		snapshot, e := s.Snapshot(module, id)
		if e != nil {
			return false, e
		}
		if snapshot != nil {
			return false, nil
		}
	}
	return false, err
}

func (s *Store) ddl(table, statement string) error {
	if s.kind != "postgres" {
		return s.sql.Exec(statement).Error
	}
	return s.sql.Transaction(func(tx *gorm.DB) error {
		if err := tx.Exec("SELECT pg_advisory_xact_lock(hashtext(?))", "huzapilot:"+table).Error; err != nil {
			return err
		}
		return tx.Exec(statement).Error
	})
}

func (s *Store) observedRemove(module, id string) error {
	err := s.removeUncached(module, id)
	if err == nil {
		s.Cache.Invalidate(module)
	}
	return err
}
func (s *Store) observedInsertIfAbsent(module, id string, input Doc) (bool, error) {
	applied, err := s.insertIfAbsentUncached(module, id, input)
	if err == nil && applied {
		s.Cache.Invalidate(module)
	}
	return applied, err
}

func (s *Store) List(module, id string) ([]Doc, error) {
	started := time.Now()
	value, err := s.observedList(module, id)
	monitoring.Observe(module, "List", started, err)
	return value, err
}

func (s *Store) Scan(module string, maxRows, maxBytes int) ([]Doc, error) {
	started := time.Now()
	value, err := s.observedScan(module, maxRows, maxBytes)
	monitoring.Observe(module, "Scan", started, err)
	return value, err
}

func (s *Store) Query(module string, schema Doc, filter, optionsValue any, maxRows, maxBytes int, count bool) ([]Doc, int64, bool, error) {
	started := time.Now()
	value, countValue, native, err := s.observedQuery(module, schema, filter, optionsValue, maxRows, maxBytes, count)
	monitoring.Observe(module, "Query", started, err)
	return value, countValue, native, err
}

func (s *Store) Group(module string, schema Doc, predicate, group any, maxRows, maxBytes int) ([]Doc, bool, error) {
	started := time.Now()
	value, native, err := s.observedGroup(module, schema, predicate, group, maxRows, maxBytes)
	monitoring.Observe(module, "Group", started, err)
	return value, native, err
}

func (s *Store) Snapshot(module, id string) (*Snapshot, error) {
	started := time.Now()
	value, err := s.observedSnapshot(module, id)
	monitoring.Observe(module, "Snapshot", started, err)
	return value, err
}

func (s *Store) Save(module string, body Doc, id string) (Doc, error) {
	started := time.Now()
	value, err := s.observedSave(module, body, id)
	monitoring.Observe(module, "Save", started, err)
	return value, err
}

func (s *Store) CompareWrite(module, id string, token any, body Doc) (bool, error) {
	started := time.Now()
	value, err := s.observedCompareWrite(module, id, token, body)
	monitoring.Observe(module, "CompareWrite", started, err)
	return value, err
}

func (s *Store) Remove(module, id string) error {
	started := time.Now()
	err := s.observedRemove(module, id)
	monitoring.Observe(module, "Remove", started, err)
	return err
}

func (s *Store) InsertIfAbsent(module, id string, input Doc) (bool, error) {
	started := time.Now()
	value, err := s.observedInsertIfAbsent(module, id, input)
	monitoring.Observe(module, "InsertIfAbsent", started, err)
	return value, err
}

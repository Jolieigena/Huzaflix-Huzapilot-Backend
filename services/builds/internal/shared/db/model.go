package db

import (
	"encoding/json"
	"fmt"
	"github.com/santhosh-tekuri/jsonschema/v6"
	"huzapilot/component/internal/shared/db/query"
	"math/rand"
	"os"
	"strconv"
	"strings"
	"time"
)

type Hooks struct {
	BeforeCreate func(Doc) (Doc, error)
	AfterCreate  func(Doc) error
	BeforeUpdate func(Doc, Doc) (Doc, error)
	AfterUpdate  func(Doc) error
	BeforeDelete func(Doc) error
	AfterDelete  func(Doc) error
}

// Model is the shared facade. Generated services supply schemas and optional hooks.
type Model struct {
	Store             *Store
	Name              string
	Hooks             Hooks
	schema            *jsonschema.Schema
	schemaDocument    Doc
	MaxRows, MaxBytes int
}

func boundedEnv(key string, fallback, min, max int) int {
	value, err := strconv.Atoi(os.Getenv(key))
	if err != nil {
		return fallback
	}
	if value < min {
		return min
	}
	if value > max {
		return max
	}
	return value
}
func NewModel(store *Store, name, schemaText string) (*Model, error) {
	var schemaDoc any
	if err := json.Unmarshal([]byte(schemaText), &schemaDoc); err != nil {
		return nil, err
	}
	compiler := jsonschema.NewCompiler()
	compiler.DefaultDraft(jsonschema.Draft7)
	compiler.AssertFormat()
	if err := compiler.AddResource("schema.json", schemaDoc); err != nil {
		return nil, err
	}
	schema, err := compiler.Compile("schema.json")
	if err != nil {
		return nil, err
	}
	return &Model{Store: store, Name: name, schema: schema, schemaDocument: schemaDoc.(map[string]any), MaxRows: boundedEnv("QUERY_MAX_ROWS", 1000, 1, 10000), MaxBytes: boundedEnv("QUERY_MAX_BYTES", 8388608, 1024, 67108864)}, nil
}
func copyDoc(data Doc) (Doc, error) {
	raw, err := json.Marshal(data)
	if err != nil {
		return nil, err
	}
	var copy Doc
	err = json.Unmarshal(raw, &copy)
	return copy, err
}
func (m *Model) valid(data Doc) (Doc, error) {
	if data == nil {
		return nil, &query.Error{Message: "Record must be an object", Code: "VALIDATION_ERROR", Status: 422}
	}
	data, err := copyDoc(data)
	if err != nil {
		return nil, err
	}
	if err = query.ValidateDocument(data); err != nil {
		return nil, err
	}
	delete(data, "id")
	delete(data, "_id")
	if err = m.schema.Validate(data); err != nil {
		return nil, &query.Error{Message: err.Error(), Code: "VALIDATION_ERROR", Status: 422}
	}
	return data, nil
}
func (m *Model) Find(filter, options any) ([]Doc, error) {
	if _, err := query.Find([]Doc{}, filter, options); err != nil {
		return nil, err
	}
	if rows, _, supported, err := m.Store.Query(m.Name, m.schemaDocument, filter, options, m.MaxRows, m.MaxBytes, false); supported || err != nil {
		return rows, err
	}
	var rows []Doc
	var err error
	if m.Store.kind != "cassandra" {
		candidate := query.CandidateFilter(m.Store.kind, m.schemaDocument, filter)
		if len(candidate) > 0 {
			rows, _, _, err = m.Store.Query(m.Name, m.schemaDocument, candidate, nil, m.MaxRows, m.MaxBytes, false)
			if err != nil {
				return nil, err
			}
		}
	}
	if rows == nil {
		rows, err = m.Store.Scan(m.Name, m.MaxRows, m.MaxBytes)
	}
	if err != nil {
		return nil, err
	}
	return query.Find(rows, filter, options)
}
func (m *Model) FindPage(filter any, size int, after string, total bool) (Doc, error) {
	if size < 1 || size >= m.MaxRows {
		return nil, &query.Error{Message: "Page size must be smaller than QUERY_MAX_ROWS", Code: "INVALID_QUERY", Status: 400}
	}
	if len(after) > 36 {
		return nil, &query.Error{Message: "Invalid page cursor", Code: "INVALID_QUERY", Status: 400}
	}
	if filter == nil {
		filter = Doc{}
	}
	predicate := filter
	if after != "" {
		predicate = Doc{"$and": []any{filter, Doc{"id": Doc{"$gt": after}}}}
	}
	rows, err := m.Find(predicate, Doc{"sort": Doc{"id": 1}, "limit": size + 1})
	if err != nil {
		return nil, err
	}
	more := len(rows) > size
	items := rows
	var next any
	if more {
		items = rows[:size]
		next = items[len(items)-1]["id"]
	}
	result := Doc{"items": items, "hasMore": more, "nextAfter": next}
	if total {
		count, err := m.CountDocuments(filter)
		if err != nil {
			return nil, err
		}
		result["total"] = count
	}
	return result, nil
}
func (m *Model) FindByID(id string) (Doc, error) {
	rows, err := m.Store.List(m.Name, id)
	if err != nil {
		return nil, err
	}
	if len(rows) == 0 {
		return nil, nil
	}
	return rows[0], nil
}
func (m *Model) FindOne(filter any) (Doc, error) {
	rows, err := m.Find(filter, Doc{"limit": 1})
	if err != nil {
		return nil, err
	}
	if len(rows) == 0 {
		return nil, nil
	}
	return rows[0], nil
}
func (m *Model) CountDocuments(filter any) (int64, error) {
	if _, err := query.Find([]Doc{}, filter, nil); err != nil {
		return 0, err
	}
	if _, count, supported, err := m.Store.Query(m.Name, m.schemaDocument, filter, nil, m.MaxRows, m.MaxBytes, true); supported || err != nil {
		return count, err
	}
	rows, err := m.Find(filter, nil)
	return int64(len(rows)), err
}
func (m *Model) Aggregate(pipeline any) ([]Doc, error) {
	if err := query.Validate(pipeline); err != nil {
		return nil, err
	}
	predicate, options, remaining, countField := query.AggregatePrefix(pipeline)
	if countField != "" {
		count, err := m.CountDocuments(predicate)
		if err != nil {
			return nil, err
		}
		if count == 0 {
			return []Doc{}, nil
		}
		return []Doc{{countField: count}}, nil
	}
	var rows []Doc
	var err error
	supported := false
	if len(options) == 0 {
		if group, rest := query.GroupStage(remaining); group != nil {
			rows, supported, err = m.Store.Group(m.Name, m.schemaDocument, predicate, group, m.MaxRows, m.MaxBytes)
			if err != nil {
				return nil, err
			}
			if supported {
				remaining = rest
			}
		}
	}
	if !supported {
		rows, err = m.Find(predicate, options)
		if err != nil {
			return nil, err
		}
	}
	result, err := query.AggregateWithLimits(rows, remaining, m.MaxRows, m.MaxBytes)
	if err != nil {
		return nil, err
	}
	raw, err := json.Marshal(result)
	if err != nil {
		return nil, err
	}
	if len(raw) > m.MaxBytes {
		return nil, &query.Error{Message: "Query result byte limit exceeded", Code: "QUERY_LIMIT", Status: 413}
	}
	return result, nil
}
func (m *Model) Create(input Doc) (Doc, error) {
	data, err := copyDoc(input)
	if err != nil {
		return nil, err
	}
	if m.Hooks.BeforeCreate != nil {
		data, err = m.Hooks.BeforeCreate(data)
		if err != nil {
			return nil, err
		}
	}
	data, err = m.valid(data)
	if err != nil {
		return nil, err
	}
	result, err := m.Store.Save(m.Name, data, "")
	if err != nil {
		return nil, err
	}
	if m.Hooks.AfterCreate != nil {
		copy, _ := copyDoc(result)
		if err = m.Hooks.AfterCreate(copy); err != nil {
			return nil, err
		}
	}
	return result, nil
}
func (m *Model) InsertMany(records []Doc) ([]Doc, error) {
	if len(records) > m.MaxRows {
		return nil, fmt.Errorf("Invalid bulk size")
	}
	for _, record := range records {
		if _, err := m.valid(record); err != nil {
			return nil, err
		}
	}
	result := []Doc{}
	for _, record := range records {
		row, err := m.Create(record)
		if err != nil {
			return result, err
		}
		result = append(result, row)
	}
	return result, nil
}
func (m *Model) mutate(id string, transform func(Doc) (Doc, error), filter any) (Doc, bool, error) {
	for attempt := 0; attempt < 16; attempt++ {
		snapshot, err := m.Store.Snapshot(m.Name, id)
		if err != nil {
			return nil, false, err
		}
		if snapshot == nil {
			return nil, false, nil
		}
		if filter != nil {
			matches, err := query.Find([]Doc{snapshot.Row}, filter, nil)
			if err != nil {
				return nil, false, err
			}
			if len(matches) == 0 {
				return nil, false, nil
			}
		}
		current, _ := copyDoc(snapshot.Row)
		data, err := transform(current)
		if err != nil {
			return nil, false, err
		}
		if m.Hooks.BeforeUpdate != nil {
			previous, _ := copyDoc(snapshot.Row)
			data, err = m.Hooks.BeforeUpdate(previous, data)
			if err != nil {
				return nil, false, err
			}
		}
		data, err = m.valid(data)
		if err != nil {
			return nil, false, err
		}
		previous, _ := copyDoc(snapshot.Row)
		delete(previous, "id")
		delete(previous, "_id")
		a, _ := json.Marshal(previous)
		b, _ := json.Marshal(data)
		if string(a) == string(b) {
			return snapshot.Row, false, nil
		}
		applied, err := m.Store.CompareWrite(m.Name, id, snapshot.Token, data)
		if err != nil {
			return nil, false, err
		}
		if applied {
			data["id"] = id
			if m.Hooks.AfterUpdate != nil {
				copy, _ := copyDoc(data)
				if err = m.Hooks.AfterUpdate(copy); err != nil {
					return nil, false, err
				}
			}
			return data, true, nil
		}
		delay := 1 << attempt
		if delay > 50 {
			delay = 50
		}
		time.Sleep(time.Duration(rand.Intn(delay+1)) * time.Millisecond)
	}
	return nil, false, &query.Error{Message: "Concurrent update; retry the operation", Code: "WRITE_CONFLICT", Status: 409}
}
func (m *Model) UpdateByID(id string, changes Doc) (Doc, error) {
	if changes == nil {
		return nil, &query.Error{Message: "Update must be an object", Code: "INVALID_QUERY", Status: 400}
	}
	result, _, err := m.mutate(id, func(current Doc) (Doc, error) {
		for k, v := range changes {
			current[k] = v
		}
		return current, nil
	}, nil)
	if err != nil {
		return nil, err
	}
	if result == nil {
		return nil, &query.Error{Message: "Not found", Code: "NOT_FOUND", Status: 404}
	}
	return result, nil
}
func (m *Model) deleteMatching(id string, filter any) (bool, error) {
	snapshot, err := m.Store.Snapshot(m.Name, id)
	if err != nil {
		return false, err
	}
	if snapshot == nil {
		return false, nil
	}
	if filter != nil {
		rows, err := query.Find([]Doc{snapshot.Row}, filter, nil)
		if err != nil {
			return false, err
		}
		if len(rows) == 0 {
			return false, nil
		}
	}
	if m.Hooks.BeforeDelete != nil {
		copy, _ := copyDoc(snapshot.Row)
		if err = m.Hooks.BeforeDelete(copy); err != nil {
			return false, err
		}
	}
	applied, err := m.Store.CompareWrite(m.Name, id, snapshot.Token, nil)
	if err != nil {
		return false, err
	}
	if !applied {
		return false, &query.Error{Message: "Concurrent update; retry the operation", Code: "WRITE_CONFLICT", Status: 409}
	}
	if m.Hooks.AfterDelete != nil {
		if err = m.Hooks.AfterDelete(snapshot.Row); err != nil {
			return false, err
		}
	}
	return true, nil
}
func (m *Model) DeleteByID(id string) (Doc, error) {
	deleted, err := m.deleteMatching(id, nil)
	if err != nil {
		return nil, err
	}
	if !deleted {
		return nil, &query.Error{Message: "Not found", Code: "NOT_FOUND", Status: 404}
	}
	return Doc{"deleted": true}, nil
}
func (m *Model) DeleteOne(filter any) (Doc, error) {
	row, err := m.FindOne(filter)
	if err != nil {
		return nil, err
	}
	count := 0
	if row != nil {
		deleted, err := m.deleteMatching(fmt.Sprint(row["id"]), filter)
		if err != nil {
			return nil, err
		}
		if deleted {
			count = 1
		}
	}
	return Doc{"acknowledged": true, "deletedCount": count}, nil
}
func (m *Model) DeleteMany(filter any) (Doc, error) {
	rows, err := m.Find(filter, nil)
	if err != nil {
		return nil, err
	}
	count := 0
	for _, row := range rows {
		deleted, err := m.deleteMatching(fmt.Sprint(row["id"]), filter)
		if err != nil {
			return nil, err
		}
		if deleted {
			count++
		}
	}
	return Doc{"acknowledged": true, "deletedCount": count}, nil
}
func (m *Model) Distinct(path string, filter any) ([]any, error) {
	if _, err := query.Distinct(nil, path); err != nil {
		return nil, err
	}
	rows, err := m.Find(filter, nil)
	if err != nil {
		return nil, err
	}
	return query.Distinct(rows, path)
}
func (m *Model) update(filter, update any, many bool) (Doc, error) {
	if _, err := query.Updated(Doc{}, update); err != nil {
		return nil, err
	}
	options := Doc{}
	if !many {
		options["limit"] = 1
	}
	rows, err := m.Find(filter, options)
	if err != nil {
		return nil, err
	}
	for _, row := range rows {
		data, err := query.Updated(row, update)
		if err != nil {
			return nil, err
		}
		if _, err = m.valid(data); err != nil {
			return nil, err
		}
	}
	matched, modified := 0, 0
	for _, row := range rows {
		result, changed, err := m.mutate(fmt.Sprint(row["id"]), func(current Doc) (Doc, error) { return query.Updated(current, update) }, filter)
		if err != nil {
			return nil, err
		}
		if result != nil {
			matched++
			if changed {
				modified++
			}
		}
	}
	return Doc{"acknowledged": true, "matchedCount": matched, "modifiedCount": modified}, nil
}
func (m *Model) UpdateOne(filter, update any) (Doc, error)  { return m.update(filter, update, false) }
func (m *Model) UpdateMany(filter, update any) (Doc, error) { return m.update(filter, update, true) }

// UpsertByID applies operators once per call; caller retries are not deduplicated.
func (m *Model) UpsertByID(id string, update any, onInsert Doc) (Doc, error) {
	if id == "" || len(id) > 36 {
		return nil, &query.Error{Message: "Invalid upsert id", Code: "INVALID_QUERY", Status: 400}
	}
	if _, err := query.Updated(Doc{}, update); err != nil {
		return nil, err
	}
	initial, err := m.valid(onInsert)
	if err != nil {
		return nil, err
	}
	for attempt := 0; attempt < 16; attempt++ {
		row, _, err := m.mutate(id, func(current Doc) (Doc, error) { return query.Updated(current, update) }, nil)
		if err != nil {
			return nil, err
		}
		if row != nil {
			return Doc{"record": row, "inserted": false}, nil
		}
		data, err := query.Updated(initial, update)
		if err != nil {
			return nil, err
		}
		if m.Hooks.BeforeCreate != nil {
			data, err = m.Hooks.BeforeCreate(data)
			if err != nil {
				return nil, err
			}
		}
		data, err = m.valid(data)
		if err != nil {
			return nil, err
		}
		applied, err := m.Store.InsertIfAbsent(m.Name, id, data)
		if err != nil {
			return nil, err
		}
		if applied {
			data["id"] = id
			if m.Hooks.AfterCreate != nil {
				copy, _ := copyDoc(data)
				if err = m.Hooks.AfterCreate(copy); err != nil {
					return nil, err
				}
			}
			return Doc{"record": data, "inserted": true}, nil
		}
	}
	return nil, &query.Error{Message: "Concurrent upsert; retry the operation", Code: "WRITE_CONFLICT", Status: 409}
}

// Explain reports planner eligibility, not a database optimizer execution plan.
func (m *Model) Explain(filter, options any) (Doc, error) {
	if _, err := query.Find([]Doc{}, filter, options); err != nil {
		return nil, err
	}
	kind := m.Store.kind
	native := false
	if kind == "cassandra" {
		_, native = query.CandidateIDs(filter)
	} else {
		dialect := kind
		if kind == "mongodb" {
			dialect = "postgres"
		}
		native = query.SQLPlan(dialect, m.schemaDocument, filter, options) != nil
	}
	execution := "bounded-scan"
	if native {
		execution = "native"
		if kind == "cassandra" {
			execution = "primary-key"
		}
	} else if kind != "cassandra" && len(query.CandidateFilter(kind, m.schemaDocument, filter)) > 0 {
		execution = "native-prefilter"
	}
	return Doc{"database": kind, "execution": execution, "maxRows": m.MaxRows, "maxBytes": m.MaxBytes, "atomicWrites": "single-record", "bulkAtomic": false, "upsert": "id", "crossModelHttpLookup": false}, nil
}

func scalarPath(row Doc, path string) any {
	var value any = row
	for _, key := range strings.Split(path, ".") {
		object, ok := value.(map[string]any)
		if !ok {
			return nil
		}
		value = object[key]
	}
	return value
}

// Populate requires an explicit target read policy. Nil policy/filter denies access.
func (m *Model) Populate(rows []Doc, relation string, target *Model, authorize func(string) (Doc, error)) ([]Doc, error) {
	relations, _ := m.schemaDocument["x-huza-relations"].(map[string]any)
	spec, _ := relations[relation].(map[string]any)
	if spec == nil || target == nil || target.Store != m.Store || target.Name != spec["model"] {
		return nil, &query.Error{Message: "Unknown relationship or target model", Code: "INVALID_QUERY", Status: 400}
	}
	if authorize == nil {
		return nil, &query.Error{Message: "Relationship read policy required", Code: "FORBIDDEN", Status: 403}
	}
	policy, err := authorize(target.Name)
	if err != nil {
		return nil, err
	}
	if policy == nil {
		return nil, &query.Error{Message: "Related model access denied", Code: "FORBIDDEN", Status: 403}
	}
	if _, err = query.Find([]Doc{}, policy, nil); err != nil {
		return nil, err
	}
	raw, err := json.Marshal(rows)
	if err != nil {
		return nil, err
	}
	if len(rows) > m.MaxRows || len(raw) > m.MaxBytes {
		return nil, &query.Error{Message: "Relationship input limit exceeded", Code: "QUERY_LIMIT", Status: 413}
	}
	local, foreign := spec["localField"].(string), spec["foreignField"].(string)
	many := spec["many"] == true
	keys := map[string]any{}
	for _, row := range rows {
		value := scalarPath(row, local)
		if value != nil {
			keys[query.ValueKey(value)] = value
		}
	}
	groups := map[string][]Doc{}
	if len(keys) > 0 {
		values := []any{}
		for _, value := range keys {
			values = append(values, value)
		}
		related, err := target.Find(Doc{"$and": []any{policy, Doc{foreign: Doc{"$in": values}}}}, nil)
		if err != nil {
			return nil, err
		}
		for _, row := range related {
			value := scalarPath(row, foreign)
			if value != nil {
				key := query.ValueKey(value)
				groups[key] = append(groups[key], row)
			}
		}
	}
	result := []Doc{}
	size := 0
	for _, source := range rows {
		value := scalarPath(source, local)
		matches := []Doc{}
		if value != nil {
			matches = append(matches, groups[query.ValueKey(value)]...)
		}
		if !many && len(matches) > 1 {
			return nil, &query.Error{Message: "Singular relationship is ambiguous", Code: "RELATION_CONFLICT", Status: 409}
		}
		row, err := copyDoc(source)
		if err != nil {
			return nil, err
		}
		if many {
			row[relation] = matches
		} else {
			row[relation] = nil
			if len(matches) > 0 {
				row[relation] = matches[0]
			}
		}
		raw, err := json.Marshal(row)
		if err != nil {
			return nil, err
		}
		size += len(raw)
		if size > m.MaxBytes {
			return nil, &query.Error{Message: "Relationship result limit exceeded", Code: "QUERY_LIMIT", Status: 413}
		}
		row, err = copyDoc(row)
		if err != nil {
			return nil, err
		}
		result = append(result, row)
	}
	return result, nil
}

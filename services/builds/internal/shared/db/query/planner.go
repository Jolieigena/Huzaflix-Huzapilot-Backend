package query

import (
	"fmt"
	"strings"
)

// Plan describes a fully pushable query; nil requests bounded evaluation.
type Plan struct {
	Where  string
	Params []any
	Order  string
}
type unsupportedPlan struct{}

func scalarColumn(kind string, schema Document, path string) (string, string, string) {
	rejectPlan := func() { panic(unsupportedPlan{}) }
	if path == "id" {
		return "id", "'string'", "string"
	}
	node := schema
	parts := strings.Split(path, ".")
	for _, part := range parts {
		if !pathPart.MatchString(part) {
			rejectPlan()
		}
		properties, ok := node["properties"].(map[string]any)
		if !ok {
			rejectPlan()
		}
		node, ok = properties[part].(map[string]any)
		if !ok || node["type"] == "array" {
			rejectPlan()
		}
	}
	rawType := node["type"]
	if types, ok := rawType.([]any); ok {
		values := []any{}
		for _, value := range types {
			if value != "null" {
				values = append(values, value)
			}
		}
		rawType = nil
		if len(values) == 1 {
			rawType = values[0]
		}
	}
	typ, ok := rawType.(string)
	if !ok || !contains([]string{"string", "integer", "number", "boolean"}, typ) {
		rejectPlan()
	}
	value, tag := "", ""
	if kind == "postgres" {
		raw := "data::jsonb #> '{" + strings.Join(parts, ",") + "}'"
		scalar := "data::jsonb #>> '{" + strings.Join(parts, ",") + "}'"
		tag = "jsonb_typeof(" + raw + ")"
		if typ == "string" {
			value = "(" + scalar + ") COLLATE \"C\""
		} else {
			cast := "numeric"
			if typ == "boolean" {
				cast = "boolean"
			}
			value = "CAST(" + scalar + " AS " + cast + ")"
		}
	} else {
		pathSQL := "$.\"" + strings.Join(parts, "\".\"") + "\""
		raw := "json_extract(data, '" + pathSQL + "')"
		tag = "LOWER(JSON_TYPE(" + raw + "))"
		if kind == "sqlite" {
			tag = "json_type(data, '" + pathSQL + "')"
			value = raw
		} else if typ == "string" {
			value = "CASE WHEN " + tag + "='null' THEN NULL ELSE JSON_UNQUOTE(" + raw + ") END"
		} else {
			value = "NULLIF(JSON_UNQUOTE(" + raw + "), 'null')"
		}
		if typ == "string" {
			if kind == "sqlite" {
				value = "(" + value + ") COLLATE BINARY"
			} else {
				length, ok := num(node["maxLength"])
				if ok && length >= 1 && length <= 191 {
					value = fmt.Sprintf("CAST((%s) AS CHAR(%d) CHARACTER SET utf8mb4) COLLATE utf8mb4_0900_bin", value, int(length))
				} else {
					value = "CAST((" + value + ") AS BINARY)"
				}
			}
		} else if kind == "mysql" {
			if typ == "boolean" {
				value = "CASE WHEN " + tag + " IS NULL OR " + tag + "='null' THEN NULL ELSE (" + raw + "=true) END"
			} else {
				value = "CAST(" + value + " AS DOUBLE)"
			}
		}
	}
	return value, tag, typ
}

func SQLPlan(kind string, schema Document, filter, options any) (result *Plan) {
	if !contains([]string{"postgres", "mysql", "sqlite"}, kind) {
		return nil
	}
	defer func() {
		if err := recover(); err != nil {
			if _, ok := err.(unsupportedPlan); ok {
				result = nil
			} else {
				panic(err)
			}
		}
	}()
	rejectPlan := func() { panic(unsupportedPlan{}) }
	params := []any{}
	bind := func(value any) string { params = append(params, value); return "?" }
	column := func(path string) (string, string, string) { return scalarColumn(kind, schema, path) }
	join := func(parts []string, op string) string {
		if len(parts) == 0 {
			if op == "AND" {
				return "1=1"
			}
			return "1=0"
		}
		return "(" + strings.Join(parts, " "+op+" ") + ")"
	}
	var clause func(string, any) string
	clause = func(path string, condition any) string {
		value, tag, typ := column(path)
		ops := Document{"$eq": condition}
		if hasOperator(condition) {
			ops = obj(condition)
		}
		out := []string{}
		for _, op := range keys(ops) {
			arg := ops[op]
			if op == "$exists" {
				not := ""
				if arg == true {
					not = "NOT "
				}
				out = append(out, tag+" IS "+not+"NULL")
				continue
			}
			if op == "$not" {
				out = append(out, "NOT "+clause(path, arg))
				continue
			}
			if op == "$in" || op == "$nin" {
				items := []string{}
				for _, v := range array(arg) {
					items = append(items, clause(path, Document{"$eq": v}))
				}
				not := ""
				if op == "$nin" {
					not = "NOT "
				}
				out = append(out, not+join(items, "OR"))
				continue
			}
			if !contains([]string{"$eq", "$ne", "$gt", "$gte", "$lt", "$lte"}, op) {
				rejectPlan()
			}
			sql := "1=0"
			if arg == nil {
				if op == "$eq" || op == "$ne" {
					sql = "(" + tag + " IS NULL OR " + tag + "='null')"
				}
			} else {
				compatible := false
				switch typ {
				case "string":
					_, compatible = arg.(string)
				case "boolean":
					_, compatible = arg.(bool)
				default:
					_, compatible = num(arg)
				}
				if compatible && !(typ == "boolean" && op != "$eq" && op != "$ne") {
					operator := map[string]string{"$eq": "=", "$ne": "=", "$gt": ">", "$gte": ">=", "$lt": "<", "$lte": "<="}[op]
					sql = "(" + value + " IS NOT NULL AND " + value + " " + operator + " " + bind(arg) + ")"
				}
			}
			if op == "$ne" {
				out = append(out, "NOT ("+sql+")")
			} else {
				out = append(out, "("+sql+")")
			}
		}
		return join(out, "AND")
	}
	var walk func(any) string
	walk = func(filter any) string {
		out := []string{}
		for _, key := range keys(filter) {
			value := obj(filter)[key]
			if contains([]string{"$and", "$or", "$nor"}, key) {
				children := []string{}
				for _, v := range array(value) {
					children = append(children, walk(v))
				}
				op := "OR"
				if key == "$and" {
					op = "AND"
				}
				sql := join(children, op)
				if key == "$nor" {
					sql = "NOT " + sql
				}
				out = append(out, sql)
			} else {
				if strings.HasPrefix(key, "$") {
					rejectPlan()
				}
				out = append(out, clause(key, value))
			}
		}
		return join(out, "AND")
	}
	if filter == nil {
		filter = Document{}
	}
	if options == nil {
		options = Document{}
	}
	where := walk(filter)
	sort := obj(options)["sort"]
	if sort == nil {
		sort = Document{}
	}
	order := []string{}
	for _, path := range keys(sort) {
		value, _, _ := column(path)
		dir := "DESC"
		if flag(obj(sort)[path], 1) {
			dir = "ASC"
		}
		order = append(order, fmt.Sprintf("(%s IS NOT NULL) %s", value, dir), value+" "+dir)
	}
	if _, ok := obj(sort)["id"]; !ok {
		order = append(order, "id ASC")
	}
	return &Plan{where, params, strings.Join(order, ", ")}
}

// Plain returns validated query data without the parser's order wrapper.
func Plain(value any) any { return plain(value) }

func MongoPlan(schema Document, filter, options any) Document {
	if SQLPlan("postgres", schema, filter, options) == nil {
		return nil
	}
	if filter == nil {
		filter = Document{}
	}
	join := func(items []any, op string) Document {
		if len(items) == 0 {
			if op == "$and" {
				return Document{}
			}
			return Document{"$expr": false}
		}
		return Document{op: items}
	}
	var clause func(string, any) any
	clause = func(path string, condition any) any {
		if path == "id" {
			path = "_id"
		}
		ops := Document{"$eq": condition}
		if hasOperator(condition) {
			ops = obj(condition)
		}
		out := []any{}
		for op, arg := range ops {
			var value any
			switch op {
			case "$not":
				value = Document{"$nor": []any{clause(path, arg)}}
			case "$in", "$nin":
				items := []any{}
				for _, v := range array(arg) {
					items = append(items, clause(path, Document{"$eq": v}))
				}
				value = join(items, "$or")
				if op == "$nin" {
					value = Document{"$nor": []any{value}}
				}
			case "$ne":
				value = Document{"$nor": []any{Document{path: Document{"$eq": plain(arg)}}}}
			default:
				_, text := arg.(string)
				_, number := num(arg)
				if contains([]string{"$gt", "$gte", "$lt", "$lte"}, op) && !text && !number {
					value = Document{"$expr": false}
				} else {
					value = Document{path: Document{op: plain(arg)}}
				}
			}
			out = append(out, value)
		}
		return join(out, "$and")
	}
	var walk func(any) any
	walk = func(filter any) any {
		out := []any{}
		for key, value := range obj(filter) {
			if contains([]string{"$and", "$or", "$nor"}, key) {
				children := []any{}
				for _, v := range array(value) {
					children = append(children, walk(v))
				}
				out = append(out, join(children, key))
			} else {
				out = append(out, clause(key, value))
			}
		}
		return join(out, "$and")
	}
	return walk(filter).(Document)
}

// SortFields preserves compound ordering from parsed JSON.
func SortFields(options any) []string {
	if options == nil {
		return nil
	}
	sort := obj(options)["sort"]
	if sort == nil {
		return nil
	}
	return keys(sort)
}

// AggregatePrefix only moves an initial match/sort/skip/limit sequence to storage.
func AggregatePrefix(pipeline any) (any, Document, any, string) {
	remaining := append([]any{}, array(pipeline)...)
	var predicate any = Document{}
	if len(remaining) > 0 {
		if value, ok := obj(remaining[0])["$match"]; ok {
			predicate = value
			remaining = remaining[1:]
		}
	}
	if len(remaining) == 1 {
		if value, ok := obj(remaining[0])["$count"]; ok {
			return predicate, Document{}, remaining, str(value)
		}
	}
	options := Document{}
	for _, name := range []string{"sort", "skip", "limit"} {
		if len(remaining) > 0 {
			if value, ok := obj(remaining[0])["$"+name]; ok {
				options[name] = value
				remaining = remaining[1:]
			}
		}
	}
	return predicate, options, remaining, ""
}

type Metric struct{ Name, Alias, NullAlias string }
type GroupPlan struct {
	Filter   *Plan
	Select   string
	Params   []any
	GroupBy  string
	Key      any
	Constant bool
	KeyType  string
	Columns  []Metric
	Mongo    []Document
}

func NativeGroupPlan(kind string, schema Document, filter, group any) (result *GroupPlan) {
	if kind == "cassandra" {
		return nil
	}
	dialect := kind
	if kind == "mongodb" {
		dialect = "postgres"
	}
	compiled := SQLPlan(dialect, schema, filter, Document{})
	if compiled == nil {
		return nil
	}
	defer func() {
		if err := recover(); err != nil {
			if _, ok := err.(unsupportedPlan); ok {
				result = nil
			} else {
				panic(err)
			}
		}
	}()
	params := []any{}
	columns := []Metric{}
	mongo := Document{"hp_count": Document{"$sum": 1}}
	key := obj(group)["_id"]
	constant := true
	keyType, keySQL := "literal", "NULL"
	if path, ok := key.(string); ok && strings.HasPrefix(path, "$") {
		constant = false
		keySQL, _, keyType = scalarColumn(dialect, schema, path[1:])
		if path == "$id" {
			path = "$_id"
		}
		mongo["_id"] = Document{"$ifNull": []any{path, nil}}
	} else if isObj(key) {
		return nil
	} else if _, ok := key.([]any); ok {
		return nil
	} else {
		mongo["_id"] = Document{"$literal": key}
	}
	selectSQL := []string{keySQL + " AS hp_key", "COUNT(*) AS hp_count"}
	for _, name := range keys(group) {
		if name == "_id" {
			continue
		}
		spec := obj(obj(group)[name])
		op := keys(spec)[0]
		arg := spec[op]
		if !contains([]string{"$sum", "$avg", "$min", "$max"}, op) {
			return nil
		}
		alias := fmt.Sprintf("hp_a%d", len(columns))
		term := "CAST(NULL AS DECIMAL)"
		numeric := false
		var expression any = Document{"$literal": plain(arg)}
		if path, ok := arg.(string); ok && strings.HasPrefix(path, "$") {
			value, _, typ := scalarColumn(dialect, schema, path[1:])
			numeric = typ == "integer" || typ == "number"
			if numeric {
				term = value
			}
			if path == "$id" {
				path = "$_id"
			}
			expression = path
		} else if _, ok := num(arg); ok {
			numeric = true
			cast := "DOUBLE PRECISION"
			if kind == "mysql" {
				cast = "DOUBLE"
			}
			term = "CAST(? AS " + cast + ")"
			params = append(params, arg)
		} else if isObj(arg) {
			return nil
		} else if _, ok := arg.([]any); ok {
			return nil
		}
		if (op == "$min" || op == "$max") && !numeric {
			return nil
		}
		sql := strings.ToUpper(op[1:]) + "(" + term + ")"
		if op == "$sum" {
			sql = "COALESCE(" + sql + ",0)"
		}
		nullAlias := ""
		if op == "$min" {
			nullAlias = fmt.Sprintf("hp_n%d", len(columns))
			selectSQL = append(selectSQL, "COUNT(*)-COUNT("+term+") AS "+nullAlias)
			if strings.HasPrefix(term, "CAST(?") {
				params = append(params, arg)
			}
			mongo[nullAlias] = Document{"$sum": Document{"$cond": []any{Document{"$eq": []any{Document{"$ifNull": []any{expression, nil}}, nil}}, 1, 0}}}
		}
		selectSQL = append(selectSQL, sql+" AS "+alias)
		mongo[alias] = Document{op: expression}
		columns = append(columns, Metric{name, alias, nullAlias})
	}
	params = append(params, compiled.Params...)
	groupBy := keySQL
	if constant {
		groupBy = ""
	}
	return &GroupPlan{compiled, strings.Join(selectSQL, ", "), params, groupBy, key, constant, keyType, columns, []Document{{"$match": MongoPlan(schema, filter, Document{})}, {"$group": mongo}}}
}

// GroupStage returns a first-stage group and the remaining ordered stages.
func GroupStage(pipeline any) (any, any) {
	stages := array(pipeline)
	if len(stages) > 0 {
		if group, ok := obj(stages[0])["$group"]; ok {
			return group, stages[1:]
		}
	}
	return nil, pipeline
}

// CandidateIDs extracts only positive conjunction constraints, never an unsafe OR.
func CandidateIDs(predicate any) ([]string, bool) {
	if predicate == nil {
		return nil, false
	}
	p := Plain(predicate).(map[string]any)
	candidates := [][]string{}
	switch value := p["id"].(type) {
	case string:
		candidates = append(candidates, []string{value})
	case map[string]any:
		if id, ok := value["$eq"].(string); ok {
			candidates = append(candidates, []string{id})
		}
		if values, ok := value["$in"].([]any); ok {
			ids := []string{}
			valid := true
			seen := map[string]bool{}
			for _, v := range values {
				id, ok := v.(string)
				if !ok {
					valid = false
					break
				}
				if !seen[id] {
					ids = append(ids, id)
					seen[id] = true
				}
			}
			if valid {
				candidates = append(candidates, ids)
			}
		}
	}
	if children, ok := p["$and"].([]any); ok {
		for _, child := range children {
			if ids, ok := CandidateIDs(child); ok {
				candidates = append(candidates, ids)
			}
		}
	}
	if len(candidates) == 0 {
		return nil, false
	}
	ids := []string{}
	for _, id := range candidates[0] {
		include := true
		for _, candidate := range candidates[1:] {
			found := false
			for _, v := range candidate {
				if v == id {
					found = true
					break
				}
			}
			if !found {
				include = false
				break
			}
		}
		if include {
			ids = append(ids, id)
		}
	}
	return ids, true
}
func CandidateFilter(kind string, schema Document, predicate any) Document {
	if predicate == nil {
		return Document{}
	}
	clauses := []any{}
	for key, value := range Plain(predicate).(map[string]any) {
		if key == "$and" {
			for _, child := range value.([]any) {
				candidate := CandidateFilter(kind, schema, child)
				if len(candidate) > 0 {
					clauses = append(clauses, candidate)
				}
			}
		} else {
			dialect := kind
			if kind == "mongodb" {
				dialect = "postgres"
			}
			clause := Document{key: value}
			if SQLPlan(dialect, schema, clause, nil) != nil {
				clauses = append(clauses, clause)
			}
		}
	}
	if len(clauses) == 0 {
		return Document{}
	}
	return Document{"$and": clauses}
}

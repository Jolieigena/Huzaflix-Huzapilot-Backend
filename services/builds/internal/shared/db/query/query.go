// Package query implements the versioned portable document query contract.
package query

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"regexp"
	"sort"
	"strings"
	"time"
	_ "time/tzdata"
)

type Document = map[string]any
type ordered struct {
	keys   []string
	values Document
}
type Error struct {
	Message, Code string
	Status        int
}

func (e *Error) Error() string { return e.Message }

const MaxRows = 10000

func reject(message string) { panic(&Error{message, "INVALID_QUERY", 400}) }
func require(ok bool, message string) {
	if !ok {
		reject(message)
	}
}
func limit(ok bool) {
	if !ok {
		panic(&Error{"Query row limit exceeded", "QUERY_LIMIT", 413})
	}
}

// Only private validation faults are recovered at the public boundary. Unexpected
// programming panics remain visible; they are never converted into query success.
func guarded(err *error) {
	if value := recover(); value != nil {
		if fault, ok := value.(*Error); ok {
			*err = fault
		} else {
			panic(value)
		}
	}
}
func obj(v any) Document {
	switch x := v.(type) {
	case *ordered:
		return x.values
	case map[string]any:
		return x
	}
	reject("Expected an object")
	return nil
}
func isObj(v any) bool {
	switch v.(type) {
	case *ordered, map[string]any:
		return true
	}
	return false
}
func keys(v any) []string {
	if x, ok := v.(*ordered); ok {
		return x.keys
	}
	out := []string{}
	for k := range obj(v) {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}
func array(v any) []any { x, ok := v.([]any); require(ok, "Expected an array"); return x }
func str(v any) string  { x, ok := v.(string); require(ok, "Expected a string"); return x }
func num(v any) (float64, bool) {
	switch x := v.(type) {
	case float64:
		return x, !math.IsNaN(x) && !math.IsInf(x, 0)
	case int:
		return float64(x), true
	case int64:
		return float64(x), true
	case json.Number:
		y, e := x.Float64()
		return y, e == nil && !math.IsNaN(y) && !math.IsInf(y, 0)
	}
	return 0, false
}
func integer(v any) (int, bool) {
	n, ok := num(v)
	return int(n), ok && math.Trunc(n) == n && n >= 0 && n <= MaxRows
}
func numeric(v any) float64      { x, ok := num(v); require(ok, "Expected a finite number"); return x }
func flag(v any, n float64) bool { x, ok := num(v); return ok && x == n }
func plain(v any) any {
	if isObj(v) {
		out := Document{}
		for k, x := range obj(v) {
			out[k] = plain(x)
		}
		return out
	}
	if a, ok := v.([]any); ok {
		out := make([]any, len(a))
		for i, x := range a {
			out[i] = plain(x)
		}
		return out
	}
	return v
}
func canonical(v any) string {
	raw, err := json.Marshal(plain(v))
	require(err == nil, "Invalid JSON value")
	return string(raw)
}
func clone(v any) any {
	if isObj(v) {
		out := Document{}
		for k, x := range obj(v) {
			out[k] = clone(x)
		}
		return out
	}
	if a, ok := v.([]any); ok {
		out := make([]any, len(a))
		for i, x := range a {
			out[i] = clone(x)
		}
		return out
	}
	return v
}

type missing struct{}

var absent = missing{}

func equal(a, b any) bool {
	_, am := a.(missing)
	_, bm := b.(missing)
	if am || bm {
		return am && bm
	}
	return canonical(a) == canonical(b)
}

var pathPart = regexp.MustCompile(`^[A-Za-z_][A-Za-z0-9_-]*$`)

func field(v any) string {
	path := str(v)
	require(len(path) > 0 && len(path) <= 256, "Invalid field path")
	for _, part := range strings.Split(path, ".") {
		require(pathPart.MatchString(part) && part != "__proto__" && part != "prototype" && part != "constructor", "Invalid field path: "+path)
	}
	return path
}
func get(row any, path string) any {
	parts := strings.Split(path, ".")
	var walk func(any, int) any
	walk = func(value any, index int) any {
		if index == len(parts) {
			return value
		}
		if items, ok := value.([]any); ok {
			result := []any{}
			found := false
			for _, item := range items {
				v := walk(item, index)
				if _, missing := v.(missing); missing {
					continue
				}
				found = true
				if values, ok := v.([]any); ok {
					result = append(result, values...)
				} else {
					result = append(result, v)
				}
			}
			if found {
				return result
			}
			return absent
		}
		if !isObj(value) {
			return absent
		}
		v, ok := obj(value)[parts[index]]
		if !ok {
			return absent
		}
		return walk(v, index+1)
	}
	return walk(row, 0)
}
func scalarCondition(value any) bool {
	if !isObj(value) {
		return false
	}
	for k := range obj(value) {
		if strings.HasPrefix(k, "$") && !contains([]string{"$and", "$or", "$nor"}, k) {
			return true
		}
	}
	return false
}
func elementMatches(item, condition any) bool {
	if scalarCondition(condition) {
		return matches(Document{"value": item}, Document{"value": condition})
	}
	if isObj(condition) {
		return isObj(item) && matches(obj(item), condition)
	}
	return equal(item, condition)
}

func put(row Document, path string, value any) {
	parts := strings.Split(field(path), ".")
	target := row
	for _, part := range parts[:len(parts)-1] {
		_, array := target[part].([]any)
		require(!array, "Array path writes require explicit array replacement")
		if !isObj(target[part]) {
			target[part] = Document{}
		}
		target = obj(target[part])
	}
	key := parts[len(parts)-1]
	if _, ok := value.(missing); ok {
		delete(target, key)
	} else {
		target[key] = clone(value)
	}
}
func selection(value any, parts []string) any {
	if len(parts) == 0 {
		return clone(value)
	}
	if items, ok := value.([]any); ok {
		out := []any{}
		for _, item := range items {
			_, array := item.([]any)
			require(isObj(item) || array, "Nested projection requires an array of documents")
			out = append(out, selection(item, parts))
		}
		return out
	}
	if !isObj(value) {
		return Document{}
	}
	child, ok := obj(value)[parts[0]]
	if !ok {
		return Document{}
	}
	return Document{parts[0]: selection(child, parts[1:])}
}
func mergeProjection(target, source any) any {
	if isObj(target) && isObj(source) {
		for key, value := range obj(source) {
			obj(target)[key] = mergeProjection(obj(target)[key], value)
		}
		return target
	}
	if a, ok := target.([]any); ok {
		if b, ok := source.([]any); ok && len(a) == len(b) {
			out := make([]any, len(a))
			for i := range a {
				out[i] = mergeProjection(a[i], b[i])
			}
			return out
		}
	}
	return clone(source)
}
func removeProjection(value any, parts []string) {
	if items, ok := value.([]any); ok {
		for _, item := range items {
			removeProjection(item, parts)
		}
	} else if isObj(value) {
		if len(parts) == 1 {
			delete(obj(value), parts[0])
		} else if child, ok := obj(value)[parts[0]]; ok {
			removeProjection(child, parts[1:])
		}
	}
}
func rank(v any) int {
	if v == nil {
		return 0
	}
	if _, ok := v.(missing); ok {
		return 0
	}
	if _, ok := v.(bool); ok {
		return 1
	}
	if _, ok := num(v); ok {
		return 2
	}
	if _, ok := v.(string); ok {
		return 3
	}
	if _, ok := v.([]any); ok {
		return 4
	}
	return 5
}
func compare(a, b any) int {
	ra, rb := rank(a), rank(b)
	if ra < rb {
		return -1
	}
	if ra > rb {
		return 1
	}
	if ra == 0 {
		return 0
	}
	if ra == 1 {
		if a == b {
			return 0
		}
		if a == false {
			return -1
		}
		return 1
	}
	if ra == 2 {
		na, _ := num(a)
		nb, _ := num(b)
		if na < nb {
			return -1
		}
		if na > nb {
			return 1
		}
		return 0
	}
	sa, sb := "", ""
	if ra >= 4 {
		sa, sb = canonical(a), canonical(b)
	} else {
		sa, sb = str(a), str(b)
	}
	return strings.Compare(sa, sb)
}
func contains(items []string, s string) bool {
	for _, x := range items {
		if x == s {
			return true
		}
	}
	return false
}
func hasOperator(v any) bool {
	if !isObj(v) {
		return false
	}
	for k := range obj(v) {
		if strings.HasPrefix(k, "$") {
			return true
		}
	}
	return false
}
func validateValue(value any, depth int) {
	require(depth <= 32, "Query nesting exceeds 32 levels")
	if isObj(value) {
		for k, v := range obj(value) {
			require(k != "__proto__" && k != "prototype" && k != "constructor", "Unsafe object key")
			validateValue(v, depth+1)
		}
	} else if a, ok := value.([]any); ok {
		require(len(a) <= MaxRows, "Array exceeds row limit")
		for _, v := range a {
			validateValue(v, depth+1)
		}
	} else if f, ok := value.(float64); ok {
		require(!math.IsNaN(f) && !math.IsInf(f, 0), "Non-finite numbers are unsupported")
	}
}

// Parse preserves object member order for compound sort specifications.
func Parse(raw []byte) (value any, err error) {
	defer guarded(&err)
	d := json.NewDecoder(bytes.NewReader(raw))
	d.UseNumber()
	value = decode(d, 0)
	_, end := d.Token()
	require(end == io.EOF, "Trailing JSON data")
	return
}
func decode(d *json.Decoder, depth int) any {
	require(depth <= 32, "Query nesting exceeds 32 levels")
	token, err := d.Token()
	require(err == nil, "Invalid JSON")
	if delim, ok := token.(json.Delim); ok {
		if delim == '{' {
			out := &ordered{values: Document{}}
			for d.More() {
				key, e := d.Token()
				require(e == nil, "Invalid JSON key")
				k := str(key)
				_, exists := out.values[k]
				require(!exists, "Duplicate JSON object key")
				out.keys = append(out.keys, k)
				out.values[k] = decode(d, depth+1)
			}
			_, e := d.Token()
			require(e == nil, "Invalid JSON object")
			return out
		}
		if delim == '[' {
			out := []any{}
			for d.More() {
				out = append(out, decode(d, depth+1))
				require(len(out) <= MaxRows, "Array exceeds row limit")
			}
			_, e := d.Token()
			require(e == nil, "Invalid JSON array")
			return out
		}
		reject("Invalid JSON delimiter")
	}
	if n, ok := token.(json.Number); ok {
		return numeric(n)
	}
	return token
}
func validateFilter(query any) {
	for key, value := range obj(query) {
		if key == "$expr" {
			validateExpression(value)
			continue
		}
		if contains([]string{"$and", "$or", "$nor"}, key) {
			a := array(value)
			require(len(a) > 0, key+" needs a nonempty array")
			for _, v := range a {
				validateFilter(v)
			}
		} else {
			field(key)
			if hasOperator(value) {
				for op, arg := range obj(value) {
					require(contains([]string{"$eq", "$ne", "$gt", "$gte", "$lt", "$lte", "$in", "$nin", "$exists", "$not", "$all", "$size", "$elemMatch", "$contains", "$startsWith"}, op), "Unsupported filter operator: "+op)
					switch op {
					case "$in", "$nin", "$all":
						array(arg)
					case "$contains", "$startsWith":
						searchConfig(arg)
					case "$exists":
						_, ok := arg.(bool)
						require(ok, "$exists needs a boolean")
					case "$size":
						_, ok := integer(arg)
						require(ok, "$size needs a nonnegative integer")
					case "$not":
						validateFilter(Document{key: arg})
					case "$elemMatch":
						if scalarCondition(arg) {
							validateFilter(Document{"value": arg})
						} else {
							validateFilter(arg)
						}
					}
				}
			}
		}
	}
}
func matches(row Document, query any) bool {
	for key, condition := range obj(query) {
		if contains([]string{"$and", "$or", "$nor"}, key) {
			all, anyMatch := true, false
			for _, q := range array(condition) {
				m := matches(row, q)
				all = all && m
				anyMatch = anyMatch || m
			}
			if key == "$and" && !all || key == "$or" && !anyMatch || key == "$nor" && anyMatch {
				return false
			}
			continue
		}
		if key == "$expr" {
			if !truth(expression(row, condition)) {
				return false
			}
			continue
		}
		actual := get(row, key)
		eq := func(v any) bool {
			_, missing := actual.(missing)
			if v == nil && missing || equal(actual, v) {
				return true
			}
			if a, ok := actual.([]any); ok {
				if _, isArray := v.([]any); !isArray {
					for _, x := range a {
						if equal(x, v) {
							return true
						}
					}
				}
			}
			return false
		}
		if !hasOperator(condition) {
			if !eq(condition) {
				return false
			}
			continue
		}
		for op, v := range obj(condition) {
			switch op {
			case "$contains", "$startsWith":
				needle, insensitive := searchConfig(v)
				if insensitive {
					needle = strings.ToLower(needle)
				}
				values, ok := actual.([]any)
				if !ok {
					values = []any{actual}
				}
				found := false
				for _, item := range values {
					value, ok := item.(string)
					if !ok {
						continue
					}
					if insensitive {
						value = strings.ToLower(value)
					}
					if op == "$contains" && strings.Contains(value, needle) || op == "$startsWith" && strings.HasPrefix(value, needle) {
						found = true
					}
				}
				if !found {
					return false
				}
			case "$eq":
				if !eq(v) {
					return false
				}
			case "$ne":
				if eq(v) {
					return false
				}
			case "$gt", "$gte", "$lt", "$lte":
				values, ok := actual.([]any)
				if !ok {
					values = []any{actual}
				}
				found := false
				for _, item := range values {
					_, an := num(item)
					_, bn := num(v)
					_, as := item.(string)
					_, bs := v.(string)
					if !(an && bn || as && bs) {
						continue
					}
					c := compare(item, v)
					if op == "$gt" && c > 0 || op == "$gte" && c >= 0 || op == "$lt" && c < 0 || op == "$lte" && c <= 0 {
						found = true
					}
				}
				if !found {
					return false
				}
			case "$in", "$nin":
				found := false
				for _, x := range array(v) {
					found = found || eq(x)
				}
				if op == "$in" && !found || op == "$nin" && found {
					return false
				}
			case "$exists":
				_, missing := actual.(missing)
				if (!missing) != v.(bool) {
					return false
				}
			case "$not":
				if matches(row, Document{key: v}) {
					return false
				}
			case "$size":
				a, ok := actual.([]any)
				n, _ := integer(v)
				if !ok || len(a) != n {
					return false
				}
			case "$all":
				a, ok := actual.([]any)
				if !ok {
					return false
				}
				for _, expected := range array(v) {
					found := false
					for _, x := range a {
						found = found || equal(x, expected)
					}
					if !found {
						return false
					}
				}
			case "$elemMatch":
				a, ok := actual.([]any)
				if !ok {
					return false
				}
				found := false
				for _, x := range a {
					if elementMatches(x, v) {
						found = true
					}
				}
				if !found {
					return false
				}
			}
		}
	}
	return true
}
func searchConfig(value any) (string, bool) {
	if v, ok := value.(string); ok {
		require(len([]rune(v)) <= 1024, "Invalid literal search")
		return v, false
	}
	m := obj(value)
	for k := range m {
		require(k == "value" || k == "caseInsensitive", "Invalid literal search")
	}
	v := str(m["value"])
	require(len([]rune(v)) <= 1024, "Invalid literal search")
	insensitive := false
	if x, ok := m["caseInsensitive"]; ok {
		var valid bool
		insensitive, valid = x.(bool)
		require(valid, "Invalid literal search")
	}
	return v, insensitive
}
func truth(value any) bool {
	if value == nil {
		return false
	}
	if v, ok := value.(bool); ok {
		return v
	}
	if n, ok := num(value); ok {
		return n != 0
	}
	return true
}

var isoDate = regexp.MustCompile(`^[0-9]{4}-[0-9]{2}-[0-9]{2}(?:T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,3})?(?:Z|[+-][0-9]{2}:[0-9]{2}))?$`)

func dateString(arg Document) any {
	if arg["date"] == nil {
		return arg["onNull"]
	}
	raw := str(arg["date"])
	require(isoDate.MatchString(raw) && !strings.HasPrefix(raw, "0000"), "Invalid ISO date")
	layout := time.RFC3339Nano
	if len(raw) == 10 {
		layout = "2006-01-02"
	}
	date, err := time.Parse(layout, raw)
	require(err == nil, "Invalid ISO date")
	zone := "UTC"
	if v, ok := arg["timezone"]; ok {
		zone = str(v)
	}
	location, err := time.LoadLocation(zone)
	require(err == nil && zone != "Local" && zone != "", "Invalid timezone")
	date = date.In(location)
	format := "%Y-%m-%dT%H:%M:%S.%L"
	if zone == "UTC" || zone == "Etc/UTC" {
		format += "Z"
	}
	if v, ok := arg["format"]; ok {
		format = str(v)
	}
	require(len(format) <= 256, "Unsupported date format")
	var out strings.Builder
	values := map[byte]string{'Y': fmt.Sprintf("%04d", date.Year()), 'm': fmt.Sprintf("%02d", date.Month()), 'd': fmt.Sprintf("%02d", date.Day()), 'H': fmt.Sprintf("%02d", date.Hour()), 'M': fmt.Sprintf("%02d", date.Minute()), 'S': fmt.Sprintf("%02d", date.Second()), 'L': fmt.Sprintf("%03d", date.Nanosecond()/1000000), '%': "%"}
	for i := 0; i < len(format); i++ {
		if format[i] != '%' {
			out.WriteByte(format[i])
			continue
		}
		i++
		require(i < len(format), "Unsupported date format")
		v, ok := values[format[i]]
		require(ok, "Unsupported date format")
		out.WriteString(v)
	}
	return out.String()
}
func validateExpression(v any) {
	if s, ok := v.(string); ok && strings.HasPrefix(s, "$") {
		field(s[1:])
		return
	}
	if a, ok := v.([]any); ok {
		for _, x := range a {
			validateExpression(x)
		}
		return
	}
	if isObj(v) {
		m := obj(v)
		if hasOperator(v) {
			require(len(m) == 1, "Unsupported expression")
			for op, arg := range m {
				require(contains([]string{"$literal", "$ifNull", "$add", "$subtract", "$multiply", "$divide", "$concat", "$toLower", "$toUpper", "$cond", "$eq", "$ne", "$gt", "$gte", "$lt", "$lte", "$and", "$or", "$not", "$in", "$size", "$arrayElemAt", "$concatArrays", "$dateToString"}, op), "Unsupported expression")
				if op == "$literal" {
					return
				}
				if contains([]string{"$eq", "$ne", "$gt", "$gte", "$lt", "$lte", "$in", "$arrayElemAt"}, op) {
					require(len(array(arg)) == 2, op+" needs two arguments")
				}
				if op == "$cond" {
					require(len(array(arg)) == 3, "$cond needs [condition, then, else]")
				}
				if op == "$not" {
					require(len(array(arg)) == 1, "$not needs one argument")
				}
				if contains([]string{"$and", "$or", "$concatArrays"}, op) {
					array(arg)
				}
				if op == "$dateToString" {
					m := obj(arg)
					_, ok := m["date"]
					require(ok, "Invalid date expression")
					for k := range m {
						require(contains([]string{"date", "format", "timezone", "onNull"}, k), "Invalid date expression")
					}
				}
				if contains([]string{"$ifNull", "$subtract", "$divide"}, op) {
					require(len(array(arg)) == 2, op+" needs two arguments")
				}
				if contains([]string{"$add", "$multiply", "$concat"}, op) {
					require(len(array(arg)) > 0, op+" needs an argument array")
				}
				validateExpression(arg)
			}
		} else {
			for k, x := range m {
				field(k)
				validateExpression(x)
			}
		}
	}
}
func expression(row Document, value any) any {
	if s, ok := value.(string); ok && strings.HasPrefix(s, "$") {
		v := get(row, s[1:])
		if _, missing := v.(missing); missing {
			return nil
		}
		return v
	}
	if a, ok := value.([]any); ok {
		out := make([]any, len(a))
		for i, v := range a {
			out[i] = expression(row, v)
		}
		return out
	}
	if !isObj(value) {
		return value
	}
	if !hasOperator(value) {
		out := Document{}
		for k, v := range obj(value) {
			out[k] = expression(row, v)
		}
		return out
	}
	for op, raw := range obj(value) {
		if op == "$literal" {
			return plain(clone(raw))
		}
		if op == "$cond" {
			a := array(raw)
			if truth(expression(row, a[0])) {
				return expression(row, a[1])
			}
			return expression(row, a[2])
		}
		if op == "$ifNull" {
			a := array(raw)
			first := expression(row, a[0])
			if first == nil {
				return expression(row, a[1])
			}
			return first
		}
		if op == "$and" || op == "$or" {
			for _, v := range array(raw) {
				t := truth(expression(row, v))
				if op == "$and" && !t {
					return false
				}
				if op == "$or" && t {
					return true
				}
			}
			return op == "$and"
		}
		arg := expression(row, raw)
		switch op {
		case "$eq", "$ne":
			a := array(arg)
			return equal(a[0], a[1]) == (op == "$eq")
		case "$gt", "$gte", "$lt", "$lte":
			a := array(arg)
			c := compare(a[0], a[1])
			return op == "$gt" && c > 0 || op == "$gte" && c >= 0 || op == "$lt" && c < 0 || op == "$lte" && c <= 0
		case "$not":
			return !truth(array(arg)[0])
		case "$in":
			a := array(arg)
			for _, v := range array(a[1]) {
				if equal(a[0], v) {
					return true
				}
			}
			return false
		case "$size":
			return len(array(arg))
		case "$arrayElemAt":
			a := array(arg)
			items := array(a[0])
			index := numeric(a[1])
			require(index == math.Trunc(index), "Invalid array index")
			if index < 0 {
				index += float64(len(items))
			}
			if index < 0 || index >= float64(len(items)) {
				return nil
			}
			return items[int(index)]
		case "$concatArrays":
			out := []any{}
			for _, v := range array(arg) {
				out = append(out, array(v)...)
			}
			return out
		case "$dateToString":
			return dateString(obj(arg))
		case "$toLower":
			return strings.ToLower(str(arg))
		case "$toUpper":
			return strings.ToUpper(str(arg))
		case "$concat":
			s := ""
			for _, v := range array(arg) {
				s += str(v)
			}
			return s
		}
		values := array(arg)
		n := make([]float64, len(values))
		for i, v := range values {
			n[i] = numeric(v)
		}
		result := 0.0
		switch op {
		case "$add":
			for _, v := range n {
				result += v
			}
		case "$multiply":
			result = 1
			for _, v := range n {
				result *= v
			}
		case "$subtract":
			result = n[0] - n[1]
		case "$divide":
			require(n[1] != 0, "Division by zero")
			result = n[0] / n[1]
		}
		require(!math.IsNaN(result) && !math.IsInf(result, 0), "Expression overflow")
		return result
	}
	return nil
}
func validatePipeline(pipeline any) {
	validateValue(pipeline, 0)
	stages := array(pipeline)
	require(len(stages) <= 32, "Pipeline must contain at most 32 stages")
	for _, stage := range stages {
		m := obj(stage)
		require(len(m) == 1, "Each stage needs exactly one operator")
		for op, value := range m {
			switch op {
			case "$match":
				validateFilter(value)
			case "$skip", "$limit":
				n, ok := integer(value)
				require(ok && (op != "$limit" || n > 0), op+" is out of range")
			case "$sort":
				m := obj(value)
				require(len(m) > 0, "$sort needs fields")
				for k, v := range m {
					field(k)
					n, ok := num(v)
					require(ok && (n == 1 || n == -1), "Sort direction must be 1 or -1")
				}
			case "$project", "$set", "$addFields":
				m := obj(value)
				require(len(m) > 0, op+" needs fields")
				included, excluded := false, false
				for k, v := range m {
					field(k)
					validateExpression(v)
					if k != "id" && k != "_id" {
						included = included || !flag(v, 0)
						excluded = excluded || flag(v, 0)
					}
				}
				if op == "$project" {
					require(!(included && excluded), "Cannot mix inclusion and exclusion projection")
				}
			case "$unset":
				if s, ok := value.(string); ok {
					field(s)
				} else {
					a := array(value)
					require(len(a) > 0, "$unset needs fields")
					for _, k := range a {
						field(k)
					}
				}
			case "$count":
				field(value)
			case "$group":
				m := obj(value)
				id, ok := m["_id"]
				require(ok, "$group needs _id")
				validateExpression(id)
				for k, v := range m {
					if k == "_id" {
						continue
					}
					field(k)
					acc := obj(v)
					require(len(acc) == 1, "Unsupported accumulator")
					for op, expr := range acc {
						require(contains([]string{"$sum", "$avg", "$min", "$max", "$first", "$last", "$push", "$addToSet"}, op), "Unsupported accumulator")
						validateExpression(expr)
					}
				}
			case "$unwind":
				m := Document{}
				if s, ok := value.(string); ok {
					m["path"] = s
				} else {
					m = obj(value)
				}
				for k := range m {
					require(contains([]string{"path", "preserveNullAndEmptyArrays", "includeArrayIndex"}, k), "Invalid $unwind options")
				}
				path := str(m["path"])
				require(strings.HasPrefix(path, "$"), "$unwind path must start with $")
				field(path[1:])
				if v, exists := m["preserveNullAndEmptyArrays"]; exists {
					_, ok := v.(bool)
					require(ok, "Invalid preserveNullAndEmptyArrays")
				}
				if v, exists := m["includeArrayIndex"]; exists {
					field(v)
				}
			default:
				panic(&Error{"Unsupported pipeline stage: " + op, "UNSUPPORTED_QUERY", 400})
			}
		}
	}
}
func Validate(pipeline any) (err error) { defer guarded(&err); validatePipeline(pipeline); return nil }
func Aggregate(input []Document, pipeline any, maxRows int) ([]Document, error) {
	return AggregateWithLimits(input, pipeline, maxRows, 8388608)
}
func byteBudget(rows []Document, stage any, maxBytes int) {
	op := keys(stage)[0]
	value := obj(stage)[op]
	expanding := op == "$set" || op == "$addFields" || op == "$project" || op == "$group"
	copies := 1
	if expanding {
		copies = max(1, len(obj(value)))
	}
	estimate := int64(0)
	for _, row := range rows {
		multiplier := copies
		if op == "$unwind" {
			path, ok := value.(string)
			if !ok {
				path = str(obj(value)["path"])
			}
			items, ok := get(row, path[1:]).([]any)
			if ok {
				multiplier = max(1, len(items))
			} else {
				multiplier = 1
			}
		}
		raw, e := json.Marshal(row)
		require(e == nil, "Invalid query value")
		estimate += int64(len(raw)) * int64(multiplier)
		if estimate > int64(maxBytes) {
			panic(&Error{"Query expansion byte budget exceeded", "QUERY_LIMIT", 413})
		}
	}
	if expanding {
		raw, e := json.Marshal(clone(value))
		require(e == nil, "Invalid query value")
		estimate += int64(len(raw)) * int64(len(rows))
	}
	if estimate > int64(maxBytes) {
		panic(&Error{"Query expansion byte budget exceeded", "QUERY_LIMIT", 413})
	}
}
func AggregateWithLimits(input []Document, pipeline any, maxRows, maxBytes int) (rows []Document, err error) {
	defer guarded(&err)
	validatePipeline(pipeline)
	if maxRows <= 0 {
		maxRows = MaxRows
	}
	rows = make([]Document, len(input))
	for i, r := range input {
		rows[i] = obj(clone(r))
	}
	limit(len(rows) <= maxRows)
	for _, stage := range array(pipeline) {
		byteBudget(rows, stage, maxBytes)
		for op, value := range obj(stage) {
			switch op {
			case "$match":
				out := []Document{}
				for _, r := range rows {
					if matches(r, value) {
						out = append(out, r)
					}
				}
				rows = out
			case "$skip":
				n, _ := integer(value)
				if n > len(rows) {
					n = len(rows)
				}
				rows = rows[n:]
			case "$limit":
				n, _ := integer(value)
				if n < len(rows) {
					rows = rows[:n]
				}
			case "$sort":
				sort.SliceStable(rows, func(i, j int) bool {
					for _, k := range keys(value) {
						d, _ := num(obj(value)[k])
						c := compare(get(rows[i], k), get(rows[j], k))
						if c != 0 {
							return float64(c)*d < 0
						}
					}
					return false
				})
			case "$count":
				if len(rows) > 0 {
					rows = []Document{{str(value): len(rows)}}
				}
			case "$set", "$addFields":
				for _, r := range rows {
					computed := Document{}
					for k, v := range obj(value) {
						computed[k] = expression(r, v)
					}
					for k, v := range computed {
						put(r, k, v)
					}
				}
			case "$unset":
				fields := []any{}
				if s, ok := value.(string); ok {
					fields = append(fields, s)
				} else {
					fields = array(value)
				}
				for _, r := range rows {
					for _, k := range fields {
						removeProjection(r, strings.Split(str(k), "."))
					}
				}
			case "$project":
				fields := obj(value)
				include := false
				for _, v := range fields {
					include = include || !flag(v, 0)
				}
				out := []Document{}
				for _, r := range rows {
					d := Document{}
					if !include {
						d = obj(clone(r))
					} else {
						for _, k := range []string{"id", "_id"} {
							if v, ok := r[k]; ok && !flag(fields[k], 0) {
								d[k] = v
							}
						}
					}
					for k, v := range fields {
						if flag(v, 0) {
							removeProjection(d, strings.Split(k, "."))
						} else if flag(v, 1) {
							mergeProjection(d, selection(r, strings.Split(k, ".")))
						} else {
							put(d, k, expression(r, v))
						}
					}
					out = append(out, d)
				}
				rows = out
			case "$unwind":
				config := Document{}
				if s, ok := value.(string); ok {
					config["path"] = s
				} else {
					config = obj(value)
				}
				path := str(config["path"])[1:]
				out := []Document{}
				for _, r := range rows {
					values := get(r, path)
					_, missing := values.(missing)
					list, isArray := values.([]any)
					if values == nil || missing || isArray && len(list) == 0 {
						if config["preserveNullAndEmptyArrays"] == true {
							d := obj(clone(r))
							if isArray {
								put(d, path, absent)
							}
							if index, ok := config["includeArrayIndex"]; ok {
								put(d, str(index), nil)
							}
							out = append(out, d)
						}
					} else {
						if !isArray {
							list = []any{values}
						}
						for i, v := range list {
							d := obj(clone(r))
							put(d, path, v)
							if index, ok := config["includeArrayIndex"]; ok {
								var position any
								if isArray {
									position = i
								}
								put(d, str(index), position)
							}
							out = append(out, d)
							limit(len(out) <= maxRows)
						}
					}
				}
				rows = out
			case "$group":
				fields := obj(value)
				type group struct {
					key     any
					members []Document
				}
				groups := map[string]*group{}
				order := []string{}
				for _, r := range rows {
					key := expression(r, fields["_id"])
					s := canonical(key)
					if _, ok := groups[s]; !ok {
						groups[s] = &group{key: key}
						order = append(order, s)
					}
					groups[s].members = append(groups[s].members, r)
				}
				out := []Document{}
				for _, s := range order {
					g := groups[s]
					d := Document{"_id": g.key}
					for k, spec := range fields {
						if k == "_id" {
							continue
						}
						for acc, expr := range obj(spec) {
							values := []any{}
							nums := []float64{}
							for _, r := range g.members {
								v := expression(r, expr)
								values = append(values, v)
								if n, ok := num(v); ok {
									nums = append(nums, n)
								}
							}
							switch acc {
							case "$sum", "$avg":
								sum := 0.0
								for _, n := range nums {
									sum += n
								}
								if acc == "$avg" {
									if len(nums) == 0 {
										d[k] = nil
									} else {
										d[k] = sum / float64(len(nums))
									}
								} else {
									d[k] = sum
								}
							case "$min", "$max":
								chosen := values[0]
								for _, v := range values[1:] {
									c := compare(chosen, v)
									if acc == "$min" && c > 0 || acc == "$max" && c < 0 {
										chosen = v
									}
								}
								d[k] = chosen
							case "$first":
								d[k] = values[0]
							case "$last":
								d[k] = values[len(values)-1]
							case "$push":
								d[k] = values
							case "$addToSet":
								unique := []any{}
								for _, v := range values {
									found := false
									for _, old := range unique {
										found = found || equal(v, old)
									}
									if !found {
										unique = append(unique, v)
									}
								}
								d[k] = unique
							}
						}
					}
					out = append(out, d)
				}
				rows = out
			}
		}
		limit(len(rows) <= maxRows)
	}
	validateValueRows := make([]any, len(rows))
	for i, r := range rows {
		validateValueRows[i] = r
	}
	validateValue(validateValueRows, 0)
	return rows, nil
}
func Find(rows []Document, filter, options any) (result []Document, err error) {
	defer guarded(&err)
	if filter == nil {
		filter = Document{}
	}
	if options == nil {
		options = Document{}
	}
	opts := obj(options)
	for k := range opts {
		require(contains([]string{"sort", "skip", "limit", "projection"}, k), "Unsupported query options")
	}
	pipeline := []any{Document{"$match": filter}}
	for _, k := range []string{"sort", "skip", "limit", "projection"} {
		if v, ok := opts[k]; ok {
			op := "$" + k
			if k == "projection" {
				op = "$project"
			}
			pipeline = append(pipeline, Document{op: v})
		}
	}
	return Aggregate(rows, pipeline, MaxRows)
}
func DecodeDocument(raw []byte) (Document, error) {
	var d Document
	err := json.Unmarshal(raw, &d)
	if err != nil {
		return nil, err
	}
	if d == nil {
		return nil, fmt.Errorf("Expected an object")
	}
	return d, nil
}

// Updated applies portable update operators to a copy. Database atomicity is not implied.
func Updated(current Document, update any) (result Document, err error) {
	defer guarded(&err)
	validateValue(update, 0)
	changesByOp := obj(update)
	require(len(changesByOp) > 0, "Update requires operators")
	result = obj(clone(current))
	paths := []string{}
	for _, op := range keys(update) {
		require(op == "$set" || op == "$unset" || op == "$inc" || op == "$push" || op == "$addToSet" || op == "$pull", "Unsupported update operator: "+op)
		changes := obj(changesByOp[op])
		for _, path := range keys(changesByOp[op]) {
			value := changes[path]
			field(path)
			identity := strings.Split(path, ".")[0]
			require(identity != "id" && identity != "_id", "Record identity cannot be updated")
			for _, p := range paths {
				require(p != path && !strings.HasPrefix(p, path+".") && !strings.HasPrefix(path, p+"."), "Conflicting update paths")
			}
			paths = append(paths, path)
			previous := get(result, path)
			_, missing := previous.(missing)
			switch op {
			case "$set":
				put(result, path, value)
			case "$unset":
				put(result, path, absent)
			case "$inc":
				v, ok := num(value)
				require(ok, "$inc requires numbers")
				p := float64(0)
				if !missing {
					p, ok = num(previous)
					require(ok, "$inc requires numbers")
				}
				next := p + v
				require(!math.IsInf(next, 0) && !math.IsNaN(next), "Update overflow")
				put(result, path, next)
			default:
				items := []any{}
				if !missing {
					items = append(items, array(previous)...)
				}
				if op == "$push" || op == "$addToSet" {
					additions := []any{value}
					if hasOperator(value) {
						m := obj(value)
						require(len(m) == 1 && m["$each"] != nil, "Only the $each array modifier is supported")
						additions = array(m["$each"])
					}
					for _, addition := range additions {
						found := false
						for _, v := range items {
							if equal(v, addition) {
								found = true
								break
							}
						}
						if op == "$push" || !found {
							items = append(items, clone(addition))
						}
					}
				} else {
					if isObj(value) {
						if scalarCondition(value) {
							validateFilter(Document{"value": value})
						} else {
							validateFilter(value)
						}
					}
					remaining := []any{}
					for _, v := range items {
						if !elementMatches(v, value) {
							remaining = append(remaining, v)
						}
					}
					items = remaining
				}
				put(result, path, items)
			}
		}
	}
	return result, nil
}
func Distinct(rows []Document, path string) (result []any, err error) {
	defer guarded(&err)
	field(path)
	result = []any{}
	for _, row := range rows {
		value := get(row, path)
		if _, missing := value.(missing); missing {
			continue
		}
		items, ok := value.([]any)
		if !ok {
			items = []any{value}
		}
		for _, item := range items {
			found := false
			for _, v := range result {
				if equal(v, item) {
					found = true
					break
				}
			}
			if !found {
				result = append(result, clone(item))
			}
		}
	}
	return result, nil
}

func ValidateDocument(value Document) (err error) {
	defer guarded(&err)
	validateValue(value, 0)
	return nil
}

// ValueKey shares portable equality with relationship hash joins.
func ValueKey(value any) string { return canonical(value) }

package security

import (
 "huzapilot/component/internal/shared/db"
 "huzapilot/component/internal/shared/config"
 "crypto/rand"
 "crypto/sha256"
 "crypto/subtle"
 "encoding/hex"
 "fmt"
 "regexp"
 "sort"
 "strings"
 "sync"
 "time"
)

type Doc = db.Doc
type ApiKeys struct {Store *db.Store; Scopes []string; mu sync.Mutex}
var keyPattern=regexp.MustCompile(`^hp_([0-9a-f-]{36})_([0-9a-f]{64})$`)
func contains(all []string,s string)bool{for _,v:=range all{if v==s{return true}};return false}
func publicKey(row Doc)Doc{out:=Doc{};for k,v:=range row{if k!="hash"{out[k]=v}};return out}
func(a *ApiKeys) fields(body Doc)(Doc,error){
 name,ok:=body["name"].(string);if !ok||len(strings.TrimSpace(name))<1||len(strings.TrimSpace(name))>120{return nil,fmt.Errorf("A name of 1–120 characters is required")}
 raw,ok:=body["scopes"].([]interface{});if !ok{return nil,fmt.Errorf("Scopes must be an array of catalog entries")}
 scopes:=[]string{};for _,v:=range raw{s,ok:=v.(string);if !ok||!contains(a.Scopes,s){return nil,fmt.Errorf("Scopes must be an array of catalog entries")};if !contains(scopes,s){scopes=append(scopes,s)}};sort.Strings(scopes)
 expiry:=body["expiresAt"];if expiry!=nil{n,ok:=expiry.(float64);if !ok||n!=float64(int64(n))||n<=float64(time.Now().Unix()){return nil,fmt.Errorf("expiresAt must be a future Unix timestamp or null")}}
 return Doc{"name":strings.TrimSpace(name),"scopes":scopes,"expiresAt":expiry},nil
}
func(a *ApiKeys) Manage(method,id string,body Doc)(int,interface{}){
 a.mu.Lock();defer a.mu.Unlock()
 bad:=func(status int,message string)(int,interface{}){return status,Doc{"error":message}}
 if id==""&&method=="GET"{rows,err:=a.Store.List("api_keys","");if err!=nil{return bad(503,"Key registry unavailable")};out:=[]Doc{};for _,r:=range rows{out=append(out,publicKey(r))};return 200,out}
 if id==""&&method=="POST"{
  fields,err:=a.fields(body);if err!=nil{return bad(400,err.Error())};b:=make([]byte,32);if _,err=rand.Read(b);err!=nil{return bad(503,"Key generation unavailable")};secret:=hex.EncodeToString(b);digest:=sha256.Sum256([]byte(secret));fields["hash"]=hex.EncodeToString(digest[:]);fields["createdAt"]=time.Now().Unix();fields["revoked"]=false
  row,err:=a.Store.Save("api_keys",fields,"");if err!=nil{return bad(503,"Key registry unavailable")};out:=publicKey(row);out["key"]="hp_"+row["id"].(string)+"_"+secret;return 201,out
 }
 if id!=""&&(method=="PUT"||method=="DELETE"){
  rows,err:=a.Store.List("api_keys",id);if err!=nil{return bad(503,"Key registry unavailable")};if len(rows)==0{return bad(404,"Key not found")};row:=rows[0]
  if method=="PUT"{if row["revoked"]==true{return bad(409,"Revoked keys cannot be changed")};fields,err:=a.fields(body);if err!=nil{return bad(400,err.Error())};for k,v:=range fields{row[k]=v}}else{row["revoked"]=true}
  saved,err:=a.Store.Save("api_keys",row,id);if err!=nil{return bad(503,"Key registry unavailable")};return 200,publicKey(saved)
 };return bad(405,"Method not allowed")
}
func(a *ApiKeys) Authorize(key,scope string)int{
 match:=keyPattern.FindStringSubmatch(key);if match==nil{return 401};rows,err:=a.Store.List("api_keys",match[1]);if err!=nil{return 503};if len(rows)==0{return 401};r:=rows[0]
 digest:=sha256.Sum256([]byte(match[2]));hash,_:=r["hash"].(string)
 if subtle.ConstantTimeCompare([]byte(hash),[]byte(hex.EncodeToString(digest[:])))!=1||r["revoked"]==true{return 401};if expiry,ok:=r["expiresAt"].(float64);ok&&expiry<=float64(time.Now().Unix()){return 401}
 if scope!=""&&contains(a.Scopes,scope){for _,v:=range r["scopes"].([]interface{}){if v==scope{return 200}}};return 403
}
func RequiredScope(cfg config.Config,path,method string)string{
 parts:=strings.Split(strings.Trim(path,"/"),"/");service:=cfg.Name
 if cfg.Role=="gateway"{if len(parts)<3||parts[0]!="api"||!contains(cfg.Services,parts[1]){return ""};service=parts[1];parts=parts[2:]}
 query:=method=="POST"&&len(parts)==2&&(parts[1]=="_query"||parts[1]=="_aggregate")
 action:=map[string]string{"GET":"read","POST":"create","PUT":"update","DELETE":"delete"}[method];if query{action="read"}
 if action==""||(len(parts)!=1&&len(parts)!=2)||(method=="POST"&&len(parts)!=1&&!query)||((method=="PUT"||method=="DELETE")&&len(parts)!=2){return ""}
 if cfg.Role=="service"&&!contains(cfg.Modules,parts[0]){return ""};return service+":"+parts[0]+":"+action
}

package main
import("context";"encoding/json";"fmt";"os";"strconv";"time";"huzapilot/component/internal/shared/messaging")
func run()error{
 if len(os.Args)<4{return fmt.Errorf("usage: messaging prepare|publish|consume kafka|rabbitmq topic [group] [count]")}
 command,broker,topic:=os.Args[1],os.Args[2],os.Args[3];group:="";if len(os.Args)>4{group=os.Args[4]}
 bus,err:=messaging.New(broker);if err!=nil{return err};defer bus.Close();ctx,cancel:=context.WithTimeout(context.Background(),45*time.Second);defer cancel()
 switch command{
 case "prepare":return bus.Prepare(ctx,topic,group)
 case "publish":var message messaging.Event;if err=json.NewDecoder(os.Stdin).Decode(&message);err!=nil{return err};return bus.Publish(ctx,topic,message)
 case "consume","consume-fail":count:=1;if len(os.Args)>5{count,err=strconv.Atoi(os.Args[5]);if err!=nil{return err}};_,err=bus.Consume(ctx,topic,group,func(_ context.Context,event messaging.Event)error{if command=="consume-fail"{return fmt.Errorf("Intentional handler failure")};return json.NewEncoder(os.Stdout).Encode(event)},messaging.Options{Limit:count,Retries:0});return err
 default:return fmt.Errorf("unknown messaging command")
 }
}
func main(){if err:=run();err!=nil{fmt.Fprintln(os.Stderr,err);os.Exit(1)}}

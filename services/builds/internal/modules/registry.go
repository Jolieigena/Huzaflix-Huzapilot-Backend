package modules
import (
"github.com/gin-gonic/gin"
"huzapilot/component/internal/shared/db"
    module0 "huzapilot/component/internal/modules/m_build"
    module1 "huzapilot/component/internal/modules/m_deployment"
)
func Register(router *gin.Engine, store *db.Store) error {
    if err := module0.Register(router, store); err != nil { return err }
    if err := module1.Register(router, store); err != nil { return err }
return nil
}

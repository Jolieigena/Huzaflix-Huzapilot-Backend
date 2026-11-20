package m_build
import (
    "github.com/gin-gonic/gin"
    "huzapilot/component/internal/shared/db"
    sharedhttp "huzapilot/component/internal/shared/http"
)
func Register(router *gin.Engine, store *db.Store) error {
    service, err := New(store)
    if err != nil { return err }
    sharedhttp.RegisterModel(router, service.Model)
    return nil
}

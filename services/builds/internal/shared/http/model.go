package http

import (
	"encoding/json"
	"errors"
	"github.com/gin-gonic/gin"
	"huzapilot/component/internal/shared/db"
	"huzapilot/component/internal/shared/db/query"
	"io"
	"log"
	stdhttp "net/http"
)

func respond(c *gin.Context, status int, data any, err error) {
	if err != nil {
		var failure *query.Error
		if errors.As(err, &failure) {
			c.JSON(failure.Status, gin.H{"error": failure.Message, "code": failure.Code})
		} else {
			log.Print(err)
			c.JSON(500, gin.H{"error": "Request failed"})
		}
		return
	}
	c.JSON(status, data)
}
func cached(c *gin.Context, model *db.Model, body any, load func() (any, error)) {
	value, status, err := model.Store.Cache.HTTP(model.Name, c.Request, body, load)
	c.Header("X-Cache", status)
	c.Header("Cache-Control", "private, no-store")
	respond(c, 200, value, err)
}
func RegisterModel(router *gin.Engine, model *db.Model) {
	group := router.Group("/" + model.Name)
	group.GET("/", func(c *gin.Context) { cached(c, model, nil, func() (any, error) { return model.Find(nil, nil) }) })
	group.POST("/", func(c *gin.Context) {
		var body db.Doc
		if c.ShouldBindJSON(&body) != nil || body == nil {
			c.AbortWithStatus(400)
			return
		}
		row, err := model.Create(body)
		respond(c, 201, row, err)
	})
	for _, endpoint := range []string{"_query", "_aggregate"} {
		endpoint := endpoint
		group.POST("/"+endpoint, func(c *gin.Context) {
			raw, err := io.ReadAll(stdhttp.MaxBytesReader(c.Writer, c.Request.Body, 1048576))
			if err != nil {
				c.AbortWithStatus(413)
				return
			}
			var body map[string]json.RawMessage
			if json.Unmarshal(raw, &body) != nil || body == nil {
				c.AbortWithStatus(400)
				return
			}
			if endpoint == "_aggregate" {
				value, ok := body["pipeline"]
				if !ok || len(body) != 1 {
					c.AbortWithStatus(400)
					return
				}
				pipeline, err := query.Parse(value)
				if err != nil {
					respond(c, 400, nil, err)
					return
				}
				cached(c, model, body, func() (any, error) { return model.Aggregate(pipeline) })
			} else {
				for k := range body {
					if k != "filter" && k != "options" {
						c.AbortWithStatus(400)
						return
					}
				}
				var filter, options any
				for k, value := range body {
					var object map[string]json.RawMessage
					if json.Unmarshal(value, &object) != nil || object == nil {
						c.AbortWithStatus(400)
						return
					}
					parsed, err := query.Parse(value)
					if err != nil {
						respond(c, 400, nil, err)
						return
					}
					if k == "filter" {
						filter = parsed
					} else {
						options = parsed
					}
				}
				cached(c, model, body, func() (any, error) { return model.Find(filter, options) })
			}
		})
	}
	group.GET("/:id", func(c *gin.Context) {
		cached(c, model, nil, func() (any, error) {
			row, err := model.FindByID(c.Param("id"))
			if err == nil && row == nil {
				return nil, &query.Error{Message: "Not found", Code: "NOT_FOUND", Status: 404}
			}
			return row, err
		})
	})
	group.PUT("/:id", func(c *gin.Context) {
		var body db.Doc
		if c.ShouldBindJSON(&body) != nil || body == nil {
			c.AbortWithStatus(400)
			return
		}
		row, err := model.UpdateByID(c.Param("id"), body)
		respond(c, 200, row, err)
	})
	group.DELETE("/:id", func(c *gin.Context) { result, err := model.DeleteByID(c.Param("id")); respond(c, 200, result, err) })
}

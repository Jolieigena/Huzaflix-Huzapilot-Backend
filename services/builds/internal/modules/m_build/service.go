package m_build
import "huzapilot/component/internal/shared/db"

// Service owns this module's business rules. Configure Model.Hooks in New.
type Service struct { *db.Model }
func New(store *db.Store) (*Service, error) {
    model, err := db.NewModel(store, Name, Schema)
    if err != nil { return nil, err }
    return &Service{Model:model}, nil
}

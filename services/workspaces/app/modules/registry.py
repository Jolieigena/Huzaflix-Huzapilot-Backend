from .m_workspace.router import register as register_m_workspace
from .m_member.router import register as register_m_member

def register(app, store):
    return {
        'workspace': register_m_workspace(app, store),
        'member': register_m_member(app, store),
    }

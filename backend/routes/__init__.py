"""Route modules for the LockScore AI backend.

Each file in this package owns one cohesive set of HTTP endpoints and
defines its own `router` (an `APIRouter` instance with `prefix="/api"`).
`server.py` imports each router and registers them on the app via
`app.include_router(...)`.

This decomposition lets us trim the historical `server.py` monolith
without breaking imports — shared deps (Mongo, logger, current_user)
live in `deps.py`.
"""

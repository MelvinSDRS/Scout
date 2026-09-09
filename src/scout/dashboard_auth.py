"""One-use dashboard links and revocable browser sessions; API bearer tokens still work."""

import hashlib
import hmac
import secrets
import time

from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

SESSION_SECONDS = 30 * 24 * 3600
TICKET_SECONDS = 300
bearer = HTTPBearer(auto_error=False)


class SignIn(BaseModel):
    ticket: str = Field(default="", max_length=256)
    token: str = Field(default="", max_length=1024)


class DashboardAuth:
    def __init__(self, store, settings):
        self.store = store
        self.secret = settings.api_token
        # Distinct installations on localhost should not overwrite each other's cookies.
        self.cookie_name = "scout_session_" + hashlib.sha256(self.secret.encode()).hexdigest()[:12]
        with store.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS dashboard_auth(
                digest TEXT PRIMARY KEY, kind TEXT NOT NULL, expires REAL NOT NULL)""")

    def digest(self, value):
        return hmac.new(self.secret.encode(), value.encode(), hashlib.sha256).hexdigest()

    def issue_ticket(self):
        ticket = secrets.token_urlsafe(32)
        with self.store.connect() as db:
            db.execute("DELETE FROM dashboard_auth WHERE expires <= ?", (time.time(),))
            db.execute(
                "INSERT INTO dashboard_auth VALUES (?, 'ticket', ?)",
                (self.digest(ticket), time.time() + TICKET_SECONDS),
            )
        return ticket

    def create_session(self, ticket="", token=""):
        session = secrets.token_urlsafe(32)
        with self.store.connect() as db:
            if ticket:
                consumed = db.execute(
                    "DELETE FROM dashboard_auth WHERE digest=? AND kind='ticket' AND expires>?",
                    (self.digest(ticket), time.time()),
                ).rowcount
                if not consumed:
                    raise HTTPException(
                        401, "This sign-in link expired or was already used. Open Scout again."
                    )
            elif not token or not secrets.compare_digest(token.encode(), self.secret.encode()):
                raise HTTPException(401, "Access token not recognized")
            db.execute("DELETE FROM dashboard_auth WHERE expires <= ?", (time.time(),))
            db.execute(
                "INSERT INTO dashboard_auth VALUES (?, 'session', ?)",
                (self.digest(session), time.time() + SESSION_SECONDS),
            )
        return session

    def valid_session(self, session):
        if not session or len(session) > 256:
            return False
        with self.store.connect() as db:
            return (
                db.execute(
                    "SELECT 1 FROM dashboard_auth WHERE digest=? AND kind='session' AND expires>?",
                    (self.digest(session), time.time()),
                ).fetchone()
                is not None
            )

    @staticmethod
    def check_origin(request, required=False):
        origin = request.headers.get("origin")
        if (required or origin) and origin != str(request.base_url).rstrip("/"):
            raise HTTPException(403, "Cross-origin requests are disabled")
        if request.headers.get("sec-fetch-site") == "cross-site":
            raise HTTPException(403, "Cross-origin requests are disabled")

    def authorize(
        self, request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(bearer)
    ):
        self.check_origin(request)
        if credentials is not None:
            if secrets.compare_digest(credentials.credentials.encode(), self.secret.encode()):
                return
        elif self.valid_session(request.cookies.get(self.cookie_name)):
            self.check_origin(request, required=request.method not in ("GET", "HEAD", "OPTIONS"))
            return
        raise HTTPException(401, "Open Scout with its launcher to sign in")

    def install(self, app):
        @app.post("/api/session")
        def sign_in(body: SignIn, request: Request):
            self.check_origin(request, required=True)
            session = self.create_session(body.ticket, body.token)
            # Replacing a browser session also revokes its old credential.
            self.revoke(request.cookies.get(self.cookie_name))
            response = JSONResponse({"authenticated": True})
            response.set_cookie(
                self.cookie_name,
                session,
                max_age=SESSION_SECONDS,
                path="/api",
                secure=request.url.scheme == "https",
                httponly=True,
                samesite="strict",
            )
            response.headers["Cache-Control"] = "no-store"
            return response

        @app.delete("/api/session", dependencies=[Depends(self.authorize)])
        def sign_out(request: Request):
            self.revoke(request.cookies.get(self.cookie_name))
            response = JSONResponse({"authenticated": False})
            response.delete_cookie(self.cookie_name, path="/api")
            response.headers["Cache-Control"] = "no-store"
            return response

    def revoke(self, session):
        if session:
            with self.store.connect() as db:
                db.execute(
                    "DELETE FROM dashboard_auth WHERE digest=? AND kind='session'",
                    (self.digest(session),),
                )

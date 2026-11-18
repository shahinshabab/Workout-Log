"""The logger's authenticated Supabase Auth and PostgREST calls."""

import json
from time import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen


class BackendError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class Supabase:
    def __init__(self, url, key, state):
        address = urlsplit(url.strip())
        if address.scheme != "https" or not address.hostname or address.path.strip("/") or address.username or address.query or address.fragment:
            raise BackendError("Enter the HTTPS project URL from your Supabase dashboard.")
        self.url = url.strip().rstrip("/")
        self.key = key.strip()
        self.state = state

    def _request(self, path, *, method="GET", body=None, query=None, prefer=None, auth=False):
        target = f"{self.url}{path}"
        if query:
            target += "?" + urlencode(query)
        headers = {"apikey": self.key, "Accept": "application/json"}
        if auth:
            headers["Authorization"] = f"Bearer {self._token()}"
        if prefer:
            headers["Prefer"] = prefer
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = Request(target, data=json.dumps(body).encode() if body is not None else None,
                          headers=headers, method=method)
        try:
            with urlopen(request, timeout=20) as response:
                content = response.read()
        except HTTPError as error:
            try:
                payload = json.loads(error.read())
                message = payload.get("message") or payload.get("msg") or payload.get("error_description")
            except (ValueError, AttributeError):
                message = None
            raise BackendError(message or f"Supabase returned an error ({error.code}).", error.code) from None
        except (URLError, TimeoutError, OSError):
            raise BackendError("Could not reach Supabase. Check your connection and try again.") from None
        return json.loads(content) if content else None

    def _token(self):
        session = self.state.get("auth")
        if not session:
            raise BackendError("Sign in to continue.", 401)
        if session.get("expires_at", 0) < time() + 60:
            try:
                self._exchange("refresh_token", {"refresh_token": session["refresh_token"]})
            except BackendError as error:
                if error.status in (400, 401, 403):
                    self.state.pop("auth", None)
                    raise BackendError("Your sign-in has expired. Sign in again.", 401) from None
                raise
            session = self.state["auth"]
        return session["access_token"]

    def _exchange(self, grant, credentials):
        session = self._request("/auth/v1/token", method="POST", body=credentials, query={"grant_type": grant})
        if not isinstance(session, dict) or "access_token" not in session or "refresh_token" not in session:
            raise BackendError("Supabase did not return a sign-in session.")
        session.setdefault("expires_at", time() + session.get("expires_in", 3600))
        self.state["auth"] = session

    def sign_in(self, email, password):
        self._exchange("password", {"email": email, "password": password})
        try:
            if self.rpc("workout_owner_allowed") is not True:
                raise BackendError("This account is not the configured owner.", 403)
        except BackendError:
            self.state.pop("auth", None)
            raise

    def sign_out(self):
        try:
            self._request("/auth/v1/logout", method="POST", auth=True, query={"scope": "local"})
        finally:
            self.state.pop("auth", None)

    def rpc(self, name, **args):
        return self._request(f"/rest/v1/rpc/{name}", method="POST", body=args, auth=True)

    def rows(self, table, **query):
        return self._request(f"/rest/v1/{table}", query=query, auth=True)

    def all_rows(self, table, **query):
        page_size = 500
        offset = 0
        while True:
            page = self.rows(table, limit=page_size, offset=offset, **query)
            yield from page
            if len(page) < page_size:
                return
            offset += page_size

    def upsert(self, table, rows, conflict):
        return self._request(f"/rest/v1/{table}", method="POST", auth=True, body=rows,
                             query={"on_conflict": conflict}, prefer="resolution=merge-duplicates,missing=default,return=minimal")

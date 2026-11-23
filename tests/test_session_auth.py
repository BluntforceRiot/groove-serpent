from email.message import Message
from http.cookies import SimpleCookie

from groove_serpent.session_auth import LoopbackSessionAuth


def test_native_and_browser_credentials_are_distinct_and_valid() -> None:
    auth = LoopbackSessionAuth()
    browser_cookie: SimpleCookie[str] = SimpleCookie()
    browser_cookie.load(auth.set_cookie_header)
    (cookie,) = browser_cookie.values()
    assert cookie.value != auth.authorization_header.removeprefix("Bearer ")
    assert cookie.value not in auth.bootstrap_path
    assert cookie["httponly"]
    assert cookie["samesite"] == "Strict"

    owner = Message()
    owner["Cookie"] = cookie.OutputString(attrs=[])
    assert auth.authentication_method(owner) == "cookie"

    native = Message()
    native["Authorization"] = auth.authorization_header
    assert auth.authentication_method(native) == "bearer"

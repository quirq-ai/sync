import pytest

from qqsync.project import identity
from qqsync.project.add import default_name


@pytest.mark.parametrize("url, expected", [
    ("https://github.com/Acme/App.git", "github.com/acme/app"),
    ("https://github.com/acme/app", "github.com/acme/app"),
    ("https://github.com/acme/app/", "github.com/acme/app"),
    ("git@github.com:acme/app.git", "github.com/acme/app"),
    ("ssh://git@github.com/acme/app.git", "github.com/acme/app"),
    ("https://user:secret@github.com/acme/app.git", "github.com/acme/app"),
    ("https://gitlab.example/group/sub/app.git", "gitlab.example/group/sub/app"),
    ("github.com", None),
    ("https://github.com/acme", None),
    ("file:///srv/git/app.git", None),
    ("/srv/git/app.git", None),
    ("", None),
    (None, None),
])
def test_normalize(url, expected):
    assert identity.normalize(url) == expected


@pytest.mark.parametrize("url, name", [
    ("https://github.com/acme/app.git", "app"),
    ("https://github.com/acme/app/", "app"),
    ("git@github.com:acme/app.git", "app"),
    ("/srv/git/app.git", "app"),
    (r"C:\git\app.git", "app"),
])
def test_default_name(url, name):
    assert default_name(url) == name

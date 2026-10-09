import os
import sys
import importlib

import pytest

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    monkeypatch.setenv('PYTHONUTF8', '1')
    import database
    importlib.reload(database)
    import app as app_mod
    importlib.reload(app_mod)
    app_mod.app.config['TESTING'] = True
    conn = database.get_conn()
    conn.execute(
        "UPDATE employees SET password=? WHERE role='超管'",
        (database.hash_password('TestPass1'),),
    )
    conn.commit()
    with app_mod.app.test_client() as c:
        yield c


@pytest.fixture
def app_mod():
    import app as app_mod
    return app_mod


def auth_header(token):
    return {'Authorization': f'Bearer {token}'}


def login_admin(client):
    r = client.post('/api/auth/login', json={'username': 'Cookie.gu', 'password': 'TestPass1'})
    data = r.get_json()
    assert r.status_code == 200 and data.get('success'), data
    return data['token']

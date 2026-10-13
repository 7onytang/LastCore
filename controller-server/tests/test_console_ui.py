from fastapi.testclient import TestClient
from app.main import Settings, create_app

def test_console_is_public_but_node_and_task_data_remain_private(tmp_path):
    admin, registration = 'a' * 32, 'b' * 32
    with TestClient(create_app(Settings(admin, registration, str(tmp_path/'db.sqlite3')))) as client:
        page = client.get('/')
        assert page.status_code == 200
        assert '算力控制台' in page.text
        assert "frame-ancestors 'none'" in page.headers['content-security-policy']
        for path in ['/ui/styles.css', '/ui/console.js']:
            assert client.get(path).status_code == 200
        assert client.get('/ui/../main.py').status_code == 404
        for path in ['/v1/nodes', '/v1/tasks']:
            assert client.get(path).status_code == 401
            response = client.get(path, headers={'Authorization': f'Bearer {admin}'})
            assert response.status_code == 200
        assert client.get('/v1/inference/workers').status_code == 404
        assert client.get('/healthz').json() == {'status':'ok'}

from fastapi.testclient import TestClient
from main import app

client = TestClient(app)

def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_rejects_missing_fields():
    response = client.post("/api/requests", json={"request_type": "Healthcare request", "data": {}})
    assert response.status_code == 422


def test_unknown_request_type():
    response = client.post("/api/requests", json={"request_type": "Made up", "data": {}})
    assert response.status_code == 400

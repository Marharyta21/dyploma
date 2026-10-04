from fastapi.testclient import TestClient
from api.main import app

client = TestClient(app)
GROUPS = {"1-2", "3-4", "7-8", "9-10"}


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_index_page():
    r = client.get("/")
    assert r.status_code == 200
    assert "<textarea" in r.text


def test_positive_review():
    text = "A wonderful, moving film. Great acting and a brilliant script, I loved every minute."
    r = client.post("/predict", json={"text": text})
    assert r.status_code == 200
    assert r.json()["label"] == "positive"
    assert 0.5 <= r.json()["probability"] <= 1
    assert r.json()["rating_group"] in GROUPS


def test_negative_review():
    text = "Terrible movie. Boring plot, awful acting, a complete waste of time. Do not watch."
    r = client.post("/predict", json={"text": text})
    assert r.json()["label"] == "negative"
    assert 0.5 <= r.json()["probability"] <= 1
    assert r.json()["rating_group"] in GROUPS

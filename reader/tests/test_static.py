from fastapi.testclient import TestClient


def test_the_page_is_served(client: TestClient):
    response = client.get("/")
    assert response.status_code == 200
    assert "<audio" in response.text
    assert 'name="viewport"' in response.text


def test_script_and_styles_are_served(client: TestClient):
    script = client.get("/static/app.js")
    assert script.status_code == 200
    assert "mediaSession" in script.text
    assert client.get("/static/styles.css").status_code == 200


def test_the_page_has_a_retry_button_for_failed_parts(client: TestClient):
    assert 'id="retry"' in client.get("/").text

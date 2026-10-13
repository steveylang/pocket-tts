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


def test_the_page_has_a_document_picker_for_the_supported_types(client: TestClient):
    page = client.get("/").text
    assert 'id="file"' in page
    for extension in (".txt", ".md", ".html", ".pdf"):
        assert extension in page


def test_the_script_sends_a_chosen_file_to_prepare_file(client: TestClient):
    assert "/prepare-file" in client.get("/static/app.js").text


def test_the_page_has_a_panel_to_add_and_delete_voices(client: TestClient):
    page = client.get("/").text
    for element_id in ("voice-file", "voice-name", "voice-add", "voice-delete"):
        assert f'id="{element_id}"' in page
    for extension in (".wav", ".mp3", ".flac", ".ogg", ".safetensors"):
        assert extension in page


def test_the_script_talks_to_the_voice_endpoints(client: TestClient):
    script = client.get("/static/app.js").text
    assert 'method: "DELETE"' in script
    assert 'fetch("/voices"' in script

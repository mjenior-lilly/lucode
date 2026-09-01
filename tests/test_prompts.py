from lucode.prompts import _identity


def test_source_identity_redacts_credentials_and_query():
    assert (
        _identity("https://user:secret@example.com/org/repo.git?token=secret")
        == "https://example.com/org/repo"
    )

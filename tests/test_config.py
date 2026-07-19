from llmarr.config import ConfigStore, DownloadClientConfig


def test_roundtrip_persists(tmp_path):
    p = tmp_path / "config.yaml"
    s = ConfigStore(p)
    s.mutate(lambda c: setattr(c.prowlarr, "url", "http://prowlarr:9696"))
    s.mutate(lambda c: setattr(c.prowlarr, "api_key", "SECRET"))
    assert p.exists()

    reloaded = ConfigStore(p)
    assert reloaded.config.prowlarr.url == "http://prowlarr:9696"
    assert reloaded.config.prowlarr.api_key == "SECRET"


def test_redaction_masks_secrets(tmp_path):
    s = ConfigStore(tmp_path / "config.yaml")
    s.mutate(lambda c: setattr(c.metadata, "tmdb_api_key", "abc"))
    s.mutate(lambda c: setattr(c.plex, "token", "xyz"))
    red = s.redacted()
    assert red["metadata"]["tmdb_api_key"] == "***set***"
    assert red["plex"]["token"] == "***set***"


def test_redaction_leaves_empty_secret(tmp_path):
    s = ConfigStore(tmp_path / "config.yaml")
    # Unset secret stays falsy, not masked.
    assert s.redacted()["prowlarr"]["api_key"] in (None, "")


def test_nested_download_client_roundtrip(tmp_path):
    p = tmp_path / "config.yaml"
    s = ConfigStore(p)

    def add(c):
        c.download_clients["qbit"] = DownloadClientConfig(
            url="http://qb:8080", username="admin", password="pw", save_path="/downloads"
        )
        c.default_download_client = "qbit"

    s.mutate(add)
    reloaded = ConfigStore(p)
    qbit = reloaded.config.download_clients["qbit"]
    assert qbit.url == "http://qb:8080"
    assert qbit.save_path == "/downloads"
    assert reloaded.config.default_download_client == "qbit"


def test_atomic_save_no_partial_file(tmp_path):
    p = tmp_path / "config.yaml"
    s = ConfigStore(p)
    s.mutate(lambda c: setattr(c.rss, "interval_minutes", 15))
    # No stray temp file left behind.
    assert not (tmp_path / "config.yaml.tmp").exists()
    assert ConfigStore(p).config.rss.interval_minutes == 15

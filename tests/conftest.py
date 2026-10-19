"""Keep route/persistence tests away from real tasks and model services."""
import os
import tempfile


def pytest_configure(config):
    from core import config as settings
    from core import database
    root = tempfile.TemporaryDirectory(prefix="bilimix-tests-")
    config._bilimix_test_root = root
    for key, leaf in (("DATA_DIR", "data"), ("DOWNLOAD_DIR", "downloads"),
                      ("RESULT_DIR", "results"), ("OUTPUT_DIR", "outputs")):
        path = os.path.join(root.name, "data", "" if key == "DATA_DIR" else leaf)
        os.makedirs(path, exist_ok=True)
        setattr(settings, key, path)
    database.DB_PATH = settings.DB_PATH = os.path.join(root.name, "test.sqlite")
    database._local = __import__("threading").local()
    database.setup_database()


def pytest_unconfigure(config):
    if hasattr(config, "_bilimix_test_root"):
        config._bilimix_test_root.cleanup()

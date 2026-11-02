from functools import cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MONICA_FACES_")

    api: str = "http://127.0.0.1:17394"
    # The "faces" entry of IMPORT_HMAC_SECRETS on the NAS (or IMPORT_HMAC_SECRET when there is none).
    secret: str
    # A faces token from the import center; revoking it stops this machine.
    token: str
    cache: Path = Path.home() / ".cache" / "monica-faces"
    model: str = "buffalo_l"
    model_root: Path = Path.home() / ".insightface"
    providers: list[str] = Field(default_factory=lambda: ["CPUExecutionProvider"])
    det_size: int = 640
    # Images asked for per work request, per signed download, and downloads kept in flight while detecting.
    page: int = 200
    batch: int = 25
    fetchers: int = 2

    @property
    def database(self) -> Path:
        return self.cache / "faces.sqlite3"

    @property
    def crops(self) -> Path:
        return self.cache / "crops"


@cache
def settings() -> Settings:
    return Settings()

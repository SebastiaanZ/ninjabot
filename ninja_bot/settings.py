from __future__ import annotations

import functools
import itertools
import pathlib
import typing

import pydantic
import yaml

BASE_DIR: pathlib.Path = pathlib.Path(__file__).parent.parent

AllowDenyElement = typing.NewType("AllowDenyElements", typing.Union[str, int])


class AllowDenySet:
    def __init__(self, allow_deny_set: typing.Collection[AllowDenyElement]) -> None:
        self._set = set(allow_deny_set)

    def __repr__(self) -> str:
        cls_name = type(self).__name__
        return f"{cls_name}({repr(self._set)})"

    def __len__(self) -> int:
        return len(self._set)

    def __contains__(self, snowflake_id: int) -> bool:
        return True if self.wildcard_set else snowflake_id in self._set

    def __iter__(self) -> typing.Iterator[AllowDenyElement]:
        return iter(self._set)

    @functools.cached_property
    def wildcard_set(self) -> bool:
        return self._set == set("*")


class AllowDenyGroup(pydantic.BaseModel):
    allow: AllowDenySet = AllowDenySet(set("*"))
    deny: AllowDenySet = AllowDenySet(set())

    @pydantic.validator("*", pre=True)
    def validate_sets(
        cls, elements: typing.Collection[AllowDenyElement]
    ) -> AllowDenySet:
        allow_deny_set = AllowDenySet(elements)

        if not allow_deny_set.wildcard_set:
            if not all(isinstance(element, int) for element in allow_deny_set):
                raise TypeError(
                    "Elements of an AllowDenySet must be integers or a single '*'."
                )

        return allow_deny_set

    class Config:
        extra = pydantic.Extra.forbid
        allow_mutation = True
        validate_assignment = True
        arbitrary_types_allowed = True


class Permissions(pydantic.BaseModel):
    categories: AllowDenyGroup = AllowDenyGroup()
    channels: AllowDenyGroup = AllowDenyGroup()

    class Config:
        extra = pydantic.Extra.forbid
        allow_mutation = False


class Guild(pydantic.BaseModel):
    admins_id: int
    moderators_id: int
    helpers_id: int
    guild_id: int
    emoji_id: int
    emoji_full: str
    summary_channel: int
    bypass_roles: typing.List[int]
    commands_channels: typing.List[int]
    emoji_confirm: int
    emoji_deny: int

    class Config:
        extra = pydantic.Extra.forbid
        allow_mutation = False


class Game(pydantic.BaseModel):
    public_only: bool
    cooldown: int
    max_time_jitter: int
    probability_multiplier: int
    max_points: int
    reaction_timeout: int
    channel_scalars: typing.Dict[int, float]
    auto_start: bool = True

    class Config:
        extra = pydantic.Extra.forbid
        validate_assignment = True


class Formatter(pydantic.BaseModel):
    class_: str = pydantic.Field(alias="class")
    datefmt: str
    format: str

    class Config:
        extra = pydantic.Extra.forbid
        allow_mutation = False


class Handler(pydantic.BaseModel):
    level: str
    class_: str = pydantic.Field(alias="class")
    formatter: str
    stream: str

    class Config:
        extra = pydantic.Extra.forbid
        allow_mutation = False


class Logger(pydantic.BaseModel):
    handlers: typing.List[str]
    level: str
    propagate: bool

    class Config:
        extra = pydantic.Extra.forbid
        allow_mutation = False


class Logging(pydantic.BaseModel):
    version: int
    disable_existing_loggers: bool
    formatters: typing.Dict[str, Formatter]
    handlers: typing.Dict[str, Handler]
    loggers: typing.Dict[str, Logger]
    root: Logger

    class Config:
        extra = pydantic.Extra.forbid
        allow_mutation = False


class Settings(pydantic.BaseSettings):
    """A class containing all the settings for the project."""

    NINJABOT_TOKEN: str
    BASE_DIR: pathlib.Path = BASE_DIR
    permissions: Permissions = Permissions()
    game: Game
    guild: Guild
    logging: Logging
    ninja_names: typing.List[str]
    ninja_image: bytes

    class Config:
        """Meta-options for the Setting's model."""

        title = "NinjaBot's Settings"
        extra = pydantic.Extra.forbid
        allow_mutation = False
        env_file = BASE_DIR / ".env"
        env_file_encoding = "utf-8"


def load_configuration_from_yaml(config_file: pathlib.Path) -> typing.Dict:
    with config_file.open(encoding="utf-8") as f:
        config = yaml.safe_load(f)

    return config


def get_ninja_names():
    file = BASE_DIR / "resources" / "ninja_names.txt"
    names = file.read_text(encoding="UTF-8")

    first_names = []
    last_names = []

    for name in names.splitlines():
        first, last = name.strip().split()
        first_names.append(first)
        last_names.append(last)

    return ["".join(p) for p in itertools.product(first_names, last_names)]


def get_ninja_image():
    image = BASE_DIR / "resources" / "duckyninja.png"
    return image.read_bytes()


def load_settings() -> Settings:
    """Initialize our settings object and return it."""
    config_file = BASE_DIR / "config.yaml"
    config = load_configuration_from_yaml(config_file)
    return Settings(
        **config, ninja_names=get_ninja_names(), ninja_image=get_ninja_image()
    )


settings = load_settings()

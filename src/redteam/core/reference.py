"""Reference domains describe relevance and never grant execution permission."""

from typing import Annotated

from pydantic import Field

ReferenceDomain = Annotated[str, Field(pattern=r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")]

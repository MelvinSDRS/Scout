import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from .image_profiles import PROFILE_ID

COUNTRIES = {"US": "USD", "CA": "CAD", "FR": "EUR"}


class SearchSpec(BaseModel):
    query: str = Field(min_length=2, max_length=200)
    countries: list[Literal["US", "CA", "FR"]] = Field(min_length=1, max_length=3)
    sources: list[Literal["facebook"]] = Field(default=["facebook"], min_length=1)
    include: list[str] = Field(default_factory=list, max_length=20)
    exclude: list[str] = Field(default_factory=list, max_length=100)
    image_profile: str | None = Field(default=None, pattern=f"^{PROFILE_ID}$")
    include_any: list[list[str]] = Field(default_factory=list, max_length=10)
    max_prices: dict[str, Decimal] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_values(self):
        self.query = self.query.strip()
        if len(self.query) < 2:
            raise ValueError("Query cannot be blank")
        self.countries = list(dict.fromkeys(self.countries))
        self.sources = list(dict.fromkeys(self.sources))
        for currency, price in self.max_prices.items():
            if currency not in COUNTRIES.values() or not price.is_finite() or price < 0:
                raise ValueError("Prices must be nonnegative amounts in USD, CAD or EUR")
        if any(not group or len(group) > 20 for group in self.include_any):
            raise ValueError("Alternative groups must contain 1–20 phrases")
        if any(not term.strip() or len(term) > 200 for group in self.include_any for term in group):
            raise ValueError("Alternative phrases must contain 1–200 characters")
        if any(not term.strip() or len(term) > 200 for term in self.include + self.exclude):
            raise ValueError("Filter terms must contain 1–200 characters")
        return self


class Watch(SearchSpec):
    name: str = Field(min_length=1, max_length=100)
    interval_minutes: int = Field(default=60, ge=30, le=10080)
    enabled: bool = True

    @model_validator(mode="after")
    def validate_name(self):
        self.name = self.name.strip()
        if not self.name:
            raise ValueError("Name cannot be blank")
        return self


@dataclass(frozen=True)
class Listing:
    source: str
    id: str
    title: str
    url: str
    country: str
    price: str | None = None
    currency: str | None = None
    location: str = ""
    price_kind: str = "price"
    image_url: str | None = None


@dataclass
class SearchResult:
    listings: list[Listing]
    saturated: bool = False
    radius_km: int | None = None
    coverage_warning: str | None = None
    collected_count: int | None = None


class AccessBlocked(RuntimeError):
    pass


def normalize(value: str) -> str:
    return " ".join(re.findall(r"\w+", unicodedata.normalize("NFKC", value).casefold()))


def matches(watch: SearchSpec, listing: Listing) -> bool:
    title = f" {normalize(listing.title)} "
    required = watch.include or (
        [] if watch.include_any else re.findall(r"\w+", normalize(watch.query))
    )
    if not all(
        any(f" {normalize(term)} " in title for term in group) for group in watch.include_any
    ):
        return False
    if not all(f" {normalize(term)} " in title for term in required):
        return False
    if any(f" {normalize(term)} " in title for term in watch.exclude):
        return False
    # Do not interpret an unknown price/currency as free or convert currencies implicitly.
    if watch.max_prices:
        if listing.currency not in watch.max_prices or listing.price is None:
            return False
        try:
            amount = Decimal(listing.price)
            if not amount.is_finite() or amount < 0:
                return False
            if amount > watch.max_prices[listing.currency]:
                return False
        except InvalidOperation:
            return False
    return True

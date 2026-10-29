"""Export public property listing attributes from Dubizzle search pages."""

from __future__ import annotations

import argparse
import csv
import json
import re
import time
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser
from urllib.request import Request, urlopen

USER_AGENT = "DubizzlePublicListingReader/0.1"
REQUEST_TIMEOUT = 15
DEFAULT_SEARCH_URL = "https://www.dubizzle.com.eg/properties/apartments-duplex-for-sale/?page=1"
CSV_FIELDS = [
	"page",
	"position",
	"ad_id",
	"title",
	"listing_url",
	"image_url",
	"price",
	"currency",
	"availability",
	"property_type",
	"area",
	"area_unit",
	"bedrooms",
	"bathrooms",
	"street_address",
	"city_or_locality",
	"region",
	"country",
	"property_attributes_json",
]


class ListingHTMLParser(HTMLParser):
	def __init__(self) -> None:
		super().__init__()
		self.title_parts: list[str] = []
		self.meta: dict[str, str] = {}
		self.json_ld: list[str] = []
		self._in_title = False
		self._in_json_ld = False
		self._script_parts: list[str] = []

	def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
		attributes = dict(attrs)
		if tag == "title":
			self._in_title = True
		elif tag == "meta":
			key = attributes.get("property") or attributes.get("name")
			content = attributes.get("content")
			if key and content:
				self.meta[key.lower()] = content.strip()
		elif tag == "script" and attributes.get("type", "").lower() == "application/ld+json":
			self._in_json_ld = True
			self._script_parts = []

	def handle_endtag(self, tag: str) -> None:
		if tag == "title":
			self._in_title = False
		elif tag == "script" and self._in_json_ld:
			self.json_ld.append("".join(self._script_parts))
			self._in_json_ld = False

	def handle_data(self, data: str) -> None:
		if self._in_title:
			self.title_parts.append(data.strip())
		if self._in_json_ld:
			self._script_parts.append(data)


def _objects(value: Any) -> list[dict[str, Any]]:
	if isinstance(value, list):
		return [item for child in value for item in _objects(child)]
	if not isinstance(value, dict):
		return []
	found = [value]
	graph = value.get("@graph")
	if graph is not None:
		found.extend(_objects(graph))
	return found


def _type_names(value: Any) -> set[str]:
	values = value if isinstance(value, list) else [value]
	return {str(item).rsplit("/", 1)[-1] for item in values}


def _place_name(value: Any) -> str | None:
	if isinstance(value, str):
		return value
	if isinstance(value, dict):
		name = value.get("name")
		if isinstance(name, str):
			return name
		address = value.get("address")
		if isinstance(address, dict):
			return address.get("addressLocality") or address.get("addressRegion")
	return None


def parse_listing_html(html: str, page_url: str) -> dict[str, Any]:
	parser = ListingHTMLParser()
	parser.feed(html)
	result: dict[str, Any] = {
		"url": page_url,
		"title": parser.meta.get("og:title") or " ".join(parser.title_parts).strip(),
	}

	for raw_data in parser.json_ld:
		try:
			data = json.loads(raw_data)
		except json.JSONDecodeError:
			continue
		listing = next(
			(item for item in _objects(data) if _type_names(item.get("@type")) & {"Product", "Vehicle"}),
			None,
		)
		if listing is None:
			continue

		if isinstance(listing.get("name"), str):
			result["title"] = listing["name"]
		offers = listing.get("offers")
		if isinstance(offers, list):
			offers = next((offer for offer in offers if isinstance(offer, dict)), None)
		if isinstance(offers, dict):
			if offers.get("price") is not None:
				result["price"] = offers["price"]
			if isinstance(offers.get("priceCurrency"), str):
				result["currency"] = offers["priceCurrency"]
		location = listing.get("availableAtOrFrom") or listing.get("areaServed")
		place = _place_name(location)
		if place:
			result["location"] = place
		break

	if not result.get("title"):
		result["title"] = parser.meta.get("twitter:title", "")
	return result


def _nested_objects(value: Any) -> list[dict[str, Any]]:
	if isinstance(value, list):
		return [item for child in value for item in _nested_objects(child)]
	if not isinstance(value, dict):
		return []
	return [value, *(item for child in value.values() for item in _nested_objects(child))]


def _is_type(value: Any, expected: str) -> bool:
	return isinstance(value, dict) and expected in _type_names(value.get("@type"))


def _first_mapping(value: Any) -> dict[str, Any]:
	if isinstance(value, list):
		return next((item for item in value if isinstance(item, dict)), {})
	return value if isinstance(value, dict) else {}


def _public_property_attributes(value: Any) -> list[dict[str, Any]]:
	if not isinstance(value, list):
		return []
	attributes = []
	private_terms = re.compile(r"\b(?:seller|owner|contact|phone|telephone|email|whatsapp|profile)\b", re.IGNORECASE)
	for item in value:
		if not isinstance(item, dict):
			continue
		name = item.get("name")
		if not isinstance(name, str) or private_terms.search(name):
			continue
		attribute = {"name": name, "value": item.get("value")}
		unit = item.get("unitText") or item.get("unitCode")
		if unit:
			attribute["unit"] = unit
		attributes.append(attribute)
	return attributes


def _listing_row(listing: dict[str, Any], page: int, position: Any, search_url: str) -> dict[str, Any] | None:
	entity = _first_mapping(listing.get("mainEntity"))
	listing_url = listing.get("url")
	if not isinstance(listing_url, str) or not listing_url:
		return None
	listing_url = urljoin(search_url, listing_url)
	if urlparse(listing_url).hostname != urlparse(search_url).hostname:
		return None

	offer = _first_mapping(listing.get("offers"))
	area = _first_mapping(entity.get("floorSize"))
	address = _first_mapping(entity.get("address"))
	image = listing.get("image")
	if isinstance(image, list):
		image = next((item for item in image if isinstance(item, str)), "")
	if not isinstance(image, str):
		image = ""
	title = listing.get("name") or entity.get("name") or ""
	match = re.search(r"-ID(\d+)(?:\.html)?(?:$|\?)", listing_url, re.IGNORECASE)
	availability = offer.get("availability", "")
	if isinstance(availability, str):
		availability = availability.rsplit("/", 1)[-1]

	return {
		"page": page,
		"position": position if position is not None else "",
		"ad_id": match.group(1) if match else "",
		"title": title,
		"listing_url": listing_url,
		"image_url": image,
		"price": offer.get("price", ""),
		"currency": offer.get("priceCurrency", ""),
		"availability": availability,
		"property_type": entity.get("accommodationCategory", ""),
		"area": area.get("value", ""),
		"area_unit": area.get("unitText") or area.get("unitCode", ""),
		"bedrooms": entity.get("numberOfBedrooms", ""),
		"bathrooms": entity.get("numberOfBathroomsTotal", ""),
		"street_address": address.get("streetAddress", ""),
		"city_or_locality": address.get("addressLocality", ""),
		"region": address.get("addressRegion", ""),
		"country": address.get("addressCountry", ""),
		"property_attributes_json": json.dumps(
			_public_property_attributes(entity.get("additionalProperty")),
			ensure_ascii=False,
		),
	}


def parse_search_listings(html: str, page: int, search_url: str) -> list[dict[str, Any]]:
	parser = ListingHTMLParser()
	parser.feed(html)
	rows = []
	seen_urls: set[str] = set()
	for raw_data in parser.json_ld:
		try:
			data = json.loads(raw_data)
		except json.JSONDecodeError:
			continue
		for node in _nested_objects(data):
			if not _is_type(node, "ListItem"):
				continue
			listing = node.get("item")
			if not _is_type(listing, "RealEstateListing"):
				continue
			row = _listing_row(listing, page, node.get("position"), search_url)
			if row and row["listing_url"] not in seen_urls:
				seen_urls.add(row["listing_url"])
				rows.append(row)
	return rows


def _validate_dubizzle_url(url: str) -> None:
	parsed_url = urlparse(url)
	host = (parsed_url.hostname or "").lower()
	allowed_domain = host == "dubizzle.com" or host.endswith(".dubizzle.com")
	allowed_egypt_domain = host == "dubizzle.com.eg" or host.endswith(".dubizzle.com.eg")
	if parsed_url.scheme != "https" or not (allowed_domain or allowed_egypt_domain):
		raise ValueError("Provide an HTTPS URL on a Dubizzle domain.")
	if parsed_url.username or parsed_url.password:
		raise ValueError("URLs containing credentials are not supported.")


def _page_url(search_url: str, page: int) -> str:
	parts = urlsplit(search_url)
	query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if key != "page"]
	query.append(("page", str(page)))
	return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _read_robots(url: str) -> RobotFileParser:
	parsed_url = urlparse(url)
	robots_url = f"{parsed_url.scheme}://{parsed_url.netloc}/robots.txt"
	request = Request(robots_url, headers={"User-Agent": USER_AGENT})
	with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
		robots_text = response.read().decode("utf-8", errors="replace")
	robots = RobotFileParser(robots_url)
	robots.parse(robots_text.splitlines())
	return robots


def export_search_pages(
	search_url: str,
	start_page: int,
	end_page: int,
	output_path: Path,
	delay: float,
	overwrite: bool = False,
) -> int:
	_validate_dubizzle_url(search_url)
	if not 1 <= start_page <= end_page <= 199:
		raise ValueError("Page range must be between 1 and 199, with start_page <= end_page.")
	if delay < 0:
		raise ValueError("Delay must be zero or greater.")
	if output_path.exists() and not overwrite:
		raise FileExistsError(f"{output_path} already exists; choose another path or pass --overwrite.")

	robots = _read_robots(search_url)
	page_urls = [_page_url(search_url, page) for page in range(start_page, end_page + 1)]
	blocked = [url for url in page_urls if not robots.can_fetch(USER_AGENT, url)]
	if blocked:
		raise PermissionError(f"robots.txt disallows a requested page: {blocked[0]}")

	output_path.parent.mkdir(parents=True, exist_ok=True)
	total_rows = 0
	with output_path.open("w", newline="", encoding="utf-8-sig") as csv_file:
		writer = csv.DictWriter(csv_file, fieldnames=CSV_FIELDS)
		writer.writeheader()
		for page, page_url in zip(range(start_page, end_page + 1), page_urls):
			if page != start_page and delay:
				time.sleep(delay)
			request = Request(page_url, headers={"User-Agent": USER_AGENT, "Accept": "text/html"})
			with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
				if response.headers.get_content_type() != "text/html":
					raise ValueError(f"Expected HTML on page {page}.")
				html = response.read().decode("utf-8", errors="replace")
			rows = parse_search_listings(html, page, page_url)
			if not rows:
				raise ValueError(f"No structured listings found on page {page}; stopped without bypassing the response.")
			writer.writerows(rows)
			csv_file.flush()
			total_rows += len(rows)
			print(f"Page {page}: {len(rows)} listings (total {total_rows})")
	return total_rows


def _allowed_by_robots(url: str) -> bool:
	return _read_robots(url).can_fetch(USER_AGENT, url)


def fetch_listing(url: str) -> dict[str, Any]:
	parsed_url = urlparse(url)
	host = (parsed_url.hostname or "").lower()
	allowed_domain = host == "dubizzle.com" or host.endswith(".dubizzle.com")
	allowed_egypt_domain = host == "dubizzle.com.eg" or host.endswith(".dubizzle.com.eg")
	if parsed_url.scheme != "https" or not (allowed_domain or allowed_egypt_domain):
		raise ValueError("Provide an HTTPS URL on a Dubizzle domain.")
	if parsed_url.username or parsed_url.password:
		raise ValueError("URLs containing credentials are not supported.")

	if not _allowed_by_robots(url):
		raise PermissionError("robots.txt disallows this URL; no page was fetched.")

	request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html"})
	with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
		content_type = response.headers.get_content_type()
		if content_type != "text/html":
			raise ValueError(f"Expected an HTML page, received {content_type}.")
		html = response.read().decode("utf-8", errors="replace")
	return parse_listing_html(html, url)


def main() -> int:
	argument_parser = argparse.ArgumentParser(description=__doc__)
	argument_parser.add_argument(
		"search_url",
		nargs="?",
		default=DEFAULT_SEARCH_URL,
		help="Dubizzle property search URL (defaults to apartments for sale in Egypt)",
	)
	argument_parser.add_argument("--start-page", type=int, default=1)
	argument_parser.add_argument("--end-page", type=int, default=199)
	argument_parser.add_argument("--delay", type=float, default=2.0, help="seconds between page requests")
	argument_parser.add_argument("--output", type=Path, default=Path("dubizzle_apartments_sale.csv"))
	argument_parser.add_argument("--overwrite", action="store_true", help="replace an existing output CSV")
	args = argument_parser.parse_args()

	try:
		total_rows = export_search_pages(
			args.search_url,
			args.start_page,
			args.end_page,
			args.output,
			args.delay,
			args.overwrite,
		)
	except (OSError, URLError, ValueError, PermissionError) as error:
		argument_parser.exit(1, f"Error: {error}\n")
	print(f"Wrote {total_rows} listing rows to {args.output}")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())

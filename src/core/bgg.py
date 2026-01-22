import contextlib
import logging
import os
import xml.etree.ElementTree as ET

import httpx

from src.core.models import Game

logger = logging.getLogger(__name__)

BGG_API_TOKEN = os.getenv("BGG_API_TOKEN")


class BGGClient:
    BASE_URL = "https://boardgamegeek.com/xmlapi2"

    def _get_headers(self) -> dict:
        """Get headers for BGG API requests."""
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
        }
        if BGG_API_TOKEN:
            headers["Authorization"] = f"Bearer {BGG_API_TOKEN}"
        return headers

    async def fetch_collection(self, username: str, exclude_expansions: bool = True) -> list[Game]:
        """
        Fetch a user's collection from BGG.

        Args:
            username: BGG Username
            exclude_expansions: If True, filters out items with subtype 'boardgameexpansion'

        Returns:
            List of Game objects
        """
        params: dict[str, str | int] = {
            "username": username,
            "own": 1,
            "stats": 1,  # Needed for play time, num players, rating/weight
        }
        if exclude_expansions:
            params["excludesubtype"] = "boardgameexpansion"

        async with httpx.AsyncClient() as client:
            # Retry logic for 202 (Accepted/Queued)
            for attempt in range(5):  # Try up to 5 times
                try:
                    response = await client.get(
                        f"{self.BASE_URL}/collection",
                        params=params,
                        headers=self._get_headers(),
                        timeout=30.0,  # Increased timeout
                    )

                    # Check specific status codes before raising
                    if response.status_code == 202:
                        wait_time = (attempt + 1) * 2  # 2, 4, 6, 8 seconds
                        if attempt < 4:
                            logger.warning(
                                f"BGG returned 202 (Queued) for {username}. "
                                f"Retrying in {wait_time}s..."
                            )
                            # Use asyncio.sleep
                            import asyncio

                            await asyncio.sleep(wait_time)
                            continue
                        else:
                            logger.warning(
                                f"BGG returned 202 (Queued) for {username} after retries."
                            )
                            return []

                    if response.status_code == 404:
                        logger.warning(f"BGG user not found: {username}")
                        raise ValueError(f"User '{username}' not found on BoardGameGeek")

                    # Raise for any other non-2xx status
                    response.raise_for_status()

                    return self._parse_collection_xml(response.content)

                except httpx.HTTPStatusError as e:
                    if e.response.status_code == 404:
                        logger.warning(f"BGG user not found: {username}")
                        raise ValueError(f"User '{username}' not found on BoardGameGeek") from e
                    logger.error(f"HTTP error fetching BGG collection for {username}: {e}")
                    raise
                except httpx.HTTPError as e:
                    logger.error(f"Error fetching BGG collection for {username}: {e}")
                    raise
            return []

    def _parse_collection_xml(self, xml_content: bytes) -> list[Game]:
        root = ET.fromstring(xml_content)
        games: list[Game] = []

        for item in root.findall("item"):
            try:
                # Basic Stats
                stats = item.find("stats")
                if stats is None:
                    continue

                # Check ownership again to be safe
                status = item.find("status")
                if status is not None and status.get("own") != "1":
                    continue

                bgg_id = int(item.get("objectid", 0))
                name = item.find("name").text if item.find("name") is not None else "Unknown"
                thumbnail = (
                    item.find("thumbnail").text if item.find("thumbnail") is not None else None
                )

                min_players = int(stats.get("minplayers", 1))
                max_players = int(stats.get("maxplayers", 1))
                playing_time = int(stats.get("playingtime", 0))
                min_playing_time = int(stats.get("minplaytime", 0)) or None
                max_playing_time = int(stats.get("maxplaytime", 0)) or None

                # Complexity (averageweight)
                rating = stats.find("rating")
                complexity = 0.0
                if rating is not None:
                    avg_weight = rating.find("averageweight")
                    if avg_weight is not None:
                        # Handle '0' or None
                        try:
                            complexity = float(avg_weight.get("value", 0))
                        except ValueError:
                            complexity = 0.0

                game = Game(
                    id=bgg_id,
                    name=name,
                    min_players=min_players,
                    max_players=max_players,
                    playing_time=playing_time,
                    min_playing_time=min_playing_time,
                    max_playing_time=max_playing_time,
                    complexity=complexity,
                    thumbnail=thumbnail,
                )
                games.append(game)

            except (ValueError, AttributeError) as e:
                logger.warning(f"Failed to parse item {item.get('objectid')}: {e}")
                continue

        return games

    async def search_games(self, query: str, limit: int = 5) -> list[dict]:
        """
        Search for board games on BGG by name.

        Args:
            query: Search string (game name)
            limit: Maximum number of results to return (default 5)

        Returns:
            List of dicts: [{id, name, year_published}, ...]
        """
        params = {
            "query": query,
            "type": "boardgame",
        }

        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(
                    f"{self.BASE_URL}/search",
                    params=params,
                    headers=self._get_headers(),
                    timeout=10.0,
                )
                response.raise_for_status()

                return self._parse_search_xml(response.content, limit)
            except httpx.HTTPError as e:
                logger.error(f"Error searching BGG for '{query}': {e}")
                return []

    def _parse_search_xml(self, xml_content: bytes, limit: int) -> list[dict]:
        """Parse BGG search XML response."""
        root = ET.fromstring(xml_content)
        results: list[dict] = []

        for item in root.findall("item")[:limit]:
            try:
                bgg_id = int(item.get("id", 0))
                name_elem = item.find("name")
                name = name_elem.get("value", "Unknown") if name_elem is not None else "Unknown"
                year_elem = item.find("yearpublished")
                year = year_elem.get("value") if year_elem is not None else None

                results.append({"id": bgg_id, "name": name, "year_published": year})
            except (ValueError, AttributeError) as e:
                logger.warning(f"Failed to parse search item: {e}")
                continue

        return results

    async def get_game_details(self, bgg_id: int) -> Game | None:
        """
        Fetch full game details from BGG by ID.

        Args:
            bgg_id: BoardGameGeek game ID

        Returns:
            Game object with full stats, or None if fetch fails
        """
        params = {
            "id": bgg_id,
            "stats": 1,
        }

        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(
                    f"{self.BASE_URL}/thing",
                    params=params,
                    headers=self._get_headers(),
                    timeout=10.0,
                )
                response.raise_for_status()

                return self._parse_thing_xml(response.content, bgg_id)
            except httpx.HTTPError as e:
                logger.error(f"Error fetching BGG game details for ID {bgg_id}: {e}")
                return None

    def _parse_thing_xml(self, xml_content: bytes, bgg_id: int) -> Game | None:
        """Parse BGG thing XML response."""
        root = ET.fromstring(xml_content)
        item = root.find("item")

        if item is None:
            return None

        try:
            # Get primary name
            name = "Unknown"
            for name_elem in item.findall("name"):
                if name_elem.get("type") == "primary":
                    name = name_elem.get("value", "Unknown")
                    break

            # Get basic info
            min_players_elem = item.find("minplayers")
            max_players_elem = item.find("maxplayers")
            playing_time_elem = item.find("playingtime")
            min_playtime_elem = item.find("minplaytime")
            max_playtime_elem = item.find("maxplaytime")
            thumbnail_elem = item.find("thumbnail")

            min_players = (
                int(min_players_elem.get("value", 1)) if min_players_elem is not None else 1
            )
            max_players = (
                int(max_players_elem.get("value", 1)) if max_players_elem is not None else 6
            )
            playing_time = (
                int(playing_time_elem.get("value", 0)) if playing_time_elem is not None else 0
            )
            min_playing_time = (
                int(min_playtime_elem.get("value", 0)) if min_playtime_elem is not None else None
            ) or None
            max_playing_time = (
                int(max_playtime_elem.get("value", 0)) if max_playtime_elem is not None else None
            ) or None
            thumbnail = thumbnail_elem.text if thumbnail_elem is not None else None

            # Complexity (averageweight) from statistics
            complexity = 0.0
            stats = item.find("statistics")
            if stats is not None:
                ratings = stats.find("ratings")
                if ratings is not None:
                    avg_weight = ratings.find("averageweight")
                    if avg_weight is not None:
                        with contextlib.suppress(ValueError):
                            complexity = float(avg_weight.get("value", 0))

            return Game(
                id=bgg_id,
                name=name,
                min_players=min_players,
                max_players=max_players,
                playing_time=playing_time,
                min_playing_time=min_playing_time,
                max_playing_time=max_playing_time,
                complexity=complexity,
                thumbnail=thumbnail,
            )
        except (ValueError, AttributeError) as e:
            logger.warning(f"Failed to parse thing item {bgg_id}: {e}")
            return None

    async def fetch_expansions(self, username: str) -> list[dict]:
        """
        Fetch a user's owned expansions from BGG.

        Args:
            username: BGG Username

        Returns:
            List of dicts: [{id, name}, ...]
        """
        params: dict[str, str | int] = {
            "username": username,
            "own": 1,
            "subtype": "boardgameexpansion",
        }

        async with httpx.AsyncClient() as client:
            for attempt in range(5):
                try:
                    response = await client.get(
                        f"{self.BASE_URL}/collection",
                        params=params,
                        headers=self._get_headers(),
                        timeout=30.0,
                    )

                    if response.status_code == 202:
                        wait_time = (attempt + 1) * 2
                        if attempt < 4:
                            logger.warning(
                                f"BGG returned 202 (Queued) for expansions. "
                                f"Retrying in {wait_time}s..."
                            )
                            import asyncio

                            await asyncio.sleep(wait_time)
                            continue
                        else:
                            return []

                    if response.status_code == 404:
                        return []

                    response.raise_for_status()
                    return self._parse_expansion_collection_xml(response.content)

                except httpx.HTTPError as e:
                    logger.error(f"Error fetching expansions for {username}: {e}")
                    return []
            return []

    def _parse_expansion_collection_xml(self, xml_content: bytes) -> list[dict]:
        """Parse expansion collection XML response."""
        root = ET.fromstring(xml_content)
        expansions: list[dict] = []

        for item in root.findall("item"):
            try:
                status = item.find("status")
                if status is not None and status.get("own") != "1":
                    continue

                bgg_id = int(item.get("objectid", 0))
                name_elem = item.find("name")
                name = name_elem.text if name_elem is not None else "Unknown"

                expansions.append({"id": bgg_id, "name": name})
            except (ValueError, AttributeError) as e:
                logger.warning(f"Failed to parse expansion item: {e}")
                continue

        return expansions

    async def get_expansion_info(self, expansion_id: int) -> dict | None:
        """
        Fetch expansion details including base game link and player count.

        Args:
            expansion_id: BGG expansion ID

        Returns:
            Dict with: {id, name, base_game_id, new_max_players, complexity_delta}
            or None if fetch fails
        """
        params = {
            "id": expansion_id,
            "stats": 1,
        }

        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(
                    f"{self.BASE_URL}/thing",
                    params=params,
                    headers=self._get_headers(),
                    timeout=10.0,
                )
                response.raise_for_status()
                return self._parse_expansion_thing_xml(response.content, expansion_id)
            except httpx.HTTPError as e:
                logger.error(f"Error fetching expansion info for ID {expansion_id}: {e}")
                return None

    def _parse_expansion_thing_xml(self, xml_content: bytes, expansion_id: int) -> dict | None:
        """Parse expansion thing XML response to extract base game link and modifiers."""
        root = ET.fromstring(xml_content)
        item = root.find("item")

        if item is None:
            return None

        try:
            # Get primary name
            name = "Unknown"
            for name_elem in item.findall("name"):
                if name_elem.get("type") == "primary":
                    name = name_elem.get("value", "Unknown")
                    break

            # Get max players (for player count expansion detection)
            max_players_elem = item.find("maxplayers")
            new_max_players = None
            if max_players_elem is not None:
                try:
                    new_max_players = int(max_players_elem.get("value", 0))
                    if new_max_players == 0:
                        new_max_players = None
                except ValueError:
                    pass

            # Find base game link (type="boardgameexpansion" with inbound="true")
            # The expansion links back to its base game with inbound="true"
            base_game_id = None
            for link in item.findall("link"):
                # inbound="true" means this is the base game that this expansion expands
                if link.get("type") == "boardgameexpansion" and link.get("inbound") == "true":
                    try:
                        base_game_id = int(link.get("id", 0))
                        break
                    except ValueError:
                        continue

            # Get complexity for potential delta calculation
            complexity = None
            stats = item.find("statistics")
            if stats is not None:
                ratings = stats.find("ratings")
                if ratings is not None:
                    avg_weight = ratings.find("averageweight")
                    if avg_weight is not None:
                        try:
                            complexity = float(avg_weight.get("value", 0))
                            if complexity == 0:
                                complexity = None
                        except ValueError:
                            pass

            return {
                "id": expansion_id,
                "name": name,
                "base_game_id": base_game_id,
                "new_max_players": new_max_players,
                "complexity": complexity,
            }
        except (ValueError, AttributeError) as e:
            logger.warning(f"Failed to parse expansion thing {expansion_id}: {e}")
            return None

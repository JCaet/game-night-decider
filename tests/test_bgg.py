import pytest

from src.core.bgg import BGGClient

# Sample BGG XML Response
MOCK_BGG_XML = b"""
<items totalitems="2">
    <item objectid="1" subtype="boardgame" collid="1">
        <name sortindex="1">Catan</name>
        <yearpublished>1995</yearpublished>
        <stats minplayers="3" maxplayers="4" playingtime="60" minplaytime="45" maxplaytime="90">
            <rating value="NULL">
                <usersrated value="100"/>
                <average value="7.5"/>
                <bayesaverage value="7.4"/>
                <stddev value="1.5"/>
                <median value="0"/>
                <averageweight value="2.32"/>
            </rating>
        </stats>
        <status own="1" prevowned="0" fortrade="0" want="0" wanttoplay="0"
            wanttobuy="0" wishlist="0" preordered="0" lastmodified="2021-01-01 00:00:00"/>
        <thumbnail>http://example.com/catan.jpg</thumbnail>
    </item>
    <item objectid="2" subtype="boardgameexpansion" collid="2">
        <name sortindex="1">Catan Extension</name>
        <stats minplayers="5" maxplayers="6" playingtime="90">
             <rating>
                <averageweight value="2.5"/>
             </rating>
        </stats>
        <status own="1"/>
    </item>
</items>
"""


@pytest.mark.asyncio
async def test_parse_collection_xml():
    client = BGGClient()
    games = client._parse_collection_xml(MOCK_BGG_XML)

    # By default, xml API returns all items, filtering happens in fetch_collection via params
    # But _parse_collection_xml just parses what it gets.
    # However, our current BGGClient implementation relies on API params to filter expansions.
    # So we should test that the parser correctly extracts data for the items provided.

    assert len(games) == 2

    # Check Catan
    catan = games[0]
    assert catan.name == "Catan"
    assert catan.min_players == 3
    assert catan.max_players == 4
    assert catan.complexity == 2.32
    assert catan.thumbnail == "http://example.com/catan.jpg"
    assert catan.id == 1
    assert catan.min_playing_time == 45
    assert catan.max_playing_time == 90

    # Check Expansion
    expansion = games[1]
    assert expansion.name == "Catan Extension"


# Mock BGG Search XML Response
MOCK_SEARCH_XML = b"""
<items total="2" termsofuse="https://boardgamegeek.com/xmlapi/termsofuse">
    <item type="boardgame" id="13">
        <name type="primary" value="Catan"/>
        <yearpublished value="1995"/>
    </item>
    <item type="boardgame" id="42">
        <name type="primary" value="Ticket to Ride"/>
        <yearpublished value="2004"/>
    </item>
</items>
"""

# Mock BGG Thing XML Response (with stats)
MOCK_THING_XML = b"""
<items termsofuse="https://boardgamegeek.com/xmlapi/termsofuse">
    <item type="boardgame" id="13">
        <thumbnail>https://example.com/catan_thumb.jpg</thumbnail>
        <name type="primary" sortindex="1" value="Catan"/>
        <name type="alternate" sortindex="1" value="Settlers of Catan"/>
        <minplayers value="3"/>
        <maxplayers value="4"/>
        <playingtime value="60"/>
        <minplaytime value="60"/>
        <maxplaytime value="120"/>
        <statistics page="1">
            <ratings>
                <average value="7.15"/>
                <averageweight value="2.32"/>
            </ratings>
        </statistics>
    </item>
</items>
"""


def test_parse_search_xml():
    """Test BGG search XML parsing."""
    client = BGGClient()
    results = client._parse_search_xml(MOCK_SEARCH_XML, limit=5)

    assert len(results) == 2

    assert results[0]["id"] == 13
    assert results[0]["name"] == "Catan"
    assert results[0]["year_published"] == "1995"

    assert results[1]["id"] == 42
    assert results[1]["name"] == "Ticket to Ride"


def test_parse_search_xml_respects_limit():
    """Test that search parsing respects the limit parameter."""
    client = BGGClient()
    results = client._parse_search_xml(MOCK_SEARCH_XML, limit=1)

    assert len(results) == 1
    assert results[0]["name"] == "Catan"


def test_parse_thing_xml():
    """Test BGG thing/stats XML parsing."""
    client = BGGClient()
    game = client._parse_thing_xml(MOCK_THING_XML, bgg_id=13)

    assert game is not None
    assert game.id == 13
    assert game.name == "Catan"
    assert game.min_players == 3
    assert game.max_players == 4
    assert game.playing_time == 60
    assert game.min_playing_time == 60
    assert game.max_playing_time == 120
    assert game.complexity == 2.32
    assert game.thumbnail == "https://example.com/catan_thumb.jpg"


def test_parse_thing_xml_empty():
    """Test thing XML parsing with empty response."""
    client = BGGClient()
    empty_xml = b"<items></items>"
    game = client._parse_thing_xml(empty_xml, bgg_id=999)

    assert game is None

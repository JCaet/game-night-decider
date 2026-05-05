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


# Suggested-numplayers poll parsing
DUNE_UPRISING_THING_XML = b"""
<items>
    <item type="boardgame" id="397598">
        <name type="primary" value="Dune: Imperium - Uprising"/>
        <minplayers value="1"/>
        <maxplayers value="6"/>
        <playingtime value="120"/>
        <minplaytime value="60"/>
        <maxplaytime value="120"/>
        <poll name="suggested_numplayers" totalvotes="500">
            <results numplayers="1">
                <result value="Best" numvotes="22"/>
                <result value="Recommended" numvotes="149"/>
                <result value="Not Recommended" numvotes="113"/>
            </results>
            <results numplayers="2">
                <result value="Best" numvotes="18"/>
                <result value="Recommended" numvotes="171"/>
                <result value="Not Recommended" numvotes="118"/>
            </results>
            <results numplayers="3">
                <result value="Best" numvotes="153"/>
                <result value="Recommended" numvotes="204"/>
                <result value="Not Recommended" numvotes="20"/>
            </results>
            <results numplayers="4">
                <result value="Best" numvotes="320"/>
                <result value="Recommended" numvotes="84"/>
                <result value="Not Recommended" numvotes="6"/>
            </results>
            <results numplayers="5">
                <result value="Best" numvotes="2"/>
                <result value="Recommended" numvotes="12"/>
                <result value="Not Recommended" numvotes="246"/>
            </results>
            <results numplayers="6">
                <result value="Best" numvotes="58"/>
                <result value="Recommended" numvotes="140"/>
                <result value="Not Recommended" numvotes="53"/>
            </results>
            <results numplayers="6+">
                <result value="Best" numvotes="1"/>
                <result value="Recommended" numvotes="1"/>
                <result value="Not Recommended" numvotes="182"/>
            </results>
        </poll>
        <statistics page="1">
            <ratings>
                <averageweight value="3.6"/>
            </ratings>
        </statistics>
    </item>
</items>
"""

# Like Red Dragon Inn 4 — community signal exists but every count has fewer than 30 votes.
LOW_SAMPLE_THING_XML = b"""
<items>
    <item type="boardgame" id="142402">
        <name type="primary" value="The Red Dragon Inn 4"/>
        <minplayers value="2"/>
        <maxplayers value="4"/>
        <playingtime value="60"/>
        <poll name="suggested_numplayers" totalvotes="4">
            <results numplayers="2">
                <result value="Best" numvotes="0"/>
                <result value="Recommended" numvotes="2"/>
                <result value="Not Recommended" numvotes="1"/>
            </results>
            <results numplayers="3">
                <result value="Best" numvotes="1"/>
                <result value="Recommended" numvotes="3"/>
                <result value="Not Recommended" numvotes="0"/>
            </results>
            <results numplayers="4">
                <result value="Best" numvotes="3"/>
                <result value="Recommended" numvotes="1"/>
                <result value="Not Recommended" numvotes="0"/>
            </results>
        </poll>
        <statistics page="1">
            <ratings><averageweight value="1.5"/></ratings>
        </statistics>
    </item>
</items>
"""

# Catan-shaped: official 3-4, community recommends both. Nothing should be blocked.
CATAN_THING_XML = b"""
<items>
    <item type="boardgame" id="13">
        <name type="primary" value="Catan"/>
        <minplayers value="3"/>
        <maxplayers value="4"/>
        <playingtime value="90"/>
        <poll name="suggested_numplayers" totalvotes="2000">
            <results numplayers="3">
                <result value="Best" numvotes="721"/>
                <result value="Recommended" numvotes="1154"/>
                <result value="Not Recommended" numvotes="112"/>
            </results>
            <results numplayers="4">
                <result value="Best" numvotes="1541"/>
                <result value="Recommended" numvotes="465"/>
                <result value="Not Recommended" numvotes="47"/>
            </results>
        </poll>
        <statistics page="1">
            <ratings><averageweight value="2.32"/></ratings>
        </statistics>
    </item>
</items>
"""


def test_parse_thing_xml_blocks_dune_uprising_5p():
    """Suggested-numplayers poll → blocks 5 within official 1-6 for Dune Uprising."""
    client = BGGClient()
    game = client._parse_thing_xml(DUNE_UPRISING_THING_XML, bgg_id=397598)

    assert game is not None
    assert game.min_players == 1
    assert game.max_players == 6
    assert game.community_unplayable_counts == "5"


def test_parse_thing_xml_low_sample_does_not_block():
    """When per-count totals are below the threshold, no count is blocked."""
    client = BGGClient()
    game = client._parse_thing_xml(LOW_SAMPLE_THING_XML, bgg_id=142402)

    assert game is not None
    # Empty string = poll seen, nothing met the blocklist threshold.
    assert game.community_unplayable_counts == ""


def test_parse_thing_xml_no_poll_returns_none_blocklist():
    """When the suggested-numplayers poll is absent, blocklist is None (unknown)."""
    client = BGGClient()
    # MOCK_THING_XML defined earlier in the file has no <poll> element.
    game = client._parse_thing_xml(MOCK_THING_XML, bgg_id=13)

    assert game is not None
    assert game.community_unplayable_counts is None


def test_parse_thing_xml_catan_recommended_counts_not_blocked():
    """All counts in Catan's official range are community-recommended → no block."""
    client = BGGClient()
    game = client._parse_thing_xml(CATAN_THING_XML, bgg_id=13)

    assert game is not None
    assert game.community_unplayable_counts == ""


# Deep Regrets-shaped: community votes the publisher's max as not playable
# (1-5, 5p at 73.8% NotRec). Per issue #55, the official max is treated as a
# supported mode regardless of community sentiment.
DEEP_REGRETS_THING_XML = b"""
<items>
    <item type="boardgame" id="397931">
        <name type="primary" value="Deep Regrets"/>
        <minplayers value="1"/>
        <maxplayers value="5"/>
        <playingtime value="90"/>
        <poll name="suggested_numplayers" totalvotes="107">
            <results numplayers="1">
                <result value="Best" numvotes="5"/>
                <result value="Recommended" numvotes="29"/>
                <result value="Not Recommended" numvotes="21"/>
            </results>
            <results numplayers="2">
                <result value="Best" numvotes="28"/>
                <result value="Recommended" numvotes="38"/>
                <result value="Not Recommended" numvotes="12"/>
            </results>
            <results numplayers="3">
                <result value="Best" numvotes="51"/>
                <result value="Recommended" numvotes="21"/>
                <result value="Not Recommended" numvotes="5"/>
            </results>
            <results numplayers="4">
                <result value="Best" numvotes="16"/>
                <result value="Recommended" numvotes="31"/>
                <result value="Not Recommended" numvotes="27"/>
            </results>
            <results numplayers="5">
                <result value="Best" numvotes="3"/>
                <result value="Recommended" numvotes="14"/>
                <result value="Not Recommended" numvotes="48"/>
            </results>
            <results numplayers="5+">
                <result value="Best" numvotes="0"/>
                <result value="Recommended" numvotes="0"/>
                <result value="Not Recommended" numvotes="46"/>
            </results>
        </poll>
        <statistics page="1">
            <ratings><averageweight value="2.5"/></ratings>
        </statistics>
    </item>
</items>
"""


def test_parse_thing_xml_does_not_block_official_max():
    """The max sets the bar: a count is only blocked if it's strictly worse than
    the publisher's official max. Deep Regrets max=5 has the highest NotRec share
    in its range, so nothing is blocked (issue #55)."""
    client = BGGClient()
    game = client._parse_thing_xml(DEEP_REGRETS_THING_XML, bgg_id=397931)

    assert game is not None
    assert game.min_players == 1
    assert game.max_players == 5
    # 5p meets the absolute threshold (73.8% NotRec, 65 votes) AND is the max,
    # so its share is the bar nothing else exceeds → empty blocklist.
    assert game.community_unplayable_counts == ""


# Massive Darkness 2: Hellscape — official 1-6, community votes 5p at 69.6% NotRec
# and 6p (max) at 83.3%. Under a naive "skip max" rule we'd expose the worse 6p
# while hiding the better 5p. The "strictly worse than max" rule keeps both.
HELLSCAPE_THING_XML = b"""
<items>
    <item type="boardgame" id="315610">
        <name type="primary" value="Massive Darkness 2: Hellscape"/>
        <minplayers value="1"/>
        <maxplayers value="6"/>
        <playingtime value="120"/>
        <poll name="suggested_numplayers" totalvotes="90">
            <results numplayers="1">
                <result value="Best" numvotes="23"/>
                <result value="Recommended" numvotes="51"/>
                <result value="Not Recommended" numvotes="16"/>
            </results>
            <results numplayers="2">
                <result value="Best" numvotes="37"/>
                <result value="Recommended" numvotes="49"/>
                <result value="Not Recommended" numvotes="4"/>
            </results>
            <results numplayers="3">
                <result value="Best" numvotes="59"/>
                <result value="Recommended" numvotes="25"/>
                <result value="Not Recommended" numvotes="5"/>
            </results>
            <results numplayers="4">
                <result value="Best" numvotes="21"/>
                <result value="Recommended" numvotes="42"/>
                <result value="Not Recommended" numvotes="18"/>
            </results>
            <results numplayers="5">
                <result value="Best" numvotes="0"/>
                <result value="Recommended" numvotes="24"/>
                <result value="Not Recommended" numvotes="55"/>
            </results>
            <results numplayers="6">
                <result value="Best" numvotes="0"/>
                <result value="Recommended" numvotes="13"/>
                <result value="Not Recommended" numvotes="65"/>
            </results>
            <results numplayers="6+">
                <result value="Best" numvotes="0"/>
                <result value="Recommended" numvotes="0"/>
                <result value="Not Recommended" numvotes="57"/>
            </results>
        </poll>
        <statistics page="1">
            <ratings><averageweight value="2.5"/></ratings>
        </statistics>
    </item>
</items>
"""


def test_parse_thing_xml_does_not_block_count_better_than_max():
    """A count whose NotRec share is *better* than the official max must never be
    blocked, even if it crosses the absolute threshold. Hellscape 5p (69.6% NotRec)
    is better than 6p max (83.3% NotRec) — both stay; the table decides."""
    client = BGGClient()
    game = client._parse_thing_xml(HELLSCAPE_THING_XML, bgg_id=315610)

    assert game is not None
    assert game.min_players == 1
    assert game.max_players == 6
    # 5p crosses 60% NotRec but sits below 6p's 83.3%, so it's not blocked.
    assert game.community_unplayable_counts == ""

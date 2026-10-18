from app.research.locality import address_terms, cell_terms, mentioned


def test_address_abbreviation_matches_full_street_name():
    terms = address_terms("Wiener Straße 12, 10999 Berlin")

    assert terms == ["Wiener Straße"]
    assert mentioned("Ein Geschäft in der Wiener Str. 12 hat eröffnet.", terms) == "Wiener Straße"


def test_address_terms_preserve_short_street_names():
    assert address_terms("U 1, 10115 Berlin") == ["U"]


def test_cell_terms_include_name_without_trailing_direction():
    terms = cell_terms("Graefekiez Nord")

    assert "Graefekiez" in terms
    assert mentioned("Im Graefekiez wird ein Café eröffnet.", terms) == "Graefekiez"


def test_other_neighborhood_does_not_match_cell_name():
    terms = cell_terms("Askanischer Platz")

    assert mentioned("Der Bericht handelt vom Kotti und Kottbusser Tor.", terms) is None


def test_cell_street_is_localization_term():
    terms = cell_terms("Askanischer Platz", ["Wiener Straße"])

    assert mentioned("An der Wiener Str. ist ein neues Geschäft geplant.", terms) == "Wiener Straße"


def test_matching_respects_word_boundaries():
    assert mentioned("Kottbusserstraße 10", ["Kottbusser"]) is None
